"""Login headless contra un thinkorswim simulado (sin red, sin credenciales reales).

El sitio falso reproduce el flujo de Schwab: Login ID -> contrasena -> pantalla
de verificacion (MFA) -> trading en Paper Money, que abre un WebSocket y manda
el frame `login` con el token. Requiere Playwright y su Chromium; si no estan,
el test se omite.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading
import time

import pytest

pytest.importorskip("playwright")
websockets = pytest.importorskip("websockets")

LOGIN = """<html><body><h1>Log in</h1><p>Login ID</p>
<input id="loginIdInput" aria-label="Login ID"><button id="continueBtn" aria-label="Continue"
 onclick="document.getElementById('pw').style.display='block';this.style.display='none'">Continue</button>
<div id="pw" style="display:none"><input id="passwordInput" type="password" aria-label="Password">
<button onclick="if(document.getElementById('loginIdInput').value==='demo-user'&&document.getElementById('passwordInput').value==='demo-pass'){location.href='/mfa'}">Log in</button></div>
</body></html>"""
MFA = """<html><body><h1>Verify your identity</h1>
<div id="opts"><button onclick="document.getElementById('opts').style.display='none';document.getElementById('code').style.display='block'">Text me a code</button></div>
<div id="code" style="display:none"><p>Enter the code we sent</p>
<input autocomplete="one-time-code" inputmode="numeric" aria-label="Security code" id="c">
<label><input type="checkbox" id="rem"> Remember this device</label>
<button onclick="if(document.getElementById('c').value==='123456'){location.href='/trade'}">Continue</button></div>
</body></html>"""
TRADE = """<html><body><h1>Positions Orders Trade</h1>
<div data-testid="toggle"><input data-testid="toggle-left" aria-label="Real Live Trading" type="radio">
<input data-testid="toggle-right" aria-label="Paper Money" type="radio" checked></div>
<script>const ws=new WebSocket('ws://127.0.0.1:%d/Services/WsJson');
ws.onopen=()=>ws.send(JSON.stringify({payload:[{header:{service:'login',id:'login'},params:{token:'tok-FAKE-123'}}]}));</script>
</body></html>"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def fake_tos():
    http_port, ws_port = _free_port(), _free_port()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            body = {"/mfa": MFA, "/trade": TRADE % ws_port}.get(path, LOGIN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", http_port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def ws_main():
        async def drain(ws):
            async for _ in ws:
                pass
        async with websockets.serve(drain, "127.0.0.1", ws_port):
            ready.set()
            await asyncio.Future()

    def run_ws():
        try:
            loop.run_until_complete(ws_main())
        except RuntimeError:
            pass  # el bucle se detiene al terminar el test

    threading.Thread(target=run_ws, daemon=True).start()
    ready.wait(10)
    yield f"http://127.0.0.1:{http_port}/"
    httpd.shutdown()
    loop.call_soon_threadsafe(loop.stop)


def test_login_mfa_token_and_renewal(fake_tos, tmp_path, monkeypatch):
    monkeypatch.setenv("TOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOS_USERNAME", "demo-user")
    monkeypatch.setenv("TOS_PASSWORD", "demo-pass")
    from tos import token_store, ws_token_capture
    from tos.headless_login import HeadlessLogin

    cdp_port = _free_port()
    login = HeadlessLogin(cdp_port=cdp_port, start_url=fake_tos, log=lambda _m: None)
    try:
        login.start()
    except Exception as exc:  # Chromium de Playwright no instalado
        pytest.skip(f"Chromium de Playwright no disponible: {exc}")

    capture_loop = asyncio.new_event_loop()

    def run_capture():
        asyncio.set_event_loop(capture_loop)
        capture_loop.create_task(ws_token_capture.run(cdp_port, lambda _m: None))
        capture_loop.run_forever()

    threading.Thread(target=run_capture, daemon=True).start()
    time.sleep(1)
    try:
        assert login.login(timeout=60)["state"] == "mfa_required"
        assert login.submit_mfa("000000", timeout=60)["state"] == "mfa_required"
        result = login.submit_mfa("123456", timeout=60)
        assert result["state"] == "ready", result
        assert token_store.valid()["token"] == "tok-FAKE-123"

        # Token vencido con la sesion viva: recargar debe volver a guardarlo
        # aunque TOS reenvie el mismo token.
        data = token_store.load()
        data["captured_at"] = time.time() - 9 * 3600
        (tmp_path / "ws_token.json").write_text(json.dumps(data), encoding="utf-8")
        assert token_store.valid() is None
        assert login.login(timeout=60)["state"] == "ready"
        assert token_store.valid()["token"] == "tok-FAKE-123"
    finally:
        login.stop()
        capture_loop.call_soon_threadsafe(capture_loop.stop)


def test_missing_credentials_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TOS_DATA_DIR", str(tmp_path))
    from tos.headless_login import HeadlessLogin

    login = HeadlessLogin(cdp_port=_free_port(), start_url="about:blank",
                          credentials=lambda: {"username": "", "password": ""}, log=lambda _m: None)
    try:
        try:
            login.start()
        except Exception as exc:
            pytest.skip(f"Chromium de Playwright no disponible: {exc}")
        result = login.login(timeout=30)
        assert result["state"] == "error"
        assert "TOS_USERNAME" in result["detail"]
    finally:
        login.stop()
