"""REST routers, auto-discovered: each module exports a module-level ``router``."""

import importlib
import pkgutil

from fastapi import APIRouter

_routers: list[APIRouter] = []

for _module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    _loaded = importlib.import_module(f"{__name__}.{_module.name}")
    _router = getattr(_loaded, "router", None)
    if isinstance(_router, APIRouter):
        _routers.append(_router)


def all_routers() -> list[APIRouter]:
    """Every module's ``router``, in module-name order."""
    return list(_routers)
