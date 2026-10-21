"""Translate orchestrator UI function calls into Atlas UI state patches (RFC 6902)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cad2anything.services.errors import ToolError

HeatmapProvider = Callable[[str, str, str], dict[str, float]]
METRIC_LABELS = {
    "sales_per_m2": "연환산 ㎡당 매출(원)",
    "sales_krw": "연환산 매출(원)",
    "aov_krw": "객단가(원)",
    "gmrof": "GMROF",
    "walk_to_escalator_m": "에스컬레이터까지 거리(m)",
    "low_efficiency": "저효율 매장",
}


@dataclass
class UiActionResult:
    output: str
    patches: list[dict[str, Any]] = field(default_factory=list)


def _replace(path: str, value: Any) -> dict[str, Any]:
    return {"op": "replace", "path": path, "value": value}


def normalize(values: dict[str, float], invert: bool = False) -> dict[str, float]:
    finite = {k: float(v) for k, v in values.items() if v is not None and v == v}
    if not finite:
        return {}
    low, high = min(finite.values()), max(finite.values())
    span = high - low or 1.0
    scaled = {k: (v - low) / span for k, v in finite.items()}
    return {k: round(1.0 - v if invert else v, 4) for k, v in scaled.items()}


class UiActionHandler:
    """Executes client-side UI tools. The heatmap provider computes metric values deterministically."""

    def __init__(self, heatmap_provider: HeatmapProvider | None = None):
        self.heatmap_provider = heatmap_provider

    def handle(self, name: str, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        method = getattr(self, f"_{name}", None)
        if method is None:
            return UiActionResult(
                json.dumps({"ok": False, "error": f"unknown ui tool {name}"}, ensure_ascii=False)
            )
        return method(args, ui_state)

    def _ui_set_floor(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        ui_state["floor"] = args["floor"]
        return UiActionResult(
            json.dumps({"ok": True, "floor": args["floor"]}), [_replace("/floor", args["floor"])]
        )

    def _ui_show_heatmap(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        if self.heatmap_provider is None:
            return UiActionResult(json.dumps({"ok": False, "error": "heatmap provider unavailable"}))
        metric = args["metric"]
        rev = args.get("rev") or ui_state.get("rev")
        floor = args.get("floor") or ui_state.get("floor")
        try:
            raw = self.heatmap_provider(metric, rev, floor)
        except (ToolError, KeyError, ValueError) as exc:  # let the model see the reason and recover
            error = {"ok": False, "error": str(exc), "metric": metric, "rev": rev, "floor": floor}
            return UiActionResult(json.dumps(error, ensure_ascii=False))
        values = normalize(raw, invert=metric == "walk_to_escalator_m")
        heatmap = {"metric": metric, "label": METRIC_LABELS.get(metric, metric), "values": values}
        patches = [_replace("/heatmap", heatmap)]
        if floor and floor != ui_state.get("floor"):
            patches.insert(0, _replace("/floor", floor))
            ui_state["floor"] = floor
        output = {"ok": True, "metric": metric, "rev": rev, "floor": floor, "units": len(values)}
        return UiActionResult(json.dumps(output, ensure_ascii=False), patches)

    def _ui_highlight_units(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        color = args.get("color") or "#e53935"
        highlights = [
            {"unit_code": code, "color": color, "reason": args.get("reason", "")}
            for code in args["unit_codes"]
        ]
        return UiActionResult(
            json.dumps({"ok": True, "highlighted": len(highlights)}), [_replace("/highlights", highlights)]
        )

    def _ui_draw_edges(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        edges = [
            {"from": e["from"], "to": e["to"], "weight": e.get("weight", 1.0), "label": e.get("label", "")}
            for e in args["edges"]
        ]
        return UiActionResult(json.dumps({"ok": True, "edges": len(edges)}), [_replace("/edges", edges)])

    def _ui_focus(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        return UiActionResult(
            json.dumps({"ok": True}), [_replace("/focus", {"unit_codes": list(args["unit_codes"])})]
        )

    def _ui_compare(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        ui_state["rev"], ui_state["compareRev"] = args["rev_to"], args["rev_from"]
        patches = [_replace("/rev", args["rev_to"]), _replace("/compareRev", args["rev_from"])]
        return UiActionResult(json.dumps({"ok": True}), patches)

    def _ui_show_panel(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        panel = {"type": args["type"], "payload": args.get("payload") or {}}
        return UiActionResult(json.dumps({"ok": True, "panel": args["type"]}), [_replace("/panel", panel)])

    def _ui_clear(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        what = args["what"]
        targets = {"heatmap": ("/heatmap", None), "highlights": ("/highlights", []), "edges": ("/edges", [])}
        selected = list(targets.values()) if what == "all" else [targets[what]]
        patches = [_replace(path, value) for path, value in selected]
        if what == "all":
            patches.append(_replace("/focus", None))
        return UiActionResult(json.dumps({"ok": True, "cleared": what}), patches)
