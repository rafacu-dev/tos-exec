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

from tos import token_store, ws_token_capture
from tos.ws_client import TosWsClient, option_symbol

DEFAULT_POSITION_FIELDS = (
    "QUANTITY",
    "AVERAGE_PRICE",
    "AVG_PRICE",
    "OPEN_PRICE",
    "MARK",
    "MARK_PRICE",
    "LAST",
    "LAST_PRICE",
    "COST",
    "PNL_DAY",
    "PL_DAY",
    "P/L_DAY",
    "PNL_OPEN",
    "PL_OPEN",
    "P/L_OPEN",
)


def _num(value, default: float | None = None) -> float | None:
    try:
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
    inst = pos.get("instrument") or {}
    return str(inst.get("symbol") or pos.get("symbol") or "")


def _position_quantity(pos: dict) -> float:
    return _first_num(pos.get("values") or {}, "QUANTITY") or 0.0


def _position_prices(pos: dict) -> tuple[float | None, float | None]:
    values = pos.get("values") or {}
    entry = _first_num(values, "AVERAGE_PRICE", "AVG_PRICE", "OPEN_PRICE")
    mark = _first_num(values, "MARK", "MARK_PRICE", "LAST", "LAST_PRICE")
    cost = _first_num(values, "COST")
    qty = abs(_position_quantity(pos))
    if entry is None and cost is not None and qty:
        entry = abs(cost) / qty
        if (pos.get("instrument") or {}).get("instrumentType") == "OPTION":
            entry = entry / 100.0
    return entry, mark


def _option_parts(sym: str) -> dict | None:
    m = re.match(r"^\.?([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])([0-9.]+)$", str(sym).upper())
    if not m:
        return None
    root, yy, mm, dd, cp, strike = m.groups()
    return {
        "root": root,
        "expiration": f"20{yy}-{mm}-{dd}",
        "side": "CALL" if cp == "C" else "PUT",
        "strike": float(strike),
    }


def _enrich_position(pos: dict) -> dict:
    inst = pos.get("instrument") or {}
    values = pos.get("values") or {}
    qty = _position_quantity(pos)
    entry, mark = _position_prices(pos)
    multiplier = 100.0 if inst.get("instrumentType") == "OPTION" else 1.0
    open_pl = _first_num(values, "PNL_OPEN", "PL_OPEN", "P/L_OPEN")
    day_pl = _first_num(values, "PNL_DAY", "PL_DAY", "P/L_DAY")
    pnl_pct = None
    if entry and mark:
        pnl_pct = round((mark - entry) / entry * 100.0, 2)
        if open_pl is None:
            open_pl = round((mark - entry) * qty * multiplier, 2)
    return {
        "symbol": _position_symbol(pos),
        "instrument_type": inst.get("instrumentType"),
        "quantity": qty,
        "entry_price": entry,
        "mark_price": mark,
        "open_pnl": open_pl,
        "day_pnl": day_pl,
        "pnl_pct": pnl_pct,
        "raw": pos,
    }


class WsRuntime:
    def __init__(self, cdp_port: int = 9333, max_hours: float = 8.0, log=print,
                 auto_login: Callable[[], object] | None = None,
                 token_wait_seconds: float = 90.0):
        self.cdp_port = cdp_port
        self.max_hours = max_hours
        self.log = log
        self.auto_login = auto_login
        self.token_wait_seconds = float(token_wait_seconds)
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

    def set_auto_login(self, callback: Callable[[], object] | None):
        """Registra el callback que el host usa para renovar sesión.

        El callback debe iniciar el auto-login del webview. Este runtime espera
        hasta token_wait_seconds a que la captura CDP guarde un token nuevo.
        En hosts Qt, normalmente el callback emite una señal al hilo de UI que
        llama a TosAutomation.ensure_logged_in_paper(...).
        """
        self.auto_login = callback

    def _ensure_token(self) -> dict:
        d = token_store.valid(self.max_hours)
        if d:
            return d
        if self.auto_login is None:
            raise RuntimeError("sin token válido — haz login en el webview")
        self.log("Token TOS vencido/no disponible; ejecutando auto-login.")
        try:
            self.auto_login()
        except Exception as exc:
            raise RuntimeError(f"auto-login falló: {exc}") from exc
        deadline = time.time() + self.token_wait_seconds
        while time.time() < deadline:
            d = token_store.valid(self.max_hours)
            if d:
                self.log("Token TOS renovado por auto-login.")
                return d
            time.sleep(1.0)
        raise RuntimeError("auto-login no produjo un token válido a tiempo")

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
        return [
            _enrich_position(p)
            for p in self.positions(fields=fields, timeout=timeout)
            if (p.get("instrument") or {}).get("instrumentType") == "OPTION"
            and _position_quantity(p) != 0
        ]

    def day_summary(self, fields: tuple[str, ...] | list[str] | None = None,
                    timeout: float = 25) -> dict:
        """Resumen de posiciones abiertas y P/L disponible/calculado del dia."""
        positions = [_enrich_position(p) for p in self.positions(fields=fields, timeout=timeout)]
        open_positions = [p for p in positions if p["quantity"] != 0]
        day_known = [p["day_pnl"] for p in open_positions if p["day_pnl"] is not None]
        open_known = [p["open_pnl"] for p in open_positions if p["open_pnl"] is not None]
        return {
            "ok": True,
            "open_count": len(open_positions),
            "option_count": sum(1 for p in open_positions if p["instrument_type"] == "OPTION"),
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

    async def _positions(self, fields):
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("sin token válido — haz login en el webview")
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
            return await c.positions(account, fields)
        finally:
            await c.close()

    async def _auto_exit_percent(self, take_profit_pct, stop_loss_pct, submit, dry_run):
        d = token_store.valid(self.max_hours)
        if not d:
            raise RuntimeError("sin token válido — haz login en el webview")
        stop_threshold = -abs(float(stop_loss_pct)) if stop_loss_pct is not None else None
        target_threshold = float(take_profit_pct) if take_profit_pct is not None else None
        c = TosWsClient()
        actions = []
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
            for raw in await c.positions(account, DEFAULT_POSITION_FIELDS):
                inst = raw.get("instrument") or {}
                if inst.get("instrumentType") != "OPTION":
                    continue
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
