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
    "walk_to_escalator_m": "에스컬레이터 접근성(보행거리)",
    "low_efficiency": "저효율 매장(층 하위 10%)",
}
# Legend hints for metrics whose color does not simply mean "higher value" (see docs/specs/atlas-api.md).
METRIC_LEGENDS: dict[str, dict[str, Any]] = {
    "walk_to_escalator_m": {
        "low": "멀다",
        "high": "가깝다",
        "note": "진할수록 에스컬레이터까지 보행거리가 짧습니다.",
    },
    "low_efficiency": {"binary": True, "low": "그 외 매장", "high": "저효율 매장(층 하위 10%)"},
}
TABLE_ROW_KEYS = ("rows", "data", "items", "records", "table", "units", "values")
MAX_TABLE_ROWS = 200
TABLE_SHAPE_ERROR = (
    "table payload에는 행이 필요합니다. 형식: {title, columns: [열 이름], rows: [[값, ...], ...]}. "
    "도구 결과의 실제 값을 rows에 넣어 다시 호출하세요."
)
JOURNEY_ARGS = ("customer_id", "date", "segment", "rank")
JOURNEY_STOP_FIELDS = (
    "order",
    "time",
    "floor",
    "unit_code",
    "brand_name",
    "category_name",
    "amount_krw",
    "gap_min",
)


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


def table_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce an agent's table payload to ``{title, columns, rows}`` with list rows; None if it has no rows.

    Accepts rows as lists or dicts, rows under common alternative keys, and column-oriented dicts.
    """
    rows: Any = next((payload[k] for k in TABLE_ROW_KEYS if isinstance(payload.get(k), list | dict)), None)
    columns = payload.get("columns")
    if isinstance(rows, dict):
        series = {str(k): v for k, v in rows.items() if isinstance(v, list)}
        if series and len({len(v) for v in series.values()}) == 1:
            columns, rows = list(series), [list(row) for row in zip(*series.values(), strict=True)]
        elif all(isinstance(v, dict) for v in rows.values()):
            rows = [{"항목": key, **value} for key, value in rows.items()]
        else:
            rows = [{"항목": key, "값": value} for key, value in rows.items()]
    if not rows:
        return None
    if not (isinstance(columns, list) and columns):
        if all(isinstance(row, dict) for row in rows):
            columns = list(dict.fromkeys(key for row in rows for key in row))
        else:
            width = max(len(row) if isinstance(row, list) else 1 for row in rows)
            columns = [f"열{i + 1}" for i in range(width)]
    columns = [str(column) for column in columns]
    width = len(columns)
    normalized = []
    for row in rows[:MAX_TABLE_ROWS]:
        if isinstance(row, dict):
            cells = [row.get(column) for column in columns]
        elif isinstance(row, list):
            cells = row[:width] + [None] * (width - len(row))
        else:
            cells = [row] + [None] * (width - 1)
        normalized.append(cells)
    title = str(payload.get("title") or "데이터 표")
    return {"title": title, "columns": columns, "rows": normalized, "truncated": len(rows) > MAX_TABLE_ROWS}


class UiActionHandler:
    """Executes client-side UI tools. Providers compute heatmap values and journeys deterministically."""

    def __init__(
        self,
        heatmap_provider: HeatmapProvider | None = None,
        journey_provider: Callable[..., dict[str, Any]] | None = None,
    ):
        self.heatmap_provider = heatmap_provider
        self.journey_provider = journey_provider

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
        if metric in METRIC_LEGENDS:
            heatmap["legend"] = METRIC_LEGENDS[metric]
        patches = [_replace("/heatmap", heatmap)]
        if floor and floor != ui_state.get("floor"):
            patches.insert(0, _replace("/floor", floor))
            ui_state["floor"] = floor
        output = {"ok": True, "metric": metric, "rev": rev, "floor": floor, "units": len(values)}
        ranked = sorted((v, k) for k, v in raw.items() if v is not None and v == v)
        if metric == "low_efficiency":
            output["flagged_units"] = [k for v, k in ranked if v > 0]
        elif ranked:
            output["lowest_units"] = [k for _, k in ranked[:3]]
            output["highest_units"] = [k for _, k in ranked[-3:][::-1]]
        return UiActionResult(json.dumps(output, ensure_ascii=False), patches)

    def _ui_show_journey(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        if self.journey_provider is None:
            return UiActionResult(json.dumps({"ok": False, "error": "journey provider unavailable"}))
        params = {k: args[k] for k in JOURNEY_ARGS if args.get(k) not in (None, "", {})}
        try:
            journey = self.journey_provider(**params)
        except (ToolError, KeyError, ValueError) as exc:  # let the model see the reason and recover
            return UiActionResult(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        patches = [_replace("/journey", journey)]
        first_floor = next((stop["floor"] for stop in journey.get("stops", []) if stop.get("floor")), None)
        if first_floor:
            patches.append(_replace("/floor", first_floor))
            ui_state["floor"] = first_floor
        patches.append(_replace("/panel", {"type": "journey", "payload": {}}))
        output = {
            "ok": True,
            "customer": journey.get("customer"),
            "date": journey.get("date"),
            "weekday": journey.get("weekday"),
            "stops": [{k: stop.get(k) for k in JOURNEY_STOP_FIELDS} for stop in journey.get("stops", [])],
            "transitions": journey.get("transitions", []),
            "summary": journey.get("summary"),
            "caveat": journey.get("caveat"),
            "shown": f"{first_floor or ''} 도면에 동선과 결제 순서를 표시하고 동선 목록을 열었습니다.",
        }
        return UiActionResult(json.dumps(output, ensure_ascii=False, default=str), patches)

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
        panel_type = args["type"]
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        output: dict[str, Any] = {"ok": True, "panel": panel_type}
        if panel_type == "table":
            table = table_payload(payload)
            if table is None:  # opening an empty table would only show "no rows"; let the model retry
                return UiActionResult(
                    json.dumps({"ok": False, "error": TABLE_SHAPE_ERROR}, ensure_ascii=False)
                )
            payload = table
            output["rows"] = len(table["rows"])
        panel = {"type": panel_type, "payload": payload}
        return UiActionResult(json.dumps(output, ensure_ascii=False), [_replace("/panel", panel)])

    def _ui_clear(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        what = args["what"]
        targets = {
            "heatmap": ("/heatmap", None),
            "highlights": ("/highlights", []),
            "edges": ("/edges", []),
            "journey": ("/journey", None),
        }
        selected = list(targets.values()) if what == "all" else [targets[what]]
        patches = [_replace(path, value) for path, value in selected]
        if what == "all":
            patches.append(_replace("/focus", None))
        return UiActionResult(json.dumps({"ok": True, "cleared": what}), patches)
