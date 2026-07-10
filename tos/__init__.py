"""Ejecucion de opciones en thinkorswim web (Paper Money).

API recomendada v1.2: compra/venta por WebSocket, salida automatica opcional
por porcentaje, posiciones y resumen. Los imports pesados (PySide6) se
resuelven de forma perezosa: `tos.client` y `tos.ws_client` funcionan sin Qt
instalado.
"""

__version__ = "1.2.0"

__all__ = [
    "WsRuntime",
    "TosAutomation",
    "TosAlert",
    "TosTradeManager",
    "ManagedPositionStore",
]


def __getattr__(name):
    if name == "WsRuntime":
        from tos.ws_runtime import WsRuntime
        return WsRuntime
    if name in ("TosAlert", "TosTradeManager", "ManagedPositionStore"):
        from tos import alerts
        return getattr(alerts, name)
    if name == "TosAutomation":
        from tos.automation import TosAutomation
        return TosAutomation
    raise AttributeError(f"module 'tos' has no attribute {name!r}")
