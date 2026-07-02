# tos-exec

Libreria independiente para ejecutar opciones en **thinkorswim web Paper Money**:
ordenes por WebSocket (gateway WsJson de Schwab), executor standalone con webview
(login + captura de token) y gestion de salidas por la prima del contrato.

Es la unica copia a mantener: cada proyecto la instala y le inyecta sus propias
fuentes de datos via `tos.hooks`.

## Instalacion

```bash
# nucleo (cliente HTTP + cliente WS, sin Qt):
pip install -e /ruta/a/tos-exec

# con la ventana del executor (webview Qt + auto-login):
pip install -e '/ruta/a/tos-exec[app]'
```

El paquete importable se llama `tos`, igual que el modulo original de
LocalQuantAI, asi que el codigo existente no cambia.

## Arranque del executor

```bash
python3 -m tos.executor_app --port 5060     # o: tos-executor --port 5060
```

La ventana abre `trade.thinkorswim.com` y expone un receptor local:

- `POST /alert`: recibe una alerta y manda la orden de entrada.
- `GET /positions`: lista posiciones gestionadas.
- `POST /close`: solicita cierre por id/alert_id/símbolo.
- `POST /ws_order`: orden directa por WebSocket (prueba).
- `GET /ws_status`: estado del token WS.

Desde otro proceso se usa `tos.client`:

```python
from tos.client import send_alert, send_close, positions
send_alert({"symbol": "SPY", "direction": "CALL", ...})
```

## Datos (token, posiciones, perfil del webview)

Todo el estado vivo va en `~/.tos-exec/` (cambiable con `TOS_DATA_DIR`):
`ws_token.json`, `managed_positions.json`, `webdata/` (sesion del navegador).
Nada de esto vive dentro del paquete ni debe ir a un repo.

## Hooks: integracion con cada proyecto

La libreria no sabe de donde salen los precios ni como elegir contratos.
Cada proyecto lo inyecta (ver `tos/hooks.py` para el contrato exacto):

```python
# mi_proyecto/tos_hooks.py
from tos import hooks

hooks.configure(
    price=mi_precio,                  # (symbol) -> (precio, ts_iso)
    select_contract=mi_selector,      # (symbol, direction, strategy=, budget=) -> dict
    option_premium=mi_prima,          # (symbol, expiration, direction, strike) -> float|None
)
```

Para el executor standalone se indica el modulo por variable de entorno y se
lanza desde el directorio del proyecto:

```bash
cd /ruta/al/proyecto
TOS_HOOKS_MODULE=tos_hooks python3 -m tos.executor_app --port 5060
```

Sin hooks la libreria sigue operando, pero las alertas deben traer
`entry_price`, `strike` y `expiration`, y no hay gestion de salida por prima.

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

`tos.alerts.TosTradeManager` consulta la prima cada ciclo (hook
`option_premium`) y gestiona por P&L del contrato:

- target **+10%** sobre la prima → activa trailing,
- trailing: cierra si el extra sobre el target retrocede **10%** desde su pico,
- stop duro **-20%**.

Ademas reconcilia contra la cuenta TOS (via WS): detecta cierres manuales y
verifica que los cierres pedidos de verdad ejecutaron.

## Seguridad

- El script de TOS bloquea el envio si detecta cuenta Live o si no confirma
  Paper Money.
- Safeguard anti-short: al cerrar solo vende lo que la cuenta tiene del
  contrato.
- Credenciales en macOS Keychain (`tos.credentials`); token de sesion (~8h)
  capturado del webview via CDP.

Ver `tos/WS_PROTOCOL.md` para el detalle del protocolo WsJson.
