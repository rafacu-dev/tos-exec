"""
Orquesta la automatización de thinkorswim en el webview Qt.

QWebEnginePage.runJavaScript NO espera las promesas de los IIFE async, así que
cada script se inyecta envuelto para dejar su resultado en window.__CT y luego
se hace polling hasta que termina (o vence el timeout).

Flujo de auto-login (igual que TC-Ai):
  estado visible → si hay login form y credenciales: login → (MFA? parar) →
  asegurar Paper Money → repetir hasta paperOk o agotar reintentos.
"""
import json

from PySide6.QtCore import QObject, QTimer, Signal

from tos import tos_scripts


class JsRunner(QObject):
    """Ejecuta un IIFE async en la página y entrega el resultado por callback."""

    def __init__(self, page):
        super().__init__()
        self.page = page

    def run(self, script: str, on_done, timeout_ms: int = 30000):
        expr = script.strip().rstrip(";")
        token = f"__CT_{id(on_done) & 0xffffff:x}"
        wrapper = (
            f"window.{token}=null;"
            f"Promise.resolve(({expr}))"
            f".then(r=>{{window.{token}=JSON.stringify(r||{{}});}})"
            f".catch(e=>{{window.{token}=JSON.stringify("
            f"{{ok:false,state:'JS_ERROR',detail:String((e&&e.message)||e)}});}});"
        )
        self.page.runJavaScript(wrapper)

        elapsed = {"ms": 0}
        poll = QTimer(self)
        poll.setInterval(500)

        def check():
            def got(val):
                if val:
                    poll.stop()
                    try:
                        on_done(json.loads(val))
                    except (ValueError, TypeError):
                        on_done({"ok": False, "state": "PARSE_ERROR", "detail": str(val)[:200]})
                else:
                    elapsed["ms"] += 500
                    if elapsed["ms"] >= timeout_ms:
                        poll.stop()
                        on_done({"ok": False, "state": "TIMEOUT",
                                 "detail": f"sin respuesta en {timeout_ms // 1000}s"})
            self.page.runJavaScript(f"window.{token}", got)

        poll.timeout.connect(check)
        poll.start()


LOGIN_FRAME_RE = "sws-gateway"


def pick_login_frame(state: dict) -> str:
    """URL del iframe de login (sws-gateway/ui/host), como TC-Ai."""
    frames = state.get("frames") or []
    for f in frames:
        src = str(f.get("src") or "")
        if "sws-gateway" in src and "/ui/host" in src:
            return src
    for f in frames:
        src = str(f.get("src") or "")
        if any(k in src.lower() for k in ("login", "oauth", "schwab", "thinkorswim")):
            return src
    return ""


