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
import re
import ssl
import time
from decimal import Decimal, InvalidOperation

import certifi
import websockets

_SSL = ssl.create_default_context(cafile=certifi.where())

PAPER_WS = "wss://papermoney-services.schwab.com/Services/WsJson"
LIVE_WS = "wss://thinkorswim-services.schwab.com/Services/WsJson"
ORIGIN = "https://trade.thinkorswim.com"
HANDSHAKE = {"ver": "27.*.*", "fmt": "json-patches-structured", "heartbeat": "2s"}


_OPTION_SYMBOL_RE = re.compile(
    r"^\.?(?P<root>[A-Z][A-Z0-9.]*)"
    r"(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d+(?:\.\d+)?)$"
)


def canonical_option_symbol(value) -> str:
    """Normaliza símbolos nativos TOS y OCC al formato ``.ROOTYYMMDDCSTRIKE``.

    TOS no siempre devuelve el símbolo con la misma representación usada para
    enviar la orden. Por ejemplo, ``.SPY260715P755`` y
    ``SPY260715P00755000`` identifican el mismo contrato. Compararlos como
    texto literal puede ocultar una posición real y bloquear su cierre.
    """
    raw = str(value or "").strip().upper()
    compact = re.sub(r"\s+", "", raw).lstrip("$")
    # Algunos payloads añaden el multiplicador al símbolo (p. ej. ``:100``).
    compact = compact.split(":", 1)[0]
    match = _OPTION_SYMBOL_RE.match(compact)
    if not match:
        return compact
    strike_token = match.group("strike")
    try:
        # OCC usa ocho dígitos y expresa el strike en milésimas.
        if "." not in strike_token and len(strike_token) == 8:
            strike = Decimal(int(strike_token)) / Decimal(1000)
        else:
            strike = Decimal(strike_token)
    except (InvalidOperation, ValueError):
        return compact
    strike_text = format(strike.normalize(), "f")
    if "." in strike_text:
        strike_text = strike_text.rstrip("0").rstrip(".")
    return (
        f".{match.group('root')}{match.group('expiry')}"
        f"{match.group('right')}{strike_text}"
    )


def position_values(position: dict) -> dict:
    """Convierte las variantes dict/list de ``values`` a un mapping estable."""
    if not isinstance(position, dict):
        return {}
    raw = position.get("values")
    if raw is None and isinstance(position.get("position"), dict):
        raw = position["position"].get("values")
    if isinstance(raw, dict):
        nested = raw.get("items")
        if isinstance(nested, list):
            raw = nested
        else:
            return dict(raw)
    values: dict = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("name") or item.get("key") or item.get("field")
            if key is not None and "value" in item:
                values[str(key)] = item.get("value")
                continue
            if len(item) == 1:
                key, value = next(iter(item.items()))
                values[str(key)] = value
    return values


def position_symbol(position: dict) -> str:
    """Extrae un símbolo aun cuando TOS cambie su ubicación/nombre de campo."""
    if not isinstance(position, dict):
        return ""
    nested_position = position.get("position")
    containers = [position]
    if isinstance(nested_position, dict):
        containers.append(nested_position)
    for container in containers:
        instrument = container.get("instrument")
        candidates = []
        if isinstance(instrument, dict):
            candidates.extend(
                instrument.get(key)
                for key in ("symbol", "optionSymbol", "displaySymbol", "code")
            )
        candidates.extend(
            container.get(key)
            for key in ("symbol", "optionSymbol", "displaySymbol", "contractSymbol")
        )
        for candidate in candidates:
            if candidate not in (None, ""):
                return str(candidate)
    return ""


def _number(value) -> float | None:
    if isinstance(value, dict) and "value" in value:
        value = value.get("value")
    try:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return None


def position_quantity_value(position: dict) -> float | None:
    """Extrae la cantidad neta; ``None`` significa esquema no reconocido."""
    if not isinstance(position, dict):
        return None
    values = {
        str(key).upper().replace(" ", "_"): value
        for key, value in position_values(position).items()
    }
    for key in ("QUANTITY", "POSITION_QUANTITY", "NET_QUANTITY", "QTY"):
        value = _number(values.get(key))
        if value is not None:
            return value
    nested_position = position.get("position")
    containers = [position]
    if isinstance(nested_position, dict):
        containers.append(nested_position)
    for container in containers:
        for key in ("quantity", "positionQuantity", "netQuantity", "qty"):
            value = _number(container.get(key))
            if value is not None:
                return value
        long_qty = _number(container.get("longQuantity"))
        short_qty = _number(container.get("shortQuantity"))
        if long_qty is not None or short_qty is not None:
            return float(long_qty or 0.0) - float(short_qty or 0.0)
    return None


def position_quantity(position: dict) -> float:
    """Compatibilidad numérica; para decisiones de riesgo use ``*_value``."""
    value = position_quantity_value(position)
    return float(value) if value is not None else 0.0


def _looks_like_position(value) -> bool:
    if not isinstance(value, dict):
        return False
    candidate = value.get("position") if isinstance(value.get("position"), dict) else value
    has_identity = bool(
        candidate.get("instrument")
        or any(candidate.get(key) not in (None, "") for key in (
            "symbol", "optionSymbol", "displaySymbol", "contractSymbol",
        ))
    )
    has_position_data = bool(
        "values" in candidate
        or any(key in candidate for key in (
            "quantity", "positionQuantity", "netQuantity", "longQuantity", "shortQuantity", "qty",
        ))
    )
    return has_identity and has_position_data


