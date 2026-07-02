"""
Captura de frames WebSocket de TOS vía Chrome DevTools Protocol (CDP).

Se conecta al endpoint BROWSER del remote-debugging de QtWebEngine y usa
Target.setAutoAttach (flatten) para adjuntarse automáticamente a TODOS los
targets — page, iframes y Web Workers (donde TOS suele abrir su gateway WS) —
multiplexando por sessionId sobre una sola conexión. Registra los eventos
Network.webSocket* (creación + frames enviados/recibidos).

Lanzar el executor con remote debugging:
  QTWEBENGINE_REMOTE_DEBUGGING=9333 \
  QTWEBENGINE_CHROMIUM_FLAGS="--remote-allow-origins=*" \
  python3 -m tos.executor_app --port 5060

Uso:  python3 -m tos.cdp_capture          (puerto 9333 por defecto)
Salida: <data_dir>/ws_cdp.jsonl  (un evento por línea)
"""
import asyncio
import itertools
import json
import sys
import time
import urllib.request

import websockets

from tos.paths import data_dir

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
OUT = data_dir() / "ws_cdp.jsonl"
_ids = itertools.count(1)
_counts = {"frames": 0, "sessions": 0}


def _log(entry: dict):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def browser_ws_url() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=4) as r:
        return json.loads(r.read().decode())["webSocketDebuggerUrl"]


async def run():
    url = browser_ws_url()
    print(f"CDP browser endpoint OK → {OUT.name}  (Ctrl-C para parar)")
    _log({"t": round(time.time(), 3), "ev": "capture_start", "port": PORT})
    async with websockets.connect(url, max_size=None, open_timeout=8) as ws:
        async def send(method, params=None, session=None):
            msg = {"id": next(_ids), "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))

        # auto-adjuntar a todos los targets (incl. workers) y multiplexar
        await send("Target.setAutoAttach",
                   {"autoAttach": True, "flatten": True, "waitForDebuggerOnStart": False})
        await send("Target.setDiscoverTargets", {"discover": True})

        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get("method", "")
            params = msg.get("params", {})
            sid = msg.get("sessionId")
            if method == "Target.attachedToTarget":
                info = params.get("targetInfo", {})
                s = params.get("sessionId")
                _counts["sessions"] += 1
                _log({"t": round(time.time(), 3), "ev": "attached",
                      "type": info.get("type"), "url": (info.get("url") or "")[:90]})
                try:
                    await send("Network.enable", {}, session=s)
                except Exception:
                    pass
            elif method.startswith("Network.webSocket"):
                entry = {"t": round(time.time(), 3), "ev": method, "sid": sid}
                if method == "Network.webSocketCreated":
                    entry["url"] = params.get("url")
                elif method in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
                    resp = params.get("response", {})
                    entry["dir"] = "send" if method.endswith("Sent") else "recv"
                    entry["opcode"] = resp.get("opcode")   # 1=texto, 2=binario
                    entry["payload"] = (resp.get("payloadData") or "")[:16000]
                _log(entry)
                _counts["frames"] += 1
                if _counts["frames"] % 25 == 0:
                    print(f"  sessions={_counts['sessions']}  frames={_counts['frames']}")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(f"\nParado. {_counts['frames']} frames, {_counts['sessions']} sesiones en {OUT}")
    except Exception as exc:
        print("ERROR:", exc)
