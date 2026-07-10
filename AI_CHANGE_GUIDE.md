# Guia de cambios para agentes de IA

Este repo esta en la linea **v1.2.x**.

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

No introducir logica de estrategia ni senales de entrada en esta API.

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
