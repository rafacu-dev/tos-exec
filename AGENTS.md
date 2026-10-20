# Guía para agentes (Claude Code, Codex) — tos-exec

Léela completa antes de tocar este paquete. Resume cómo está hecho, cómo se
prueba y qué hacer cuando la web de thinkorswim (Schwab) cambia.

Documentos relacionados (leer según la tarea):

| Archivo | Para qué |
|---|---|
| `tos/WS_PROTOCOL.md` | Protocolo WebSocket: handshake, login, `place_order`, OCO, `cancel_order`, cómo recapturar. |
| `tos/WEB_DOM_REFERENCE.md` | Estructura HTML de la web (selectores `data-testid`) con fecha, y scripts para inventariarla. |
| `tos/protocol_fixtures/*.json` | Frames reales capturados: la verdad de referencia del protocolo. |
| `AI_CHANGE_GUIDE.md` | Qué API es pública y qué no se debe hacer. |
| `README.md`, `CHANGELOG.md`, `MIGRATION_V1_TO_V2.md` | Uso, historial, migraciones. |

## 1. Qué es

Librería Python (paquete `tos`) que opera en **thinkorswim web** a través del
gateway WebSocket privado de Schwab, sin hacer clics en la página:

```
login (navegador, solo para el token) → token en ~/.tos-exec/ws_token.json
        ↓
WsRuntime (hilo asyncio) → TosWsClient → wss://papermoney-services.schwab.com/Services/WsJson
        ↑
TosHttpServer (POST /buy /sell /bracket /cancel_order …)  ←  bots / tos.client
```

| Módulo | Rol |
|---|---|
| `tos/ws_client.py` | Cliente WS de bajo nivel: handshake, login, `accounts`, `positions`, `place_order` (SINGLE y bracket OCO), `cancel_order`, parsers de respuestas. **Aquí se construyen los frames.** |
| `tos/ws_runtime.py` | API pública síncrona (`buy_option`, `sell_option`, `place_bracket`, `bracket_option`, `cancel_order`, `auto_exit_percent`, posiciones). Resuelve token, cuenta y enrutado Live/Paper. Mantiene el safeguard anti-short. |
| `tos/server.py` | API HTTP local. Rutas de orden protegidas con `X-TOS-Order-Token`. |
| `tos/client.py` | Helpers HTTP para quien llama al servidor. |
| `tos/headless_login.py`, `tos/servicio.py` | Login con Chromium headless (Playwright) para servidor/Docker; MFA por `POST /mfa`. |
| `tos/ws_token_capture.py`, `tos/cdp_capture.py`, `tos/ws_capture.py` | Capturan el token y el tráfico WS por CDP. |
| `tos/tos_scripts.py`, `tos/automation.py` | JS de automatización DOM (login, Live/Paper, legado de órdenes por clics). Dependen de `WEB_DOM_REFERENCE.md`. |
| `tos/alerts.py`, `tos/hooks.py` | Legado; solo corregir bugs. |
| `Dockerfile` | Imagen de servidor: `python -m tos.servicio` en el puerto 5060. |

## 2. Reglas que no se rompen

1. **Todas las pruebas que envían órdenes se hacen en paperMoney.** Verificar
   antes de enviar: socket `papermoney-services…`, aviso "These are simulated
   values", cuenta tipo `D-…` (en la UI se ve como `margin *XXX`). Nunca usar
   `LIVE_WS` ni el toggle Live si el usuario no lo pidió de forma explícita para
   esa acción.
2. **Cancelar toda orden de prueba** al terminar (`cancel_order` con el id de la
   entrada, o el botón Cancel) y confirmar que "Working" queda vacío.
3. **Nunca** guardar en el repo, logs, fixtures ni mensajes: `authCode`,
   `token`, `refreshToken`, `session`, contraseñas, ni el código de cuenta. En
   fixtures, reemplazar la cuenta por `<ACCOUNT>`.
4. No saltar ni automatizar el MFA. No reintentar logins en bucle (el límite de
   3 fallos de `tos.servicio` protege la cuenta).
5. No meter lógica de estrategia ni selección de contratos en esta librería.
6. Un error después de enviar SUBMIT **no** es un rechazo: la orden pudo
   entrar. Mantener los estados `unknown_*` / `submission_may_have_occurred` y
   no reintentar órdenes automáticamente.
7. Ventas: mantener el safeguard anti-short. Los brackets solo aceptan entradas
   de compra (`quantity > 0`).

## 3. Cómo correr los tests

Los tests de este paquete viven en **dos sitios**. El paquete está instalado en
modo editable (`pip install -e`), así que `import tos` resuelve a esta carpeta.

```bash
# Desde la raíz de LocalQuantAI: tests del executor (rápidos, sin red)
cd ~/Desktop/LocalQuantAI
python3 -m pytest -q tests/test_tos_*.py
```

