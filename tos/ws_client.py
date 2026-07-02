"""
Cliente WebSocket para el gateway WsJson de thinkorswim/Schwab (paper).

Reemplaza el DOM-automation: handshake → login (por token o authCode) →
accounts → place_order (CONFIRM → SUBMIT). Ver tos/WS_PROTOCOL.md.

Auth: el webview hace el OAuth y obtiene un `token` (login/schwab). Este cliente
puede reusar ese token vía el servicio `login`. Si el token no es reusable entre
conexiones, hay que pasar un authCode fresco a login_authcode().
"""
from __future__ import annotations

import asyncio
import itertools
import json
import ssl
import time

import certifi
import websockets

_SSL = ssl.create_default_context(cafile=certifi.where())

PAPER_WS = "wss://papermoney-services.schwab.com/Services/WsJson"
LIVE_WS = "wss://thinkorswim-services.schwab.com/Services/WsJson"
ORIGIN = "https://trade.thinkorswim.com"
HANDSHAKE = {"ver": "27.*.*", "fmt": "json-patches-structured", "heartbeat": "2s"}


def submit_result(submit_body: dict | None) -> dict:
    """Lee los patches del SUBMIT: éxito = se asignó un /orders/0/orderId no-cero."""
    order_id = None
    for p in (submit_body or {}).get("patches", []):
        if p.get("op") == "replace" and str(p.get("path", "")).endswith("/orderId"):
            order_id = p.get("value")
    return {"ok": order_id not in (None, 0), "order_id": order_id}


def option_symbol(root: str, expiration: str, side: str, strike: float) -> str:
    """.GOOG260626P335  ← root, exp YYYY-MM-DD, CALL/PUT, strike."""
    y, m, d = expiration.split("-")
    cp = "C" if side.upper() in ("CALL", "C") else "P"
    st = f"{strike:g}"
    return f".{root.upper()}{y[2:]}{m}{d}{cp}{st}"


