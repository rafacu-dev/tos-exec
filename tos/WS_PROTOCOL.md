# Protocolo WebSocket de thinkorswim (Schwab) — reversado

Capturado vía CDP (`tos/cdp_capture.py`) desde el webview autenticado, jun 2026.
**Es JSON puro** (opcode texto) — viable construir un cliente en Python.

> ⚠️ La captura cruda (`tos/ws_cdp.jsonl`) contiene `authCode` y código de cuenta.
> Está en `.gitignore`. No commitear ni compartir.

## Endpoints
- **Paper:** `wss://papermoney-services.schwab.com/Services/WsJson`
- Live: `wss://thinkorswim-services.schwab.com/Services/WsJson`
- (ignorar `wss://*.glance.net/visitorws` — widget de soporte de terceros)

## Handshake (primer frame enviado)
```json
{"ver": "27.*.*", "fmt": "json-patches-structured", "heartbeat": "2s"}
```
Luego heartbeat cada 2s.

## Envoltura de mensajes
Cada request/response va como:
```json
{"payload": [{"header": {"service": "<svc>", "id": "<id>", "ver": <n>}, "params": {...}}]}
```
Las respuestas traen `header.type` (`snapshot`/`patch`) y `body`.

## Autenticación
```json
{"payload":[{"header":{"service":"login/schwab","id":"login/schwab","ver":0},
  "params":{"clientId":"TOSWeb","tag":"TOSWeb",
            "redirectUri":"https://trade.thinkorswim.com/oauth",
            "authCode":"<OAUTH_AUTH_CODE>"}}]}
```
- `authCode` = código OAuth de la redirección de Schwab tras el login (usuario/clave/2FA).
- **Pendiente de verificar:** si el authCode es de un solo uso (probable). Si lo es,
  cada conexión WS nueva necesita un authCode fresco del flujo OAuth del webview.

## Cuenta
Servicio `accounts` → `body.items[]` con `code` (p.ej. `XXXXXXXXSCHW`), `tradeable`,
`optionApprovalLevel`, etc. Ese `code` es el `accountCode` de las órdenes.

## Colocar orden — servicio `place_order`
Flujo de 3 frames (review → submit):
1. `action:CONFIRM` + `requestType:INIT_OPTION`  → crea borrador (review)
2. `action:CONFIRM` + `requestType:EDIT_ORDER`   → confirma (añade tif/tag)
3. `action:SUBMIT`  + `requestType:EDIT_ORDER`   → envía

```json
{"header":{"service":"place_order","id":"update-draft-order-<SYM>","ver":<n>},
 "params":{
   "action":"SUBMIT",            // CONFIRM | SUBMIT
   "accountCode":"<ACCT>",
   "marker":"SINGLE",
   "orders":[{
     "requestType":"EDIT_ORDER", // INIT_OPTION en el primer CONFIRM
     "orderType":"MARKET",       // o LIMIT
     "limitPrice":1.16,
     "tif":"DAY",
     "tag":"TOSWeb",
     "legs":[{"symbol":".GOOG260626P335","quantity":1}]  // qty>0 compra, qty<0 venta
   }]
 }}
```

### Símbolo de opción
`.{ROOT}{YYMMDD}{C|P}{STRIKE}` → `.GOOG260626P335` = GOOG, 2026-06-26, Put, strike 335.

### Compra vs venta
Solo el **signo de `quantity`**: `+N` compra, `-N` venta/cierre.

## Otros servicios útiles vistos
`quotes`, `quotes/options`, `option_chain/get`, `optionSeries`, `optionStrikes`,
`positions`, `statement`, `order_events` (estado de órdenes), `instruments`,
`market_hours`, `alerts/*`.

## Arquitectura recomendada para el pivot
**B2 — webview para login, Python WS para operar:**
1. El webview hace el login OAuth (maneja 2FA) — se queda solo para autenticar.
2. Se extrae el `authCode` durante el login (vía CDP, igual que esta captura).
3. Python abre su propio WS a `papermoney-services` y autentica con ese authCode.
4. Lee `accounts` → accountCode; coloca órdenes con `place_order` (CONFIRM→SUBMIT).
5. Suscribe `order_events`/`positions` para el estado.

Esto elimina TODO el DOM-automation (clicks, REVIEW_BUTTON_NOT_FOUND). Riesgo abierto:
reuso del authCode (verificar con una prueba de conexión Python).

## Órdenes compuestas: stop loss + take profit (OCO) — capturado 2026-10-20

Capturado en thinkorswim web, **paperMoney**, build `27.2640.4-Q3`, con un hook
sobre `WebSocket.prototype.send` (ver "Cómo recapturar" abajo). Frames reales en
`tos/protocol_fixtures/bracket_oco_2026-10-20.json` (cuenta reemplazada por
`<ACCOUNT>`). El test `tests/test_tos_bracket_order.py` (raíz de LocalQuantAI)
compara lo que construye `ws_client` contra ese fixture.

### Tipos (`params.marker`)
En el ticket web, menú *Advanced Orders*:

| UI                | `marker`              | Estado                       |
|-------------------|-----------------------|------------------------------|
| (orden simple)    | `SINGLE`              | verificado                   |
| **1st trgs OCO**  | `FIRST_TRIGGERS_OCO`  | **verificado** (bracket)     |
| OCO               | probable `OCO`        | sin capturar                 |
| 1st trgs Seq      | probable `FIRST_TRIGGERS_SEQ` | sin capturar         |
| 1st trgs All      | probable `FIRST_TRIGGERS_ALL` | sin capturar         |

### Bracket = `FIRST_TRIGGERS_OCO`
Una sola petición `place_order` con **3 órdenes, en este orden**:
1. `orders[0]` entrada (dispara a las otras dos cuando se llena)
2. `orders[1]` take profit — `LIMIT` con `limitPrice`
3. `orders[2]` stop loss — `STOP` con `stopPrice`

Las salidas llevan la cantidad con **signo contrario** a la entrada. `1` y `2`
son OCO entre sí: al llenarse una, TOS cancela la otra.

```json
{"payload":[{"header":{"service":"place_order","id":"update-draft-order-F","ver":0},
 "params":{"accountCode":"<ACCOUNT>","action":"SUBMIT","marker":"FIRST_TRIGGERS_OCO",
  "orders":[
   {"tif":"DAY","orderType":"LIMIT","limitPrice":10,"requestType":"EDIT_ORDER","legs":[{"symbol":"F","quantity":1}],"tag":"TOSWeb"},
   {"tif":"DAY","orderType":"LIMIT","limitPrice":14,"requestType":"EDIT_ORDER","legs":[{"symbol":"F","quantity":-1}],"tag":"TOSWeb"},
   {"tif":"DAY","orderType":"STOP","stopPrice":9,"requestType":"EDIT_ORDER","legs":[{"symbol":"F","quantity":-1}],"tag":"TOSWeb"}]}}]}
```

Flujo observado en la web: **CONFIRM** (botón Review, mismo cuerpo con
`action:"CONFIRM"`) → **SUBMIT**. Todas las patas con `requestType:"EDIT_ORDER"`;
no hubo frame `INIT_OPTION` porque el borrador ya existía en el ticket. Este
cliente manda igualmente `CONFIRM` antes de `SUBMIT`.

Respuesta a SUBMIT: `header.type:"patch"` con un `orderId` por pata:
`/orders/0/orderId` (entrada), `/orders/1/orderId`, `/orders/2/orderId`, y un
texto `confirmation` por pata, p.ej. `"TOSWeb SELL -1 F STP 9.00 TRG BY OCO"`.
Las notificaciones de la web mostraron además un id del grupo OCO
(`#5413682773`) distinto de los de las patas.

**Verificado con el código** (2026-10-20, paperMoney): los frames que genera
`TosWsClient._bracket_params` se mandaron por el socket autenticado; CONFIRM
devolvió `#2 ... TRG BY #1 OCO` / `#3 ... TRG BY #1 OCO`, SUBMIT devolvió los 3
`orderId` (entrada 5413686540, TP …542, SL …543; grupo OCO …541) y
`cancel_order` con la entrada canceló las 3 patas.

### Cancelar
```json
{"payload":[{"header":{"service":"cancel_order","id":"cancel-<orderId>","ver":0},
 "params":{"orderId":<orderId de la ENTRADA>}}]}
```
Cancelar la entrada cancela también las dos salidas pendientes (verificado).

### Pendiente de verificar
- Bracket sobre **opciones** (`legs[].symbol` tipo `.SPY260715P755`): se espera
  el mismo formato, sin capturar todavía. La prueba fue con acciones (F).
- Entrada `MARKET` con bracket: la UI lo permite, no capturado.
- Valores de `tif` distintos de `DAY` (p.ej. `GTC`).

## Cómo recapturar y detectar cambios

Cuando una orden empiece a fallar o se actualice la web (`build` del handshake
distinto de `27.2640.4-Q3`):

1. Abrir thinkorswim web, iniciar sesión y pasar a **paperMoney**.
2. En la consola de DevTools (o `javascript_tool` de Claude in Chrome), **sin
   recargar la página después**, instalar el hook:
   ```js
   window.__wsLog = [];
   const _send = WebSocket.prototype.send;
   WebSocket.prototype.send = function (d) {
     window.__wsLog.push({t: Date.now(), url: this.url, data: String(d)});
     return _send.call(this, d);
   };
   ```
   (Es en el prototipo, así que captura también el socket ya abierto.)
3. Armar la orden en el ticket (Advanced Orders → 1st trgs OCO), Review, Send.
4. Extraer: `window.__wsLog.filter(x => x.data.includes('place_order')).map(x => x.data)`
5. Comparar contra `tos/protocol_fixtures/bracket_oco_2026-10-20.json`:
   claves de `params`, `marker`, orden de `orders[]`, nombres
   `limitPrice`/`stopPrice`, `requestType`, `tag`. Si algo cambió, guardar un
   fixture nuevo con fecha, ajustar `ws_client._bracket_params` y el test.
6. Cancelar la orden de prueba (`cancel_order` o el botón *Cancel*).

Nunca guardar en el repo el `authCode`, `token`, `refreshToken` ni el código de
cuenta que aparecen en la captura.
