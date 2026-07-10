# Changelog

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
