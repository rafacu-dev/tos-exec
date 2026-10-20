"""Cliente pequeno para enviar ordenes al ejecutor TOS local."""
from __future__ import annotations

import requests

DEFAULT_ENDPOINT = "http://127.0.0.1:5060"


def _order_headers(auth_token: str | None) -> dict[str, str]:
    return {"X-TOS-Order-Token": str(auth_token)} if auth_token else {}


def _order_response(resp: requests.Response) -> dict:
    """Conserva la clasificación del executor incluso en respuestas HTTP 4xx/5xx.

    Una excepción HTTP genérica borra justo la información necesaria para
    distinguir un rechazo previo al SUBMIT de una orden enviada cuyo resultado
    quedó ambiguo. Las rutas de orden siempre devuelven un objeto clasificable.
    """
    try:
        data = resp.json()
    except ValueError:
        data = None
    status = int(getattr(resp, "status_code", 0) or 0)
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data.setdefault("http_status", status)
        return data
    if 200 <= status < 300 and isinstance(data, dict):
        return data

    error = (
        str((data or {}).get("error") or (data or {}).get("reason") or "")
        if isinstance(data, dict) else str(getattr(resp, "text", "") or "")
    )
    pre_submit = bool(
        status in {401, 403}
        or error in {"order_token_required", "WS no activo"}
        or (isinstance(data, dict) and data.get("submission_may_have_occurred") is False)
    )
    result = {
        "ok": False,
        "accepted": False,
        "order_id": None,
        "submission_state": "rejected_pre_submit" if pre_submit else "unknown_http_error",
        "explicit_rejection": pre_submit,
        "submission_may_have_occurred": not pre_submit,
        "reason": error or f"executor_http_{status or 'unknown'}",
        "error": error or f"executor_http_{status or 'unknown'}",
        "http_status": status,
    }
    return {
        "ok": False,
        "error": result["error"],
        "http_status": status,
        "result": result,
    }


def _option_payload(symbol: str, direction: str, expiration: str, strike: float,
                    quantity: int = 1, order_type: str = "MARKET",
                    limit_price=None, submit: bool = True) -> dict:
    payload = {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "expiration": str(expiration),
        "strike": float(strike),
        "quantity": int(quantity),
        "order_type": str(order_type).upper(),
        "submit": bool(submit),
    }
    if limit_price is not None:
        payload["limit_price"] = float(limit_price)
    return payload


def buy_option(symbol: str, direction: str, expiration: str, strike: float,
               quantity: int = 1, order_type: str = "MARKET", limit_price=None,
               submit: bool = True, endpoint: str = DEFAULT_ENDPOINT,
               timeout: int = 45, auth_token: str | None = None) -> dict:
    """Compra directa por WebSocket. No registra ni gestiona salida."""
    payload = _option_payload(symbol, direction, expiration, strike, quantity,
                              order_type, limit_price, submit)
    resp = requests.post(
        f"{endpoint.rstrip('/')}/buy", json=payload, timeout=timeout,
        headers=_order_headers(auth_token),
    )
    return _order_response(resp)


def sell_option(symbol: str, direction: str, expiration: str, strike: float,
                quantity: int = 1, order_type: str = "MARKET", limit_price=None,
                submit: bool = True, endpoint: str = DEFAULT_ENDPOINT,
                timeout: int = 45, auth_token: str | None = None) -> dict:
    """Venta directa por WebSocket. El servidor aplica safeguard anti-short."""
    payload = _option_payload(symbol, direction, expiration, strike, quantity,
                              order_type, limit_price, submit)
    resp = requests.post(
        f"{endpoint.rstrip('/')}/sell", json=payload, timeout=timeout,
        headers=_order_headers(auth_token),
    )
    return _order_response(resp)


