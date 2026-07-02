"""
Almacén del token de sesión de TOS (capturado del login del webview).

El usuario configuró la sesión a 8h, así que un login antes del mercado cubre el
día. Persistimos token + instante de captura; `valid()` lo da por bueno si está
dentro de la ventana (8h por defecto). Vive en el data dir (~/.tos-exec por
defecto): es una credencial viva, nunca dentro del paquete instalado.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from tos.paths import data_dir

DEFAULT_MAX_HOURS = 8.0


def _token_path() -> Path:
    return data_dir() / "ws_token.json"


def save(token: str, account_code: str | None = None, **extra) -> None:
    data = {"token": token, "captured_at": time.time(), "account_code": account_code}
    data.update(extra)
    _token_path().write_text(json.dumps(data), encoding="utf-8")


def load() -> dict | None:
    try:
        return json.loads(_token_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def valid(max_hours: float = DEFAULT_MAX_HOURS) -> dict | None:
    """Devuelve los datos si hay token y no superó la ventana; si no, None."""
    d = load()
    if not d or not d.get("token"):
        return None
    age_h = (time.time() - float(d.get("captured_at", 0))) / 3600.0
    if age_h > max_hours:
        return None
    d["age_hours"] = round(age_h, 2)
    return d


def clear() -> None:
    try:
        _token_path().unlink()
    except Exception:
        pass
