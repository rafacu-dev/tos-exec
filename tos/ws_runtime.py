"""
Runtime WS para el executor: corre un loop asyncio en un hilo que (1) mantiene
la captura del token desde el webview (CDP) y (2) coloca órdenes por WebSocket
reusando el token guardado. Expone métodos thread-safe para el código Qt.
"""
from __future__ import annotations

import asyncio
import threading
import time

from tos import token_store, ws_token_capture
from tos.ws_client import TosWsClient, option_symbol


class WsRuntime:
    def __init__(self, cdp_port: int = 9333, max_hours: float = 8.0, log=print):
        self.cdp_port = cdp_port
        self.max_hours = max_hours
        self.log = log
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._capture_forever())
        self.loop.run_forever()

    async def _capture_forever(self):
        while True:
            try:
                await ws_token_capture.run(self.cdp_port, self.log)
            except Exception:
                await asyncio.sleep(5)

    def token_status(self) -> dict | None:
        return token_store.valid(self.max_hours)

    def place_option(self, root: str, expiration: str, side: str, strike: float,
                     quantity: int, order_type: str = "MARKET", limit_price=None,
                     submit: bool = False, timeout: float = 40) -> dict:
        """quantity>0 compra, <0 venta. submit=False solo hace el review (CONFIRM)."""
        fut = asyncio.run_coroutine_threadsafe(
            self._place(root, expiration, side, strike, quantity, order_type, limit_price, submit),
            self.loop)
        return fut.result(timeout=timeout)

    def held_option_symbols(self, timeout: float = 25):
        """Set de símbolos de opción con qty>0 en la cuenta. None si no se pudo
        consultar (no reconciliar en ese caso, para no cerrar por un fallo de red)."""
        d = token_store.valid(self.max_hours)
        if not d:
            return None
        try:
            return asyncio.run_coroutine_threadsafe(self._held(d), self.loop).result(timeout)
        except Exception:
            return None

    async def _held(self, d):
        c = TosWsClient()
        try:
            await c.connect()
            await c.login_token(d["token"])
            if not c.authenticated:
                return None
            account = d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            if not account:
                return None
            held = set()
            for it in await c.positions(account, ["QUANTITY"]):
                inst = it.get("instrument", {})
                if inst.get("instrumentType") != "OPTION":
                    continue
                try:
                    q = float(it.get("values", {}).get("QUANTITY", 0) or 0)
                except (TypeError, ValueError):
                    q = 0.0
                if q > 0:
                    held.add(inst.get("symbol") or it.get("symbol"))
            return held
        finally:
            await c.close()

    def test_order(self, root: str, expiration: str, side: str, strike: float,
                   quantity: int = 1, submit: bool = True, timeout: float = 55) -> dict:
        """Test completo end-to-end. Devuelve traza con cada paso (para guardar a JSON)."""
        fut = asyncio.run_coroutine_threadsafe(
            self._test(root, expiration, side, strike, quantity, submit), self.loop)
        return fut.result(timeout=timeout)

    async def _test(self, root, expiration, side, strike, quantity, submit):
        trace = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": False,
                 "request": {"symbol": root, "expiration": expiration, "side": side,
                             "strike": strike, "quantity": quantity, "submit": submit}}
        d = token_store.valid(self.max_hours)
        if not d:
            trace["error"] = "sin token válido — haz login en el webview"
            return trace
        sym = option_symbol(root, expiration, side, strike)
        trace["option_symbol"] = sym
        trace["token_age_hours"] = d.get("age_hours")
        c = TosWsClient()
        try:
            ack = await c.connect()
            trace["handshake"] = {"session": ack.get("session"), "build": ack.get("build")}
            body = await c.login_token(d["token"])
            trace["auth"] = {"authenticated": c.authenticated,
                             "status": body.get("authenticationStatus"),
                             "userCode": body.get("userCode")}
            if not c.authenticated:
                trace["error"] = "login WS rechazado"
                return trace
            account = d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            trace["account"] = account
            if not account:
                trace["error"] = "sin accountCode"
                return trace
            trace["order"] = await c.order_full(account, sym, int(quantity), do_submit=submit)
            trace["ok"] = True
        except Exception as exc:
            trace["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            await c.close()
        return trace

    async def _place(self, root, expiration, side, strike, quantity, order_type, limit_price, submit):
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("sin token válido — haz login en el webview")
        sym = option_symbol(root, expiration, side, strike)
        c = TosWsClient()
        try:
            await c.connect()
            body = await c.login_token(d["token"])
            if not c.authenticated:
                raise RuntimeError(f"login WS rechazado: {body.get('authenticationStatus')}")
            account = d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            if not account:
                raise RuntimeError("sin accountCode disponible")
            # SAFEGUARD anti-short: al CERRAR (qty<0) vender solo lo que la cuenta
            # tiene de ese contrato. Si no hay long, NO vender (evita short en descubierto).
            if int(quantity) < 0:
                held = await c.held_quantity(account, sym)
                if held <= 0:
                    return {"ok": False, "order_id": None, "price": None, "symbol": sym,
                            "stage": "skipped", "reason": "sin long que cerrar (safeguard anti-short)"}
                quantity = -min(abs(int(quantity)), int(held))
            trace = await c.order_full(account, sym, int(quantity),
                                       order_type=order_type, limit_price=limit_price, do_submit=submit)
            from tos.ws_client import submit_result
            price = None
            try:
                o = trace["init"]["orders"][0]
                price = o.get("askPrice") or o.get("midPrice") or o.get("limitPrice")
            except Exception:
                pass
            sr = submit_result(trace.get("submit")) if submit else {"ok": True, "order_id": None}
            return {"ok": bool(sr["ok"]), "order_id": sr["order_id"], "price": price,
                    "symbol": sym, "stage": "submitted" if submit else "confirmed"}
        finally:
            await c.close()