class TosWsClient:
    def __init__(self, url: str = PAPER_WS, on_event=None):
        self.url = url
        self.on_event = on_event
        self.ws = None
        self._ids = itertools.count(1)
        self.session = None
        self.authenticated = False
        self.account_code = None
        self._waiters: dict[str, asyncio.Future] = {}
        self._recv_task = None
        self._hb_task = None

    async def connect(self) -> dict:
        self.ws = await websockets.connect(
            self.url, max_size=None, open_timeout=15, origin=ORIGIN,
            ping_interval=None, ssl=_SSL)
        await self.ws.send(json.dumps(HANDSHAKE))
        ack = json.loads(await self.ws.recv())
        self.session = ack.get("session")
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._hb_task = asyncio.create_task(self._heartbeat())
        return ack

    async def _heartbeat(self):
        # El handshake declara heartbeat 2s. Mantenemos viva la conexión con ping WS.
        while self.ws is not None:
            await asyncio.sleep(2)
            try:
                await self.ws.ping()
            except Exception:
                break

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                for item in msg.get("payload", []):
                    hid = item.get("header", {}).get("id")
                    w = self._waiters.get(hid)
                    if w and not w.done():
                        w.set_result(item)
                    if self.on_event:
                        try:
                            self.on_event(item)
                        except Exception:
                            pass
        except Exception:
            pass

    async def _request(self, service: str, params: dict, req_id: str | None = None,
                       ver: int = 0, timeout: float = 15) -> dict:
        req_id = req_id or service
        fut = asyncio.get_event_loop().create_future()
        self._waiters[req_id] = fut
        env = {"payload": [{"header": {"service": service, "id": req_id, "ver": ver}, "params": params}]}
        await self.ws.send(json.dumps(env))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(req_id, None)

    # --- auth ---
    async def login_token(self, token: str) -> dict:
        body = (await self._request("login", {
            "domain": "TOS", "platform": "PROD", "token": token, "tag": "TOSWeb"},
            req_id="login")).get("body", {})
        self.authenticated = bool(body.get("authenticated"))
        return body

    async def login_authcode(self, auth_code: str) -> dict:
        body = (await self._request("login/schwab", {
            "clientId": "TOSWeb", "tag": "TOSWeb",
            "redirectUri": "https://trade.thinkorswim.com/oauth", "authCode": auth_code},
            req_id="login/schwab")).get("body", {})
        self.authenticated = bool(body.get("authenticated"))
        return body

    async def accounts(self) -> list[dict]:
        items = (await self._request("accounts", {}, req_id="accounts")).get("body", {}).get("items", [])
        for a in items:
            if a.get("tradeable") and not self.account_code:
                self.account_code = a.get("code")
        return items

    # --- órdenes (CONFIRM → SUBMIT) ---
    def _order_params(self, action: str, request_type: str, account: str,
                      sym: str, qty: int, order_type: str, limit_price, ver: int):
        leg = {"symbol": sym, "quantity": qty}
        order = {"requestType": request_type, "orderType": order_type, "legs": [leg]}
        if limit_price is not None:
            order["limitPrice"] = round(float(limit_price), 2)
        if request_type == "EDIT_ORDER":
            order["tif"] = "DAY"
            order["tag"] = "TOSWeb"
        return {"action": action, "accountCode": account, "marker": "SINGLE", "orders": [order]}

    async def place_option_order(self, account: str, sym: str, quantity: int,
                                 order_type: str = "MARKET", limit_price=None,
                                 submit: bool = False) -> dict:
        """quantity>0 compra, quantity<0 venta. submit=False solo hace el CONFIRM (review)."""
        rid = f"update-draft-order-{sym}"
        # 1) CONFIRM INIT_OPTION (crea borrador)
        await self._request("place_order", self._order_params(
            "CONFIRM", "INIT_OPTION", account, sym, quantity, order_type, limit_price, 0), req_id=rid, ver=0)
        # 2) CONFIRM EDIT_ORDER (confirma)
        confirm = await self._request("place_order", self._order_params(
            "CONFIRM", "EDIT_ORDER", account, sym, quantity, order_type, limit_price, 1), req_id=rid, ver=1)
        if not submit:
            return {"stage": "confirmed", "body": confirm.get("body")}
        # 3) SUBMIT EDIT_ORDER (envía)
        sent = await self._request("place_order", self._order_params(
            "SUBMIT", "EDIT_ORDER", account, sym, quantity, order_type, limit_price, 0), req_id=rid, ver=0)
        res = submit_result(sent.get("body"))
        return {"stage": "submitted", "ok": res["ok"], "order_id": res["order_id"], "body": sent.get("body")}

    async def positions(self, account: str, fields=("QUANTITY",)) -> list[dict]:
        r = await self._request("positions", {"account": account, "fields": list(fields)},
                                req_id="positions")
        return r.get("body", {}).get("items", [])

    async def held_quantity(self, account: str, option_symbol: str) -> float:
        """Cantidad LONG que la cuenta tiene de ese contrato (0 si no hay). Para
        no vender en descubierto al cerrar."""
        for it in await self.positions(account, ["QUANTITY"]):
            inst = it.get("instrument", {})
            if inst.get("symbol") == option_symbol or it.get("symbol") == option_symbol:
                try:
                    return float(it.get("values", {}).get("QUANTITY", 0) or 0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    async def order_full(self, account: str, sym: str, qty: int,
                         order_type: str = "MARKET", limit_price=None, do_submit: bool = True) -> dict:
        """Devuelve el cuerpo de CADA paso (init, confirm, submit) para diagnóstico."""
        rid = f"update-draft-order-{sym}"
        init = await self._request("place_order", self._order_params(
            "CONFIRM", "INIT_OPTION", account, sym, qty, order_type, limit_price, 0), req_id=rid, ver=0)
        confirm = await self._request("place_order", self._order_params(
            "CONFIRM", "EDIT_ORDER", account, sym, qty, order_type, limit_price, 1), req_id=rid, ver=1)
        trace = {"init": init.get("body"), "confirm": confirm.get("body")}
        if do_submit:
            submit = await self._request("place_order", self._order_params(
                "SUBMIT", "EDIT_ORDER", account, sym, qty, order_type, limit_price, 0), req_id=rid, ver=0)
            trace["submit"] = submit.get("body")
        return trace

    async def subscribe_order_events(self):
        return await self._request("order_events", {}, req_id="order_events")

    async def close(self):
        for t in (self._hb_task, self._recv_task):
            if t:
                t.cancel()
        if self.ws:
            await self.ws.close()
            self.ws = None
