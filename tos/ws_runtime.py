"""
Runtime WS para el executor: corre un loop asyncio en un hilo que (1) mantiene
la captura del token desde el webview (CDP) y (2) coloca órdenes por WebSocket
reusando el token guardado.

API v1.2 recomendada: buy_option() y sell_option(). place_option() queda como
método base para compatibilidad, pero las integraciones nuevas deben expresar
la intención de compra/venta con los métodos explícitos.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Callable

from .ws_client import LIVE_WS, PAPER_WS

from tos import token_store, ws_token_capture
from tos.ws_client import (
    TosWsClient,
    submit_result,
    canonical_option_symbol,
    option_symbol,
    position_quantity,
    validate_bracket,
    position_quantity_value,
    position_symbol,
    position_values,
)

DEFAULT_POSITION_FIELDS = (
    "QUANTITY",
    "OPEN_PRICE",
    "MARK",
    "PL_DAY",
    "PL_OPEN",
    "PL_PERCENT",
)


def _num(value, default: float | None = None) -> float | None:
    try:
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        if value in (None, ""):
            return default
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").replace("%", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_num(values: dict, *keys: str) -> float | None:
    folded = {str(k).upper().replace(" ", "_"): v for k, v in (values or {}).items()}
    for key in keys:
        val = _num(folded.get(key.upper().replace(" ", "_")))
        if val is not None:
            return val
    return None


def _position_symbol(pos: dict) -> str:
    return position_symbol(pos)


def _position_quantity(pos: dict) -> float:
    return position_quantity(pos)


def _instrument_type(pos: dict) -> str:
    if not isinstance(pos, dict):
        return ""
    nested = pos.get("position") if isinstance(pos.get("position"), dict) else pos
    inst = nested.get("instrument") or {}
    if not isinstance(inst, dict):
        inst = {}
    return str(
        inst.get("instrumentType")
        or inst.get("assetType")
        or nested.get("instrumentType")
        or nested.get("assetType")
        or ""
    )


def _position_prices(pos: dict) -> tuple[float | None, float | None]:
    values = position_values(pos)
    entry = _first_num(values, "AVERAGE_PRICE", "AVG_PRICE", "OPEN_PRICE")
    mark = _first_num(values, "MARK", "MARK_PRICE", "LAST", "LAST_PRICE")
    cost = _first_num(values, "COST")
    qty = abs(_position_quantity(pos))
    if entry is None and cost is not None and qty:
        entry = abs(cost) / qty
        if _is_option_position(pos):
            entry = entry / 100.0
    return entry, mark


def _option_parts(sym: str) -> dict | None:
    normalized = canonical_option_symbol(sym)
    m = re.match(
        r"^\.?(?P<root>[A-Z][A-Z0-9.]*)"
        r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
        r"(?P<cp>[CP])(?P<strike>[0-9.]+)$",
        normalized,
    )
    if not m:
        return None
    root, yy, mm, dd, cp, strike = (
        m.group("root"), m.group("yy"), m.group("mm"), m.group("dd"),
        m.group("cp"), m.group("strike"),
    )
    return {
        "root": root,
        "expiration": f"20{yy}-{mm}-{dd}",
        "side": "CALL" if cp == "C" else "PUT",
        "strike": float(strike),
    }


def _is_option_position(pos: dict) -> bool:
    instrument_type = _instrument_type(pos).upper()
    return "OPTION" in instrument_type or _option_parts(_position_symbol(pos)) is not None


def _enrich_position(pos: dict) -> dict:
    values = position_values(pos)
    qty = _position_quantity(pos)
    entry, mark = _position_prices(pos)
    is_option = _is_option_position(pos)
    multiplier = 100.0 if is_option else 1.0
    open_pl = _first_num(values, "PNL_OPEN", "PL_OPEN", "P/L_OPEN")
    day_pl = _first_num(values, "PNL_DAY", "PL_DAY", "P/L_DAY")
    pnl_pct = None
    if entry and mark:
        pnl_pct = round((mark - entry) / entry * 100.0, 2)
        if open_pl is None:
            open_pl = round((mark - entry) * qty * multiplier, 2)
    return {
        "symbol": (
            canonical_option_symbol(_position_symbol(pos))
            if is_option else _position_symbol(pos)
        ),
        "instrument_type": _instrument_type(pos),
        "is_option": is_option,
        "quantity": qty,
        "entry_price": entry,
        "mark_price": mark,
        "open_pnl": open_pl,
        "day_pnl": day_pl,
        "pnl_pct": pnl_pct,
        "raw": pos,
    }


class TokenProvider:
    """De donde sale el token de sesion de TOS.

    El cliente WebSocket NO necesita navegador: `login_token(token)` basta. Lo
    unico que ataba este runtime al webview era conseguir ese token. Separarlo
    en un proveedor permite que quien ENVIA ordenes viva donde sea, siempre que
    alguien le haga llegar un token vigente.

    Contrato: `arrancar()` puede lanzar tareas de fondo; `renovar()` intenta
    conseguir uno nuevo y devuelve True si lo consiguio.
    """

    #: True si el proveedor puede obtener un token por si mismo.
    puede_renovar = False

    async def arrancar(self, log) -> None:
        return None

    def renovar(self, log, espera_s: float) -> bool:
        return False


class ProveedorCdp(TokenProvider):
    """El de siempre: escucha el trafico del webview por CDP y captura el token.

    Requiere un navegador con sesion iniciada en la misma maquina.
    """

    puede_renovar = True

    def __init__(self, cdp_port: int = 9333,
                 auto_login: Callable[[], object] | None = None):
        self.cdp_port = cdp_port
        self.auto_login = auto_login

    async def arrancar(self, log) -> None:
        while True:
            try:
                await ws_token_capture.run(self.cdp_port, log)
            except Exception:
                await asyncio.sleep(5)

    def renovar(self, log, espera_s: float) -> bool:
        if self.auto_login is None:
            return False
        log("Token TOS vencido/no disponible; ejecutando auto-login.")
        try:
            self.auto_login()
        except Exception as exc:
            raise RuntimeError(f"auto-login fallo: {exc}") from exc
        limite = time.time() + espera_s
        while time.time() < limite:
            if token_store.valid():
                log("Token TOS renovado por auto-login.")
                return True
            time.sleep(1.0)
        return False


class ProveedorAlmacen(TokenProvider):
    """Solo lee el token que otro haya dejado en el almacen. Sin navegador.

    Es el proveedor para un emisor sin interfaz grafica: alguien con sesion
    (el webview, u otra maquina que lo publique) escribe el token; este lo usa.
    No puede renovarlo, y por eso falla EXPLICITAMENTE en vez de quedarse
    esperando a un login que nunca va a ocurrir.
    """

    puede_renovar = False

    async def arrancar(self, log) -> None:
        return None


class WsRuntime:
    def __init__(self, cdp_port: int = 9333, max_hours: float = 8.0, log=print,
                 ws_url: str = PAPER_WS, account: str | None = None,
                 auto_login: Callable[[], object] | None = None,
                 token_wait_seconds: float = 90.0,
                 token_provider: "TokenProvider | None" = None,
                 enrutador: "Callable[[], tuple[str, str | None]] | None" = None):
        # Sin proveedor explicito se conserva el comportamiento de siempre
        # (captura CDP), para no romper a los hosts Qt que ya lo usan.
        self.token_provider = token_provider or ProveedorCdp(cdp_port, auto_login)
        self.cdp_port = cdp_port
        self.max_hours = max_hours
        self.log = log
        self.auto_login = auto_login
        self.token_wait_seconds = float(token_wait_seconds)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        # Endpoint del broker. PAPER por defecto: el modo real se pide
        # EXPLICITAMENTE pasando LIVE_WS, nunca por omision.
        self.ws_url = ws_url
        # Cuenta EXPLICITA. Si es None se usa la del token y, si tampoco,
        # la primera "tradeable" que devuelva el broker — que es justo como
        # se manda una orden a la cuenta equivocada teniendo varias.
        self.account = account
        # Resuelve (ws_url, cuenta) JUSTO ANTES de cada orden en vez de fijarlo
        # al construir el runtime. Asi lo que manda es lo que el operador tiene
        # seleccionado en ese momento, no lo que hubiera al arrancar la app: una
        # app abierta desde ayer no puede enviar a un sitio que ya cambio.
        self.enrutador = enrutador

    def _resolver_enrutado(self) -> None:
        """Aplica el enrutado vigente. Silencioso si no hay enrutador."""
        if self.enrutador is None:
            return
        try:
            url, cuenta = self.enrutador()
        except Exception as exc:
            raise RuntimeError(f"no se pudo resolver el enrutado: {exc}") from exc
        if not url:
            raise RuntimeError("el enrutador no devolvio endpoint")
        if url != self.ws_url or cuenta != self.account:
            destino = "REAL" if "papermoney" not in url else "PAPER"
            self.log(f"Enrutado de la orden: {destino} · cuenta {cuenta or '(sin cuenta)'}")
        self.ws_url, self.account = url, cuenta

    def start(self):
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._capture_forever())
        self.loop.run_forever()

    async def _capture_forever(self):
        await self.token_provider.arrancar(self.log)

    def token_status(self) -> dict | None:
        return token_store.valid(self.max_hours)

    def set_auto_login(self, callback: Callable[[], object] | None):
        """Registra el callback que el host usa para renovar sesión.

        El callback debe iniciar el auto-login del webview. Este runtime espera
        hasta token_wait_seconds a que la captura CDP guarde un token nuevo.
        En hosts Qt, normalmente el callback emite una señal al hilo de UI que
        llama a TosAutomation.ensure_logged_in_paper(...).
        """
        self.auto_login = callback
        if isinstance(self.token_provider, ProveedorCdp):
            self.token_provider.auto_login = callback

    def _ensure_token(self) -> dict:
        d = token_store.valid(self.max_hours)
        if d:
            return d
        if not self.token_provider.puede_renovar:
            # Un emisor sin navegador no puede resolverlo solo. Falla claro en
            # vez de esperar a un login que nunca va a ocurrir.
            raise RuntimeError(
                "sin token válido y este proveedor no puede renovarlo — "
                "alguien con sesión tiene que publicar un token vigente")
        if not self.token_provider.renovar(self.log, self.token_wait_seconds):
            raise RuntimeError("no se consiguió un token válido a tiempo")
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("el proveedor dijo haber renovado pero no hay token")
        return d

    def buy_option(self, root: str, expiration: str, side: str, strike: float,
                   quantity: int = 1, order_type: str = "MARKET", limit_price=None,
                   submit: bool = True, timeout: float = 40) -> dict:
        """Compra contratos de opción por WebSocket.

        side debe ser CALL/PUT. No registra posición ni gestiona salidas.
        """
        return self.place_option(root=root, expiration=expiration, side=side,
                                 strike=strike, quantity=abs(int(quantity)),
                                 order_type=order_type, limit_price=limit_price,
                                 submit=submit, timeout=timeout)

    def sell_option(self, root: str, expiration: str, side: str, strike: float,
                    quantity: int = 1, order_type: str = "MARKET", limit_price=None,
                    submit: bool = True, timeout: float = 40) -> dict:
        """Vende/cierra contratos de opción por WebSocket.

        Mantiene el safeguard anti-short: si la cuenta no tiene long del
        contrato, no envía venta en descubierto.
        """
        return self.place_option(root=root, expiration=expiration, side=side,
                                 strike=strike, quantity=-abs(int(quantity)),
                                 order_type=order_type, limit_price=limit_price,
                                 submit=submit, timeout=timeout)

    def place_bracket(self, symbol: str, quantity: int, take_profit: float,
                      stop_loss: float, entry_type: str = "LIMIT", entry_limit=None,
                      tif: str = "DAY", submit: bool = True,
                      timeout: float = 40) -> dict:
        """Entrada de compra + take profit (LIMIT) + stop loss (STOP) en una
        sola orden 1st-trgs-OCO de TOS.

        `symbol` es el símbolo TOS tal cual (acción ``F`` u opción
        ``.SPY260715P755``). Las salidas solo se activan si la entrada se
        llena y se cancelan entre sí. Valida precios antes de conectar.
        """
        entry_type, tif = str(entry_type).upper(), str(tif).upper()
        validate_bracket(quantity, take_profit, stop_loss, entry_type, entry_limit, tif)
        self._resolver_enrutado()
        self._ensure_token()
        fut = asyncio.run_coroutine_threadsafe(
            self._bracket(symbol, int(quantity), take_profit, stop_loss,
                          entry_type, entry_limit, tif, submit),
            self.loop)
        return fut.result(timeout=timeout)

    def bracket_option(self, root: str, expiration: str, side: str, strike: float,
                       quantity: int, take_profit: float, stop_loss: float,
                       entry_type: str = "LIMIT", entry_limit=None,
                       tif: str = "DAY", submit: bool = True,
                       timeout: float = 40) -> dict:
        """`place_bracket` para un contrato de opción (precios por contrato)."""
        return self.place_bracket(option_symbol(root, expiration, side, strike),
                                  quantity, take_profit, stop_loss,
                                  entry_type=entry_type, entry_limit=entry_limit,
                                  tif=tif, submit=submit, timeout=timeout)

    def cancel_order(self, order_id, timeout: float = 25) -> dict:
        """Cancela una orden por id. Con el id de la entrada de un bracket
        cancela también sus salidas pendientes."""
        self._resolver_enrutado()
        self._ensure_token()
        fut = asyncio.run_coroutine_threadsafe(self._cancel(order_id), self.loop)
        return fut.result(timeout=timeout)

    def list_accounts(self, timeout: float = 25) -> list[dict]:
        """Cuentas visibles con el token actual, para poder ELEGIR una."""
        self._ensure_token()
        fut = asyncio.run_coroutine_threadsafe(self._accounts(), self.loop)
        return fut.result(timeout=timeout)

    async def _accounts(self):
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("sin token válido — haz login en el webview")
        c = TosWsClient(self.ws_url)
        try:
            await c.connect()
            await c.login_token(d["token"])
            return await c.accounts()
        finally:
            await c.close()

    def positions(self, fields: tuple[str, ...] | list[str] | None = None,
                  timeout: float = 25) -> list[dict]:
        """Devuelve posiciones crudas del broker via WebSocket."""
        self._ensure_token()
        fut = asyncio.run_coroutine_threadsafe(
            self._positions(fields or DEFAULT_POSITION_FIELDS), self.loop)
        return fut.result(timeout=timeout)

    def open_option_positions(self, fields: tuple[str, ...] | list[str] | None = None,
                              timeout: float = 25) -> list[dict]:
        """Devuelve posiciones OPTION abiertas, enriquecidas con P/L calculable."""
        opened = []
        for position in self.positions(fields=fields, timeout=timeout):
            if not _is_option_position(position):
                continue
            quantity = position_quantity_value(position)
            if quantity is None:
                raise RuntimeError(
                    "position_quantity_unrecognized:"
                    + canonical_option_symbol(_position_symbol(position))
                )
            if quantity != 0:
                opened.append(_enrich_position(position))
        return opened

    def day_summary(self, fields: tuple[str, ...] | list[str] | None = None,
                    timeout: float = 25) -> dict:
        """Resumen de posiciones abiertas y P/L disponible/calculado del dia."""
        raw_positions = self.positions(fields=fields, timeout=timeout)
        for position in raw_positions:
            if _is_option_position(position) and position_quantity_value(position) is None:
                raise RuntimeError(
                    "position_quantity_unrecognized:"
                    + canonical_option_symbol(_position_symbol(position))
                )
        positions = [_enrich_position(p) for p in raw_positions]
        open_positions = [p for p in positions if p["quantity"] != 0]
        day_known = [p["day_pnl"] for p in open_positions if p["day_pnl"] is not None]
        open_known = [p["open_pnl"] for p in open_positions if p["open_pnl"] is not None]
        return {
            "ok": True,
            "open_count": len(open_positions),
            "option_count": sum(1 for p in open_positions if p["is_option"]),
            "day_pnl": round(sum(day_known), 2) if day_known else None,
            "open_pnl": round(sum(open_known), 2) if open_known else None,
            "positions": open_positions,
        }

    def auto_exit_percent(self, take_profit_pct: float | None = None,
                          stop_loss_pct: float | None = None,
                          submit: bool = True, dry_run: bool = False,
                          timeout: float = 55) -> dict:
        """Revisa posiciones OPTION abiertas y vende si cumplen umbral porcentual.

        - take_profit_pct: cierra si P/L% >= este valor.
        - stop_loss_pct: cierra si P/L% <= -abs(stop_loss_pct).

        Esta funcion no elige estrategias ni abre posiciones; solo gestiona
        salida automatica por porcentaje sobre posiciones abiertas.
        """
        if take_profit_pct is None and stop_loss_pct is None:
            raise ValueError("define take_profit_pct o stop_loss_pct")
        self._ensure_token()
        fut = asyncio.run_coroutine_threadsafe(
            self._auto_exit_percent(take_profit_pct, stop_loss_pct, submit, dry_run),
            self.loop)
        return fut.result(timeout=timeout)

    def place_option(self, root: str, expiration: str, side: str, strike: float,
                     quantity: int, order_type: str = "MARKET", limit_price=None,
                     submit: bool = False, timeout: float = 40) -> dict:
        """quantity>0 compra, <0 venta. submit=False solo hace el review (CONFIRM)."""
        self._resolver_enrutado()
        self._ensure_token()
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
        c = TosWsClient(self.ws_url)
        try:
            await c.connect()
            await c.login_token(d["token"])
            if not c.authenticated:
                return None
            account = self.account or d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            if not account:
                return None
            held = set()
            for it in await c.positions(account, ["QUANTITY"]):
                if not _is_option_position(it):
                    continue
                q = position_quantity_value(it)
                if q is None:
                    raise RuntimeError(
                        "position_quantity_unrecognized:"
                        + canonical_option_symbol(_position_symbol(it))
                    )
                if q > 0:
                    held.add(canonical_option_symbol(_position_symbol(it)))
            return held
        finally:
            await c.close()

    async def _positions(self, fields):
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("sin token válido — haz login en el webview")
        c = TosWsClient(self.ws_url)
        try:
            await c.connect()
            body = await c.login_token(d["token"])
            if not c.authenticated:
                raise RuntimeError(f"login WS rechazado: {body.get('authenticationStatus')}")
            account = self.account or d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            if not account:
                raise RuntimeError("sin accountCode disponible")
            return await c.positions(account, fields)
        finally:
            await c.close()

    async def _authed_client(self) -> tuple[TosWsClient, str]:
        """Cliente conectado y autenticado, con la cuenta resuelta.
        Quien llama debe cerrarlo."""
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("sin token válido — haz login en el webview")
        c = TosWsClient(self.ws_url)
        try:
            await c.connect()
            body = await c.login_token(d["token"])
            if not c.authenticated:
                raise RuntimeError(f"login WS rechazado: {body.get('authenticationStatus')}")
            account = self.account or d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            if not account:
                raise RuntimeError("sin accountCode disponible")
            return c, account
        except Exception:
            await c.close()
            raise

    async def _bracket(self, sym, quantity, take_profit, stop_loss,
                       entry_type, entry_limit, tif, submit):
        c, account = await self._authed_client()
        try:
            trace = await c.place_bracket_order(
                account, sym, quantity, take_profit, stop_loss,
                entry_type=entry_type, entry_limit=entry_limit, tif=tif,
                submit=submit)
        finally:
            await c.close()
        order_ids = trace.get("order_ids") or {}
        if not submit:
            state, accepted = "preview", True
        elif trace.get("submit_error"):
            state, accepted = "unknown_submit_transport_error", False
        elif order_ids.get("entry"):
            state, accepted = "accepted", True
        else:
            # Igual que en órdenes simples: sin orderId no se afirma rechazo.
            sr = submit_result(trace.get("submit"))
            state, accepted = sr["submission_state"], False
        return {
            "ok": accepted, "accepted": accepted, "symbol": sym,
            "stage": "submitted" if submit else "confirmed",
            "submission_state": state,
            "submission_may_have_occurred": bool(submit and state != "rejected"),
            "order_id": order_ids.get("entry"), "order_ids": order_ids,
            "error": trace.get("submit_error"),
            "trace": trace,
        }

    async def _cancel(self, order_id):
        c, _account = await self._authed_client()
        try:
            body = await c.cancel_order(order_id)
        finally:
            await c.close()
        return {"ok": True, "order_id": int(order_id), "body": body}

    async def _auto_exit_percent(self, take_profit_pct, stop_loss_pct, submit, dry_run):
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("sin token válido — haz login en el webview")
        stop_threshold = -abs(float(stop_loss_pct)) if stop_loss_pct is not None else None
        target_threshold = float(take_profit_pct) if take_profit_pct is not None else None
        c = TosWsClient(self.ws_url)
        actions = []
        try:
            await c.connect()
            body = await c.login_token(d["token"])
            if not c.authenticated:
                raise RuntimeError(f"login WS rechazado: {body.get('authenticationStatus')}")
            account = self.account or d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            if not account:
                raise RuntimeError("sin accountCode disponible")
            for raw in await c.positions(account, DEFAULT_POSITION_FIELDS):
                if not _is_option_position(raw):
                    continue
                if position_quantity_value(raw) is None:
                    raise RuntimeError(
                        "position_quantity_unrecognized:"
                        + canonical_option_symbol(_position_symbol(raw))
                    )
                pos = _enrich_position(raw)
                qty = int(pos["quantity"])
                if qty <= 0 or pos["pnl_pct"] is None:
                    actions.append({"symbol": pos["symbol"], "action": "skip",
                                    "reason": "sin cantidad long o P/L% calculable",
                                    "position": pos})
                    continue
                reason = None
                if target_threshold is not None and pos["pnl_pct"] >= target_threshold:
                    reason = f"take_profit_{target_threshold:g}%"
                if reason is None and stop_threshold is not None and pos["pnl_pct"] <= stop_threshold:
                    reason = f"stop_loss_{abs(stop_threshold):g}%"
                if reason is None:
                    actions.append({"symbol": pos["symbol"], "action": "hold",
                                    "reason": "umbral no alcanzado", "position": pos})
                    continue
                parts = _option_parts(pos["symbol"])
                if not parts:
                    actions.append({"symbol": pos["symbol"], "action": "skip",
                                    "reason": "simbolo de opcion no parseable",
                                    "position": pos})
                    continue
                if dry_run:
                    actions.append({"symbol": pos["symbol"], "action": "would_sell",
                                    "reason": reason, "quantity": qty,
                                    "position": pos})
                    continue
                trace = await c.order_full(
                    account,
                    option_symbol(parts["root"], parts["expiration"], parts["side"], parts["strike"]),
                    -qty,
                    do_submit=bool(submit),
                )
                from tos.ws_client import submit_result
                sr = submit_result(trace.get("submit")) if submit else {"ok": True, "order_id": None}
                actions.append({"symbol": pos["symbol"], "action": "sell",
                                "reason": reason, "quantity": qty,
                                "ok": bool(sr["ok"]), "order_id": sr["order_id"],
                                "stage": "submitted" if submit else "confirmed",
                                "position": pos})
        finally:
            await c.close()
        return {"ok": True, "actions": actions}

    def test_order(self, root: str, expiration: str, side: str, strike: float,
                   quantity: int = 1, submit: bool = True, timeout: float = 55) -> dict:
        """Test completo end-to-end. Devuelve traza con cada paso (para guardar a JSON)."""
        self._ensure_token()
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
        c = TosWsClient(self.ws_url)
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
            account = self.account or d.get("account_code")
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
        c = TosWsClient(self.ws_url)
        try:
            await c.connect()
            body = await c.login_token(d["token"])
            if not c.authenticated:
                raise RuntimeError(f"login WS rechazado: {body.get('authenticationStatus')}")
            account = self.account or d.get("account_code")
            if not account:
                await c.accounts()
                account = c.account_code
            if not account:
                raise RuntimeError("sin accountCode disponible")
            try:
                held_before = await c.held_quantity(account, sym)
            except Exception as exc:
                error = f"pre_submit_position_check_failed:{type(exc).__name__}: {exc}"
                self.log(f"Orden bloqueada antes de SUBMIT {sym}: {error}")
                return {
                    "ok": False,
                    "accepted": False,
                    "order_id": None,
                    "price": None,
                    "symbol": sym,
                    "stage": "pre_submit_blocked",
                    "submission_state": "rejected_pre_submit",
                    "explicit_rejection": True,
                    "submission_may_have_occurred": False,
                    "reason": error,
                    "error": error,
                    "position_effect_confirmed": False,
                }
            # SAFEGUARD anti-short: al CERRAR (qty<0) vender solo lo que la cuenta
            # tiene de ese contrato. Si no hay long, NO vender (evita short en descubierto).
            if int(quantity) < 0:
                if held_before <= 0:
                    return {"ok": False, "order_id": None, "price": None, "symbol": sym,
                            "stage": "skipped", "reason": "sin long que cerrar (safeguard anti-short)",
                            "held_before": held_before, "held_after": held_before,
                            "position_effect_confirmed": True}
                quantity = -min(abs(int(quantity)), int(held_before))
            trace = await c.order_full(account, sym, int(quantity),
                                       order_type=order_type, limit_price=limit_price, do_submit=submit)
            from tos.ws_client import submit_result
            price = None
            try:
                o = trace["init"]["orders"][0]
                price = o.get("askPrice") or o.get("midPrice") or o.get("limitPrice")
            except Exception:
                pass
            submit_error = trace.get("submit_error")
            if submit and submit_error:
                sr = {
                    "ok": False,
                    "accepted": False,
                    "order_id": None,
                    "submission_state": "unknown_submit_transport_error",
                    "explicit_rejection": False,
                    "reason": f"submit_transport_error:{submit_error}",
                }
            else:
                sr = submit_result(trace.get("submit")) if submit else {
                    "ok": True, "accepted": True, "order_id": None,
                    "submission_state": "preview", "explicit_rejection": False,
                    "reason": None,
                }
            held_after = held_before
            position_effect_confirmed = not submit
            position_reconcile_error = None
            if submit:
                # El frame SUBMIT ya salió aunque su respuesta no incluya
                # orderId. Siempre reconciliamos posición: su efecto puede
                # confirmar la aceptación y, si no aparece, el estado queda
                # unknown (nunca "rechazado" por omisión ni reintentable).
                deadline = asyncio.get_running_loop().time() + 12.0
                expected = (
                    held_before + int(quantity)
                    if int(quantity) > 0
                    else max(0.0, held_before - abs(int(quantity)))
                )
                while True:
                    try:
                        held_after = await c.held_quantity(account, sym)
                    except Exception as exc:
                        position_reconcile_error = f"{type(exc).__name__}: {exc}"
                        self.log(
                            f"ALERTA ejecución ambigua {sym}: SUBMIT ya intentado; "
                            f"reconciliación de posición falló ({position_reconcile_error})"
                        )
                        break
                    if (
                        (int(quantity) > 0 and held_after >= expected)
                        or (int(quantity) < 0 and held_after <= expected)
                    ):
                        position_effect_confirmed = True
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.5)
            accepted = bool(sr.get("accepted") or position_effect_confirmed)
            if position_effect_confirmed and submit:
                submission_state = "position_effect_confirmed"
            elif sr.get("explicit_rejection"):
                submission_state = "rejected"
            elif accepted:
                submission_state = str(sr.get("submission_state") or "accepted")
            elif position_reconcile_error:
                submission_state = "unknown_position_reconcile_error"
            else:
                submission_state = str(sr.get("submission_state") or "unknown")
            reason = None if accepted else sr.get("reason")
            if not accepted and position_reconcile_error and not sr.get("explicit_rejection"):
                reason = f"post_submit_position_reconcile_error:{position_reconcile_error}"
            return {"ok": accepted, "accepted": accepted,
                    "submission_state": submission_state,
                    "explicit_rejection": bool(sr.get("explicit_rejection")),
                    "submission_may_have_occurred": bool(
                        submit and not sr.get("explicit_rejection")
                    ),
                    "reason": reason,
                    "error": reason,
                    "submit_transport_error": submit_error,
                    "position_reconcile_error": position_reconcile_error,
                    "order_id": sr.get("order_id"), "price": price,
                    "symbol": sym, "stage": "submitted" if submit else "confirmed",
                    "held_before": held_before, "held_after": held_after,
                    "position_effect_confirmed": position_effect_confirmed,
                    "position_closed_confirmed": bool(
                        submit and int(quantity) < 0 and position_effect_confirmed
                        and held_after <= 0
                    )}
        finally:
            await c.close()