def positions_from_body(body) -> list[dict]:
    """Extrae posiciones de payloads conocidos y rechaza esquemas ambiguos.

    Devolver ``[]`` ante un esquema desconocido equivale a afirmar que no hay
    exposición. Para un executor esa afirmación falsa es peligrosa, así que el
    parser sólo devuelve vacío cuando el broker envió una colección vacía de
    forma explícita.
    """
    explicit_collection = False
    rows: list[dict] = []

    def is_empty_account_snapshot(value) -> bool:
        """TOS puede responder posiciones vacías como ``{"account": ...}``.

        No incluye ``items: []`` ni ``positions: []``.  Ese payload sigue siendo
        un snapshot explícito del servicio de posiciones: trae la cuenta, pero
        ninguna colección/fila.  Tratarlo como esquema desconocido bloquea las
        entradas aun cuando el broker está sano y la cuenta está plana.
        """
        if not isinstance(value, dict):
            return False
        keys = {str(key).lower().replace("_", "") for key in value}
        account_keys = {"account", "accountcode"}
        return bool(keys & account_keys) and keys <= (account_keys | {"type"})

    def visit(value, *, collection: bool = False):
        nonlocal explicit_collection
        if _looks_like_position(value):
            rows.append(value.get("position") if isinstance(value.get("position"), dict) else value)
            return
        if isinstance(value, list):
            if collection:
                explicit_collection = True
            for item in value:
                visit(item, collection=collection)
            return
        if not isinstance(value, dict):
            return
        if collection:
            explicit_collection = True
            for nested in value.values():
                visit(nested, collection=True)
            return
        if is_empty_account_snapshot(value):
            explicit_collection = True
            return
        for key, nested in value.items():
            folded = str(key).lower().replace("_", "")
            is_collection = folded in {"items", "positions", "positionitems"}
            if is_collection and isinstance(nested, (list, dict)):
                explicit_collection = True
            # ``patches[].value`` puede contener la colección completa.
            if is_collection or folded in {"body", "data", "result", "value", "patches"}:
                visit(nested, collection=is_collection)

    if isinstance(body, list):
        explicit_collection = True
        visit(body, collection=True)
    elif isinstance(body, dict):
        visit(body)
    else:
        raise RuntimeError("positions_payload_unrecognized:body_not_object_or_list")
    if rows:
        return rows
    if explicit_collection:
        return []
    raise RuntimeError("positions_payload_unrecognized:no_explicit_positions_collection")


def submit_result(submit_body: dict | None) -> dict:
    """Clasifica la respuesta a SUBMIT sin confundir ausencia de id con rechazo.

    El gateway puede aceptar el frame y no incluir ``orderId`` en el patch que
    responde a esta petición. En ese caso el resultado es *unknown*: reintentar
    BUY/SELL automáticamente podría duplicar la orden. Sólo se devuelve
    ``rejected`` cuando el cuerpo contiene evidencia explícita de rechazo.
    """
    order_id = None
    for p in (submit_body or {}).get("patches", []):
        if p.get("op") == "replace" and str(p.get("path", "")).endswith("/orderId"):
            order_id = p.get("value")
    if order_id not in (None, 0, "0", ""):
        return {
            "ok": True,
            "accepted": True,
            "submission_state": "accepted",
            "explicit_rejection": False,
            "order_id": order_id,
            "reason": None,
        }

    rejection_reason = None
    rejected_statuses = {"rejected", "denied", "failed", "invalid", "cancelled", "canceled"}
    for patch in (submit_body or {}).get("patches", []):
        path = str(patch.get("path") or "").lower()
        value = patch.get("value")
        key = path.rsplit("/", 1)[-1]
        if key in {"error", "errors", "rejectionreason", "rejectreason"} and value:
            rejection_reason = str(value)
            break
        if key in {"status", "orderstatus", "state"} and str(value).lower() in rejected_statuses:
            rejection_reason = str(value)
            break
    for key in ("error", "errors", "rejectionReason", "rejectReason"):
        value = (submit_body or {}).get(key)
        if value:
            rejection_reason = str(value)
            break

    state = "rejected" if rejection_reason else "unknown"
    return {
        "ok": False,
        "accepted": False,
        "submission_state": state,
        "explicit_rejection": bool(rejection_reason),
        "order_id": None,
        "reason": rejection_reason or "submit_response_without_order_id",
    }


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
        return positions_from_body(r.get("body"))

    async def held_quantity(self, account: str, option_symbol: str) -> float:
        """Cantidad LONG que la cuenta tiene de ese contrato (0 si no hay). Para
        no vender en descubierto al cerrar."""
        target = canonical_option_symbol(option_symbol)
        held = 0.0
        for it in await self.positions(account, ["QUANTITY"]):
            if canonical_option_symbol(position_symbol(it)) == target:
                quantity = position_quantity_value(it)
                if quantity is None:
                    raise RuntimeError(
                        f"position_quantity_unrecognized:{target}"
                    )
                held += quantity
        return held

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
            try:
                submit = await self._request("place_order", self._order_params(
                    "SUBMIT", "EDIT_ORDER", account, sym, qty, order_type, limit_price, 0), req_id=rid, ver=0)
                trace["submit"] = submit.get("body")
            except Exception as exc:
                # ``_request`` ya intentó enviar el frame SUBMIT. Un timeout o
                # EOF posterior no demuestra rechazo y no puede provocar un
                # reintento automático de la orden.
                trace["submit"] = None
                trace["submit_error"] = f"{type(exc).__name__}: {exc}"
                trace["submission_may_have_occurred"] = True
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
