"""Puntos de integracion con el proyecto anfitrion.

La libreria no sabe de donde salen los precios ni como elegir contratos: cada
proyecto lo inyecta aqui. Sin hooks, la libreria sigue funcionando pero exige
alertas completas (entry_price + strike + expiration) y no gestiona salidas
por prima.

Dos maneras de configurar:

1. En el mismo proceso:

     from tos import hooks
     hooks.configure(price=..., select_contract=..., option_premium=...)

2. Executor standalone (proceso aparte): exporta TOS_HOOKS_MODULE=<modulo>
   antes de lanzar `python3 -m tos.executor_app`. El modulo se importa al
   arrancar (con el cwd en sys.path) y debe llamar a configure() durante su
   import.

Contratos de las funciones:

  price(symbol) -> (precio: float, timestamp_iso: str)
      Ultimo precio del subyacente.

  select_contract(symbol, direction, strategy="", budget=500.0) -> dict
      Elige un contrato de opcion. Devuelve:
      {"strike": float, "expiration": "YYYY-MM-DD",
       "cost": float | None,    # costo de 1 contrato (prima x 100)
       "delta": float | None,   # |delta| del contrato
       "reason": str}           # explicacion de la eleccion

  option_premium(symbol, expiration, direction, strike) -> float | None
      Prima (mid) actual del contrato; None si no se pudo leer.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Callable

price: Callable[[str], tuple[float, str]] | None = None
select_contract: Callable[..., dict] | None = None
option_premium: Callable[[str, str, str, float], float | None] | None = None


def configure(*, price=None, select_contract=None, option_premium=None) -> None:
    g = globals()
    if price is not None:
        g["price"] = price
    if select_contract is not None:
        g["select_contract"] = select_contract
    if option_premium is not None:
        g["option_premium"] = option_premium


def load_from_env(log=print) -> None:
    """Importa el modulo indicado en TOS_HOOKS_MODULE (si esta definido).

    Mete el cwd en sys.path para que funcione igual lanzado como
    `python3 -m tos.executor_app` o via el script `tos-executor`.
    """
    name = os.getenv("TOS_HOOKS_MODULE")
    if not name:
        return
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        importlib.import_module(name)
    except Exception as exc:
        log(f"⚠️ No pude cargar TOS_HOOKS_MODULE={name}: {exc}")
        return
    active = [n for n in ("price", "select_contract", "option_premium") if globals()[n] is not None]
    log(f"Hooks cargados desde {name}: {', '.join(active) or 'ninguno'}")
