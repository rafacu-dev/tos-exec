# Migracion de v1 a v2

Esta guia esta escrita para humanos y agentes de IA que migran un proyecto host
que usa `tos-exec` como repo local o como dependencia instalada.

En este documento, **v2** se refiere a la linea nueva de API explicita:
compra/venta por WebSocket, salida automatica por porcentaje, consulta de
posiciones, resumen del dia y auto-login inyectable. La version del paquete
puede verse en `tos.__version__`.

## Resumen del cambio

v1 usaba principalmente:

- `TosTradeManager`
- `TosAlert`
- `tos.hooks.configure(...)`
- `POST /alert`
- `POST /close`
- `tos.client.send_alert(...)`
- `tos.client.send_close(...)`
- posiciones gestionadas en `managed_positions.json`

v2 debe usar principalmente:

- `WsRuntime.buy_option(...)`
- `WsRuntime.sell_option(...)`
- `WsRuntime.auto_exit_percent(...)`
- `WsRuntime.open_option_positions(...)`
- `WsRuntime.day_summary(...)`
- `POST /buy`
- `POST /sell`
- `POST /auto_exit`
- `GET /broker_positions`
- `GET /day_summary`
- `tos.client.buy_option(...)`
- `tos.client.sell_option(...)`
- `tos.client.auto_exit_percent(...)`
- `tos.client.broker_positions(...)`
- `tos.client.day_summary(...)`

Los modulos antiguos siguen disponibles por compatibilidad, pero no son la ruta
recomendada para integraciones nuevas.

## Migracion si usas la dependencia

Actualiza la instalacion:

```bash
pip install --upgrade "tos-exec @ git+https://github.com/<usuario>/tos-exec.git"
```

Si el host usa Qt/webview para auto-login y captura CDP:

```bash
pip install --upgrade "tos-exec[qt] @ git+https://github.com/<usuario>/tos-exec.git"
```

Verifica la version importada:

```python
import tos
print(tos.__version__)
```

## Migracion si usas el repo local

Actualiza el repo `tos-exec`, instala editable y compila:

```bash
git pull
pip install -e '../tos-exec[qt]'
python -m compileall tos
```

No copies archivos de `~/.tos-exec/` al repo. Ese directorio contiene estado
vivo como tokens y posiciones.

## Reemplazo de inicializacion

### Antes

```python
from tos.alerts import TosTradeManager
from tos.server import TosHttpServer
from tos.ws_runtime import WsRuntime

ws = WsRuntime(cdp_port=9333, log=print)
ws.start()

manager = TosTradeManager(ws_runtime=ws, on_log=print)
manager.start()

server = TosHttpServer(manager, port=5060, ws_runtime=ws)
server.start()
```

### Despues

```python
from tos.server import TosHttpServer
from tos.ws_runtime import WsRuntime

def auto_login():
    # En un host Qt: emitir una senal al hilo de UI y llamar:
    # automation.ensure_logged_in_paper(username, password)
    trigger_tos_webview_auto_login()

ws = WsRuntime(cdp_port=9333, log=print, auto_login=auto_login)
ws.start()

server = TosHttpServer(port=5060, ws_runtime=ws)
server.start()
```

Notas:

- No arranques `TosTradeManager` salvo que necesites compatibilidad antigua.
- En hosts Qt, no bloquees el hilo de UI esperando ordenes o token. Llama los
  metodos de orden desde un worker/request thread y usa `auto_login` para
  encolar el login al hilo de UI.
- `QTWEBENGINE_REMOTE_DEBUGGING=9333` debe estar activo para que CDP capture el
  token renovado.

## Reemplazo de compra

### Antes

```python
from tos.client import send_alert

send_alert({
    "symbol": "SPY",
    "direction": "CALL",
    "strike": 625,
    "expiration": "2026-07-17",
    "max_contracts": 1,
})
```

### Despues

```python
from tos.client import buy_option

buy_option(
    "SPY",
    "CALL",
    "2026-07-17",
    625,
    quantity=1,
    submit=True,
)
```

O directo en proceso:

```python
ws.buy_option(
    root="SPY",
    side="CALL",
    expiration="2026-07-17",
    strike=625,
    quantity=1,
    submit=True,
)
```

## Reemplazo de venta/cierre manual

### Antes

```python
from tos.client import send_close

send_close(symbol="SPY", reason="manual")
```

### Despues

