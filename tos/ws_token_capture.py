"""
Captura automática del token de sesión desde el webview de TOS, vía CDP.

Vigila el tráfico WebSocket del webview (remote-debugging de QtWebEngine) y, en
cuanto ve el frame del servicio `login` (que lleva el token) o la respuesta de
`login/schwab` (que trae body.token), lo guarda en token_store. Así Python puede
reusar la sesión sin authCode fresco.

Embebible (run() en un loop asyncio) o standalone:
  python3 -m tos.ws_token_capture            (puerto 9333)
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
import urllib.request

import websockets

from tos import token_store

DEFAULT_PORT = 9333


def _extract_token(payload: str):
    """Devuelve token si el frame es del servicio login / login/schwab."""
    try:
        d = json.loads(payload)
    except Exception:
        return None
    for item in d.get("payload", []):
        hdr = item.get("header", {})
        svc = hdr.get("service")
        if svc == "login":
            tok = item.get("params", {}).get("token")
            if tok:
                return tok
        if svc == "login/schwab":
            tok = item.get("body", {}).get("token")
            if tok:
                return tok
    return None


def _extract_account(payload: str):
    try:
        d = json.loads(payload)
    except Exception:
        return None
    for item in d.get("payload", []):
        if item.get("header", {}).get("service") == "accounts":
            for a in item.get("body", {}).get("items", []):
                if a.get("tradeable") and a.get("code"):
                    return a["code"]
    return None


async def run(port: int = DEFAULT_PORT, log=print):
    ids = itertools.count(1)
    url = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/version", timeout=4).read())["webSocketDebuggerUrl"]
    last_token = None
    last_acct = None
    async with websockets.connect(url, max_size=None, open_timeout=8) as ws:
        async def send(method, params=None, session=None):
            m = {"id": next(ids), "method": method, "params": params or {}}
            if session:
                m["sessionId"] = session
            await ws.send(json.dumps(m))

        await send("Target.setAutoAttach",
                   {"autoAttach": True, "flatten": True, "waitForDebuggerOnStart": False})
        log("Captura de token activa (CDP). Esperando login del webview…")
        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get("method", "")
            params = msg.get("params", {})
            if method == "Target.attachedToTarget":
                try:
                    await send("Network.enable", {}, session=params.get("sessionId"))
                except Exception:
                    pass
            elif method in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
                pl = params.get("response", {}).get("payloadData", "")
                if not pl:
                    continue
                tok = _extract_token(pl)
                if tok and tok != last_token:
                    last_token = tok
                    token_store.save(tok, account_code=last_acct)
                    log("✓ Token de sesión capturado y guardado (válido ~8h).")
                acct = _extract_account(pl)
                if acct and acct != last_acct:
                    last_acct = acct
                    d = token_store.load() or {}
                    if d.get("token"):
                        token_store.save(d["token"], account_code=acct)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    try:
        asyncio.run(run(port))
    except KeyboardInterrupt:
        pass
