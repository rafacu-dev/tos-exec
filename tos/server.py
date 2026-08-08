"""Receptor HTTP local de alertas: metodos puros, sin UI ni Qt.

Expone API local para integraciones:

- POST /alert     recibe una alerta y manda la orden de entrada.
- POST /close     cierra por id / alert_id / simbolo.
- POST /buy       compra directa por WebSocket (API v1.2, sin gestion).
- POST /sell      vende directa por WebSocket (API v1.2, sin gestion).
- POST /auto_exit salida automatica por porcentaje sobre posiciones abiertas.
- POST /ws_order  orden directa por WebSocket (prueba; submit=false = review).
- GET  /positions posiciones abiertas (broker si no hay manager; legado si hay manager).
- GET  /day_summary resumen del dia con P/L disponible/calculado.
- GET  /logs      ultimas lineas de log.
- GET  /ws_status estado del token WS.

Uso directo v1.2 (thread-safe con la ruta WS):

    server = TosHttpServer(port=5060, ws_runtime=rt)
    server.start()

Un host Qt que use la automatizacion DOM debe pasar `on_alert`/`on_close` para
reencolar las llamadas en su hilo (p.ej. emitiendo una senal); si no se pasan,
el servidor llama a manager.submit_alert / manager.request_close directamente
desde el hilo del request.
"""
from __future__ import annotations

import json
import hmac
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: dict):
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def resolve_position_id(store, payload: dict) -> str:
    """Resuelve la posicion abierta a cerrar por id, alert_id o simbolo
    (el emisor no conoce el id interno; una posicion por simbolo)."""
    pos_id = str(payload.get("id") or payload.get("position_id") or "")
    if pos_id:
        return pos_id
    alert_id = str(payload.get("alert_id") or "")
    symbol = str(payload.get("symbol") or "").upper()
    for p in store.open_positions():
        if alert_id and p.get("alert_id") == alert_id:
            return p.get("id")
        if not alert_id and symbol and str(p.get("symbol", "")).upper() == symbol:
            return p.get("id")
    return ""


def _strict_bool(payload: dict, key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} debe ser booleano")
    return value


def _order_payload(payload: dict) -> dict:
    return {
        "root": str(payload.get("symbol") or payload.get("root") or "").upper(),
        "expiration": str(payload["expiration"]),
        "side": str(payload.get("direction") or payload.get("side") or "").upper(),
        "strike": float(payload["strike"]),
        "quantity": int(payload.get("quantity", 1)),
        "order_type": str(payload.get("order_type", "MARKET")).upper(),
        "limit_price": payload.get("limit_price"),
        "submit": _strict_bool(payload, "submit", True),
    }


def _order_failure(error: str, *, may_have_submitted: bool, state: str) -> dict:
    result = {
        "ok": False,
        "accepted": False,
        "order_id": None,
        "submission_state": state,
        "explicit_rejection": not may_have_submitted,
        "submission_may_have_occurred": may_have_submitted,
        "reason": str(error),
        "error": str(error),
    }
    return {"ok": False, "error": str(error), "result": result}


