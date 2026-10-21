"""Loader for ``calibration.yaml``: every parameter is a ``{value, source}`` node."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CALIBRATION_PATH = Path(__file__).with_name("calibration.yaml")


def _is_param(node: Any) -> bool:
    return isinstance(node, dict) and "value" in node and "source" in node


@dataclass(frozen=True)
class Calibration:
    """Read-only view over the calibration tree; keys are dotted paths such as ``"targets.avg_ticket_krw"``."""

    tree: dict[str, Any]
    path: str = str(CALIBRATION_PATH)

    def _node(self, key: str) -> dict[str, Any]:
        node: Any = self.tree
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"unknown calibration parameter: {key}")
            node = node[part]
        if not _is_param(node):
            raise KeyError(f"{key} is a group, not a parameter")
        return node

    def value(self, key: str) -> Any:
        return self._node(key)["value"]

    def source(self, key: str) -> str:
        return str(self._node(key)["source"])

    def parameters(self) -> dict[str, dict[str, Any]]:
        """Flattened ``{dotted key: {"value", "source"}}`` for documentation and data cards."""
        flat: dict[str, dict[str, Any]] = {}

        def walk(node: Any, prefix: str) -> None:
            for name, child in node.items():
                key = f"{prefix}.{name}" if prefix else str(name)
                if _is_param(child):
                    flat[key] = {"value": child["value"], "source": child["source"]}
                elif isinstance(child, dict):
                    walk(child, key)

        walk(self.tree, "")
        return flat


def validate(tree: dict[str, Any]) -> None:
    """Every leaf must be a ``{value, source}`` node with a non-empty source."""

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict) or not node:
            raise ValueError(f"calibration group {prefix or '<root>'} must be a non-empty mapping")
        for name, child in node.items():
            key = f"{prefix}.{name}" if prefix else str(name)
            if _is_param(child):
                if not str(child["source"]).strip():
                    raise ValueError(f"calibration parameter {key} has an empty source")
            else:
                walk(child, key)

    walk(tree, "")


def load_calibration(path: str | Path | None = None) -> Calibration:
    """Load and validate the calibration file (default: the packaged ``calibration.yaml``)."""
    file = Path(path) if path is not None else CALIBRATION_PATH
    tree = yaml.safe_load(file.read_text(encoding="utf-8"))
    validate(tree)
    return Calibration(tree, str(file))
