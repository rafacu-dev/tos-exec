# tos-exec

Version actual: **1.4.0**.

Libreria independiente para interactuar con **thinkorswim web Paper Money**
mediante el gateway WebSocket de Schwab. No trae UI propia: el proyecto host
crea el webview, guarda/renueva la sesion y llama estos metodos.

## Proposito de v1.2

La API recomendada de esta version es deliberadamente pequena y explicita:

- `WsRuntime.buy_option(...)`: compra contratos de opcion por WebSocket.
- `WsRuntime.sell_option(...)`: vende contratos de opcion por WebSocket.
- `WsRuntime.auto_exit_percent(...)`: revisa posiciones abiertas y vende cuando
  una posicion alcanza el porcentaje configurado de ganancia o perdida.
- `WsRuntime.open_option_positions(...)`: devuelve posiciones OPTION abiertas.
- `WsRuntime.day_summary(...)`: devuelve resumen del dia/P&L disponible.

La libreria no debe decidir estrategias ni seleccionar entradas. Solo ejecuta
compras/ventas explicitas y, si el host lo pide, una salida automatica por
porcentaje configurable.

Los modulos historicos de alertas/gestion siguen en el paquete para no romper
instalaciones existentes, pero quedan como **legado** para integraciones nuevas.

## Instalacion

```bash
# nucleo WebSocket, sin Qt:
pip install "tos-exec @ git+https://github.com/<usuario>/tos-exec.git"

# con helpers Qt para webview, auto-login y captura CDP:
pip install "tos-exec[qt] @ git+https://github.com/<usuario>/tos-exec.git"

# desarrollo local:
pip install -e '../tos-exec[qt]'
```

El paquete importable se llama `tos`.

## Uso recomendado

```python
from tos.ws_runtime import WsRuntime

ws = WsRuntime(cdp_port=9333, log=print)
ws.start()

buy = ws.buy_option(
    root="SPY",
    expiration="2026-07-17",
    side="CALL",
    strike=625,
    quantity=1,
    submit=True,
)

sell = ws.sell_option(
    root="SPY",
    expiration="2026-07-17",
    side="CALL",
    strike=625,
    quantity=1,
    submit=True,
)
```

`sell_option()` mantiene el safeguard anti-short: si la cuenta no tiene una
posicion long de ese contrato, no envia una venta en descubierto.

## Salida automatica por porcentaje

La salida automatica se ejecuta como una revision explicita. No abre posiciones
ni selecciona estrategia; solo revisa posiciones OPTION abiertas y vende las que
alcancen el porcentaje configurado.

```python
result = ws.auto_exit_percent(
    take_profit_pct=15,   # vende si P/L% >= +15
    stop_loss_pct=20,     # vende si P/L% <= -20
    submit=True,
)
```

Para probar sin enviar orden:

```python
preview = ws.auto_exit_percent(take_profit_pct=15, stop_loss_pct=20, dry_run=True)
```

El P/L% se calcula con los campos que devuelva TOS (`AVERAGE_PRICE` /
`OPEN_PRICE` y `MARK` / `LAST`). Si TOS no entrega datos suficientes para una
posicion, esa posicion se reporta como `skip` y no se vende a ciegas.

## Bracket: stop loss + take profit (OCO)

Una sola orden `1st trgs OCO` de TOS: la entrada de compra dispara dos salidas
que se cancelan entre si (take profit LIMIT y stop loss STOP).

```python
res = ws.place_bracket("F", quantity=1, entry_type="LIMIT", entry_limit=10.00,
                       take_profit=14.00, stop_loss=9.00, submit=True)
res["order_ids"]   # {"entry": ..., "take_profit": ..., "stop_loss": ...}

# opciones (precios por contrato):
ws.bracket_option("SPY", "2026-07-15", "PUT", 755, quantity=1,
                  entry_limit=2.00, take_profit=3.00, stop_loss=1.00)

ws.cancel_order(res["order_ids"]["entry"])  # cancela las 3 patas
```

Solo entradas de compra; se exige `stop_loss < entry_limit < take_profit`
(con `entry_type="MARKET"`, solo `stop_loss < take_profit`). `submit=False` hace
solo el review (CONFIRM). Protocolo y como detectar cambios del broker:
`tos/WS_PROTOCOL.md`.

