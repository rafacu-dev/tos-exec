# Changelog

## 1.4.0

- Ordenes bracket (entrada + take profit + stop loss) en una sola orden
  `FIRST_TRIGGERS_OCO` de TOS: `WsRuntime.place_bracket()`,
  `WsRuntime.bracket_option()`, `POST /bracket`, `tos.client.place_bracket()`.
  Solo entradas de compra; valida `stop_loss < entry_limit < take_profit`
  antes de conectar.
- Cancelacion por id: `WsRuntime.cancel_order()`, `POST /cancel_order`,
  `tos.client.cancel_order()`. Con la entrada de un bracket cancela las 3 patas.
- Protocolo OCO documentado en `tos/WS_PROTOCOL.md` con la captura real en
  `tos/protocol_fixtures/bracket_oco_2026-10-20.json` y el procedimiento para
  recapturar y detectar cambios del broker.

## 1.3.0

- Login en un servidor sin pantalla: `tos.headless_login` abre un Chromium
  headless (Playwright, extra `[server]`), usa `TOS_USERNAME`/`TOS_PASSWORD`
  del entorno, deja la sesion en Paper Money y captura el token por CDP.
- `ProveedorNavegador`: proveedor de token para `WsRuntime` que renueva solo.
- MFA por HTTP: `GET /login_status`, `POST /login`, `POST /mfa`,
  `GET /login_screenshot` (las tres ultimas con `X-TOS-Order-Token`).
- `python -m tos.servicio`: login + API HTTP en un proceso; renueva el token al
  vencer y se detiene tras 3 fallos seguidos para no bloquear la cuenta.
- Corrige la captura del token: si TOS reenvia el mismo token tras recargar y el
  guardado ya vencio, se vuelve a guardar (antes la sesion nunca se renovaba en
  un proceso de larga vida).

## 1.2.0

- Define la API recomendada como metodos explicitos de compra/venta:
  `WsRuntime.buy_option()` y `WsRuntime.sell_option()`.
- Agrega endpoints HTTP directos `POST /buy` y `POST /sell`.
- Agrega helpers HTTP `tos.client.buy_option()` y `tos.client.sell_option()`.
- Agrega `WsRuntime.auto_exit_percent()` y `POST /auto_exit` para salida
  automatica por porcentaje configurable.
- Agrega `WsRuntime.open_option_positions()`, `WsRuntime.day_summary()`,
  `GET /broker_positions` y `GET /day_summary`.
- Agrega auto-login inyectable en `WsRuntime`: si el token vence, el runtime
  puede llamar un callback del host y esperar a que CDP capture el token nuevo.
- Documenta que la seleccion de estrategia/entradas queda fuera del flujo
  recomendado de v1.2.
- Mantiene el gestor anterior (`TosTradeManager`) como compatibilidad legada.
