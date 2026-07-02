"""Credenciales locales para TOS usando macOS Keychain.

No guarda passwords dentro del repo. En macOS usa el comando nativo
`security`; en otros sistemas simplemente no persiste.
"""
from __future__ import annotations

import platform
import subprocess

SERVICE = "PowerTradeAi TOS"
USERNAME_ACCOUNT = "__username__"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


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
    return res.stdout.strip() if res.returncode == 0 else ""


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
