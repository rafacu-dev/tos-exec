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