## Posiciones y resumen del dia

```python
positions = ws.open_option_positions()
summary = ws.day_summary()
```

`open_option_positions()` devuelve cada posicion abierta enriquecida con
cantidad, precio de entrada, mark, P/L abierto, P/L del dia si TOS lo entrega,
y raw broker data. `day_summary()` suma lo disponible y conserva la lista de
posiciones para inspeccion.

## Auto-login obligatorio

El token WebSocket vive fuera del repo, en `~/.tos-exec/ws_token.json`, y vence
aproximadamente a las 8 horas. En v1.2, cuando un metodo de orden detecta que
el token no existe o vencio, debe dispararse auto-login automaticamente desde
el host.

Como `tos-exec` no crea la UI, el auto-login se inyecta como callback:

```python
from tos.ws_runtime import WsRuntime

def auto_login():
    # En un host Qt, normalmente emite una senal al hilo de UI y llama:
    # automation.ensure_logged_in_paper(username, password)
    trigger_tos_webview_auto_login()

ws = WsRuntime(cdp_port=9333, log=print, auto_login=auto_login)
ws.start()
```

El callback debe iniciar el login en el webview del host. La captura CDP
(`QTWEBENGINE_REMOTE_DEBUGGING=9333`) detecta el nuevo token de TOS y lo guarda.
`WsRuntime` espera hasta `token_wait_seconds` a que aparezca el token renovado.
En hosts Qt, llama los metodos de orden desde un worker, request thread o cola
externa; el callback de auto-login debe encolar el trabajo al hilo de UI para
que el webview pueda seguir procesando eventos.

Si Schwab pide MFA, el usuario debe completarlo en el webview; despues la captura
CDP guarda el token y la orden continua.

## Login en un servidor (sin pantalla)

En un servidor no hay webview: `tos.headless_login` abre su propio Chromium sin
pantalla (Playwright), rellena usuario y contrasena desde **secretos del
entorno**, deja la sesion en Paper Money y el token se captura igual que en el
escritorio. El perfil del navegador es persistente (`<TOS_DATA_DIR>/chromium-profile`)
y conserva el "recordar este dispositivo", asi que Schwab pide MFA lo menos posible.

```bash
pip install "tos-exec[server] @ git+https://github.com/<usuario>/tos-exec.git"
python -m playwright install --with-deps chromium

export TOS_USERNAME=...          # secreto
export TOS_PASSWORD=...          # secreto
export TOS_ORDER_TOKEN=...       # clave de la API (X-TOS-Order-Token)
export TOS_DATA_DIR=/data/tos    # disco persistente: token + perfil
python -m tos.servicio           # login + API HTTP en un proceso
```

Variables opcionales: `TOS_HTTP_HOST` (127.0.0.1), `TOS_HTTP_PORT` (5060),
`TOS_CDP_PORT` (9333), `TOS_RENEW_MINUTES` (5). Si la API escucha fuera de
localhost, `TOS_ORDER_TOKEN` es obligatorio.

El servicio hace login al arrancar y vuelve a hacerlo cuando el token vence.
Tras 3 fallos seguidos deja de reintentar solo (una contrasena mala, insistida,
bloquearia la cuenta); `POST /login` lo vuelve a intentar.

**MFA.** Si Schwab pide un codigo, el login se detiene y `GET /login_status`
devuelve `"state": "mfa_required"` (el servicio ya pidio que te lo envien). Envia
el codigo que recibiste:

```bash
curl -X POST "$URL/mfa" -H "X-TOS-Order-Token: $TOS_ORDER_TOKEN" \
     -H "Content-Type: application/json" -d '{"code": "123456"}'
```

Nunca se intenta saltar el MFA. Si algo no avanza, `GET /login_screenshot`
(protegida con la misma clave) devuelve una captura PNG del navegador.

Los selectores de la pantalla de MFA se eligen por patron (texto, `one-time-code`,
`inputmode=numeric`) y se probaron contra un thinkorswim simulado, no contra el
DOM real de Schwab: si Schwab cambia esa pantalla, ajustarlos en
`tos/tos_scripts.py` usando la captura.

