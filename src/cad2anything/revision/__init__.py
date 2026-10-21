"""Revision diff: units, brands, fixtures and walkways (exports are loaded lazily for ``python -m``)."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["diff_models", "evaluate_diff"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.diff"), name)
