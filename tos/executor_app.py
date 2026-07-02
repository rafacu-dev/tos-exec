"""Ejecutor standalone: thinkorswim web + receptor HTTP de alertas.

Uso:
  python3 -m tos.executor_app --port 5060

Luego enviar:
  curl -X POST http://127.0.0.1:5060/alert \
    -H 'Content-Type: application/json' \
    -d '{"symbol":"SPY","direction":"CALL","target_price":745,"stop_price":739}'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from tos import hooks, ws_capture
from tos.alerts import ManagedPositionStore, TosTradeManager
from tos.automation import TosAutomation
from tos.credentials import load_credentials, save_credentials
from tos.paths import data_dir

TOS_URL = "https://trade.thinkorswim.com/"
TEST_SYMBOLS = ["GOOG", "SPY", "QQQ", "AAPL", "NVDA", "AMZN", "MU"]
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class AlertBridge(QObject):
    alert = Signal(dict)
    close = Signal(str, str)


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: dict):
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def make_handler(bridge: AlertBridge, store: ManagedPositionStore, log_lines: list[str], ws_runtime=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):
            return

        def do_GET(self):
            if self.path.rstrip("/") == "/positions":
                _json_response(self, 200, {"ok": True, "positions": store.all()})
                return
            if self.path.rstrip("/") == "/logs":
                _json_response(self, 200, {"ok": True, "logs": list(log_lines[-200:])})
                return
            if self.path.rstrip("/") == "/ws_status":
                d = ws_runtime.token_status() if ws_runtime else None
                _json_response(self, 200, {"ok": True, "ws_enabled": ws_runtime is not None,
                                           "token_valid": bool(d), "age_hours": (d or {}).get("age_hours")})
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": f"invalid_json: {exc}"})
                return

            path = self.path.rstrip("/")
            if path == "/alert":
                bridge.alert.emit(payload)
                _json_response(self, 202, {"ok": True, "accepted": True})
                return
            if path == "/close":
                pos_id = str(payload.get("id") or payload.get("position_id") or "")
                alert_id = str(payload.get("alert_id") or "")
                symbol = str(payload.get("symbol") or "").upper()
                if not pos_id and (alert_id or symbol):
                    # el scanner no conoce el id interno: resuelve por alert_id (preciso)
                    # o por símbolo (una posición por símbolo).
                    for p in store.open_positions():
                        if alert_id and p.get("alert_id") == alert_id:
                            pos_id = p.get("id"); break
                        if not alert_id and symbol and str(p.get("symbol", "")).upper() == symbol:
                            pos_id = p.get("id"); break
                if not pos_id:
                    _json_response(self, 404, {"ok": False, "error": "sin posición abierta que matchee id/alert_id/symbol"})
                    return
                bridge.close.emit(pos_id, str(payload.get("reason") or "api"))
                _json_response(self, 202, {"ok": True, "accepted": True})
                return
            if path == "/ws_order":
                # Prueba directa de orden por WebSocket (decoupled del manager).
                # submit=false (default) solo hace el review (CONFIRM); true envía.
                if ws_runtime is None:
                    _json_response(self, 400, {"ok": False, "error": "WS no activo (lanza con TOS_WS=1)"})
                    return
                try:
                    res = ws_runtime.place_option(
                        root=str(payload["symbol"]).upper(),
                        expiration=str(payload["expiration"]),
                        side=str(payload["direction"]).upper(),
                        strike=float(payload["strike"]),
                        quantity=int(payload.get("quantity", 1)),
                        order_type=str(payload.get("order_type", "MARKET")).upper(),
                        limit_price=payload.get("limit_price"),
                        submit=bool(payload.get("submit", False)))
                    _json_response(self, 200, {"ok": True, "result": res})
                except Exception as exc:
                    _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})

    return Handler


class MainWindow(QMainWindow):
    _ws_msg = Signal(str)

    def __init__(self, port: int):
        super().__init__()
        self.setWindowTitle("PowerTradeAi - TOS Executor")
        self.resize(1450, 900)

        self.store = ManagedPositionStore()
        self.log_lines: list[str] = []
        self.bridge = AlertBridge()
        self.bridge.alert.connect(self._on_alert)
        self.bridge.close.connect(self._on_close)
        self._ws_msg.connect(self._log)

        root = QSplitter()
        root.addWidget(self._build_panel(port))
        self.web = self._build_web()
        root.addWidget(self.web)
        root.setSizes([420, 1030])
        self.setCentralWidget(root)

        self.automation = TosAutomation(self.web.page(), log=self._log)
        self.automation.navigate.connect(lambda url: self.web.setUrl(QUrl(url)))
        self.web.loadFinished.connect(lambda _ok: self.automation.on_page_loaded(self.web.url().toString()))
        # Runtime WS por DEFAULT (desactivar con TOS_WS_OFF=1): captura de token +
        # colocación de órdenes por WebSocket. Se crea ANTES del manager para que
        # éste rute las órdenes por WS cuando haya token válido (DOM si no).
        self.ws_runtime = None
        if not os.getenv("TOS_WS_OFF"):
            from tos.ws_runtime import WsRuntime
            self.ws_runtime = WsRuntime(cdp_port=9333, log=self._log)
            self.ws_runtime.start()

        self.manager = TosTradeManager(self.automation, store=self.store, ws_runtime=self.ws_runtime)
        self.manager.log.connect(self._log)
        self.manager.position_changed.connect(lambda _p: self._refresh_positions())
        self.manager.start()

        self.server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.bridge, self.store, self.log_lines, self.ws_runtime))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self._log(f"HTTP receptor activo en http://127.0.0.1:{port}")
        self.web.setUrl(QUrl(TOS_URL))
        self._refresh_positions()

        if self.ws_runtime is not None:
            d = self.ws_runtime.token_status()
            if d:
                self._log(f"WS: token válido (~{d.get('age_hours')}h). Órdenes por WebSocket listas (POST /ws_order).")
            else:
                self._log("WS: sin token — haz login en el webview (se captura solo). Sesión dura 8h.")

        # auto-relogin: si la sesión se cae (sin token), re-loguea solo con las
        # credenciales del Keychain. ensure_logged_in_paper es idempotente (revisa estado;
        # si ya está logueado no hace nada) y se DETIENE si Schwab pide MFA (intervienes tú).
        self._auto_relogin = os.getenv("TOS_NO_AUTOLOGIN") is None
        self._last_relogin_ts = 0.0
        if self._auto_relogin:
            self._relogin_timer = QTimer(self)
            self._relogin_timer.timeout.connect(self._auto_relogin_check)
            self._relogin_timer.start(60000)   # cada 60s
            self._log("Auto-relogin ACTIVO: re-loguea solo si la sesión se cae (Keychain). "
                      "MFA requiere tu intervención. Desactiva con TOS_NO_AUTOLOGIN=1.")
            # login automático también AL ABRIR: da ~8s para que cargue el webview y
            # luego inicia sesión si hace falta (idempotente; no-op si ya está logueado).
            QTimer.singleShot(8000, self._startup_login)

        if os.getenv("TOS_WS_CAPTURE"):
            self._ws_dump_path = data_dir() / "ws_capture.json"
            self._ws_timer = QTimer(self)
            self._ws_timer.timeout.connect(self._dump_wslog)
            self._ws_timer.start(10000)
            self._log(f"Captura WS activada → vuelca cada 10s a {self._ws_dump_path.name}")

    def _dump_wslog(self):
        def got(raw):
            try:
                if raw:
                    self._ws_dump_path.write_text(str(raw), encoding="utf-8")
            except Exception as exc:
                self._log(f"Volcado WS falló: {exc}")
        try:
            self.page.runJavaScript(ws_capture.DUMP_JS, got)
        except Exception:
            pass

    def _build_panel(self, port: int) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 8, 12)
        lay.setSpacing(8)

        title = QLabel("TOS Executor")
        title.setStyleSheet("font-size:17px;font-weight:600;")
        lay.addWidget(title)
        endpoint = QLabel(f"POST /alert  ·  http://127.0.0.1:{port}")
        endpoint.setStyleSheet("color:#8b94a3;")
        lay.addWidget(endpoint)

        row = QHBoxLayout()
        saved = load_credentials()
        self.user = QLineEdit(os.getenv("TOS_USER") or os.getenv("SCHWAB_USER") or saved.get("username") or "")
        self.user.setPlaceholderText("Login ID")
        self.password = QLineEdit(os.getenv("TOS_PASSWORD") or os.getenv("SCHWAB_PASSWORD") or saved.get("password") or "")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Password")
        self.save_credentials = QCheckBox("Guardar")
        self.save_credentials.setChecked(True)
        self.save_credentials.setToolTip("Guarda usuario y password en macOS Keychain")
        login = QPushButton("Auto-login Paper")
        login.clicked.connect(self._autologin)
        row.addWidget(self.user)
        row.addWidget(self.password)
        row.addWidget(self.save_credentials)
        row.addWidget(login)
        lay.addLayout(row)

        reload_btn = QPushButton("Recargar TOS")
        reload_btn.clicked.connect(lambda: self.web.setUrl(QUrl(TOS_URL)))
        lay.addWidget(reload_btn)

        test_row = QHBoxLayout()
        self.test_symbol = QComboBox()
        self.test_symbol.setEditable(True)
        self.test_symbol.addItems(TEST_SYMBOLS)
        self.test_symbol.setCurrentText("GOOG")
        self.test_symbol.setToolTip("Simbolo para prueba manual de compra/venta")
        self.test_side = QComboBox()
        self.test_side.addItems(["PUT", "CALL"])
        self.test_side.setCurrentText("PUT")
        self.test_side.setToolTip("Tipo de contrato para la prueba")
        buy_test = QPushButton("Probar compra")
        buy_test.clicked.connect(self._test_buy)
        sell_test = QPushButton("Probar venta")
        sell_test.clicked.connect(self._test_sell)
        test_row.addWidget(QLabel("Test:"))
        test_row.addWidget(self.test_symbol)
        test_row.addWidget(self.test_side)
        test_row.addWidget(buy_test)
        test_row.addWidget(sell_test)
        lay.addLayout(test_row)

        # --- Prueba de orden por WebSocket (usa el símbolo/lado de arriba) ---
        ws_row = QHBoxLayout()
        self.ws_strike = QLineEdit()
        self.ws_strike.setPlaceholderText("strike (ej. 335)")
        self.ws_exp = QLineEdit()
        self.ws_exp.setPlaceholderText("venc YYYY-MM-DD")
        ws_test_btn = QPushButton("Testear WS")
        ws_test_btn.setToolTip("Prueba completa por WebSocket (auth + cuenta + orden CONFIRM→SUBMIT en paper). Guarda ws_test_result.json")
        ws_test_btn.setStyleSheet("font-weight:600;")
        ws_test_btn.clicked.connect(self._ws_test)
        ws_row.addWidget(QLabel("WS:"))
        ws_row.addWidget(self.ws_strike)
        ws_row.addWidget(self.ws_exp)
        ws_row.addWidget(ws_test_btn)
        lay.addLayout(ws_row)

        # --- STRADDLE (multidireccional): compra CALL + PUT mismo strike ATM ---
        st_row = QHBoxLayout()
        self.straddle_symbol = QComboBox()
        self.straddle_symbol.setEditable(True)
        self.straddle_symbol.addItems(["TSLA", "NVDA", "MU"] + TEST_SYMBOLS)
        self.straddle_symbol.setCurrentText("TSLA")
        self.straddle_symbol.setToolTip("Símbolo para el straddle (CALL+PUT). Mejor en beta alta: TSLA/NVDA/MU.")
        self.straddle_budget = QComboBox()
        self.straddle_budget.addItems(["100", "250", "500", "1000"])
        self.straddle_budget.setCurrentText("500")
        self.straddle_budget.setToolTip("Presupuesto POR PATA (mismo $ en CALL y PUT). Cada pata elige su mejor strike.")
        straddle_btn = QPushButton("Enviar Straddle (paper)")
        straddle_btn.setStyleSheet("font-weight:600;background:#2d6cdf;color:white;")
        straddle_btn.setToolTip("Manda a TOS paper un STRADDLE: elige por SEPARADO el mejor strike del CALL y del PUT con el mismo $, balanceado en delta.")
        straddle_btn.clicked.connect(self._send_straddle)
        st_row.addWidget(QLabel("Straddle:"))
        st_row.addWidget(self.straddle_symbol)
        st_row.addWidget(QLabel("$/pata"))
        st_row.addWidget(self.straddle_budget)
        st_row.addWidget(straddle_btn)
        lay.addLayout(st_row)

        lay.addWidget(QLabel("Posiciones gestionadas"))
        self.positions = QPlainTextEdit()
        self.positions.setReadOnly(True)
        self.positions.setMaximumBlockCount(500)
        lay.addWidget(self.positions, stretch=2)

        lay.addWidget(QLabel("Log"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(800)
        lay.addWidget(self.log, stretch=3)
        return panel

    def _build_web(self) -> QWebEngineView:
        app = QApplication.instance()
        self.profile = QWebEngineProfile("localquantai-tos", app)
        self.profile.setHttpUserAgent(CHROME_UA)
        storage = data_dir() / "webdata"
        storage.mkdir(exist_ok=True)
        self.profile.setPersistentStoragePath(str(storage))
        self.profile.setCachePath(str(storage / "cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        if os.getenv("TOS_WS_CAPTURE"):
            self.profile.scripts().insert(ws_capture.build_script())
        view = QWebEngineView()
        self.page = QWebEnginePage(self.profile, view)
        view.setPage(self.page)
        return view

    def _autologin(self):
        username = self.user.text().strip()
        password = self.password.text()
        if self.save_credentials.isChecked():
            if save_credentials(username, password):
                self._log("Credenciales TOS guardadas en macOS Keychain.")
            else:
                self._log("No pude guardar credenciales TOS; revisa Keychain/macOS.")
        self.automation.ensure_logged_in_paper(username, password)

    def _creds(self) -> tuple[str, str]:
        """Credenciales de los campos de la UI; si están vacíos, del Keychain."""
        u = self.user.text().strip() if hasattr(self, "user") else ""
        p = self.password.text() if hasattr(self, "password") else ""
        if u and p:
            return u, p
        saved = load_credentials()
        return saved.get("username", ""), saved.get("password", "")

    def _startup_login(self):
        """Login automático al abrir el executor (si hay credenciales guardadas).
        ensure_logged_in_paper es idempotente: si ya hay sesión, no hace nada."""
        if not getattr(self, "_auto_relogin", False):
            return
        u, p = self._creds()
        if not (u and p):
            self._log("Auto-login al abrir: sin credenciales guardadas. Mete usuario/clave "
                      "una vez y marca «Guardar» para que entre solo a partir de ahora.")
            return
        import time as _t
        self._last_relogin_ts = _t.time()
        self._log("Auto-login al abrir: iniciando sesión con credenciales guardadas…")
        self.automation.ensure_logged_in_paper(u, p)

    def _auto_relogin_check(self):
        """Dispara el re-login si la sesión se cayó (sin token válido). Idempotente y
        con cooldown para no spamear (sobre todo si Schwab pide MFA)."""
        if not getattr(self, "_auto_relogin", False) or self.ws_runtime is None:
            return
        if self.ws_runtime.token_status():      # token válido → sesión viva, nada que hacer
            return
        import time as _t
        if _t.time() - self._last_relogin_ts < 120:   # cooldown anti-spam
            return
        u, p = self._creds()
        if not (u and p):
            return   # sin credenciales guardadas → no hay nada que reintentar
        self._last_relogin_ts = _t.time()
        self._log("Auto-relogin: sesión caída (sin token) → reintentando login con credenciales guardadas…")
        self.automation.ensure_logged_in_paper(u, p)

    def _test_symbol_side(self) -> tuple[str, str]:
        symbol = self.test_symbol.currentText().strip().upper()
        side = self.test_side.currentText().strip().upper()
        return symbol, side

    def _test_buy(self):
        symbol, side = self._test_symbol_side()
        if not symbol:
            self._log("Prueba compra cancelada: falta simbolo.")
            return
        try:
            price, ts = self.manager.get_price(symbol)
            price = float(price)
            sign = 1 if side == "CALL" else -1
            move = max(round(price * 0.0035, 2), 1.0)
            alert = {
                "symbol": symbol,
                "direction": side,
                "entry_price": price,
                "target_price": round(price + sign * move, 2),
                "stop_price": round(price - sign * move, 2),
                "strategy": "manual_tos_test",
                "max_investment": 500,
                "max_contracts": 1,
                "quantity": 1,
                "submit": True,
                "note": f"PowerTradeAi prueba manual TOS {symbol} {side} {ts}",
            }
            alert_id = self.manager.submit_alert(alert)
            self._log(
                f"Prueba compra enviada: {symbol} {side} entry {price:.2f} "
                f"target {alert['target_price']:.2f} stop {alert['stop_price']:.2f} ({alert_id})"
            )
        except Exception as exc:
            self._log(f"Prueba compra fallo: {exc}")

    def _test_sell(self):
        symbol, side = self._test_symbol_side()
        open_positions = [
            p for p in self.store.open_positions()
            if str(p.get("symbol")).upper() == symbol and str(p.get("direction")).upper() == side
        ]
        if not open_positions:
            self._log(f"Prueba venta cancelada: no hay posicion abierta gestionada para {symbol} {side}.")
            return
        pos = open_positions[-1]
        if self.manager.request_close(pos["id"], "manual_test_sell"):
            self._log(f"Prueba venta solicitada: {symbol} {side} posicion {pos['id']}.")

    def _send_straddle(self):
        """STRADDLE balanceado: elige por SEPARADO el mejor strike del CALL y del PUT con
        el MISMO presupuesto por pata (no fuerza el mismo strike, que desbalancea). Cada
        pata se sizea por su costo y queda gestionada por la prima (+10% trail / -20% stop)."""
        symbol = self.straddle_symbol.currentText().strip().upper()
        try:
            budget = float(self.straddle_budget.currentText())
        except Exception:
            budget = 500.0
        if not symbol:
            self._log("Straddle cancelado: falta símbolo.")
            return
        if hooks.select_contract is None:
            self._log("Straddle cancelado: sin hook select_contract (configura tos.hooks / TOS_HOOKS_MODULE).")
            return
        try:
            price, ts = self.manager.get_price(symbol)
            price = float(price)
        except Exception as exc:
            self._log(f"Straddle {symbol}: no pude leer precio ({exc}).")
            return
        self._log(f"STRADDLE {symbol} · ${budget:.0f}/pata · precio {price:.2f} → eligiendo mejor "
                  f"strike de CALL y PUT por separado…")
        for side in ("CALL", "PUT"):
            try:
                sel = hooks.select_contract(symbol, side, strategy="trend-cross-15m", budget=budget)
                strike = float(sel["strike"])
                exp = str(sel["expiration"])
                cost = float(sel.get("cost") or 0)                    # ask × 100 (un contrato)
                delta = sel.get("delta")
                qty = max(1, int(budget // cost)) if cost > 0 else 1
            except Exception as exc:
                self._log(f"  ✗ pata {side}: no pude seleccionar contrato ({exc}).")
                continue
            alert = {
                "symbol": symbol, "direction": side, "entry_price": price,
                "strike": strike, "expiration": exp, "strategy": "straddle",
                "max_investment": budget, "max_contracts": qty,
                "quantity": qty, "submit": True,
                "note": f"PowerTradeAi STRADDLE {symbol} {side} k{strike} {ts}",
            }
            try:
                alert_id = self.manager.submit_alert(alert)
                dtxt = f"Δ{delta:.2f}" if delta is not None else "Δ?"
                self._log(f"  ✓ {side}: strike {strike} {exp} · {qty}×${cost:.0f} ({dtxt}) enviada ({alert_id})")
            except Exception as exc:
                self._log(f"  ✗ pata {side} FALLÓ: {exc}")

    def _ws_test(self):
        """Botón 'Testear WS': prueba completa y guarda la traza en ws_test_result.json."""
        if self.ws_runtime is None:
            self._log("WS desactivado (TOS_WS_OFF). No se puede testear por WebSocket.")
            return
        symbol, side = self._test_symbol_side()
        strike_txt = self.ws_strike.text().strip()
        exp = self.ws_exp.text().strip()
        if not symbol or not strike_txt or not exp:
            self._log("Testear WS: faltan símbolo, strike o vencimiento.")
            return
        try:
            strike = float(strike_txt)
        except ValueError:
            self._log(f"Testear WS: strike inválido '{strike_txt}'.")
            return
        self._log(f"Testear WS: {symbol} {side} strike {strike:g} venc {exp} (auth+cuenta+orden)…")
        out_path = data_dir() / "ws_test_result.json"

        def job():
            try:
                trace = self.ws_runtime.test_order(
                    root=symbol, expiration=exp, side=side, strike=strike, quantity=1, submit=True)
            except Exception as exc:
                trace = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                out_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            summary = "✓ TODO OK" if trace.get("ok") else f"✗ {trace.get('error')}"
            self._ws_msg.emit(f"Testear WS: {summary} → traza en {out_path.name}")

        threading.Thread(target=job, daemon=True).start()

    def _on_alert(self, payload: dict):
        try:
            alert_id = self.manager.submit_alert(payload)
            self._log(f"Alerta aceptada: {alert_id}")
        except Exception as exc:
            self._log(f"Alerta rechazada: {exc}")

    def _on_close(self, pos_id: str, reason: str):
        self.manager.request_close(pos_id, reason)

    def _refresh_positions(self):
        rows = []
        for p in self.store.all()[-30:]:
            rows.append(
                f"{p.get('status'):<8} {p.get('id')} {p.get('symbol')} {p.get('direction')} "
                f"target={p.get('current_target')} stop={p.get('stop_price')} "
                f"hits={p.get('target_hits')} last={p.get('last_price')}"
            )
        self.positions.setPlainText("\n".join(rows))

    def _log(self, msg: str):
        line = str(msg)
        self.log_lines.append(line)
        del self.log_lines[:-500]
        self.log.appendPlainText(line)

    def closeEvent(self, event):
        try:
            self.manager.stop()
            self.server.shutdown()
        finally:
            super().closeEvent(event)


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5060)
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")   # claves del proyecto anfitrion (para sus hooks)
    except ImportError:
        pass
    hooks.load_from_env(log=print)
    if not os.getenv("TOS_WS_OFF"):
        # WS por defecto: remote-debugging para capturar el token del webview vía CDP
        os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", "9333")
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--remote-allow-origins=*")
    app = QApplication(sys.argv)
    w = MainWindow(args.port)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
