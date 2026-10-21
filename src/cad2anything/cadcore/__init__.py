"""CAD understanding pipeline: DXF drawings -> versioned ``SpatialModel`` with a QA report.

Modules: ``reader`` (DXF/manifest, units, encodings), ``geometry`` (polygon reconstruction), ``classify``
(layer semantics: manifest -> heuristics -> optional LLM -> review queue), ``labels`` (unit/zone/link
labels), ``fixtures`` (fixture blocks), ``pipeline`` (``parse_revision`` and CLI) and ``evaluate``
(metrics against synthetic ground truth). Exports are loaded lazily so ``python -m`` runs stay clean.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    "SEMANTICS": "classify",
    "HeuristicClassifier": "classify",
    "LayerClassification": "classify",
    "LayerClassifier": "classify",
    "LayerDecision": "classify",
    "LayerFeatures": "classify",
    "LlmClassifier": "classify",
    "ManifestClassifier": "classify",
    "classify_layers": "classify",
    "compare_with_truth": "evaluate",
    "ParseReport": "pipeline",
    "load_brand_registry": "pipeline",
    "parse_revision": "pipeline",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{_EXPORTS[name]}"), name)