| Test | Qué cubre |
|---|---|
| `tests/test_tos_bracket_order.py` | **Frames OCO contra el fixture capturado**, ids por pata, validación de precios, CONFIRM→SUBMIT, `/bracket`. Es el detector de cambios del protocolo. |
| `tests/test_tos_submit_classification.py` | Clasificación de respuestas a SUBMIT, parseo de posiciones, símbolos de opción. |
| `tests/test_tos_executor_guard.py` | Token de órdenes del servidor HTTP, payloads inválidos, errores después de SUBMIT. |
| `tests/test_tos_credentials.py` | Lectura de credenciales del Llavero de macOS para el auto-login. |
| `tests/test_tos_paper_supervisor.py` | Supervisor de paper del proyecto padre. |

```bash
# Desde tos-exec: login headless contra un thinkorswim simulado (~30 s,
# necesita playwright + chromium; si no están, se omite)
cd ~/Desktop/LocalQuantAI/other_investigation/tos-exec
python3 -m pytest -q tests

# Comprobación de sintaxis del checklist
python3 -m compileall -q tos
```

`pytest` desde la raíz excluye `other_investigation/` (ver `pytest.ini`), por eso
los tests de tos-exec se corren por ruta. La suite completa (`python3 -m pytest
-q` en la raíz) tiene fallos conocidos ajenos a tos-exec cuando
`tests/test_strategy_evidence_manifest.py` tiene congelada otra versión de
tos-exec, o cuando `nucleo/paper/tos_runtime_config.json` no está en
`armed:false, dry_run:true`. No "arreglarlos" cambiando el executor.

Antes de dar por buena una tarea: los tests `test_tos_*.py` en verde, y si se
tocó el protocolo, además la prueba en vivo en paper de la sección 6.

## 4. Inspeccionar la web (DOM y WebSocket)

Herramientas, de más cómoda a menos:

- **Claude in Chrome** (`mcp__claude-in-chrome__*`): `javascript_tool` para
  ejecutar JS en la página, `read_page`/`find` para el árbol accesible,
  `computer` para clics y capturas. El usuario inicia sesión él mismo; el
  agente no escribe credenciales.
- **DevTools** del navegador (consola + pestaña Network → WS).
- **CDP** desde Python: `tos/cdp_capture.py` guarda el tráfico WS en
  `tos/ws_cdp.jsonl` (en `.gitignore`; contiene secretos).

### 4.1 Capturar frames WS sin recargar

Un hook en el prototipo captura también el socket ya abierto. **No recargar la
página después de instalarlo** (se pierde).

```js
if (!window.__wsHooked) {
  window.__wsHooked = true; window.__wsLog = [];
  const push = (dir, ws, data) => window.__wsLog.push({t: Date.now(), dir, url: ws.url,
    data: typeof data === 'string' ? data : '[binary]'});
  const send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (d) { push('out', this, d); return send.call(this, d); };
  // entrantes: el objeto MessageEvent conoce su socket (target)
  const dataDesc = Object.getOwnPropertyDescriptor(MessageEvent.prototype, 'data');
  Object.defineProperty(MessageEvent.prototype, 'data', { configurable: true, get() {
    const v = dataDesc.get.call(this), t = this.target;
    if (t instanceof WebSocket) {
      if (t.url.includes('papermoney')) window.__paperWs = t;   // socket de paper
      if (!this.__seen) { this.__seen = true; push('in', t, v); }
    }
    return v;
  }});
}
```

Extraer lo relevante (los servicios de interés son `place_order`, `cancel_order`, `order_events`):

```js
window.__wsLog.filter(x => /place_order|cancel_order/.test(x.data)).map(x => x.dir + ' ' + x.data)
```

Al copiar resultados a docs/fixtures, reemplazar cuenta y tokens.

### 4.2 Leer la estructura HTML

Seguir `tos/WEB_DOM_REFERENCE.md`: usar `data-testid`/`aria-label`, nunca
clases CSS. Ese archivo trae dos scripts: uno lista los `data-testid` del ticket
y otro comprueba que los selectores críticos existen (0 = la web cambió).

Truco útil: los `data-testid` del menú Advanced Orders llevan el nombre exacto
del `marker` del WebSocket (`advanced-order-dropdown:FIRST_TRIGGERS_OCO`). Si
Schwab añade o renombra tipos de orden, mirar ahí primero.

## 5. Procedimiento cuando la web de thinkorswim cambia

Síntomas típicos: órdenes que vuelven sin `orderId`, `login WS rechazado`,
`positions_payload_unrecognized`, el handshake devuelve otro `build` (el actual
de referencia es `27.2640.4-Q3`), o el login headless/Live-Paper deja de
encontrar elementos (`PAPER_NOT_FOUND`, `REVIEW_BUTTON_NOT_FOUND`).

1. **Ubicar la capa rota.**
   - Falla el envío o la respuesta de órdenes → protocolo WS (`ws_client.py`).
   - Falla el login, el toggle Live/Paper o los clics → DOM (`tos_scripts.py`).
2. **Tomar el punto de referencia actual.** En paperMoney, con el usuario ya
   logueado: instalar el hook de 4.1, hacer la operación a mano en la UI
   (Review → Send) y extraer los frames. Para el DOM, correr los scripts de
   `WEB_DOM_REFERENCE.md`.
