"""Local helpers for the skeleton suite."""

import pytest

from app import config


def patch_settings(monkeypatch: pytest.MonkeyPatch, **values: object) -> None:
    """Patch the module-level settings and the cached ``get_settings()`` object."""
    targets = {id(config.settings): config.settings}
    current = config.get_settings()
    targets[id(current)] = current
    for target in targets.values():
        for name, value in values.items():
            monkeypatch.setattr(target, name, value)