Uso como libreria, sin el servicio:

```python
from tos.headless_login import HeadlessLogin, ProveedorNavegador
from tos.ws_runtime import WsRuntime

login = HeadlessLogin(cdp_port=9333)
ws = WsRuntime(cdp_port=9333, token_provider=ProveedorNavegador(login))
ws.start()
login.login()            # -> {"state": "ready"} o {"state": "mfa_required"}
login.submit_mfa("123456")
```

## HTTP local opcional

`TosHttpServer` puede exponer metodos directos por HTTP:

- `POST /buy`: compra directa por WebSocket.
- `POST /sell`: venta directa por WebSocket.
- `POST /auto_exit`: revision de salida automatica por porcentaje.
- `POST /bracket`: entrada + take profit + stop loss (1st trgs OCO). Cuerpo:
  `symbol`, `quantity`, `entry_type`, `entry_limit`, `take_profit`,
  `stop_loss`, `tif`, `submit`; para opciones ademas `expiration`,
  `direction`, `strike`.
- `POST /cancel_order`: `{"order_id": ...}`.
- `GET /broker_positions`: posiciones OPTION abiertas del broker.
- `GET /day_summary`: resumen del dia y P/L disponible/calculado.
- `GET /ws_status`: estado del token.
- `GET /logs`: ultimas lineas de log.
- `GET /login_status`, `POST /login`, `POST /mfa`, `GET /login_screenshot`:
  login headless en un servidor (ver arriba).

Ejemplo:

```json
{
  "symbol": "SPY",
  "direction": "CALL",
  "expiration": "2026-07-17",
  "strike": 625,
  "quantity": 1,
  "submit": true
}
```

Cliente Python:

```python
from tos.client import buy_option, sell_option
from tos.client import auto_exit_percent, broker_positions, day_summary

buy_option("SPY", "CALL", "2026-07-17", 625, quantity=1)
sell_option("SPY", "CALL", "2026-07-17", 625, quantity=1)
auto_exit_percent(take_profit_pct=15, stop_loss_pct=20)
broker_positions()
day_summary()
```

## Modulos

| Modulo | Estado | Que hace |
|---|---|---|
| `tos.ws_runtime` | recomendado | Runtime thread-safe con compra/venta, salida por porcentaje, posiciones, resumen, captura CDP y auto-login inyectable |
| `tos.ws_client` | recomendado | Cliente WebSocket bajo nivel: login, cuentas, posiciones, ordenes CONFIRM/SUBMIT |
| `tos.server` | opcional | Receptor HTTP local con `/buy` y `/sell` |
| `tos.client` | opcional | Cliente HTTP para enviar compras/ventas al receptor local |
| `tos.token_store` / `tos.ws_token_capture` | recomendado | Persistencia y captura del token de sesion |
| `tos.headless_login` + `tos.servicio` | opcional `[server]` | Login en un servidor con Chromium headless, MFA por HTTP y servicio listo para ejecutar |
| `tos.automation` + `tos.tos_scripts` | opcional `[qt]` | Auto-login Paper Money en un `QWebEnginePage` creado por el host |
| `tos.alerts` | legado | Gestor de alertas y salidas por prima; no usar en integraciones v1.2 nuevas |
| `tos.hooks` | legado | Hooks de precios/contratos usados por el gestor anterior |

## Guia para cambios por IA

Cuando un agente de IA o colaborador modifique este repo para v1.2:

- Mantener la API publica centrada en compra/venta, consulta de posiciones,
  resumen y salida automatica por porcentaje.
- No agregar seleccion de estrategia, senales de entrada ni gestion compleja de
  trailing al flujo recomendado.
- No meter credenciales, tokens ni estado vivo dentro del repo.
- No hacer que `tos.ws_runtime` dependa de Qt; el auto-login debe seguir siendo
  callback inyectable desde el host.
- Mantener Paper Money como contexto de seguridad por defecto.
- Conservar el safeguard anti-short en ventas.

Ver tambien `AI_CHANGE_GUIDE.md` y `MIGRATION_V1_TO_V2.md`.