```python
from tos.client import sell_option

sell_option(
    "SPY",
    "CALL",
    "2026-07-17",
    625,
    quantity=1,
    submit=True,
)
```

O directo:

```python
ws.sell_option(
    root="SPY",
    side="CALL",
    expiration="2026-07-17",
    strike=625,
    quantity=1,
    submit=True,
)
```

`sell_option()` mantiene safeguard anti-short: si no hay long del contrato, no
envia venta en descubierto.

## Salida automatica por porcentaje

v2 conserva salida automatica, pero como funcion explicita. El host decide
cuando correrla: por timer, por boton, por job programado o despues de una
actualizacion de precios.

```python
result = ws.auto_exit_percent(
    take_profit_pct=15,
    stop_loss_pct=20,
    submit=True,
)
```

Prueba sin enviar orden:

```python
preview = ws.auto_exit_percent(
    take_profit_pct=15,
    stop_loss_pct=20,
    dry_run=True,
)
```

Por HTTP:

```bash
curl -X POST http://127.0.0.1:5060/auto_exit \
  -H 'Content-Type: application/json' \
  -d '{"take_profit_pct":15,"stop_loss_pct":20,"dry_run":true}'
```

La funcion calcula P/L% con los campos que entregue TOS. Si una posicion no
tiene datos suficientes, se marca como `skip` y no se vende a ciegas.

## Posiciones y resumen del dia

Directo:

```python
positions = ws.open_option_positions()
summary = ws.day_summary()
```

Cliente HTTP:

```python
from tos.client import broker_positions, day_summary

positions = broker_positions()
summary = day_summary()
```

HTTP:

```bash
curl http://127.0.0.1:5060/broker_positions
curl http://127.0.0.1:5060/day_summary
```

`day_summary()` suma P/L del dia si TOS lo devuelve. Si TOS no devuelve ese
campo, conserva la posicion con raw broker data para inspeccion.

## Mapa rapido de APIs

| v1 | v2 |
|---|---|
| `TosTradeManager.submit_alert(...)` | `WsRuntime.buy_option(...)` |
| `TosTradeManager.request_close(...)` | `WsRuntime.sell_option(...)` |
| `manager.check_prices()` | `WsRuntime.auto_exit_percent(...)` |
| `tos.client.send_alert(...)` | `tos.client.buy_option(...)` |
| `tos.client.send_close(...)` | `tos.client.sell_option(...)` |
| `tos.client.positions(...)` | `tos.client.broker_positions(...)` o `tos.client.day_summary(...)` |
| `POST /alert` | `POST /buy` |
| `POST /close` | `POST /sell` |
| `GET /positions` gestionadas | `GET /broker_positions` |

## Que eliminar del host

- Dependencia obligatoria de `tos.hooks` para entradas nuevas.
- Seleccion automatica de contrato dentro de `tos-exec`.
- Logica que espera un `position_id` gestionado por `TosTradeManager`.
- Timers que llaman `manager.check_prices()` para la ruta nueva.

## Que mantener en el host

- La UI/webview de thinkorswim.
- Credenciales fuera del repo.
- Auto-login del webview, inyectado como callback.
- Decision de estrategia: cuando entrar, contrato a comprar, cantidad y cuando
  ejecutar `auto_exit_percent(...)`.
- Manejo de MFA si Schwab lo solicita.

## Checklist para agentes de IA

1. Buscar usos antiguos:

   ```bash
   rg "TosTradeManager|send_alert|send_close|POST /alert|/alert|/close|tos.hooks|check_prices"
   ```

2. Reemplazar compras por `buy_option(...)`.
3. Reemplazar cierres por `sell_option(...)`.
4. Reemplazar gestion automatica por `auto_exit_percent(...)`.
5. Reemplazar vista de posiciones por `broker_positions()` o `day_summary()`.
6. Configurar `WsRuntime(auto_login=...)`.
7. Confirmar que no se bloquea el hilo de UI durante auto-login.
8. Compilar:

   ```bash
   python -m compileall tos
   ```

9. Probar primero con `submit=False` o `dry_run=True`.
10. Probar en Paper Money, nunca Live.

## Compatibilidad

`tos.alerts`, `TosTradeManager`, `TosAlert`, `tos.hooks`, `/alert` y `/close`
siguen existiendo para no romper proyectos anteriores. Para v2, tratalos como
compatibilidad legada. No construyas nuevas integraciones encima de ellos.
