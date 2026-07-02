"""Ejecucion de opciones en thinkorswim web (Paper Money).

Ordenes por WebSocket, executor standalone con webview y gestion de salidas
por la prima del contrato. Los imports pesados (PySide6) se resuelven de forma
perezosa: `tos.client` y `tos.ws_client` funcionan sin Qt instalado.
"""

__version__ = "0.1.0"

__all__ = [
    "TosAlert",
    "TosTradeManager",
    "ManagedPositionStore",
    "TosAutomation",
]


def __getattr__(name):
    if name in ("TosAlert", "TosTradeManager", "ManagedPositionStore"):
        from tos import alerts
        return getattr(alerts, name)
    if name == "TosAutomation":
        from tos.automation import TosAutomation
        return TosAutomation
    raise AttributeError(f"module 'tos' has no attribute {name!r}")
