# tos-exec

Libreria independiente para ejecutar opciones en **thinkorswim web Paper Money**.
**Solo metodos, sin UI**: la interfaz (ventana, webview, botones) la implementa
cada sistema que la instala. Es la unica copia a mantener: cada proyecto la
instala y le inyecta sus propias fuentes de datos via `tos.hooks`.

## Instalacion

```bash
# nucleo (WS + gestion + receptor HTTP + cliente, sin Qt):
pip install "tos-exec @ git+https://github.com/<usuario>/tos-exec.git"

# con los helpers Qt (automation DOM / captura WS en un webview del host):
pip install "tos-exec[qt] @ git+https://github.com/<usuario>/tos-exec.git"

# desarrollo local (repo hermano):
pip install -e '../tos-exec[qt]'
```

El paquete importable se llama `tos`.

## Que trae (todo son metodos/clases)

| Modulo | Que hace | Qt |
|---|---|---|
| `tos.ws_client` | Cliente WebSocket del gateway WsJson de Schwab: login por token, cuentas, posiciones, ordenes CONFIRM→SUBMIT, safeguard anti-short | no |
| `tos.ws_runtime` | Loop asyncio en un hilo: captura de token via CDP + `place_option()` / `held_option_symbols()` thread-safe | no |
| `tos.alerts` | `TosTradeManager`: recibe alertas, coloca la entrada y gestiona la salida por prima (+10% trail / -20% stop); reconcilia contra el broker. Callbacks `on_log` / `on_position_changed` | no |
| `tos.server` | `TosHttpServer`: receptor HTTP (`/alert`, `/close`, `/positions`, `/ws_order`, `/ws_status`, `/logs`) | no |
| `tos.client` | Cliente para mandar alertas a un receptor (`send_alert`, `send_close`, `positions`) | no |
| `tos.token_store` / `tos.ws_token_capture` | Persistencia del token de sesion (~8h) y captura automatica desde el webview del host via CDP | no |
| `tos.hooks` | Inyeccion de datos del proyecto (precio, contrato, prima) | no |
| `tos.credentials` | Usuario/clave en macOS Keychain | no |
| `tos.automation` + `tos.tos_scripts` | Fallback DOM: auto-login Paper Money y ordenes clicando el DOM de un `QWebEnginePage` que crea el HOST | si (`[qt]`) |

## Lo que pone cada sistema (la interfaz)

El host crea su propia ventana/webview donde el usuario hace login en
`trade.thinkorswim.com` (con `QTWEBENGINE_REMOTE_DEBUGGING=9333` para que la
captura de token via CDP funcione) y conecta las piezas:

```python
from tos.alerts import TosTradeManager
from tos.server import TosHttpServer
from tos.ws_runtime import WsRuntime

ws = WsRuntime(cdp_port=9333, log=print)
ws.start()                                  # captura token + ordenes por WS

manager = TosTradeManager(
    ws_runtime=ws,
    on_log=print,                           # la UI decide como mostrarlo
    on_position_changed=mi_refresco_ui,
)
manager.start()                             # sondeo interno (o llama
                                            # manager.check_prices() con tu timer)

server = TosHttpServer(manager, port=5060, ws_runtime=ws)
server.start()                              # POST /alert desde el scanner
```

Notas de hilos: la ruta WebSocket es thread-safe. Si ademas usas el fallback
DOM (`tos.automation`), llama `submit_alert` / `check_prices` desde el hilo de
Qt (timer propio + señales) y pasa `on_alert`/`on_close` a `TosHttpServer`
para reencolar los requests en tu hilo.

## Hooks: datos del proyecto

```python
from tos import hooks

hooks.configure(
    price=mi_precio,              # (symbol) -> (precio, ts_iso)
    select_contract=mi_selector,  # (symbol, direction, strategy=, budget=) ->
                                  #   {"strike","expiration","cost","delta","reason"}
    option_premium=mi_prima,      # (symbol, expiration, direction, strike) -> float|None
)
```

Tambien por entorno: `TOS_HOOKS_MODULE=<modulo>` + `tos.hooks.load_from_env()`.
Sin hooks la libreria opera igual, pero las alertas deben traer `entry_price`,
`strike` y `expiration`, y no hay gestion de salida por prima.

## Datos (token, posiciones)

Estado vivo en `~/.tos-exec/` (cambiable con `TOS_DATA_DIR`): `ws_token.json`,
`managed_positions.json`. Nunca dentro del paquete ni en un repo.

## Formato de alerta

```json
{
  "symbol": "SPY",
  "direction": "CALL",
  "strategy": "yoel:investep:midpoint-bounce",
  "entry_price": 742.0,
  "strike": 745.0,
  "expiration": "2026-07-02",
  "max_investment": 500,
  "max_contracts": 1
}
```

Si `strike`/`expiration` no vienen, se usa el hook `select_contract`.

## Gestion de salida (prima del contrato)

`TosTradeManager` consulta la prima en cada ciclo (hook `option_premium`):

- target **+10%** sobre la prima → activa trailing,
- trailing: cierra si el extra sobre el target retrocede **10%** desde su pico,
- stop duro **-20%**.

Ademas reconcilia contra la cuenta TOS (via WS): detecta cierres manuales y
verifica que los cierres pedidos de verdad ejecutaron.

## Seguridad

- El script DOM bloquea el envio si detecta cuenta Live o si no confirma Paper Money.
- Safeguard anti-short: al cerrar solo vende lo que la cuenta tiene del contrato.
- Token de sesion (~8h) capturado del webview del host via CDP.

Ver `tos/WS_PROTOCOL.md` para el detalle del protocolo WsJson.
