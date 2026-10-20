"""
Servicio para un servidor: login headless + API HTTP de ordenes, en un proceso.

    python -m tos.servicio

Configuracion por entorno (los secretos, en el gestor de secretos del servidor):
  TOS_USERNAME, TOS_PASSWORD   credenciales de thinkorswim (secretos)
  TOS_ORDER_TOKEN              clave de la API (cabecera X-TOS-Order-Token);
                               obligatoria si TOS_HTTP_HOST no es local
  TOS_HTTP_HOST                por defecto 127.0.0.1
  TOS_HTTP_PORT                por defecto 5060
  TOS_CDP_PORT                 puerto CDP interno del Chromium, por defecto 9333
  TOS_RENEW_MINUTES            cada cuanto revisar el token, por defecto 5
  TOS_DATA_DIR                 token y perfil del navegador (disco persistente)

Si Schwab pide MFA, GET /login_status muestra "mfa_required"; envia el codigo
con POST /mfa {"code": "123456"} (con la cabecera X-TOS-Order-Token).
"""
from __future__ import annotations

import os
import signal
import threading
import time

from tos.headless_login import HeadlessLogin, ProveedorNavegador
from tos.server import TosHttpServer
from tos.ws_runtime import WsRuntime

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main() -> None:
    host = os.getenv("TOS_HTTP_HOST", "127.0.0.1")
    port = int(os.getenv("TOS_HTTP_PORT", "5060"))
    cdp_port = int(os.getenv("TOS_CDP_PORT", "9333"))
    renew_s = max(60.0, float(os.getenv("TOS_RENEW_MINUTES", "5")) * 60)
    order_token = os.getenv("TOS_ORDER_TOKEN") or None
    if host not in _LOCAL_HOSTS and not order_token:
        raise SystemExit("TOS_ORDER_TOKEN es obligatorio cuando la API escucha fuera de localhost")

    logs: list[str] = []

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line, flush=True)
        logs.append(line)
        del logs[:-500]

    login = HeadlessLogin(cdp_port=cdp_port, log=log)
    runtime = WsRuntime(cdp_port=cdp_port, log=log, token_provider=ProveedorNavegador(login))
    server = TosHttpServer(port=port, host=host, ws_runtime=runtime, log_lines=logs,
                           order_token=order_token, login_manager=login)

    runtime.start()          # arranca el navegador y la captura del token
    server.start()
    log(f"tos-exec servicio escuchando en {server.url}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # Login al arrancar y renovacion cuando el token vence. Con MFA pendiente
    # no se reintenta en bucle: se espera a que llegue el codigo por POST /mfa.
    # Tras 3 fallos seguidos se deja de reintentar solo: con una contrasena mala,
    # insistir cada pocos minutos acabaria bloqueando la cuenta en Schwab.
    # POST /login (manual) vuelve a intentarlo.
    failures = 0
    while not stop.is_set():
        try:
            state = login.status()
            if state["token_valid"]:
                failures = 0
            elif state["state"] not in ("mfa_required", "logging_in") and failures < 3:
                login.start()
                result = login.login()
                if result.get("state") == "error":
                    failures += 1
                    if failures >= 3:
                        log("Login TOS pausado tras 3 fallos: revisa credenciales y usa POST /login")
        except Exception as exc:
            failures += 1
            log(f"Login TOS fallo: {exc}")
        stop.wait(renew_s)

    server.stop()
    login.stop()


if __name__ == "__main__":
    main()
