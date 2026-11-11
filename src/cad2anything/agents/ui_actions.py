"""Translate orchestrator UI function calls into Atlas UI state patches (RFC 6902).

Overlays follow docs/specs/atlas-api.md §3. A journey, a route or a flow replaces the overlays of earlier
answers, and a heatmap, highlights, relation lines or a comparison replace the path of an earlier answer.
Overlays drawn earlier in the same answer stay, unless the revision on screen changes. ``ui_state`` is the
browser's screen for this turn (``rev``, ``floor``, ``selection`` and ``overlays``, the names of the overlays it
shows); handlers update it as they draw and keep per-turn bookkeeping under keys that start with "_".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cad2anything.agents.labels import floor_ko
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
# Revisions without transactions (hypothetical layouts, drawings that take effect after the data period) only
# have the scoring model's expected sales for these metrics (services/metrics.py).
SALES_METRICS = ("sales_per_m2", "sales_krw")
ESTIMATE_SUFFIX = " (모델 추정치)"
ESTIMATE_NOTE = (
    "이 개정은 거래 실적이 없어 색은 레이아웃 점수 모델이 추정한 기대 매출이에요. 실적이라고 말하지 마세요."
)
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
ROUTE_ARGS = ("origin", "destination", "destination_category", "destination_floor", "via")
FLOW_ARGS = ("mode", "floor", "segment", "day_type", "hours", "category", "min_width_m", "max_travel_m")
FLOW_SHOWN = {
    "traffic": "도면 통로에 추정 유동을 굵기와 색으로 그렸습니다(굵고 진할수록 많이 지남).",
    "popup": "도면에 추정 유동과 팝업 후보 위치를 번호로 표시했습니다.",
    "egress": "도면에 기준보다 좁은 통로와 계단·출입구까지 먼 지점을 경고 색으로 표시했습니다.",
}
ROUTE_BOX = "도면 왼쪽 위 길 찾기 상자에서 층을 바꿔 볼 수 있습니다."
# Overlay name (as in ui_state["overlays"]) -> (UI state key, empty value).
OVERLAYS: dict[str, tuple[str, Any]] = {
    "heatmap": ("heatmap", None),
    "highlights": ("highlights", []),
    "edges": ("edges", []),
    "compare": ("compareRev", None),
    "journey": ("journey", None),
    "route": ("route", None),
    "flow": ("flow", None),
}
# Analysis displays and the paths that are drawn alone.
MARKS = ("heatmap", "highlights", "edges", "compare")
PATHS = ("journey", "route", "flow")
# Per-turn bookkeeping in ui_state: overlays this answer drew, and overlays it cleared.
DRAWN = "_drawn"
CLEARED = "_cleared"
# Where each panel appears, so answers point users to the right place.
PANEL_PLACES = {
    "changes": "개정 검토 결과를 준비했습니다. 채팅 위의 '개정 검토 보기' 또는 개정 검토 탭에서 볼 수 있습니다.",
    "artifact": "산출물을 개정 검토 탭에 준비했습니다. 채팅 위의 '개정 검토 보기' 또는 개정 검토 탭에서 볼 수 있습니다.",
    "layout": "레이아웃 결과를 준비했습니다. 채팅 위의 '레이아웃 대안 보기' 또는 레이아웃 대안 탭에서 볼 수 있습니다.",
    "table": "도면 아래 표 패널에 표를 열었습니다.",
    "journey": "도면 왼쪽 위에 동선 카드를 열었습니다.",
    "uploads": "업로드 영역을 준비했습니다. 채팅 위의 '개정 검토 보기' 또는 개정 검토 탭에서 도면·메타데이터를 올릴 수 있습니다.",
    "none": "패널을 닫았습니다.",
}
RELAYOUT_PLACE = (
    "시즌 개편 프로젝트(진단, 대안, 결정, 도면 대조, 제안서)를 준비했습니다. "
    "채팅 위의 '레이아웃 대안 보기' 또는 레이아웃 대안 탭에서 볼 수 있습니다."
)


@dataclass
class UiActionResult:
    output: str
    patches: list[dict[str, Any]] = field(default_factory=list)


def _replace(path: str, value: Any) -> dict[str, Any]:
    return {"op": "replace", "path": path, "value": value}


def _shown_earlier(ui_state: dict[str, Any], name: str) -> bool | None:
    """Whether an earlier answer's overlay is on screen; None when the browser did not say.

    The browser reports visible overlays by name in ``overlays``; a full UI state reports them as values."""
    if name in (ui_state.get(CLEARED) or ()):
        return False
    key, _ = OVERLAYS[name]
    overlays = ui_state.get("overlays")
    if (isinstance(overlays, list) and name in overlays) or ui_state.get(key) not in (None, [], {}):
        return True
    return False if isinstance(overlays, list) or key in ui_state else None


def _bookkeeping(ui_state: dict[str, Any], key: str) -> list[str]:
    value = ui_state.get(key)
    if not isinstance(value, list):
        value = ui_state[key] = []
    return value


def _clear(ui_state: dict[str, Any], names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """Clear the overlays of earlier answers among ``names``; overlays this answer drew stay.

    An overlay is cleared when the browser reported it, or when it did not report its overlays at all
    (clearing an overlay that is not shown changes nothing)."""
    drawn, patches = ui_state.get(DRAWN) or (), []
    for name in names:
        if name in drawn or _shown_earlier(ui_state, name) is False:
            continue
        key, empty = OVERLAYS[name]
        patches.append(_replace(f"/{key}", empty))
        _bookkeeping(ui_state, CLEARED).append(name)
    return patches


def _drew(ui_state: dict[str, Any], name: str) -> None:
    drawn, cleared = _bookkeeping(ui_state, DRAWN), _bookkeeping(ui_state, CLEARED)
    if name not in drawn:
        drawn.append(name)
    if name in cleared:
        cleared.remove(name)


def _switch_rev(ui_state: dict[str, Any], rev: str | None, keep: str) -> list[dict[str, Any]]:
    """Show ``rev``: overlays drawn for the revision on screen are cleared, whichever answer drew them."""
    current = ui_state.get("rev")
    if not rev or rev == current:
        return []
    ui_state["rev"] = rev
    patches = [_replace("/rev", rev)]
    if (
        not current
        and not ui_state.get(DRAWN)
        and not any(_shown_earlier(ui_state, name) is True for name in OVERLAYS)
    ):
        return patches
    drawn = _bookkeeping(ui_state, DRAWN)
    stale = [name for name in drawn if name != keep]
    for name in stale:
        drawn.remove(name)
        key, empty = OVERLAYS[name]
        patches.append(_replace(f"/{key}", empty))
        _bookkeeping(ui_state, CLEARED).append(name)
    patches += _clear(ui_state, [name for name in OVERLAYS if name != keep and name not in stale])
    return patches


def screen_state(ui_state: dict[str, Any]) -> dict[str, Any]:
    """The screen as the agent sees it: revision, floor, comparison, selection and the visible overlays."""
    state = {
        key: ui_state[key]
        for key in ("rev", "floor", "compareRev", "selection")
        if ui_state.get(key) not in (None, "", [])
    }
    overlays = [name for name in OVERLAYS if _shown_earlier(ui_state, name)]
    if overlays:
        state["overlays"] = overlays
    return state


def normalize(values: dict[str, float], invert: bool = False) -> dict[str, float]:
    finite = {k: float(v) for k, v in values.items() if v is not None and v == v}
    if not finite:
        return {}
    low, high = min(finite.values()), max(finite.values())
    span = high - low or 1.0
    scaled = {k: (v - low) / span for k, v in finite.items()}
    return {k: round(1.0 - v if invert else v, 4) for k, v in scaled.items()}


def _journey_shown(journey: dict[str, Any], first_floor: str | None) -> str:
    """What ui_show_journey drew: a path only where the journey has drawable segments."""
    where = f"{floor_ko(first_floor)} " if first_floor else ""
    stops = journey.get("stops") or []
    segments = journey.get("segments") or []
    if segments:
        drawn = f"{where}도면에 결제 순서와 추정 경로를 그리고"
    elif len(stops) > 1:
        drawn = f"{where}도면에 결제한 매장을 순서대로 표시하고"
    else:
        drawn = f"{where}도면에 결제한 매장을 표시하고"
    text = (
        f"{drawn}, 도면 왼쪽 위에 동선 카드(시간표)를 열었습니다. 층 버튼의 주황색 숫자는 층별 결제 수입니다."
    )
    if len(stops) > 1 and journey.get("segments") == []:
        text += " 매장 사이 경로는 통로로 이어지지 않아 도면에 그리지 못했습니다."
    elif any(isinstance(s, dict) and s.get("straight") for s in segments):
        text += " 통로로 이어지지 않는 구간은 직선(추정)으로 그렸습니다."
    return text


def _route_shown(route: dict[str, Any], origin_floor: str | None) -> str:
    """What ui_show_route drew: a line only where the route has drawable segments (docs/specs/atlas-api.md §3)."""
    where = f"{floor_ko(origin_floor)} " if origin_floor else ""
    segments = route.get("segments")
    straight = bool(route.get("straight"))
    if segments is not None and not segments:
        parts = [
            "통로로 이어진 길을 찾지 못해 도면에 경로 선을 그리지 않았습니다.",
            "도면 왼쪽 위 길 찾기 상자에서 출발지와 도착지를 볼 수 있습니다.",
        ]
    elif straight:
        parts = [f"{where}도면에 통로로 이어진 길을 찾지 못해 직선(추정)으로 표시했습니다.", ROUTE_BOX]
    else:
        parts = [f"{where}도면에 경로를 파란 선으로 그렸습니다.", ROUTE_BOX]
    if route.get("walk_m") is None:
        parts.append("걷는 거리는 알 수 없습니다.")
    elif straight:
        parts.append("거리는 직선으로 잰 추정치입니다.")
    return " ".join(parts)


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
    """Executes client-side UI tools. Providers compute heatmap values and journeys deterministically.

    ``has_sales(rev)`` tells whether a revision has observed sales; without it heatmaps are not flagged as
    model estimates."""

    def __init__(
        self,
        heatmap_provider: HeatmapProvider | None = None,
        journey_provider: Callable[..., dict[str, Any]] | None = None,
        route_provider: Callable[..., dict[str, Any]] | None = None,
        flow_provider: Callable[..., dict[str, Any]] | None = None,
        has_sales: Callable[[str], bool] | None = None,
    ):
        self.heatmap_provider = heatmap_provider
        self.journey_provider = journey_provider
        self.route_provider = route_provider
        self.flow_provider = flow_provider
        self.has_sales = has_sales

    def handle(self, name: str, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        method = getattr(self, f"_{name}", None) if str(name).startswith("ui_") else None
        if method is None:
            return UiActionResult(
                json.dumps({"ok": False, "error": f"unknown ui tool {name}"}, ensure_ascii=False)
            )
        try:
            return method(args, ui_state)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:  # missing or malformed arguments
            error = f"인자가 잘못됐어요({type(exc).__name__}: {exc}). 함수 설명의 필수 인자를 넣어 다시 호출하세요."
            return UiActionResult(json.dumps({"ok": False, "error": error}, ensure_ascii=False))

    def _ui_set_floor(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        ui_state["floor"] = args["floor"]
        return UiActionResult(
            json.dumps({"ok": True, "floor": args["floor"]}), [_replace("/floor", args["floor"])]
        )

    def _estimated(self, metric: str, rev: str | None) -> bool:
        if metric not in SALES_METRICS or not rev or self.has_sales is None:
            return False
        try:
            return not self.has_sales(rev)
        except (ToolError, KeyError, ValueError):
            return False

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
        estimated = self._estimated(metric, rev)
        values = normalize(raw, invert=metric == "walk_to_escalator_m")
        label = METRIC_LABELS.get(metric, metric) + (ESTIMATE_SUFFIX if estimated else "")
        heatmap = {"metric": metric, "label": label, "rev": rev, "estimated": estimated, "values": values}
        if metric in METRIC_LEGENDS:
            heatmap["legend"] = METRIC_LEGENDS[metric]
        # The revision, the floor and the colors change together, so the values never color another revision.
        switched = bool(rev and ui_state.get("rev") and rev != ui_state.get("rev"))
        patches = _switch_rev(ui_state, rev, keep="heatmap")
        if floor and floor != ui_state.get("floor"):
            patches.append(_replace("/floor", floor))
            ui_state["floor"] = floor
        patches.append(_replace("/heatmap", heatmap))
        _drew(ui_state, "heatmap")
        patches += _clear(ui_state, PATHS)
        output: dict[str, Any] = {
            "ok": True,
            "metric": metric,
            "rev": rev,
            "floor": floor,
            "units": len(values),
        }
        if switched:
            output["rev_switched"] = True
        if estimated:
            output.update(estimated=True, note=ESTIMATE_NOTE)
        ranked = sorted((v, k) for k, v in raw.items() if v is not None and v == v)
        if metric == "low_efficiency":
            output["flagged_units"] = [k for v, k in ranked if v > 0]
        elif ranked:
            output["lowest_units"] = [k for _, k in ranked[:3]]
            output["highest_units"] = [k for _, k in ranked[-3:][::-1]]
        where = " ".join(
            part for part in (f"{rev} 개정" if rev else "", floor_ko(floor) if floor else "") if part
        )
        shown = [f"{where + ' ' if where else ''}도면을 '{label}' 기준으로 색칠했습니다."]
        if switched:
            shown.append(f"화면도 {rev} 개정으로 바꿨습니다.")
        if estimated:
            shown.append("이 개정은 실적이 없어 색은 모델 추정치입니다.")
        output["shown"] = " ".join(shown)
        return UiActionResult(json.dumps(output, ensure_ascii=False), patches)

    def _ui_show_journey(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        if self.journey_provider is None:
            return UiActionResult(json.dumps({"ok": False, "error": "journey provider unavailable"}))
        params = {k: args[k] for k in JOURNEY_ARGS if args.get(k) not in (None, "", {})}
        if not (params.get("customer_id") or params.get("date")) and ui_state.get("rev"):
            # Pick a day when the revision on screen was the store layout, so stops match the drawing.
            params["rev"] = ui_state["rev"]
        try:
            journey = self.journey_provider(**params)
        except (ToolError, KeyError, ValueError) as exc:  # let the model see the reason and recover
            return UiActionResult(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        # The path stands alone: overlays from earlier answers would hide it.
        patches = _clear(ui_state, (*MARKS, "route", "flow"))
        patches.append(_replace("/journey", journey))
        shown = []
        journey_rev = journey.get("rev")
        if journey_rev and journey_rev != ui_state.get("rev"):
            patches += _switch_rev(ui_state, journey_rev, keep="journey")
            shown.append(f"구매 날짜에 맞춰 도면을 {journey_rev} 개정으로 바꿨습니다.")
        _drew(ui_state, "journey")
        first_floor = next((stop["floor"] for stop in journey.get("stops", []) if stop.get("floor")), None)
        if first_floor:
            patches.append(_replace("/floor", first_floor))
            ui_state["floor"] = first_floor
        patches.append(_replace("/panel", {"type": "journey", "payload": {}}))
        shown.insert(0, _journey_shown(journey, first_floor))
        output = {
            "ok": True,
            "customer": journey.get("customer"),
            "date": journey.get("date"),
            "weekday": journey.get("weekday"),
            "rev": journey_rev,
            "stops": [{k: stop.get(k) for k in JOURNEY_STOP_FIELDS} for stop in journey.get("stops", [])],
            "transitions": journey.get("transitions", []),
            "summary": journey.get("summary"),
            "caveat": journey.get("caveat"),
            "shown": " ".join(shown),
        }
        return UiActionResult(json.dumps(output, ensure_ascii=False, default=str), patches)

    def _ui_show_route(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        if self.route_provider is None:
            return UiActionResult(json.dumps({"ok": False, "error": "route provider unavailable"}))
        params = {k: args[k] for k in ROUTE_ARGS if args.get(k) not in (None, "")}
        try:
            route = self.route_provider(rev=ui_state.get("rev"), **params)
        except (
            ToolError,
            KeyError,
            ValueError,
            TypeError,
        ) as exc:  # let the model see the reason and recover
            return UiActionResult(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        patches = _clear(ui_state, (*MARKS, "journey", "flow"))
        patches.append(_replace("/route", route))
        _drew(ui_state, "route")
        origin_floor = (route.get("origin") or {}).get("floor")
        if origin_floor and origin_floor != ui_state.get("floor"):
            patches.append(_replace("/floor", origin_floor))
            ui_state["floor"] = origin_floor
        output = {
            "ok": True,
            "title": route.get("title"),
            "origin": {k: (route.get("origin") or {}).get(k) for k in ("label", "floor")},
            "destination": {k: (route.get("destination") or {}).get(k) for k in ("label", "floor")},
            "walk_m": route.get("walk_m"),
            "legs": route.get("legs", []),
            "transitions": route.get("transitions", []),
            "alternatives": route.get("alternatives", []),
            "caveat": route.get("caveat"),
            "shown": _route_shown(route, origin_floor),
        }
        if route.get("straight"):
            output["straight"] = True
        return UiActionResult(json.dumps(output, ensure_ascii=False, default=str), patches)

    def _ui_show_flow(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        if self.flow_provider is None:
            return UiActionResult(json.dumps({"ok": False, "error": "flow provider unavailable"}))
        params = {k: args[k] for k in FLOW_ARGS if args.get(k) not in (None, "", {}, [])}
        try:
            flow = self.flow_provider(rev=ui_state.get("rev"), **params)
        except (ToolError, KeyError, ValueError, TypeError) as exc:  # let the model see the reason
            return UiActionResult(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        patches = _clear(ui_state, (*MARKS, "journey", "route"))
        patches.append(_replace("/flow", flow))
        shown = []
        flow_rev = flow.get("rev")
        if flow_rev and flow_rev != ui_state.get("rev"):
            # Traffic needs purchases made on that layout: hypothetical or future revisions use their base.
            patches += _switch_rev(ui_state, flow_rev, keep="flow")
            shown.append(f"실적이 있는 {flow_rev} 개정 도면으로 바꿔 표시했습니다.")
        _drew(ui_state, "flow")
        floors = flow.get("floors") or {}
        markers = flow.get("markers") or []
        target = (flow.get("query") or {}).get("floor") or params.get("floor")
        if not target and markers:
            target = markers[0].get("floor")
        if not target and floors:
            target = max(floors, key=lambda f: (floors[f] or {}).get("total") or 0)
        if target and target != ui_state.get("floor"):
            patches.append(_replace("/floor", target))
            ui_state["floor"] = target
        mode = flow.get("mode") or params.get("mode")
        output = {
            "ok": True,
            "mode": mode,
            "title": flow.get("title"),
            "summary": flow.get("summary"),
            "markers": [
                {
                    k: m.get(k)
                    for k in ("rank", "floor", "kind", "title", "unit_code", "per_day", "reasons")
                    if m.get(k) is not None
                }
                for m in markers[:10]
            ],
            "legend": flow.get("legend"),
            "caveat": flow.get("caveat"),
            "shown": " ".join(
                [(f"{floor_ko(target)} " if target else "") + FLOW_SHOWN.get(str(mode), ""), *shown]
            ),
        }
        return UiActionResult(json.dumps(output, ensure_ascii=False, default=str), patches)

    def _ui_highlight_units(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        color = args.get("color") or "#e53935"
        highlights = [
            {"unit_code": code, "color": color, "reason": args.get("reason", "")}
            for code in args["unit_codes"]
        ]
        patches = [_replace("/highlights", highlights)]
        _drew(ui_state, "highlights")
        patches += _clear(ui_state, PATHS)
        return UiActionResult(json.dumps({"ok": True, "highlighted": len(highlights)}), patches)

    def _ui_draw_edges(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        edges = [
            {"from": e["from"], "to": e["to"], "weight": e.get("weight", 1.0), "label": e.get("label", "")}
            for e in args["edges"]
        ]
        patches = [_replace("/edges", edges)]
        _drew(ui_state, "edges")
        patches += _clear(ui_state, PATHS)
        return UiActionResult(json.dumps({"ok": True, "edges": len(edges)}), patches)

    def _ui_focus(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        return UiActionResult(
            json.dumps({"ok": True}), [_replace("/focus", {"unit_codes": list(args["unit_codes"])})]
        )

    def _ui_compare(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        patches = _switch_rev(ui_state, args["rev_to"], keep="compare")
        patches.append(_replace("/compareRev", args["rev_from"]))
        ui_state["compareRev"] = args["rev_from"]
        _drew(ui_state, "compare")
        patches += _clear(ui_state, PATHS)
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
        output["shown"] = (
            RELAYOUT_PLACE
            if panel_type == "layout" and payload.get("brief_id")
            else PANEL_PLACES.get(panel_type, "")
        )
        return UiActionResult(json.dumps(output, ensure_ascii=False), [_replace("/panel", panel)])

    def _ui_clear(self, args: dict[str, Any], ui_state: dict[str, Any]) -> UiActionResult:
        what = args["what"]
        names = [name for name in OVERLAYS if name != "compare"] if what == "all" else [what]
        if any(name not in OVERLAYS for name in names):
            return UiActionResult(json.dumps({"ok": False, "error": f"unknown overlay {what}"}))
        patches = []
        for name in names:
            key, empty = OVERLAYS[name]
            patches.append(_replace(f"/{key}", empty))
            drawn = _bookkeeping(ui_state, DRAWN)
            if name in drawn:
                drawn.remove(name)
            _bookkeeping(ui_state, CLEARED).append(name)
        if what == "all":
            patches.append(_replace("/focus", None))
        return UiActionResult(json.dumps({"ok": True, "cleared": what}), patches)
