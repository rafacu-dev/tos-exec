"""
Login de thinkorswim en un servidor, con un Chromium sin pantalla (Playwright).

En el escritorio el login lo hace el webview Qt del host. En un servidor no hay
pantalla, asi que esta pieza abre su propio Chromium headless, rellena usuario
y contrasena desde secretos del entorno y deja la sesion en Paper Money. El
token se captura igual que siempre (``ws_token_capture`` por CDP), asi que el
resto de la libreria no cambia: ``WsRuntime`` envia ordenes con ese token.

Si Schwab pide MFA, el login se detiene en estado ``mfa_required``; el usuario
envia el codigo que recibio (``submit_mfa`` / ``POST /mfa``) y el flujo sigue.
Nunca se intenta saltar el MFA. El perfil del navegador es persistente
(``<data_dir>/chromium-profile``): conserva cookies y el "recordar este
dispositivo", para que Schwab lo pida lo menos posible.

Secretos (variables de entorno del servidor, nunca en el repo):
  TOS_USERNAME, TOS_PASSWORD

Requiere el extra ``server``:  pip install "tos-exec[server]"
y el navegador:              python -m playwright install chromium
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable

from tos import token_store, tos_scripts, ws_token_capture
from tos.paths import data_dir
from tos.ws_runtime import TokenProvider

DEFAULT_START_URL = "https://trade.thinkorswim.com/"
DEFAULT_CDP_PORT = 9333


def server_credentials() -> dict[str, str]:
    """Usuario y contrasena desde secretos del entorno.

    En el escritorio cae al Llavero de macOS, como hasta ahora.
    """
    username = (os.getenv("TOS_USERNAME") or "").strip()
    password = os.getenv("TOS_PASSWORD") or ""
    if username and password:
        return {"username": username, "password": password}
    try:
        from tos.credentials import load_credentials
        return load_credentials()
    except Exception:
        return {"username": "", "password": ""}


class HeadlessLogin:
    """Chromium headless con sesion de thinkorswim, manejado desde otros hilos.

    Playwright corre en su propio hilo y bucle asyncio; los metodos publicos son
    sincronos y seguros entre hilos. Estados (``status()["state"]``):
    idle, starting, logging_in, mfa_required, ready, error.
    """

    def __init__(self, cdp_port: int = DEFAULT_CDP_PORT, start_url: str | None = None,
                 credentials: Callable[[], dict[str, str]] = server_credentials,
                 headless: bool = True, log=print, max_hours: float = token_store.DEFAULT_MAX_HOURS):
        self.cdp_port = int(cdp_port)
        self.start_url = start_url or os.getenv("TOS_START_URL") or DEFAULT_START_URL
        self.credentials = credentials
        self.headless = headless
        self.log = log
        self.max_hours = max_hours
        self.profile_dir = data_dir() / "chromium-profile"
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tos-headless")
        self._thread_lock = threading.Lock()
        self._opening: asyncio.Future | None = None
        self._lock: asyncio.Lock | None = None
        self._pw = None
        self._context = None
        self._page = None
        self._state = {"state": "idle", "detail": "", "updated_at": time.time()}

    # --- ciclo de vida -------------------------------------------------------
    def start(self, timeout: float = 60.0) -> None:
        """Arranca el hilo y el navegador. Idempotente."""
        with self._thread_lock:
            if not self._thread.is_alive():
                self._thread.start()
        self._call(self._open_browser(), timeout)

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._call(self._close_browser(), 20)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # --- API publica (sincrona) ----------------------------------------------
    def status(self) -> dict:
        data = dict(self._state)
        token = token_store.valid(self.max_hours)
        data["token_valid"] = bool(token)
        data["token_age_hours"] = token.get("age_hours") if token else None
        return data

    def login(self, timeout: float = 120.0) -> dict:
        """Deja la sesion lista en Paper Money o se detiene en MFA."""
        return self._call(self._login(), timeout + 15)

    def submit_mfa(self, code: str, timeout: float = 120.0) -> dict:
        """Envia el codigo MFA que recibio el usuario y continua el login."""
        code = str(code or "").strip()
        if not code:
            raise ValueError("falta el codigo MFA")
        return self._call(self._submit_mfa(code), timeout + 15)

    def screenshot(self) -> str:
        """Guarda una captura de la pagina actual (para diagnosticar el login).

        Va al data dir, nunca al repo: puede mostrar datos de la cuenta.
        """
        return self._call(self._screenshot(), 30)

    # --- implementacion (hilo de Playwright) ---------------------------------
    def _set(self, state: str, detail: str = "") -> dict:
        self._state = {"state": state, "detail": detail, "updated_at": time.time()}
        if detail:
            self.log(f"Login TOS [{state}]: {detail}")
        return dict(self._state)

    async def _open_browser(self) -> None:
        if self._context is not None:
            return
        # Dos llamadas a la vez (servicio y proveedor) comparten una apertura.
        if self._opening is not None:
            await asyncio.shield(self._opening)
            return
        self._opening = self.loop.create_future()
        try:
            await self._launch()
            self._opening.set_result(None)
        except Exception as exc:
            self._opening.set_exception(exc)
            self._opening.exception()  # marcado como leido
            raise
        finally:
            self._opening = None

    async def _launch(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Falta Playwright: pip install 'tos-exec[server]' && "
                "python -m playwright install chromium") from exc
        self._set("starting", "abriendo Chromium headless")
        self._lock = asyncio.Lock()
        self._pw = await async_playwright().start()
        # El puerto CDP deja que ws_token_capture escuche el trafico de TODOS
        # los targets (incluidos los workers donde TOS abre su WebSocket).
        self._context = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            args=[f"--remote-debugging-port={self.cdp_port}", "--remote-allow-origins=*"],
            viewport={"width": 1440, "height": 900},
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._set("idle")

    async def _close_browser(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._pw is not None:
            await self._pw.stop()
        self._context = self._page = self._pw = None

    async def _eval(self, frame, script: str) -> dict:
        try:
            result = await frame.evaluate(script)
        except Exception as exc:
            message = str(exc)
            # Un clic que navega (enviar login, enviar MFA, redireccion OAuth)
            # destruye el contexto antes de que el script responda: no es un
            # error, la pagina esta cargando.
            if "context was destroyed" in message or "navigat" in message or "Frame was detached" in message:
                return {"ok": True, "state": "NAVIGATED"}
            return {"ok": False, "state": "JS_ERROR", "detail": message[:300]}
        return result if isinstance(result, dict) else {}

    async def _frames(self):
        page = self._page
        return [page.main_frame] + [f for f in page.frames if f is not page.main_frame]

    async def _frame_with(self, script: str, predicate) -> tuple[object | None, dict]:
        """Primer frame cuyo resultado cumple el predicado (login en iframe)."""
        for frame in await self._frames():
            res = await self._eval(frame, script)
            if predicate(res):
                return frame, res
        return None, {}

    async def _login(self) -> dict:
        await self._open_browser()
        async with self._lock:
            creds = self.credentials() or {}
            if not (creds.get("username") and creds.get("password")):
                return self._set("error", "faltan TOS_USERNAME / TOS_PASSWORD en el entorno")
            waiting_mfa = self._state["state"] == "mfa_required"
            self._set("logging_in", "comprobando estado de thinkorswim")
            if not self._page.url.startswith("http"):
                await self._page.goto(self.start_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
            elif not waiting_mfa and not token_store.valid(self.max_hours):
                # Pagina abierta pero token vencido: recargar hace que TOS vuelva
                # a autenticarse (y emita un token nuevo) o muestre el login.
                await self._page.reload(wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
            return await self._drive(creds, login_tries=0)

    async def _drive(self, creds: dict, login_tries: int) -> dict:
        """Avanza el login hasta Paper Money, MFA o error (como TosAutomation)."""
        paper_tries = 0
        for _ in range(12):
            frame, mfa = await self._frame_with(tos_scripts.mfa_state_script(), lambda r: r.get("mfa"))
            if frame is not None:
                if not mfa.get("codeInput"):
                    res = await self._eval(frame, tos_scripts.mfa_request_code_script())
                    self.log(f"MFA: {res.get('state')}")
                return self._set("mfa_required",
                                 "Schwab pide un codigo de verificacion: envialo con POST /mfa")

            main = await self._eval(self._page.main_frame, tos_scripts.visible_state_script())
            if main.get("paperOk"):
                if await self._wait_token(30):
                    return self._set("ready", "sesion lista en Paper Money")
                return self._set("error", "sesion abierta pero no se capturo el token (revisa el puerto CDP)")

            form_frame, _ = await self._frame_with(
                tos_scripts.visible_state_script(),
                lambda r: r.get("loginForm") and r.get("deepInputs", 0) > 0 and not r.get("paperOk"))
            if form_frame is not None:
                if login_tries >= 2:
                    return self._set("error", "no pude completar el login tras 2 intentos")
                login_tries += 1
                self._set("logging_in", f"enviando credenciales (intento {login_tries})")
                res = await self._eval(form_frame, tos_scripts.login_script(creds["username"], creds["password"]))
                if res.get("state") == "MFA_REQUIRED":
                    continue
                if not res.get("ok") and res.get("state") not in ("ALREADY_LOGGED_IN", "PASSWORD_FIELD_NOT_FOUND"):
                    return self._set("error", f"login bloqueado: {res.get('detail') or res.get('state')}")
                await asyncio.sleep(5)
                continue

            if main.get("tradeVisible"):
                if paper_tries >= 3:
                    return self._set("error", "no pude confirmar Paper Money tras varios intentos")
                paper_tries += 1
                res = await self._eval(self._page.main_frame, tos_scripts.paper_mode_script())
                if res.get("state") == "LIVE_RISK":
                    return self._set("error", "la cuenta aparece en Live; no se cambia sola a Paper")
                await asyncio.sleep(3)
                continue

            # Aun cargando o redirigiendo (OAuth): esperar y volver a mirar.
            await asyncio.sleep(3)
        return self._set("error", "el login no termino a tiempo; pide una captura con GET /login_screenshot")

    async def _submit_mfa(self, code: str) -> dict:
        await self._open_browser()
        async with self._lock:
            frame, _ = await self._frame_with(tos_scripts.mfa_state_script(), lambda r: r.get("mfa"))
            if frame is None:
                return self._set(self._state["state"], "no hay una pantalla de MFA abierta")
            self._set("logging_in", "enviando codigo MFA")
            res = await self._eval(frame, tos_scripts.mfa_submit_script(code))
            if not res.get("ok"):
                return self._set("mfa_required", f"no pude enviar el codigo: {res.get('detail') or res.get('state')}")
            await asyncio.sleep(5)
            creds = self.credentials() or {}
            return await self._drive(creds, login_tries=2)

    async def _wait_token(self, seconds: float) -> bool:
        limit = time.time() + seconds
        while time.time() < limit:
            if token_store.valid(self.max_hours):
                return True
            await asyncio.sleep(1)
        return False

    async def _screenshot(self) -> str:
        await self._open_browser()
        path = data_dir() / "login_screenshot.png"
        await self._page.screenshot(path=str(path), full_page=True)
        return str(path)


class ProveedorNavegador(TokenProvider):
    """Proveedor de token para un servidor: Chromium headless propio.

    Arranca el navegador, escucha su trafico por CDP (igual que ProveedorCdp)
    y, si el token vence, repite el login. Si Schwab pide MFA, falla con un
    mensaje claro: alguien tiene que enviar el codigo (POST /mfa).
    """

    puede_renovar = True

    def __init__(self, login: HeadlessLogin):
        self.login = login

    async def arrancar(self, log) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self.login.start)
        while True:
            try:
                await ws_token_capture.run(self.login.cdp_port, log)
            except Exception:
                await asyncio.sleep(5)

    def renovar(self, log, espera_s: float) -> bool:
        result = self.login.login(timeout=espera_s)
        if result.get("state") == "mfa_required":
            raise RuntimeError("Schwab pide MFA: envia el codigo con POST /mfa y reintenta la orden")
        return result.get("state") == "ready"