def place_bracket(symbol: str, quantity: int, take_profit: float, stop_loss: float,
                  entry_type: str = "LIMIT", entry_limit=None, tif: str = "DAY",
                  submit: bool = True, expiration: str | None = None,
                  direction: str | None = None, strike: float | None = None,
                  endpoint: str = DEFAULT_ENDPOINT, timeout: int = 45,
                  auth_token: str | None = None) -> dict:
    """Entrada + take profit + stop loss (1st trgs OCO).

    Para opciones pasar `expiration`, `direction` y `strike`; si no, `symbol`
    es el símbolo TOS directo. `result.order_ids.entry` sirve para cancelar.
    """
    payload: dict = {
        "symbol": str(symbol).upper(),
        "quantity": int(quantity),
        "take_profit": float(take_profit),
        "stop_loss": float(stop_loss),
        "entry_type": str(entry_type).upper(),
        "tif": str(tif).upper(),
        "submit": bool(submit),
    }
    if entry_limit is not None:
        payload["entry_limit"] = float(entry_limit)
    if expiration is not None:
        payload.update(expiration=str(expiration), direction=str(direction).upper(),
                       strike=float(strike))
    resp = requests.post(
        f"{endpoint.rstrip('/')}/bracket", json=payload, timeout=timeout,
        headers=_order_headers(auth_token),
    )
    return _order_response(resp)


def cancel_order(order_id: int, endpoint: str = DEFAULT_ENDPOINT, timeout: int = 30,
                 auth_token: str | None = None) -> dict:
    """Cancela una orden; con la entrada de un bracket cancela sus 3 patas."""
    resp = requests.post(
        f"{endpoint.rstrip('/')}/cancel_order", json={"order_id": int(order_id)},
        timeout=timeout, headers=_order_headers(auth_token),
    )
    resp.raise_for_status()
    return resp.json()


def auto_exit_percent(take_profit_pct: float | None = None,
                      stop_loss_pct: float | None = None,
                      submit: bool = True, dry_run: bool = False,
                      endpoint: str = DEFAULT_ENDPOINT,
                      timeout: int = 60) -> dict:
    """Ejecuta una revision de salida automatica por porcentaje."""
    payload = {"submit": bool(submit), "dry_run": bool(dry_run)}
    if take_profit_pct is not None:
        payload["take_profit_pct"] = float(take_profit_pct)
    if stop_loss_pct is not None:
        payload["stop_loss_pct"] = float(stop_loss_pct)
    resp = requests.post(f"{endpoint.rstrip('/')}/auto_exit", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def broker_positions(endpoint: str = DEFAULT_ENDPOINT, timeout: int = 30) -> dict:
    """Posiciones OPTION abiertas del broker via WebSocket."""
    resp = requests.get(f"{endpoint.rstrip('/')}/broker_positions", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def day_summary(endpoint: str = DEFAULT_ENDPOINT, timeout: int = 30) -> dict:
    """Resumen del dia y P/L disponible/calculado."""
    resp = requests.get(f"{endpoint.rstrip('/')}/day_summary", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def send_alert(alert: dict, endpoint: str = DEFAULT_ENDPOINT, timeout: int = 10) -> dict:
    """API legada: envia alerta al TosTradeManager con gestion de salida."""
    resp = requests.post(f"{endpoint.rstrip('/')}/alert", json=alert, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def positions(endpoint: str = DEFAULT_ENDPOINT, timeout: int = 10) -> dict:
    resp = requests.get(f"{endpoint.rstrip('/')}/positions", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def send_close(alert_id: str | None = None, symbol: str | None = None,
               reason: str = "api", endpoint: str = DEFAULT_ENDPOINT,
               timeout: int = 10) -> dict:
    """Pide al ejecutor TOS cerrar la posición abierta que matchee alert_id (o, en su
    defecto, el símbolo). Una posición por símbolo, así que el símbolo basta de fallback."""
    payload: dict = {"reason": reason}
    if alert_id:
        payload["alert_id"] = alert_id
    if symbol:
        payload["symbol"] = symbol
    resp = requests.post(f"{endpoint.rstrip('/')}/close", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
