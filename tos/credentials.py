"""Credenciales locales para TOS usando macOS Keychain.

No guarda passwords dentro del repo. En macOS usa el comando nativo
`security`; en otros sistemas simplemente no persiste.
"""
from __future__ import annotations

import platform
import re
import subprocess

SERVICE = "PowerTradeAi TOS"
USERNAME_ACCOUNT = "__username__"
_HEX_RE = re.compile(r"[0-9a-fA-F]+")


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _decode_security_output(raw: str) -> str:
    """Traduce la salida de ``security -w``.

    ``security`` sólo devuelve texto plano cuando el dato es ASCII puro.  En
    cuanto contiene un byte no-ASCII (una tilde, un guión largo pegado por la
    autocorrección de macOS) devuelve el volcado **hexadecimal**, sin prefijo y
    sin avisar.  Tomarlo tal cual metía esa cadena hex en el campo de usuario y
    dejaba el login imposible.
    """
    if not raw or len(raw) % 2 or not _HEX_RE.fullmatch(raw):
        return raw
    try:
        decoded = bytes.fromhex(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        # Un secreto que de verdad sea hexadecimal (un token) se devuelve intacto.
        return raw
    # El caso observado de ``security`` es un dump provocado por caracteres
    # no-ASCII. Si el resultado también es ASCII, no existe señal suficiente
    # para distinguirlo de un usuario/password hexadecimal legítimo (``6162``);
    # conservar el valor original es la única decisión no destructiva.
    return decoded if any(ord(char) > 127 for char in decoded) else raw


def _find(account: str) -> str:
    if not _is_macos():
        return ""
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if res.returncode != 0:
        return ""
    return _decode_security_output(res.stdout.strip())


def _save(account: str, secret: str) -> bool:
    if not _is_macos() or not account or secret is None:
        return False
    try:
        res = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", account, "-s", SERVICE, "-w", secret],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return res.returncode == 0


def load_credentials() -> dict[str, str]:
    username = _find(USERNAME_ACCOUNT)
    password = _find(username) if username else ""
    return {"username": username, "password": password}


def save_credentials(username: str, password: str) -> bool:
    username = str(username or "").strip()
    if not username or not password:
        return False
    ok_user = _save(USERNAME_ACCOUNT, username)
    ok_pass = _save(username, password)
    return ok_user and ok_pass
