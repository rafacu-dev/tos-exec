"""Directorio de datos de la libreria (token, posiciones, perfil del webview).

Por defecto ~/.tos-exec; se cambia con TOS_DATA_DIR. Vive fuera del paquete
porque la libreria se instala en site-packages y estos archivos son estado
vivo (credenciales de sesion incluidas): nunca deben ir dentro del codigo
ni a un repo.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DIR = Path.home() / ".tos-exec"


def data_dir() -> Path:
    d = Path(os.getenv("TOS_DATA_DIR") or DEFAULT_DIR).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d
