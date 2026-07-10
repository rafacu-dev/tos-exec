"""Cliente pequeno para enviar ordenes al ejecutor TOS local."""
from __future__ import annotations

import requests

DEFAULT_ENDPOINT = "http://127.0.0.1:5060"


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
               timeout: int = 45) -> dict:
    """Compra directa por WebSocket. No registra ni gestiona salida."""
    payload = _option_payload(symbol, direction, expiration, strike, quantity,
                              order_type, limit_price, submit)
    resp = requests.post(f"{endpoint.rstrip('/')}/buy", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def sell_option(symbol: str, direction: str, expiration: str, strike: float,
                quantity: int = 1, order_type: str = "MARKET", limit_price=None,
                submit: bool = True, endpoint: str = DEFAULT_ENDPOINT,
                timeout: int = 45) -> dict:
    """Venta directa por WebSocket. El servidor aplica safeguard anti-short."""
    payload = _option_payload(symbol, direction, expiration, strike, quantity,
                              order_type, limit_price, submit)
    resp = requests.post(f"{endpoint.rstrip('/')}/sell", json=payload, timeout=timeout)
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
