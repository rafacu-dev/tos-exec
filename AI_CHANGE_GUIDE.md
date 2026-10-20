# Guia de cambios para agentes de IA

Este repo esta en la linea **v1.3.x**.

## Regla principal

La API recomendada de v1.2 compra/vende opciones en thinkorswim web Paper Money
por WebSocket y expone utilidades explicitas de gestion:

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

Desde v1.4 tambien:

- `WsRuntime.place_bracket(...)` / `bracket_option(...)`: entrada de compra con
  take profit y stop loss en una orden `FIRST_TRIGGERS_OCO` de TOS.
- `WsRuntime.cancel_order(...)`, `POST /bracket`, `POST /cancel_order`,
  `tos.client.place_bracket(...)`, `tos.client.cancel_order(...)`.

No introducir logica de estrategia ni senales de entrada en esta API.

## Si TOS cambia el protocolo

El formato del gateway no es publico. Ante ordenes que fallan o un `build`
nuevo en el handshake: recapturar segun "Como recapturar y detectar cambios" en
`tos/WS_PROTOCOL.md`, guardar un fixture nuevo con fecha en
`tos/protocol_fixtures/` y correr `tests/test_tos_bracket_order.py` (raiz de
LocalQuantAI) contra el: el test muestra que campo cambio. Ajustar
`TosWsClient._bracket_params` y documentar el cambio en WS_PROTOCOL.md.

## No hacer

- No agregar trailing complejo ni seleccion automatica de entradas/contratos.
- La salida automatica permitida es `auto_exit_percent(...)`: cierre por X% de
  ganancia/perdida sobre posiciones abiertas.
- No seleccionar contratos automaticamente dentro del runtime WebSocket.
- No convertir alertas en posiciones gestionadas en el flujo recomendado.
- No guardar tokens, passwords, datos de cuenta ni estado vivo dentro del repo.
- No hacer que el nucleo WebSocket dependa de PySide6/Qt.
- No enviar ventas en descubierto al cerrar: mantener el safeguard anti-short.

## Auto-login

Si el token de TOS vence, los metodos de orden deben disparar auto-login antes
de fallar. Como la libreria no posee la UI, el auto-login debe entrar por
callback:

```python
ws = WsRuntime(auto_login=mi_callback_de_login)
```

El callback pertenece al host y debe iniciar el login en el webview. La captura
CDP guarda el token renovado en `~/.tos-exec/ws_token.json`; despues el runtime
continua la orden.

En hosts Qt, no bloquear el hilo de UI esperando el token. Las llamadas de orden
deben venir de un worker/request thread, y el callback debe encolar el login al
hilo de UI.

Si Schwab pide MFA, el usuario lo completa en el webview. No intentar saltar MFA.

## Login en servidor

`tos.headless_login` hace el login con un Chromium headless propio y credenciales
de `TOS_USERNAME`/`TOS_PASSWORD` (secretos del entorno, nunca en el repo). El
MFA lo aporta el usuario por `POST /mfa`; no automatizar ni saltar su obtencion.
No reintentar logins fallidos en bucle: el limite de 3 fallos en `tos.servicio`
protege la cuenta de bloqueos.

## Compatibilidad legada

`tos.alerts`, `TosTradeManager` y `tos.hooks` existen por compatibilidad con
instalaciones anteriores. No son la direccion de producto de v1.2. Si se tocan,
hacerlo solo para corregir bugs o preservar compatibilidad.

## Checklist antes de subir

- `python -m compileall tos`
- Confirmar que `pyproject.toml`, `tos/__init__.py` y README tengan la misma version.
- Confirmar que README siga presentando compra/venta, posiciones, resumen y
  `auto_exit_percent()` como la API recomendada.
- Confirmar que `MIGRATION_V1_TO_V2.md` siga actualizado si cambia una API publica.
- Confirmar que no se agregaron archivos con tokens o datos de `~/.tos-exec/`.