def make_handler(manager, ws_runtime=None, log_lines: list[str] | None = None,
                 on_alert: Callable[[dict], None] | None = None,
                 on_close: Callable[[str, str], None] | None = None,
                 order_token: str | None = None):
    logs = log_lines if log_lines is not None else []

    def record_order_error(path: str, payload: dict, phase: str, exc) -> None:
        safe_payload = payload if isinstance(payload, dict) else {}
        symbol = str(safe_payload.get("symbol") or safe_payload.get("root") or "?").upper()
        line = (
            f"{datetime.now(timezone.utc).isoformat()} ORDER_ERROR "
            f"path={path} symbol={symbol} phase={phase} "
            f"error={type(exc).__name__}: {exc}"
        )
        logs.append(line)
        del logs[:-500]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):
            return

        def do_GET(self):
            path = self.path.rstrip("/")
            if path == "/positions":
                if manager is None and ws_runtime is not None:
                    try:
                        _json_response(self, 200, {"ok": True, "positions": ws_runtime.open_option_positions()})
                    except Exception as exc:
                        _json_response(self, 400, {"ok": False, "error": str(exc)})
                    return
                if manager is None:
                    _json_response(self, 400, {"ok": False, "error": "manager no activo"})
                    return
                _json_response(self, 200, {"ok": True, "positions": manager.store.all()})
                return
            if path == "/broker_positions":
                if ws_runtime is None:
                    _json_response(self, 400, {"ok": False, "error": "WS no activo"})
                    return
                try:
                    _json_response(self, 200, {"ok": True, "positions": ws_runtime.open_option_positions()})
                except Exception as exc:
                    _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            if path == "/day_summary":
                if ws_runtime is None:
                    _json_response(self, 400, {"ok": False, "error": "WS no activo"})
                    return
                try:
                    _json_response(self, 200, ws_runtime.day_summary())
                except Exception as exc:
                    _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            if path == "/logs":
                _json_response(self, 200, {"ok": True, "logs": list(logs[-200:])})
                return
            if path == "/ws_status":
                d = ws_runtime.token_status() if ws_runtime else None
                _json_response(self, 200, {"ok": True, "ws_enabled": ws_runtime is not None,
                                           "token_valid": bool(d), "age_hours": (d or {}).get("age_hours")})
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": f"invalid_json: {exc}"})
                return

            path = self.path.rstrip("/")
            protected = {"/alert", "/close", "/buy", "/sell", "/auto_exit", "/ws_order"}
            if order_token and path in protected:
                supplied = str(self.headers.get("X-TOS-Order-Token") or "")
                if not hmac.compare_digest(supplied, str(order_token)):
                    if path in {"/buy", "/sell", "/ws_order"}:
                        response = _order_failure(
                            "order_token_required",
                            may_have_submitted=False,
                            state="rejected_pre_submit",
                        )
                    else:
                        response = {"ok": False, "error": "order_token_required"}
                    _json_response(self, 403, response)
                    return
            if path == "/alert":
                if manager is None:
                    _json_response(self, 400, {"ok": False, "error": "manager no activo"})
                    return
                if on_alert is not None:
                    on_alert(payload)
                    _json_response(self, 202, {"ok": True, "accepted": True})
                    return
                try:
                    alert_id = manager.submit_alert(payload)
                    _json_response(self, 202, {"ok": True, "accepted": True, "id": alert_id})
                except Exception as exc:
                    _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            if path == "/close":
                if manager is None:
                    _json_response(self, 400, {"ok": False, "error": "manager no activo"})
                    return
                pos_id = resolve_position_id(manager.store, payload)
                if not pos_id:
                    _json_response(self, 404, {"ok": False, "error":
                                               "sin posición abierta que matchee id/alert_id/symbol"})
                    return
                reason = str(payload.get("reason") or "api")
                if on_close is not None:
                    on_close(pos_id, reason)
                else:
                    manager.request_close(pos_id, reason)
                _json_response(self, 202, {"ok": True, "accepted": True})
                return
            if path == "/buy":
                if ws_runtime is None:
                    _json_response(self, 400, _order_failure(
                        "WS no activo", may_have_submitted=False,
                        state="rejected_pre_submit",
                    ))
                    return
                try:
                    args = _order_payload(payload)
                except Exception as exc:
                    record_order_error(path, payload, "validation_pre_submit", exc)
                    _json_response(self, 400, _order_failure(
                        f"invalid_order_payload:{exc}", may_have_submitted=False,
                        state="rejected_pre_submit",
                    ))
                    return
                try:
                    res = ws_runtime.buy_option(**args)
                    _json_response(self, 200, {"ok": True, "result": res})
                except Exception as exc:
                    # La excepción puede haber ocurrido después de que el frame
                    # SUBMIT saliera. Nunca se presenta como rechazo reintentable.
                    record_order_error(path, payload, "runtime_submission_unknown", exc)
                    _json_response(self, 502, _order_failure(
                        f"executor_runtime_error:{type(exc).__name__}: {exc}",
                        may_have_submitted=True,
                        state="unknown_runtime_exception",
                    ))
                return
            if path == "/sell":
                if ws_runtime is None:
                    _json_response(self, 400, _order_failure(
                        "WS no activo", may_have_submitted=False,
                        state="rejected_pre_submit",
                    ))
                    return
                try:
                    args = _order_payload(payload)
                except Exception as exc:
                    record_order_error(path, payload, "validation_pre_submit", exc)
                    _json_response(self, 400, _order_failure(
                        f"invalid_order_payload:{exc}", may_have_submitted=False,
                        state="rejected_pre_submit",
                    ))
                    return
                try:
                    res = ws_runtime.sell_option(**args)
                    _json_response(self, 200, {"ok": True, "result": res})
                except Exception as exc:
                    record_order_error(path, payload, "runtime_submission_unknown", exc)
                    _json_response(self, 502, _order_failure(
                        f"executor_runtime_error:{type(exc).__name__}: {exc}",
                        may_have_submitted=True,
                        state="unknown_runtime_exception",
                    ))
                return
            if path == "/auto_exit":
                if ws_runtime is None:
                    _json_response(self, 400, {"ok": False, "error": "WS no activo"})
                    return
                try:
                    res = ws_runtime.auto_exit_percent(
                        take_profit_pct=payload.get("take_profit_pct"),
                        stop_loss_pct=payload.get("stop_loss_pct"),
                        submit=_strict_bool(payload, "submit", True),
                        dry_run=_strict_bool(payload, "dry_run", False))
                    _json_response(self, 200, res)
                except Exception as exc:
                    _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            if path == "/ws_order":
                if ws_runtime is None:
                    _json_response(self, 400, {"ok": False, "error": "WS no activo"})
                    return
                try:
                    res = ws_runtime.place_option(
                        root=str(payload["symbol"]).upper(),
                        expiration=str(payload["expiration"]),
                        side=str(payload["direction"]).upper(),
                        strike=float(payload["strike"]),
                        quantity=int(payload.get("quantity", 1)),
                        order_type=str(payload.get("order_type", "MARKET")).upper(),
                        limit_price=payload.get("limit_price"),
                        submit=_strict_bool(payload, "submit", False))
                    _json_response(self, 200, {"ok": True, "result": res})
                except Exception as exc:
                    _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})

    return Handler


class TosHttpServer:
    """ThreadingHTTPServer en un hilo demonio, con start()/stop()."""

    def __init__(self, manager=None, port: int = 5060, host: str = "127.0.0.1",
                 ws_runtime=None, log_lines: list[str] | None = None,
                 on_alert: Callable[[dict], None] | None = None,
                 on_close: Callable[[str, str], None] | None = None,
                 order_token: str | None = None):
        self.host = host
        self.port = int(port)
        self.httpd = ThreadingHTTPServer(
            (host, self.port),
            make_handler(manager, ws_runtime=ws_runtime, log_lines=log_lines,
                         on_alert=on_alert, on_close=on_close,
                         order_token=order_token))
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            self.httpd.shutdown()
            self._thread.join(timeout=2)
        self.httpd.server_close()