3. **Comparar** con lo documentado:
   - Protocolo: `tos/protocol_fixtures/bracket_oco_2026-10-20.json` y
     `WS_PROTOCOL.md` (claves de `params`, `marker`, orden de `orders[]`,
     `limitPrice`/`stopPrice`, `requestType`, `tag`, rutas `/orders/N/orderId`).
   - DOM: tablas de `WEB_DOM_REFERENCE.md`.
4. **Guardar la nueva referencia**: fixture nuevo con fecha
   (`bracket_oco_AAAA-MM-DD.json`, cuenta → `<ACCOUNT>`), sin borrar el viejo, y
   apuntar `FIXTURE` en `tests/test_tos_bracket_order.py` al nuevo. Al correr el
   test, el diff dice exactamente qué campo cambió.
5. **Ajustar el código** (`TosWsClient._bracket_params`, `_order_params`,
   `submit_result`, `bracket_order_ids`, o los selectores de `tos_scripts.py`).
6. **Tests en verde** (sección 3).
7. **Prueba en vivo en paper** (sección 6) y cancelar lo enviado.
8. **Documentar**: sección nueva con fecha en `WS_PROTOCOL.md` o actualizar
   `WEB_DOM_REFERENCE.md` (fecha de la instantánea), entrada en `CHANGELOG.md`.

## 6. Prueba en vivo en paperMoney

### 6.1 Por la API (como en producción)
Requiere un token vigente en `~/.tos-exec/ws_token.json` (lo deja el login del
host o `tos.servicio`).

```python
from tos.ws_runtime import WsRuntime, ProveedorAlmacen
ws = WsRuntime(token_provider=ProveedorAlmacen()); ws.start()
r = ws.place_bracket("F", 1, entry_type="LIMIT", entry_limit=10.00,
                     take_profit=14.00, stop_loss=9.00, submit=False)   # solo review
r = ws.place_bracket("F", 1, entry_type="LIMIT", entry_limit=10.00,
                     take_profit=14.00, stop_loss=9.00, submit=True)
ws.cancel_order(r["order_ids"]["entry"])
```

Usar un precio de entrada lejos del mercado (o fuera de horario) para que la
orden quede en Working y se pueda cancelar sin llenarse.

### 6.2 Por el socket del navegador (sin token en disco)
Así se verificó el bracket el 2026-10-20: generar el frame con el código y
mandarlo por el socket autenticado de la página.

```bash
cd ~/Desktop/LocalQuantAI && python3 -c "
import json; from tos.ws_client import TosWsClient
p = TosWsClient._bracket_params('CONFIRM', '<ACCOUNT>', 'F', 1, 14, 9, 'LIMIT', 10, 'DAY')
print(json.dumps({'payload':[{'header':{'service':'place_order','id':'update-draft-order-F','ver':0},'params':p}]}))"
```

En la página (con el hook de 4.1 y `window.__paperWs` ya capturado): poner la
cuenta real de paper en lugar de `<ACCOUNT>`, `window.__paperWs.send(frame)`,
leer la respuesta en `window.__wsLog`. Repetir con `SUBMIT` y luego
`cancel_order`. Esperado: CONFIRM con filas `TRG BY #1 OCO`, SUBMIT con
`/orders/0|1|2/orderId`, cancel → notificaciones `CANCELED` de las 3 patas.

## 7. Docker / servidor

```bash
cd ~/Desktop/LocalQuantAI/other_investigation/tos-exec
docker build -t tos-exec .
docker run -p 5060:5060 -v tos-data:/data \
  -e TOS_USERNAME=... -e TOS_PASSWORD=... -e TOS_ORDER_TOKEN=... tos-exec
```

- Los secretos solo por variables de entorno; `/data` persiste token y perfil.
- Flujo: `GET /login_status` → si pide MFA, `POST /mfa {"code": "..."}`.
- Bracket por HTTP:

```bash
curl -s -X POST localhost:5060/bracket -H 'Content-Type: application/json' \
  -H "X-TOS-Order-Token: $TOS_ORDER_TOKEN" \
  -d '{"symbol":"F","quantity":1,"entry_type":"LIMIT","entry_limit":10,
       "take_profit":14,"stop_loss":9,"submit":false}'
curl -s -X POST localhost:5060/cancel_order -H 'Content-Type: application/json' \
  -H "X-TOS-Order-Token: $TOS_ORDER_TOKEN" -d '{"order_id": 123}'
```

Para opciones, añadir `expiration`, `direction` y `strike` al cuerpo de `/bracket`.

## 8. Checklist antes de commit

- `python3 -m pytest -q tests/test_tos_*.py` (raíz de LocalQuantAI) y
  `python3 -m pytest -q tests` (tos-exec) en verde.
- `python3 -m compileall -q tos`.
- Misma versión en `pyproject.toml`, `tos/__init__.py` y `README.md`; entrada en `CHANGELOG.md`.
- Si cambió el protocolo o el DOM: fixture/documento con fecha actualizado.
- `git diff` sin tokens, authCode, cuenta ni archivos de `~/.tos-exec/`.
- Órdenes de prueba en paper canceladas.
