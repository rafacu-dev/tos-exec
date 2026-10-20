# Estructura HTML de thinkorswim web — punto de referencia

Instantánea tomada el **2026-10-20** en `https://trade.thinkorswim.com/trade?symbol=F`,
cuenta **paperMoney**, build del gateway `27.2640.4-Q3`. Sirve para comparar:
si un selector de aquí ya no existe, la web cambió y hay que actualizar
`tos/tos_scripts.py` (automatización DOM) o este documento.

El protocolo WebSocket (lo que de verdad envía las órdenes) está en
`WS_PROTOCOL.md`. El DOM solo importa para: login, cambiar Live/Paper, la
automatización DOM legada y **descubrir** valores nuevos del protocolo (los
`data-testid` del ticket suelen llevar el mismo nombre que el `marker` del WS).

## Reglas para leer el DOM

- **Usar `data-testid` y `aria-label`.** Las clases CSS son de styled-components
  (`sc-eldPxv GUpvu`) y cambian en cada build: nunca usarlas como selector.
- Varios `data-testid` llevan índices de pata: `limit-price-1-input`,
  `buy-sell-toggle-2-0-Sell`, `remove-leg-button-trade#1-leg#0`. El primer número
  es la pata del ticket (0 = entrada, 1 = take profit, 2 = stop loss).
- Filtrar siempre por visible (`el.offsetParent !== null`): hay elementos
  duplicados ocultos.
- `tos_scripts.py` usa `deepQueryAll` porque partes de la página de login de
  Schwab usan shadow DOM (`sdps-button`). En `trade.thinkorswim.com` el ticket
  no usa shadow DOM.

## Barra superior y navegación

| Elemento | Selector |
|---|---|
| Buscador de símbolos | `#navigation-symbol-search`, `[data-testid='navigation-symbol-search-form'] input` |
| Estado de mercado | `[data-testid='market-status']`, `market-status-time-compact` |
| Notificaciones | `[data-testid='notifications-button-alert']`; tarjetas `notification-card-container` (texto tipo `CANCELED SELL -1 F STP 9.00 TRG BY #<id> OCO #<id>`) |
| Navegación lateral | `[data-testid='sidebar-navigation']`, `positions-page-button` |

## Cuenta y modo Live / Paper

| Elemento | Selector |
|---|---|
| Panel | `[data-testid='account-summary']` |
| Interruptor Live/Paper | `[data-testid='toggle']` |
| → Paper | `input[data-testid='toggle-right'][aria-label='Paper Money']` |
| → Live | `input[data-testid='toggle-left'][aria-label='Real Live Trading']` |
| Selector de cuenta | `[data-testid='account-dropdown-value']` (aria-label `select account margin *XXX`) |
| Valor de cuenta | `[data-testid='account-value-value']` |
| Poder de compra | `stock-buying-power-value`, `option-buying-power-value`, `intraday-buying-power-value` |

En paper aparece el aviso "These are simulated values" y el logo dice
`paperMoney`. **Comprobar esto antes de cualquier prueba que envíe órdenes.**

## Ticket de orden

Contenedores:
- `[data-testid='trade-ticket-wrapper']`: el editor del ticket.
- `[data-testid='trade-table']`: tabla de trades/órdenes (aria-label `Show working Trades`).
- `[data-testid='confirmation-tray']`: barra inferior con Delete / Review / Send.

### Menú Advanced Orders (el `marker` del WebSocket está en el testid)

| UI | Selector | `marker` WS |
|---|---|---|
| botón del menú | `[data-testid='advanced-order-dropdown-value']` | — |
| Blast All | `[data-testid='advanced-order-dropdown:BLAST_ALL']` | `BLAST_ALL` |
| OCO | `[data-testid='advanced-order-dropdown:OCO']` | `OCO` |
| 1st trgs Seq | `[data-testid='advanced-order-dropdown:FIRST_TRIGGERS_SEQ']` | `FIRST_TRIGGERS_SEQ` |
| 1st trgs All | `[data-testid='advanced-order-dropdown:FIRST_TRIGGERS_ALL']` | `FIRST_TRIGGERS_ALL` |
| **1st trgs OCO** | `[data-testid='advanced-order-dropdown:FIRST_TRIGGERS_OCO']` | `FIRST_TRIGGERS_OCO` (verificado por WS) |

