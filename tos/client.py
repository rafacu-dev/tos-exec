"""Cliente pequeno para enviar alertas al ejecutor TOS local."""
from __future__ import annotations

import requests

DEFAULT_ENDPOINT = "http://127.0.0.1:5060"


def send_alert(alert: dict, endpoint: str = DEFAULT_ENDPOINT, timeout: int = 10) -> dict:
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