class TosAutomation(QObject):
    """Máquina de estados para login + Paper Money sobre un QWebEnginePage."""

    finished = Signal(bool, str)   # (ok, mensaje)
    order_result = Signal(dict)    # resultado estructurado de BUY/SELL
    progress = Signal(str)         # línea de log
    navigate = Signal(str)         # pedir a la ventana que cargue una URL

    def __init__(self, page, log=None):
        super().__init__()
        self.page = page
        self.runner = JsRunner(page)
        self._log = log or (lambda _m: None)
        self._user = ""
        self._password = ""
        self._paper_tries = 0
        self._login_tries = 0
        self._nav_to_frame_done = False
        self._nav_callback = None   # se llama tras la próxima carga de página
        self._order = None
        self._paper_confirmed = False  # verificado por toggle vía login
        self._busy = False

    @property
    def paper_confirmed(self) -> bool:
        return self._paper_confirmed

    @property
    def busy(self) -> bool:
        return self._busy

    def _emit(self, msg: str):
        self._log(msg)
        self.progress.emit(msg)

    def _end(self, ok: bool, msg: str):
        self._busy = False
        self._emit(("✓ " if ok else "⚠ ") + msg)
        self.finished.emit(ok, msg)

    # --- ejecución de orden de opción ---
    def buy_option(self, symbol: str, side: str, strike: float, expiration: str,
                   quantity: int = 1, max_investment: float = 500.0,
                   submit: bool = True, auto_size: bool = True,
                   max_contracts: int = 1):
        if self._busy:
            self._emit("Automatización ocupada; espera a que termine…")
            return False
        self._busy = True
        self._order = dict(symbol=symbol, side=side, strike=strike,
                           expiration=expiration, quantity=quantity,
                           max_investment=max_investment, submit=submit,
                           action="BUY",
                           auto_size=auto_size,
                           max_contracts=max_contracts)
        # navegar a la página Trade del símbolo (como TC-Ai) y luego ejecutar
        url = f"https://trade.thinkorswim.com/trade?symbol={symbol}"
        self._emit(f"Abriendo {symbol} en Trade…")
        self._nav_callback = self._run_pending_order
        self.navigate.emit(url)
        return True

    def _run_pending_order(self):
        o = self._order
        if not o:
            self._end(False, "Orden perdida.")
            return
        verb = "ENVIAR" if o["submit"] else "PREPARAR (sin enviar)"
        self._emit(f"{verb}: {o['symbol']} {o['side']} strike {o['strike']} "
                   f"venc {o['expiration']} x{o['quantity']} (máx ${o['max_investment']:g}) "
                   f"[paper_confirmado={self._paper_confirmed}]…")
        script = tos_scripts.option_order_script(
            o["symbol"], o["side"], o["strike"], o["expiration"],
            o["quantity"], o["max_investment"], o["submit"],
            paper_confirmed=self._paper_confirmed,
            auto_size=o.get("auto_size", True),
            max_contracts=o.get("max_contracts", 1),
            action=o.get("action", "BUY"))
        self.runner.run(script, self._on_order, timeout_ms=80000)

    def sell_option(self, symbol: str, side: str, strike: float, expiration: str,
                    quantity: int, submit: bool = True):
        if self._busy:
            self._emit("Automatización ocupada; cierre queda pendiente…")
            return False
        self._busy = True
        self._order = dict(symbol=symbol, side=side, strike=strike,
                           expiration=expiration, quantity=quantity,
                           max_investment=0.0, submit=submit,
                           action="SELL", auto_size=False)
        url = f"https://trade.thinkorswim.com/trade?symbol={symbol}"
        self._emit(f"Abriendo {symbol} para cierre…")
        self._nav_callback = self._run_pending_order
        self.navigate.emit(url)
        return True

    def _on_order(self, res: dict):
        if self._order:
            res.setdefault("action", self._order.get("action"))
            res.setdefault("symbol", self._order.get("symbol"))
            res.setdefault("side", self._order.get("side"))
            res.setdefault("strike", self._order.get("strike"))
            res.setdefault("expiration", self._order.get("expiration"))
        state = res.get("state", "")
        detail = res.get("detail", "")
        steps = res.get("steps", [])
        if steps:
            self._emit("Pasos: " + " · ".join(str(s) for s in steps[-8:]))
        self.order_result.emit(res)
        self._end(bool(res.get("ok")), f"{state}: {detail}")

    # --- entrada pública ---
    def ensure_logged_in_paper(self, username: str, password: str):
        """Punto de entrada: deja la sesión lista en Paper Money."""
        if self._busy:
            self._emit("Auto-login ya en curso…")
            return
        self._busy = True
        self._user = username
        self._password = password
        self._paper_tries = 0
        self._login_tries = 0
        self._nav_to_frame_done = False
        self._emit("Auto-login: comprobando estado de thinkorswim…")
        self._check_state()

    def on_page_loaded(self, url: str):
        """La ventana avisa cuando termina una carga (tras navegar a una URL)."""
        if self._busy and self._nav_callback:
            cb = self._nav_callback
            self._nav_callback = None
            QTimer.singleShot(2500, cb)

    # --- pasos ---
    def _check_state(self):
        self.runner.run(tos_scripts.visible_state_script(), self._on_state, timeout_ms=12000)

    def _on_state(self, st: dict):
        if st.get("paperOk"):
            self._paper_confirmed = True
            self._end(True, "Sesión lista en Paper Money.")
            return

        url = st.get("url", "")
        in_login_frame = "sws-gateway" in url or "schwab.com/login" in url
        login_form_here = st.get("loginForm") or st.get("deepInputs", 0) > 0

        # caso 1: shell superior con iframe de login → navegar al iframe
        if (not in_login_frame and not login_form_here
                and not st.get("tradeVisible")):
            frame_url = pick_login_frame(st)
            if frame_url and not self._nav_to_frame_done:
                self._nav_to_frame_done = True
                self._nav_callback = self._check_state
                self._emit("Login dentro de iframe; navegando al formulario de Schwab…")
                self.navigate.emit(frame_url)
                return

        # caso 2: hay formulario de login visible. Schwab/TOS puede mostrarlo
        # dentro de sws-gateway o directamente en trade.thinkorswim.com.
        if login_form_here:
            if not (self._user and self._password):
                self._end(False, "Hace falta login pero no hay credenciales guardadas.")
                return
            if self._login_tries >= 2:
                self._end(False, "No pude completar el login tras 2 intentos.")
                return
            self._login_tries += 1
            self._emit(f"Login: enviando credenciales (intento {self._login_tries})…")
            self.runner.run(tos_scripts.login_script(self._user, self._password),
                            self._on_login, timeout_ms=20000)
            return

        # caso 3: ya en el dominio de trade (post-oauth) o logueado → asegurar paper
        self._ensure_paper()

    def _on_login(self, res: dict):
        state = res.get("state", "")
        if state == "MFA_REQUIRED":
            self._end(False, "Login enviado: completa el MFA a mano en la ventana. "
                             "Luego pulsa 'Auto-login' otra vez.")
            return
        if state in ("ALREADY_LOGGED_IN", "PASSWORD_FIELD_NOT_FOUND"):
            # puede que ya estemos dentro (post-oauth) → revalidar/asegurar paper
            self._emit("Sin formulario de login; revalidando sesión…")
            QTimer.singleShot(3000, self._check_state)
            return
        if not res.get("ok"):
            self._end(False, f"Login bloqueado: {res.get('detail') or state}")
            return
        # login enviado: esperar a que cargue y revalidar estado
        self._emit("Login enviado; esperando carga de la sesión…")
        QTimer.singleShot(4000, self._check_state)

    def ensure_live_account(self):
        """Pone thinkorswim en Real Live Trading. Espejo de ensure_logged_in_paper.

        No hace login: se asume sesion abierta. Solo mueve el selector de cuenta
        para que la captura de token recoja el token del entorno real.
        """
        if self._busy:
            self._emit("Automatización ocupada; espera a que termine…")
            return False
        self._busy = True
        self._live_tries = 0
        self._ensure_live()
        return True

    def _ensure_live(self):
        if getattr(self, "_live_tries", 0) >= 3:
            self._end(False, "No pude activar Real Live Trading tras varios intentos.")
            return
        self._live_tries = getattr(self, "_live_tries", 0) + 1
        self._emit(f"Cambiando a Real Live Trading (intento {self._live_tries})…")
        self.runner.run(tos_scripts.live_mode_script(), self._on_live, timeout_ms=16000)

    def _on_live(self, res: dict):
        state = res.get("state", "")
        if res.get("ok") and state == "LIVE_CONFIRMED":
            self._paper_confirmed = False
            self._end(True, "Real Live Trading activo.")
            return
        if state == "LIVE_SWITCH_REQUESTED":
            self._emit("Cambio a Real solicitado; revalidando…")
            QTimer.singleShot(2500, self._ensure_live)
            return
        self._end(False, f"No pude activar Real Live Trading ({state}).")

    def ensure_paper_account(self):
        """Devuelve thinkorswim a Paper Money SIN relanzar el login.

        Espejo exacto de ``ensure_live_account``. Antes, para volver a papel se
        llamaba a ``ensure_logged_in_paper``, que es el flujo COMPLETO de login:
        con la sesion ya abierta toma otra rama y puede no llegar a tocar el
        selector de cuenta. El cambio de cuenta y el login son cosas distintas.
        """
        if self._busy:
            self._emit("Automatización ocupada; espera a que termine…")
            return False
        self._busy = True
        self._paper_tries = 0
        self._ensure_paper()
        return True

    def _ensure_paper(self):
        if self._paper_tries >= 3:
            self._end(False, "No pude confirmar Paper Money tras varios intentos.")
            return
        self._paper_tries += 1
        self._emit(f"Confirmando Paper Money (intento {self._paper_tries})…")
        self.runner.run(tos_scripts.paper_mode_script(), self._on_paper, timeout_ms=16000)

    def _on_paper(self, res: dict):
        state = res.get("state", "")
        if res.get("ok") and state == "PAPER_CONFIRMED":
            self._paper_confirmed = True
            self._end(True, "Paper Money confirmado.")
            return
        if state == "LIVE_RISK":
            self._end(False, "ATENCIÓN: la cuenta parece Live. No activé Paper automáticamente; "
                             "cámbiala a Paper Money a mano antes de operar.")
            return
        if state == "PAPER_SWITCH_REQUESTED":
            self._emit("Cambio a Paper solicitado; revalidando…")
            QTimer.singleShot(2500, self._ensure_paper)
            return
        # PAPER_NOT_FOUND u otro: reintentar tras un momento
        QTimer.singleShot(2500, self._ensure_paper)