Solo `FIRST_TRIGGERS_OCO` está verificado por WebSocket; los demás valores salen
del testid y deben confirmarse con una captura antes de usarlos en código.

### Campos por pata (`N` = 0 entrada, 1 take profit, 2 stop loss)

| Elemento | Selector |
|---|---|
| Comprar/Vender | `button[data-testid^='buy-sell-toggle-N-0-']` (sufijo `Buy`/`Sell`; aria-label `Buy Sell Toggle: Buy is selected`) |
| Cantidad | `input[data-testid='trade-quantity-input']` (aria-label `Quantity Input`) |
| Tipo de cantidad | `[data-testid='trade-quantity-type-dropdown-value']` |
| Tipo de orden | `[data-testid='order-type-dropdown-value']` (aria-label `Order Type`; MARKET, LIMIT, STOP, MOC, LOC…) |
| Vigencia | `[data-testid='tif-dropdown-value']` (aria-label `Time in Force`) |
| Precio de las salidas | `input[data-testid='limit-price-N-input']` (también para el STOP de la pata 2) |
| Botones ± precio | `stepper-decrement-button`, `stepper-increment-button` |
| Bloqueo de precio | `input[data-testid='price-lock-toggle-N']` |
| Bid / Ask | `bid-price-button`, `ask-price-button` |
| Relación entre patas | `contingency-toggle-N-button-0/1/2` = And / Or / Then |
| Quitar pata | `remove-leg-button-trade#N-leg#0` |

En un `1st trgs OCO`, la relación entre 0 y 1 es **Then** y entre 1 y 2 es **Or**.

### Revisar y enviar

| Elemento | Selector |
|---|---|
| Review | `[data-testid='review-order-button']` (aria-label `Review Draft Trade`) → manda `place_order` `action:CONFIRM` |
| **Send** | `[data-testid='send-order-button']` (aria-label `Send Draft Trade`) → manda `action:SUBMIT` |
| Borrar borrador | `button[aria-label='Delete Draft Trade']` |
| Costo en review | `cost-value`, `cost-1`, `cost-2` |
| Resumen | `[data-testid='order-summary-text']` (p.ej. `1st trgs OCO`) |

**Cuidado:** hacer clic en `send-order-button` envía la orden. En pruebas, solo
con paperMoney confirmado.

### Órdenes abiertas
- Filtros: `filter-working-button` (Working), `filter-positions-button`, `filter-completed-button`.
- Filas: `[data-testid='trades-table-tbody']`; cada borrador u orden lleva un
  checkbox con aria-label tipo `Draft Buy 100 F @ MARKET Day [First triggers OCO]`.
- Cancelar: botón "Cancel Selected" al seleccionar una orden activa. Por WS:
  `cancel_order` (ver WS_PROTOCOL.md).

## Scripts para inventariar el DOM (pegar en DevTools)

Listar todos los `data-testid` visibles del ticket, para comparar con esta tabla:

```js
const root = document.querySelector("[data-testid='trade-ticket-wrapper']");
[...new Set([...root.querySelectorAll('[data-testid],input,button')]
  .filter(e => e.offsetParent)
  .map(e => [e.tagName.toLowerCase(), e.getAttribute('data-testid') || '',
             e.getAttribute('aria-label') || ''].join(' | ')))]
```

Comprobar que los selectores críticos siguen existiendo (0 = cambió):

```js
Object.fromEntries(Object.entries({
  paper: "input[data-testid='toggle-right'][aria-label='Paper Money']",
  live: "input[data-testid='toggle-left'][aria-label='Real Live Trading']",
  symbolSearch: "#navigation-symbol-search",
  ticket: "[data-testid='trade-ticket-wrapper']",
  qty: "input[data-testid='trade-quantity-input']",
  orderType: "[data-testid='order-type-dropdown-value']",
  advanced: "[data-testid='advanced-order-dropdown-value']",
  review: "[data-testid='review-order-button']",
}).map(([k, s]) => [k, document.querySelectorAll(s).length]))
```
(`qty`, `orderType` y `review` solo existen con un ticket abierto: pulsar Buy primero.)
