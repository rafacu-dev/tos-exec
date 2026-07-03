"""Recepcion de alertas y gestion dinamica de salidas para thinkorswim.

Este modulo no genera senales. Recibe una alerta ya decidida por el proyecto
anfitrion con direccion (y opcionalmente contrato); abre la opcion en TOS
Paper Money y gestiona la salida por la prima del contrato (ver contract_exit).

Los datos del proyecto (precio del subyacente, seleccion de contrato, prima de
la opcion) llegan por `tos.hooks`; sin hooks, la alerta debe traer entry_price,
strike y expiration, y no hay gestion de salida por prima.

Sin dependencia de UI ni de Qt: el host recibe eventos por callbacks (on_log,
on_position_changed) y puede sondear con su propio timer (check_prices) o con
el hilo interno (start). Si se usa la automatizacion DOM de tos.automation
(Qt), el host debe llamar submit_alert/check_prices desde el hilo de Qt; la
ruta WebSocket (la normal) es thread-safe.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import threading
import uuid
from typing import Callable

from tos import hooks
from tos.paths import data_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sign(direction: str) -> int:
    return 1 if str(direction).upper() == "CALL" else -1


@dataclass
class TosAlert:
    """Alerta ejecutable.

    `target_price` y `stop_price` son precios del subyacente. Si `strike` y
    `expiration` no vienen definidos, el modulo intentara seleccionar un
    contrato de opcion con el hook `tos.hooks.select_contract`.
    """

    symbol: str
    direction: str
    # target/stop del SUBYACENTE: opcionales. TOS gestiona la salida por la PRIMA del
    # contrato (contract_exit), no por estos niveles. Se aceptan pero se ignoran.
    target_price: float | None = None
    stop_price: float | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    strategy: str = ""
    entry_price: float | None = None
    strike: float | None = None
    expiration: str | None = None
    quantity: int = 1
    max_investment: float = 500.0
    max_contracts: int = 1
    submit: bool = True
    trail_gap: float | None = None
    trail_step: float | None = None
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "TosAlert":
        clean = dict(data)
        if "target" in clean and "target_price" not in clean:
            clean["target_price"] = clean.pop("target")
        if "stop" in clean and "stop_price" not in clean:
            clean["stop_price"] = clean.pop("stop")
        clean["symbol"] = str(clean["symbol"]).upper()
        clean["direction"] = str(clean["direction"]).upper()
        return cls(**clean)

    def validate(self):
        if self.direction not in ("CALL", "PUT"):
            raise ValueError("direction debe ser CALL o PUT")
        # target/stop son opcionales (TOS sale por la prima). Solo se validan si vienen.
        if self.target_price is None or self.stop_price is None:
            return
        if self.target_price <= 0 or self.stop_price <= 0:
            raise ValueError("target_price y stop_price deben ser positivos")
        if self.entry_price is not None:
            s = _sign(self.direction)
            if s * (self.target_price - self.entry_price) <= 0:
                raise ValueError("target_price debe estar a favor de la direccion")
            if s * (self.entry_price - self.stop_price) <= 0:
                raise ValueError("stop_price debe estar contra la direccion")


class ManagedPositionStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else data_dir() / "managed_positions.json"
        self.positions: list[dict] = []
        if self.path.exists():
            try:
                self.positions = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self.positions = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.positions, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    def add(self, data: dict) -> dict:
        pos = {
            "id": uuid.uuid4().hex[:10],
            "status": "open",
            "opened_at": _now(),
            "closed_at": None,
            "exit_reason": None,
            "last_error": None,
            **data,
        }
        self.positions.append(pos)
        self._save()
        return pos

    def all(self) -> list[dict]:
        return list(self.positions)

    def open_positions(self) -> list[dict]:
        return [p for p in self.positions if p.get("status") == "open"]

    def update(self, pos_id: str, **fields) -> dict | None:
        for pos in self.positions:
            if pos.get("id") == pos_id:
                pos.update(fields)
                self._save()
                return pos
        return None

    def mark_closing(self, pos_id: str, reason: str) -> dict | None:
        return self.update(pos_id, status="closing", exit_reason=reason,
                           closing_requested_at=_now())

    def mark_closed(self, pos_id: str, exit_reason: str, order_result: dict | None = None):
        return self.update(pos_id, status="closed", exit_reason=exit_reason,
                           closed_at=_now(), close_order_result=order_result or {})

    def mark_open_error(self, pos_id: str, error: str):
        return self.update(pos_id, status="open", last_error=error)


# (trailing_update / stop_hit sobre el subyacente: ELIMINADOS — TOS gestiona por la
#  prima del contrato vía contract_exit. Las copias activas viven en alpaca_exec.)

# margen tras pedir un cierre antes de declarar que NO ejecutó (y reintentar)
CLOSE_GRACE_SEC = 25
# no re-disparar un cierre automático más seguido que esto (evita spam de órdenes)
CLOSE_RETRY_COOLDOWN_SEC = 30


def _seconds_since(ts: str | None) -> float:
    if not ts:
        return 1e9
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return 1e9


# --- Gestión por P&L del CONTRATO (prima de la opción), no del subyacente ---
CONTRACT_TARGET_PCT = 10.0     # +10% sobre la prima = target; activa el trailing
CONTRACT_GIVEBACK_FRAC = 0.10  # cierra si el EXTRA (sobre el target) retrocede 10% del pico
CONTRACT_STOP_PCT = -20.0      # stop duro a -20% de la prima


def contract_exit(pos: dict, premium: float) -> dict:
    """Gestiona la salida por la GANANCIA DEL CONTRATO (prima de la opción).
      · target = entrada × 1.10 (+10%). Al alcanzarlo se activa el trailing y se deja
        correr el precio.
      · El "extra" = ganancia por ENCIMA del target. Cierra cuando el extra retrocede
        10% desde su pico → umbral = target + 90%·(pico − target) = pico − 10%·(pico − target).
        Ej: entrada 50, target 55, pico 70 (extra 15) → cierra a 70 − 1.5 = 68.5 (+37%).
      · Antes de activarse, stop duro a -20%.
    Devuelve {fields, reason}; reason no-None ⇒ cerrar."""
    entry = float(pos.get("option_entry_price") or 0)
    if entry <= 0 or premium <= 0:
        return {"fields": {}, "reason": None}
    # redondeo a 2 decimales: evita que el ruido de float deje pasar el umbral exacto.
    profit = round((premium - entry) / entry * 100.0, 2)
    prev_peak = pos.get("peak_profit_pct")
    peak = round(max(float(prev_peak) if prev_peak is not None else profit, profit), 2)
    active = bool(pos.get("contract_trailing_active")) or peak >= CONTRACT_TARGET_PCT
    fields = {
        "option_last_price": round(premium, 4),
        "option_pnl_pct": profit,
        "peak_profit_pct": peak,
        "contract_trailing_active": active,
    }
    reason = None
    if active:
        # umbral = target + 90% del extra-pico (el extra puede retroceder 10%). El piso
        # es +10% (cuando el pico == target el extra es 0 y el umbral queda en el target).
        extra_peak = peak - CONTRACT_TARGET_PCT
        trail = round(CONTRACT_TARGET_PCT + (1.0 - CONTRACT_GIVEBACK_FRAC) * extra_peak, 2)
        # solo cierra si YA retrocedió desde el pico (no al tocar el target subiendo):
        # mientras hace nuevos máximos profit==peak y se deja correr.
        if profit < peak and profit <= trail:
            reason = f"trailing_contrato ({profit:.0f}% <= umbral {trail:.0f}%, pico {peak:.0f}%)"
    elif profit <= CONTRACT_STOP_PCT:
        reason = "stop_contrato (-20%)"
    return {"fields": fields, "reason": reason}


class TosTradeManager:
    """Conecta alertas del proyecto anfitrion -> TOS -> gestion por prima.

    `automation` (opcional) es el ejecutor DOM de tos.automation; si expone la
    senal Qt `order_result`, se conecta sola a handle_order_result. La ruta
    principal es `ws_runtime` (ordenes por WebSocket, sin Qt).
    """

    def __init__(
        self,
        automation=None,
        store: ManagedPositionStore | None = None,
        price_fn: Callable[[str], tuple[float, str]] | None = None,
        poll_seconds: float = 20.0,
        ws_runtime=None,
        on_log: Callable[[str], None] | None = None,
        on_position_changed: Callable[[dict], None] | None = None,
    ):
        self.automation = automation
        self.ws_runtime = ws_runtime
        self.store = store or ManagedPositionStore()
        self._price_fn = price_fn
        self.poll_seconds = float(poll_seconds)
        self.on_log = on_log or (lambda msg: None)
        self.on_position_changed = on_position_changed or (lambda pos: None)
        self._warned_no_premium = False
        self._lock = threading.RLock()
        self._queue: list[dict] = []
        self._active_request: dict | None = None
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        if automation is not None and hasattr(automation, "order_result"):
            automation.order_result.connect(self.handle_order_result)

    def start(self):
        """Sondeo con hilo interno. Un host Qt que use la automatizacion DOM
        debe preferir su propio timer llamando check_prices()."""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_event.clear()

        def loop():
            while not self._stop_event.wait(self.poll_seconds):
                try:
                    self.check_prices()
                except Exception as exc:
                    self._emit(f"check_prices fallo: {exc}")

        self._poll_thread = threading.Thread(target=loop, daemon=True)
        self._poll_thread.start()
        self._emit(f"Monitor TOS iniciado: revisa precios cada {self.poll_seconds:.0f}s.")

    def stop(self):
        self._stop_event.set()
        self._poll_thread = None
        self._emit("Monitor TOS detenido.")

    def get_price(self, symbol: str) -> tuple[float, str]:
        """Precio del subyacente via price_fn del constructor o tos.hooks.price."""
        fn = self._price_fn or hooks.price
        if fn is None:
            raise RuntimeError(
                "sin fuente de precios: configura tos.hooks.price o incluye entry_price en la alerta")
        return fn(symbol)

    def submit_alert(self, alert: TosAlert | dict) -> str:
        alert = alert if isinstance(alert, TosAlert) else TosAlert.from_dict(alert)
        if alert.entry_price is None:
            price, _ts = self.get_price(alert.symbol)
            alert.entry_price = price
        alert.validate()

        contract_reason = ""
        if alert.strike is None or not alert.expiration:
            if hooks.select_contract is None:
                raise RuntimeError(
                    f"alerta {alert.symbol} sin strike/expiration y sin hook "
                    "select_contract configurado (tos.hooks)")
            try:
                sel = hooks.select_contract(
                    alert.symbol,
                    alert.direction,
                    strategy=alert.strategy,
                    budget=alert.max_investment,
                )
            except Exception as exc:
                raise RuntimeError(f"No pude seleccionar contrato para {alert.symbol}: {exc}") from exc
            alert.strike = float(sel["strike"])
            alert.expiration = str(sel["expiration"])
            contract_reason = str(sel.get("reason") or "")

        req = {"type": "entry", "alert": alert, "contract_reason": contract_reason}
        self._enqueue(req)
        return alert.id

    def _held_symbol(self, pos: dict):
        from tos.ws_client import option_symbol
        try:
            return option_symbol(pos["symbol"], str(pos["expiration"]),
                                 pos["direction"], float(pos["strike"]))
        except Exception:
            return None

    def _verify_closing(self, held):
        """Confirma con el broker las posiciones en 'closing': si YA no están en la
        cuenta → cierre confirmado (marca closed + graba P&L). Si SIGUEN tras el margen
        → el cierre no ejecutó: las regresa a 'open' para que contract_exit reintente
        (con cooldown). Esto evita que el sistema 'crea que cerró' algo que sigue abierto."""
        if held is None:
            return
        for pos in [p for p in self.store.all() if p.get("status") == "closing"]:
            sym = self._held_symbol(pos)
            if sym is None:
                continue
            if sym not in held:
                premium = self._option_premium(pos)
                entry = float(pos.get("option_entry_price") or 0)
                if premium is not None and entry > 0:
                    self.store.update(pos["id"], option_last_price=round(premium, 4),
                                      option_pnl_pct=round((premium - entry) / entry * 100.0, 2))
                closed = self.store.mark_closed(pos["id"], pos.get("exit_reason") or "closed",
                                                pos.get("close_order_result") or {"ok": True, "via": "verified"})
                if closed:
                    self._emit(f"✓ Cierre CONFIRMADO en broker: {pos['symbol']} {pos['direction']} {pos['strike']}.")
                    self.on_position_changed(closed)
            elif _seconds_since(pos.get("closing_requested_at")) >= CLOSE_GRACE_SEC:
                reopened = self.store.update(pos["id"], status="open",
                                             last_error="cierre no ejecutó en el broker; reintentando")
                if reopened:
                    self._emit(f"⚠️ Cierre NO ejecutó: {pos['symbol']} {pos['strike']} sigue en la cuenta → reintentando.")
                    self.on_position_changed(reopened)

    def _reconcile_with_broker(self, held):
        """Marca cerradas las posiciones que ya NO están en la cuenta TOS (cerradas
        manualmente en la web). Evita que el sistema intente cerrar lo ya cerrado."""
        if held is None:   # no se pudo consultar → no reconciliar (evita falsos cierres)
            return
        for pos in self.store.open_positions():
            sym = self._held_symbol(pos)
            if sym is None:
                continue
            if sym not in held:
                # graba la prima actual como estimación de la salida (la reconciliación
                # corre cada poll, así que detecta el cierre manual dentro de ~20s → la
                # prima de ahora ≈ la del cierre). Mejor que dejar la P&L en 0/None.
                premium = self._option_premium(pos)
                entry = float(pos.get("option_entry_price") or 0)
                if premium is not None and entry > 0:
                    self.store.update(pos["id"], option_last_price=round(premium, 4),
                                      option_pnl_pct=round((premium - entry) / entry * 100.0, 2))
                self.store.mark_closed(pos["id"], "reconciled_manual_close",
                                       {"ok": True, "via": "reconcile"})
                changed = next((p for p in self.store.all() if p.get("id") == pos["id"]), None)
                if changed:
                    self.on_position_changed(changed)
                pnl_txt = f" P&L≈{changed.get('option_pnl_pct'):+.0f}%" if changed and changed.get("option_pnl_pct") else ""
                self._emit(f"Reconciliado: {pos['symbol']} {pos['direction']} "
                           f"{pos['strike']} cerrada manualmente en TOS.{pnl_txt}")

    def _option_premium(self, pos: dict) -> float | None:
        """Prima (mid) actual de la opción de la posición, vía tos.hooks.option_premium.
        None si no se pudo leer (no gestionar ese ciclo, para no cerrar a ciegas)."""
        if hooks.option_premium is None:
            if not self._warned_no_premium:
                self._warned_no_premium = True
                self._emit("⚠️ Sin hook option_premium: no puedo gestionar salidas por "
                           "prima (configura tos.hooks).")
            return None
        try:
            mid = hooks.option_premium(pos["symbol"], str(pos["expiration"]),
                                       pos["direction"], float(pos["strike"]))
            mid = float(mid or 0)
            return mid if mid > 0 else None
        except Exception as exc:
            self._emit(f"{pos['symbol']}: sin prima de opción ({exc})")
            return None

    def check_prices(self):
        """Gestión por la P&L del CONTRATO (prima): target +10% activa trailing; cierra
        si el extra retrocede 10% del pico; stop duro -20%. Ver contract_exit().
        Antes de gestionar: reconcilia cierres manuales y VERIFICA contra el broker que
        los cierres pedidos de verdad ejecutaron (si no, reintenta)."""
        held = self.ws_runtime.held_option_symbols() if self.ws_runtime else None
        self._reconcile_with_broker(held)
        self._verify_closing(held)
        for pos in self.store.open_positions():
            premium = self._option_premium(pos)
            if premium is None:
                continue
            # si no se registró prima de entrada (p.ej. entrada por DOM), ancla la
            # primera prima observada como entrada para poder gestionar por contrato.
            if not pos.get("option_entry_price"):
                self.store.update(pos["id"], option_entry_price=premium)
                pos = {**pos, "option_entry_price": premium}
            out = contract_exit(pos, premium)
            changed = self.store.update(pos["id"], **out["fields"])
            if changed:
                self.on_position_changed(changed)
            if out["reason"]:
                # cooldown: no re-disparar un cierre si ya se intentó hace poco (evita
                # spam de órdenes mientras se confirma/reintenta el cierre anterior).
                if _seconds_since(pos.get("last_close_attempt_at")) < CLOSE_RETRY_COOLDOWN_SEC:
                    continue
                self._emit(f"{pos['symbol']}: cierre por contrato → {out['reason']} "
                           f"(prima {premium:.2f})")
                self.request_close(pos["id"], out["reason"])

    def request_close(self, pos_id: str, reason: str = "manual"):
        pos = next((p for p in self.store.open_positions() if p.get("id") == pos_id), None)
        if not pos:
            self._emit(f"Cierre ignorado: posicion {pos_id} no esta abierta.")
            return False
        self.store.mark_closing(pos_id, reason)
        self._enqueue({"type": "exit", "position": pos, "reason": reason})
        return True

    def _enqueue(self, req: dict):
        with self._lock:
            if self._active_request or getattr(self.automation, "busy", False):
                self._queue.append(req)
                self._emit(f"Solicitud TOS en cola ({req['type']}).")
                return
            self._send(req)

    def _ws_available(self) -> bool:
        return self.ws_runtime is not None and self.ws_runtime.token_status() is not None

    def _ws_order_async(self, root, side, strike, expiration, qty, submit, is_entry):
        """Coloca por WS en un hilo y reinyecta el resultado vía order_result."""
        def job():
            try:
                r = self.ws_runtime.place_option(root=root, expiration=expiration, side=side,
                                                 strike=strike, quantity=qty, order_type="MARKET",
                                                 submit=submit)
                if not is_entry and r.get("stage") == "skipped":
                    # No había long que cerrar → reconciliar como CERRADA (no es error,
                    # no reabrir, no reintentar). Evita el bucle y el short.
                    res = {"ok": True, "state": "PAPER_EXIT_SUBMITTED", "price": None,
                           "quantity": abs(int(qty)), "via": "ws",
                           "detail": "reconciliada: sin long en la cuenta (safeguard)"}
                else:
                    good = bool(r.get("ok"))
                    state = ("PAPER_ORDER_SUBMITTED" if is_entry else "PAPER_EXIT_SUBMITTED") if good else "WS_REJECTED"
                    res = {"ok": good, "state": state, "price": r.get("price"),
                           "quantity": abs(int(qty)), "order_id": r.get("order_id"),
                           "via": "ws", "detail": "" if good else "SUBMIT sin orderId"}
            except Exception as exc:
                res = {"ok": False, "state": "WS_ERROR", "detail": str(exc), "via": "ws"}
            self.handle_order_result(res)
        threading.Thread(target=job, daemon=True).start()

    def _send(self, req: dict):
        with self._lock:
            self._active_request = req
        via_ws = self._ws_available()
        if req["type"] == "entry":
            alert: TosAlert = req["alert"]
            self._emit(
                f"TOS entrada [{'WS' if via_ws else 'DOM'}]: {alert.symbol} {alert.direction} strike {alert.strike} "
                f"venc {alert.expiration} target {alert.target_price} stop {alert.stop_price}"
            )
            if via_ws:
                self._ws_order_async(alert.symbol, alert.direction, float(alert.strike),
                                     str(alert.expiration), int(alert.quantity),
                                     bool(alert.submit), is_entry=True)
                return
            if self.automation is None:
                self._fail_no_executor(req)
                return
            ok = self.automation.buy_option(
                alert.symbol,
                alert.direction,
                float(alert.strike),
                str(alert.expiration),
                quantity=int(alert.quantity),
                max_investment=float(alert.max_investment),
                submit=bool(alert.submit),
                auto_size=True,
                max_contracts=int(alert.max_contracts),
            )
        else:
            pos = req["position"]
            self._emit(f"TOS salida [{'WS' if via_ws else 'DOM'}]: {pos['symbol']} {pos['direction']} ({req['reason']}).")
            if via_ws:
                self._ws_order_async(pos["symbol"], pos["direction"], float(pos["strike"]),
                                     str(pos["expiration"]), -int(pos.get("quantity") or 1),
                                     True, is_entry=False)
                return
            if self.automation is None:
                self._fail_no_executor(req)
                return
            ok = self.automation.sell_option(
                pos["symbol"],
                pos["direction"],
                float(pos["strike"]),
                str(pos["expiration"]),
                quantity=int(pos.get("quantity") or 1),
                submit=True,
            )
        if not ok:
            if req["type"] == "exit":
                self.store.mark_open_error(req["position"]["id"], "automation busy/rejected")
            with self._lock:
                self._active_request = None
            self._drain_queue()

    def _fail_no_executor(self, req: dict):
        """Ni token WS valido ni automation DOM: la orden no puede salir."""
        self._emit("⚠️ Orden rechazada: sin token WS válido y sin automation DOM.")
        if req["type"] == "exit":
            self.store.update(req["position"]["id"], status="open",
                              last_error="sin ejecutor (WS/DOM) disponible")
        with self._lock:
            self._active_request = None
        self._drain_queue()

    def handle_order_result(self, res: dict):
        """Resultado de una orden (WS o DOM). La automation Qt se conecta sola
        via su senal order_result; un ejecutor propio debe llamarlo al terminar."""
        with self._lock:
            req = self._active_request
            self._active_request = None
        if not req:
            self._drain_queue()
            return

        ok = bool(res.get("ok"))
        state = str(res.get("state", ""))
        if req["type"] == "entry":
            alert: TosAlert = req["alert"]
            if ok and state == "PAPER_ORDER_SUBMITTED":
                pos = self.store.add({
                    "alert_id": alert.id,
                    "symbol": alert.symbol,
                    "direction": alert.direction,
                    "strategy": alert.strategy,
                    "underlying_entry": float(alert.entry_price) if alert.entry_price is not None else None,
                    "initial_target": float(alert.target_price) if alert.target_price is not None else None,
                    "current_target": float(alert.target_price) if alert.target_price is not None else None,
                    "stop_price": float(alert.stop_price) if alert.stop_price is not None else None,
                    "trail_gap": alert.trail_gap,
                    "trail_step": alert.trail_step,
                    "target_hits": 0,
                    "trailing_active": False,
                    "best_price": float(alert.entry_price),
                    "strike": float(alert.strike),
                    "expiration": str(alert.expiration),
                    "quantity": int(res.get("quantity") or alert.quantity),
                    "option_entry_price": res.get("price"),
                    "max_investment": float(alert.max_investment),
                    "contract_reason": req.get("contract_reason", ""),
                    "entry_order_result": res,
                })
                self._emit(f"Posicion gestionada abierta: {pos['id']} {pos['symbol']}.")
                self.on_position_changed(pos)
            else:
                self._emit(f"Entrada no registrada: {state} {res.get('detail', '')}")
        else:
            pos = req["position"]
            if ok and state == "PAPER_EXIT_SUBMITTED":
                # NO marcar cerrado todavía: el submit NO garantiza que la posición se
                # haya reducido en el broker. Se confirma contra held_quantity en
                # _verify_closing; mientras tanto queda en estado "closing".
                self.store.update(pos["id"], close_order_result=res,
                                  last_close_attempt_at=_now())
                self._emit(f"Cierre ENVIADO: {pos['symbol']} ({req['reason']}) — verificando en broker…")
            else:
                self.store.update(pos["id"], status="open",
                                  last_error=f"cierre rechazado {state}: {res.get('detail', '')}")
                self._emit(f"⚠️ Cierre rechazado: {pos['symbol']} {state} → sigue abierta.")
                self._emit(f"Cierre fallo; posicion vuelve a abierta: {state}")

        self._drain_queue()

    def _drain_queue(self):
        with self._lock:
            if not self._queue or getattr(self.automation, "busy", False):
                return
            nxt = self._queue.pop(0)
        threading.Timer(0.25, lambda: self._send(nxt)).start()

    def _emit(self, msg: str):
        try:
            self.on_log(str(msg))
        except Exception:
            pass
