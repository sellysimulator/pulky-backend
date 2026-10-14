"""Startup checks, auto-discovered; a check module calls ``register_check``."""

import importlib
import logging
import pkgutil
from collections.abc import Callable

logger = logging.getLogger(__name__)

Check = Callable[[], None]

_checks: list[Check] = []


def register_check(fn: Check) -> Check:
    """Register ``fn`` to run at startup. Usable as a decorator; idempotent."""
    if fn not in _checks:
        _checks.append(fn)
    return fn


def run_startup_checks() -> None:
    """Import every module in this package, then run each check.

    A check that raises is logged and never aborts startup.
    """
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")
    for check in list(_checks):
        try:
            check()
        except Exception:
            logger.exception("Startup check %s failed.", getattr(check, "__name__", ""))
