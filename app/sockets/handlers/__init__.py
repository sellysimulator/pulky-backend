"""Handler modules, auto-discovered: importing the package imports each module."""

import importlib
import pkgutil

for _module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    importlib.import_module(f"{__name__}.{_module.name}")
