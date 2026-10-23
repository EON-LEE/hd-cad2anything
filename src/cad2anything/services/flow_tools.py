"""Flow maps, popup spots and corridor checks for the Toolkit (docs/specs/atlas-api.md §1, §3 ``flow``).

- ``analytics_flow_map``: estimated foot traffic of a customer group on the corridors ("VIP가 많이 지나는 통로").
  Consecutive purchases of one customer on one day are joined by the shortest walk along the corridor
  centerlines (``cad2anything.spatial.flows``) and added up. Values are full-scale estimates: each sampled
  trip counts with its sampling weight, like receipts in the other analytics.
- ``spatial_popup_candidates``: vacant units, popup units and open corridor spots where a booth fits, ranked by
  the estimated traffic passing them in a time window, the view from escalator landings and, optionally, the
  cross-shopping lift of nearby categories with a given category.
- ``spatial_egress_check``: clear corridor width and the walk to stairs or building entrances against review
  values (examples, not legal criteria).
- ``flow_object``: the ``flow`` UI state object of the three modes.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point

from cad2anything.catalog import CATEGORIES
from cad2anything.model import RevisionKind
from cad2anything.services.analytics_tools import _floor_code, _json, _parse_date, parse_segment
from cad2anything.services.errors import NotFoundToolError, ToolError
from cad2anything.spatial.egress import (
    DEFAULT_MAX_TRAVEL_M,
    DEFAULT_MIN_WIDTH_M,
    exit_distances,
    narrow_stretches,
    width_stats,
)
from cad2anything.spatial.features import floor_codes
from cad2anything.spatial.flows import (
    MAX_SEGMENTS,
    FlowMap,
    FlowRouter,
    booth_spots,
    build_corridor_graph,
    purchase_trips,
)

FLOW_MODES = ("traffic", "popup", "egress")
DAY_TYPES = {
    "all": "all",
    "전체": "all",
    "weekday": "weekday",
    "weekdays": "weekday",
    "평일": "weekday",
    "weekend": "weekend",
    "weekends": "weekend",
    "주말": "weekend",
}
DAY_TEXT = {"all": "", "weekday": "평일", "weekend": "주말"}
DEFAULT_TOP = 10
MAX_TOP = 50
SUMMARY_TOP = 5  # busiest corridors and units named in the UI flow object's summary
POPUP_CLEAR_M = 2.4  # walkway kept free on every side of a booth
POPUP_KEEP_M = 5.0  # booth distance from escalator landings, stairs, elevators and entrances
SPOT_REACH_M = 6.0  # a spot counts the corridor traffic within this distance in clear sight
POPUP_SPACING_M = 10.0
SPOTS_PER_FLOOR = 3
VIEW_MAX_M = 40.0
AFFINITY_M = 15.0
AFFINITY_MIN_LIFT = 1.1
AFFINITY_MIN_CUSTOMERS = 20
MAX_WIDTH_M = 20.0
MAX_TRAVEL_M = 500.0
WARNING_COLOR = "#e53935"
POPUP_BAND = re.compile(r"^[^-]+-P\d", re.IGNORECASE)
HOURS_TEXT = re.compile(r"^\s*(\d{1,2})(?::00)?\s*시?\s*(?:[-~–]\s*(\d{1,2})(?::00)?\s*시?)?\s*$")
TRAFFIC_LEGEND_NOTE = "결제 순서를 통로망 최단 경로로 이어 모은 값"
TRAFFIC_CAVEAT = (
    "결제 순서를 통로망 최단 경로로 이은 추정이에요. 실제로 걸은 길이 아니에요. "
    "같은 날 같은 고객(가명 회원)의 연속 결제만 이었고, 표본 가중치를 곱해 전체 규모로 환산했어요. "
    "구매 없이 지나간 고객과 비회원 결제는 빠져 있고, 층을 오가는 이동은 에스컬레이터로 가정했어요."
)
POPUP_CAVEAT = (
    "추정 유동으로 고른 검토용 후보예요. 피난 통로, 임대 조건, 시설·소방 기준은 따로 확인해야 해요. "
    "통로 위 후보는 부스 둘레에 2.4m 이상 지나갈 공간이 남고 에스컬레이터 승강장·계단·출입구에서 5m 넘게 "
    "떨어진 곳만 골랐어요."
)
EGRESS_CAVEATS = [
    "법규 인증이 아니라 검토용 기준이에요. 기준값은 바꿀 수 있어요.",
    "통로 폭은 도면의 통로 경계(매장 전면, 벽, 에스컬레이터)로 잰 값이에요. 집기나 임시 매대는 모델에 있을 때만 반영돼요.",
    "걷는 거리는 매장 출입구(통로와 맞닿은 면의 가운데)에서 같은 층의 가장 가까운 계단이나 건물 출입구까지 통로를 따라 "
    "잰 값이에요. 매장 안에서 걷는 거리는 넣지 않았고, 엘리베이터·에스컬레이터는 피난 경로로 보지 않았어요.",
]


# --- arguments --------------------------------------------------------------------------------------


def _day_type(value: Any, default: str) -> str:
    if value in (None, ""):
        return default
    key = str(value).strip().lower()
    if key not in DAY_TYPES:
        raise ToolError("day_type은 all(전체), weekday(평일), weekend(주말) 중 하나예요.")
    return DAY_TYPES[key]


def _hours(value: Any) -> tuple[int, int] | None:
    """``"10-14"``, ``"10~14시"``, ``"10:00-14:00"``, ``[10, 14]`` or one hour ``"12"`` -> [start, end)."""
    if value is None or (isinstance(value, str | list | tuple) and not value):
        return None
    if isinstance(value, list | tuple):
        if len(value) != 2:
            raise ToolError("hours는 [시작, 끝] 두 값이에요. 예: [10, 14]")
        low, high = value
    else:
        match = HOURS_TEXT.match(str(value))
        if not match:
            raise ToolError("hours는 '10-14'처럼 시작·끝 시각으로 주세요(끝 시각은 포함하지 않아요).")
        low = match.group(1)
        high = match.group(2) if match.group(2) is not None else int(match.group(1)) + 1
    try:
        low, high = int(low), int(high)
    except (TypeError, ValueError) as exc:
        raise ToolError("hours는 '10-14'처럼 시작·끝 시각으로 주세요.") from exc
    if not 0 <= low < high <= 24:
        raise ToolError("hours는 0~24시 사이이고 시작이 끝보다 빨라야 해요. 예: 10-14")
    return low, high


def _limit(value: Any, name: str, default: float, upper: float) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name}는 숫자여야 해요: {value}") from exc
    if not math.isfinite(number) or not 0 < number <= upper:
        raise ToolError(f"{name}는 0보다 크고 {upper:g} 이하여야 해요: {value}")
    return number


def _top(value: Any, default: int) -> int:
    try:
        return max(1, min(int(value if value is not None else default), MAX_TOP))
    except (TypeError, ValueError) as exc:
        raise ToolError(f"top은 정수여야 해요: {value}") from exc


def window_text(day_type: str, hours: tuple[int, int] | None) -> str:
    """Korean time window such as "주말 10~14시" (empty for every day and hour)."""
    parts = [DAY_TEXT.get(day_type, ""), f"{hours[0]}~{hours[1]}시" if hours else ""]
    return " ".join(part for part in parts if part)


def segment_text(segment: dict[str, Any] | None) -> str:
    """Korean customer group such as "40대 여성 VIP·VVIP" ("전체" without a segment)."""
    from cad2anything.analytics.journey import AGE_LABELS, GENDER_LABELS, TIER_LABELS

    if not segment:
        return "전체"
    parts = []
    for key, labels in (("age_band", AGE_LABELS), ("gender", GENDER_LABELS), ("tier", TIER_LABELS)):
        if key in segment:
            values = segment[key] if isinstance(segment[key], list) else [segment[key]]
            parts.append("·".join(labels.get(str(v), str(v)) for v in values))
    if "home_area" in segment:
        values = segment["home_area"] if isinstance(segment["home_area"], list) else [segment["home_area"]]
        parts.append(f"{'·'.join(str(v) for v in values)} 거주")
    return " ".join(parts) or "전체"


def _final(word: str) -> bool:
    """True when the last syllable of a Korean word ends with a consonant (받침)."""
    last = word.strip()[-1:]
    return bool(last) and "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0


def _josa(word: str, with_final: str, without_final: str) -> str:
    """Word plus the particle that fits it: 과/와, 이/가..."""
    return f"{word}{with_final if _final(word) else without_final}"


def _unit_code(entity: Any) -> str:
    return str(entity.attributes.get("unit_code") or entity.entity_id)


def _vacant(attributes: dict[str, Any]) -> bool:
    value = attributes.get("vacant")
    return bool(value) if value is not None else not attributes.get("brand_id")


def _category_name(code: Any) -> str:
    return CATEGORIES[code].name_ko if code in CATEGORIES else str(code or "")


def _middle(points: list[tuple[float, float]]) -> tuple[float, float]:
    point = LineString(points).interpolate(0.5, normalized=True) if len(points) > 1 else Point(points[0])
    return round(float(point.x), 2), round(float(point.y), 2)


@dataclass
class FlowQuery:
    """Validated flow arguments with the revision and period they resolve to."""

    rev: str
    start: date
    end: date
    segment: dict[str, Any] | None
    day_type: str
    hours: tuple[int, int] | None
    floor: str | None
    notes: list[str] = field(default_factory=list)

    def echo(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "day_type": self.day_type,
            "hours": list(self.hours) if self.hours else None,
            "floor": self.floor,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass
class FlowState:
    """Routed trips of one revision, period, customer group and time window (shared by every floor)."""

    router: FlowRouter
    flows: FlowMap
    days: int  # store days in the period that match the day type
    trips: float
    sample_trips: int
    customer_days: float
    sample_customer_days: int
    floor_customer_days: dict[str, float]
    pass_by: dict[str, float]

    def per_day(self, value: float) -> float:
        return value / self.days if self.days else 0.0


class FlowTools:
    """Mixin for Toolkit. Needs the AnalyticsTools helpers (``_memo``, ``_data_key``, ``_model_key``,
    ``_enriched``, ``_journey_rows``, ``_data_range``, ``_table``, ``revision_period``, ``latest_sales_rev``,
    ``_has_sales``, ``_revision_on``, ``data_through``, ``_route_planner``, ``_pairs``, ``_resolve_category``)
    and ``_model``."""

    ws: Any

    # Revisions, periods and arguments --------------------------------------------------------------
    def _known_rev(self, rev: Any) -> str:
        text = str(rev or "").strip()
        if not text or not self.ws.has_model(text):
            known = ", ".join(r.rev_id for r in self.ws.revisions())
            raise NotFoundToolError(f"개정 {text}가 없습니다. 사용 가능한 개정: {known}")
        return text

    def _sales_rev(self, rev: Any) -> str:
        """An actual revision with transactions: flows need purchases made on that layout."""
        rev = self._known_rev(rev)
        revision = self._model(rev).revision
        if revision.kind != RevisionKind.ACTUAL:
            raise ToolError(
                f"{rev}는 가상 개정이라 그 배치에서 쌓인 거래가 없어 유동을 추정할 수 없어요. "
                f"실적이 있는 실제 개정({self.latest_sales_rev()})으로 보거나 rev를 비워 주세요."
            )
        if not self._has_sales(rev):
            raise ToolError(
                f"{rev}는 {revision.effective_date}부터 적용되는 개정이라 아직 거래 실적이 없어요"
                f"(데이터 {self.data_through()}까지). 유동은 실적이 있는 개정({self.latest_sales_rev()})으로 "
                "볼 수 있어요."
            )
        return rev

    def _flow_router(self, rev: str) -> FlowRouter:
        def compute() -> FlowRouter:
            model = self._model(rev)
            planner = self._route_planner(rev)
            return FlowRouter(model, {floor: planner.network(floor) for floor in floor_codes(model)})

        return self._memo(("flow_router", rev, self._model_key(rev)), compute)

    def _flow_query(
        self,
        rev: Any,
        segment: Any,
        floor: Any,
        day_type: Any,
        hours: Any,
        start: Any = None,
        end: Any = None,
        default_day: str = "all",
        fallback: bool = False,
    ) -> FlowQuery:
        """Validated arguments; the revision defaults to the layout in effect for the period, and the period
        is clipped to the days that layout was in use. With ``fallback`` a revision without transactions
        (hypothetical or not yet in effect) is replaced by its actual base revision instead of an error."""
        parsed = parse_segment(segment)
        day = _day_type(day_type, default_day)
        window = _hours(hours)
        first, last = _parse_date(start, "start"), _parse_date(end, "end", end=True)
        notes = []
        if rev and fallback:
            asked = self._known_rev(rev)
            rev = self._fit_revision(asked)
            if rev != asked:
                notes.append(f"{asked}에는 거래 실적이 없어 기준 실제 개정 {rev}의 배치로 추정했어요.")
        elif rev:
            rev = self._sales_rev(rev)
        elif first or last:
            data_first, data_last = self._data_range()
            rev = self._revision_on(max(min(last or data_last, data_last), data_first))
        else:
            rev = self.latest_sales_rev()
        rev_start, rev_end, _ = self.revision_period(rev)
        layout_start, layout_end = date.fromisoformat(rev_start), date.fromisoformat(rev_end)
        start_day = max(first or layout_start, layout_start)
        end_day = min(last or layout_end, layout_end)
        if start_day > end_day:
            raise ToolError(
                f"요청한 기간에는 {rev} 도면이 쓰이지 않았어요({rev} 적용 기간 {rev_start}~{rev_end}). "
                "rev나 기간을 바꿔 주세요."
            )
        if (first and first < start_day) or (last and last > end_day):
            notes.append(f"요청 기간 중 {rev} 도면이 쓰인 {start_day}~{end_day}만 집계했어요.")
        code = None
        if floor:
            code = _floor_code(floor)
            if code not in self._flow_router(rev).graphs:
                raise NotFoundToolError(f"{rev}에 {code} 통로가 없습니다.")
        return FlowQuery(rev, start_day, end_day, parsed, day, window, code, notes)

    def _segment_members(self, segment: dict[str, Any]) -> set[str]:
        from cad2anything.analytics.prep import segment_customers

        try:
            members = segment_customers(self._table("customers", required=False), segment)
        except ValueError as exc:
            raise ToolError(f"고객군으로 거를 수 없어요: {exc}") from exc
        return members or set()

    def _open_days(self, start: date, end: date, day_type: str) -> int:
        def compute() -> int:
            dates = self._enriched()["date"]
            inside = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
            days = pd.DatetimeIndex(inside.unique())
            if day_type != "all":
                days = days[(days.dayofweek >= 5) == (day_type == "weekend")]
            return int(len(days))

        return self._memo(
            ("flow_days", self._data_key(), start.isoformat(), end.isoformat(), day_type), compute
        )

    def _flow_state(self, query: FlowQuery) -> FlowState:
        key = (
            "flow_state",
            query.rev,
            self._model_key(query.rev),
            self._data_key(),
            query.start.isoformat(),
            query.end.isoformat(),
            json.dumps(query.segment, sort_keys=True),
            query.day_type,
            query.hours,
        )

        def compute() -> FlowState:
            rows = self._journey_rows()
            ts = rows["ts"]
            mask = (ts >= pd.Timestamp(query.start)) & (ts < pd.Timestamp(query.end) + pd.Timedelta(days=1))
            if query.segment:
                mask &= rows["customer_key"].isin(self._segment_members(query.segment))
            if query.day_type != "all":
                mask &= (rows["weekday"] >= 5) == (query.day_type == "weekend")
            if query.hours:
                mask &= (rows["hour"] >= query.hours[0]) & (rows["hour"] < query.hours[1])
            trips = purchase_trips(rows.loc[mask])
            router = self._flow_router(query.rev)
            pairs = trips.groupby(["unit_a", "unit_b"], sort=True, as_index=False)["weight"].sum()
            flows = router.route(pairs)
            done = trips.merge(pairs.loc[flows.routed_rows, ["unit_a", "unit_b"]], on=["unit_a", "unit_b"])
            by_day = done.groupby(["customer_key", "day"], sort=False)["weight"].mean()
            floor_of = {code: floor for code, (floor, _) in router.unit_node.items()}
            touched = pd.concat(
                [
                    done.assign(floor=done["unit_a"].map(floor_of)),
                    done.assign(floor=done["unit_b"].map(floor_of)),
                ]
            )
            by_floor = (
                touched.drop_duplicates(["customer_key", "day", "floor"]).groupby("floor")["weight"].sum()
            )
            pass_by: dict[str, float] = {}
            for floor, graph in router.graphs.items():
                pass_by.update(graph.pass_by(flows.edge_flow[floor]))
            return FlowState(
                router=router,
                flows=flows,
                days=self._open_days(query.start, query.end, query.day_type),
                trips=float(done["weight"].sum()),
                sample_trips=int(len(done)),
                customer_days=float(by_day.sum()),
                sample_customer_days=int(len(by_day)),
                floor_customer_days={str(k): float(v) for k, v in by_floor.items()},
                pass_by=pass_by,
            )

        return self._memo(key, compute)

    # Traffic ---------------------------------------------------------------------------------------
    @staticmethod
    def _flow_floors(query: FlowQuery, state: FlowState) -> list[str]:
        if query.floor:
            return [query.floor]
        return [floor for floor in state.router.graphs if state.flows.legs.get(floor, 0.0) > 0]

    def _traffic_layers(self, query: FlowQuery, state: FlowState) -> dict[str, Any]:
        layers: dict[str, Any] = {}
        for floor in self._flow_floors(query, state):
            graph = state.router.graphs[floor]
            segments = graph.segments(state.flows.edge_flow[floor])
            peak = max((value for _, value in segments), default=0.0)
            layers[floor] = {
                "segments": [
                    {
                        "points": [list(point) for point in points],
                        "weight": round(value / peak, 3) if peak > 0 else 0.0,
                        "value": int(round(value)),
                        "color": None,
                        "label": graph.edge_label(points),
                    }
                    for points, value in segments
                ],
                "max_value": int(round(peak)),
                "total": int(round(state.flows.legs.get(floor, 0.0))),
            }
        return layers

    def _corridors(self, query: FlowQuery, state: FlowState) -> list[dict[str, Any]]:
        """Busiest corridor stretches (between junctions), one per label and floor."""
        found: list[dict[str, Any]] = []
        for floor in self._flow_floors(query, state):
            graph = state.router.graphs[floor]
            walks = state.flows.legs.get(floor, 0.0)
            labels: set[str] = set()
            for group, value, peak in graph.runs(state.flows.edge_flow[floor]):
                points = graph.edges[peak].points
                label = graph.edge_label(points)
                if label in labels:
                    continue
                labels.add(label)
                x, y = _middle(points)
                found.append(
                    {
                        "floor": floor,
                        "label": label,
                        "value": int(round(value)),
                        "per_day": round(state.per_day(value), 1),
                        "share_of_floor": round(value / walks, 3) if walks else None,
                        "length_m": round(sum(graph.edges[e].length for e in group), 1),
                        "x": x,
                        "y": y,
                    }
                )
        found.sort(key=lambda row: (-row["value"], row["floor"], row["label"]))
        return found

    def _passing_units(self, query: FlowQuery, state: FlowState) -> list[dict[str, Any]]:
        floors = set(self._flow_floors(query, state))
        rows: list[dict[str, Any]] = []
        for floor, graph in state.router.graphs.items():
            if floor not in floors:
                continue
            for unit in graph.units:
                code = _unit_code(unit)
                value = state.pass_by.get(code, 0.0)
                if value <= 0:
                    continue
                vacant = _vacant(unit.attributes)
                rows.append(
                    {
                        "unit_code": code,
                        "floor": floor,
                        "brand_name": None if vacant else unit.attributes.get("brand_name"),
                        "category_name": "공실"
                        if vacant
                        else _category_name(unit.attributes.get("category")),
                        "value": int(round(value)),
                        "per_day": round(state.per_day(value), 1),
                    }
                )
        rows.sort(key=lambda row: (-row["value"], row["unit_code"]))
        return rows

    def _flow_notes(self, query: FlowQuery, state: FlowState) -> list[str]:
        notes = list(query.notes)
        if state.trips <= 0:
            notes.append("조건에 맞는 고객 이동이 없어요. 고객군·요일·시간 조건을 넓혀 보세요.")
        if state.flows.unrouted > 0:
            notes.append(
                f"도면에서 찾지 못했거나 통로로 이어지지 않는 이동 약 {state.flows.unrouted:,.0f}건(추정)은 뺐어요."
            )
        return notes

    def analytics_flow_map(
        self,
        rev: str | None = None,
        segment: str | dict[str, Any] | None = None,
        floor: str | None = None,
        day_type: str = "all",
        hours: str | None = None,
        start: str | None = None,
        end: str | None = None,
        top: int = DEFAULT_TOP,
    ) -> dict[str, Any]:
        """Estimated traffic of a customer group on the corridors: busiest corridors and passing units."""
        query = self._flow_query(rev, segment, floor, day_type, hours, start, end)
        state = self._flow_state(query)
        k = _top(top, DEFAULT_TOP)
        corridors = self._corridors(query, state)
        floors = []
        for code in self._flow_floors(query, state):
            busiest = next((row for row in corridors if row["floor"] == code), None)
            floors.append(
                {
                    "floor": code,
                    "walks": int(round(state.flows.legs.get(code, 0.0))),
                    "customer_days": int(round(state.floor_customer_days.get(code, 0.0))),
                    "busiest": busiest["label"] if busiest else None,
                    "busiest_value": busiest["value"] if busiest else None,
                }
            )
        notes = self._flow_notes(query, state)
        notes.append(
            "value는 기간 합계, per_day는 해당 요일 영업일 하루 평균이에요. share_of_floor는 그 층을 걸은 "
            "이동 중 그 통로를 지난 비율이에요. walks는 층별 이동 수예요(층을 오가는 이동은 두 층에 셉니다)."
        )
        return _json(
            {
                "rev": query.rev,
                "period": {"start": query.start, "end": query.end, "days": state.days},
                "query": {**query.echo(), "segment_label": segment_text(query.segment)},
                "label": "추정치",
                "customer_days": int(round(state.customer_days)),
                "trips": int(round(state.trips)),
                "sample": {"customer_days": state.sample_customer_days, "trips": state.sample_trips},
                "floors": floors,
                "top_corridors": [{"rank": i, **row} for i, row in enumerate(corridors[:k], 1)],
                "top_units": [
                    {"rank": i, **row} for i, row in enumerate(self._passing_units(query, state)[:k], 1)
                ],
                "notes": notes,
                "caveat": TRAFFIC_CAVEAT,
            }
        )

    # Popup spots -----------------------------------------------------------------------------------
    def _booth_spots(self, rev: str, side: float) -> dict[str, pd.DataFrame]:
        def compute() -> dict[str, pd.DataFrame]:
            router = self._flow_router(rev)
            return {
                floor: booth_spots(graph, side, POPUP_CLEAR_M, POPUP_KEEP_M)
                for floor, graph in router.graphs.items()
            }

        return self._memo(("booth_spots", rev, self._model_key(rev), round(side, 3)), compute)

    def _lifts(self, focal: str, query: FlowQuery) -> dict[str, float]:
        """Cross-shopping lift of every category with ``focal`` (same customer, same day; whole period)."""
        try:
            pairs = self._pairs(
                "category",
                None,
                query.start.isoformat(),
                query.end.isoformat(),
                "customer_day",
                AFFINITY_MIN_CUSTOMERS,
            )
        except ToolError:
            return {}
        lifts: dict[str, float] = {}
        for a, b, lift in zip(pairs["item_a"], pairs["item_b"], pairs["lift"], strict=True):
            if str(a) == focal:
                lifts[str(b)] = float(lift)
            elif str(b) == focal:
                lifts[str(a)] = float(lift)
        return lifts

    def _popup_candidates(
        self, query: FlowQuery, state: FlowState, side: float, area_min: float
    ) -> list[dict[str, Any]]:
        """Vacant and popup units plus open corridor spots with the traffic passing them."""
        spots_by_floor = self._booth_spots(query.rev, side)
        candidates: list[dict[str, Any]] = []
        for floor in [query.floor] if query.floor else list(state.router.graphs):
            graph = state.router.graphs[floor]
            for unit in graph.units:
                code = _unit_code(unit)
                attrs = unit.attributes
                popup = str(attrs.get("band") or "").upper() == "P" or bool(POPUP_BAND.match(code))
                vacant = _vacant(attrs)
                if not (vacant or popup) or unit.geometry.area + 1e-9 < area_min:
                    continue
                nodes = graph.access.get(unit.entity_id)
                x, y = (
                    graph.node_xy[nodes[0]] if nodes else (unit.geometry.centroid.x, unit.geometry.centroid.y)
                )
                candidates.append(
                    {
                        "kind": "unit",
                        "floor": floor,
                        "unit_code": code,
                        "x": float(x),
                        "y": float(y),
                        "area_m2": round(float(unit.geometry.area), 1),
                        "vacant": vacant,
                        "popup_unit": popup,
                        "brand_name": None if vacant else attrs.get("brand_name"),
                        "passers": state.pass_by.get(code, 0.0),
                        "entity_id": unit.entity_id,
                    }
                )
            spots = spots_by_floor.get(floor)
            if spots is None or spots.empty:
                continue
            xy = spots[["x", "y"]].to_numpy(dtype=float)
            passers = graph.nearby_max(xy, graph.cell_flow(state.flows.edge_flow[floor]), SPOT_REACH_M)
            chosen: list[int] = []
            for k in sorted(range(len(xy)), key=lambda k: (-passers[k], xy[k, 0], xy[k, 1])):
                if passers[k] <= 0 or len(chosen) >= SPOTS_PER_FLOOR:
                    break
                if all(math.dist(xy[k], xy[j]) >= POPUP_SPACING_M for j in chosen):
                    chosen.append(k)
            for k in chosen:
                candidates.append(
                    {
                        "kind": "spot",
                        "floor": floor,
                        "unit_code": None,
                        "x": float(xy[k, 0]),
                        "y": float(xy[k, 1]),
                        "clear_width_m": round(float(spots["open_m"].iloc[k]), 1),
                        "booth_m2": round(side * side, 1),
                        "passers": float(passers[k]),
                    }
                )
        return candidates

    def _popup(
        self, query: FlowQuery, state: FlowState, category: Any, min_area_m2: Any, top: Any
    ) -> dict[str, Any]:
        area_min = _limit(min_area_m2, "min_area_m2", 9.0, 400.0)
        side = math.sqrt(area_min)
        focal = self._resolve_category(category) if category else None
        lifts = self._lifts(focal, query) if focal else {}
        candidates = self._popup_candidates(query, state, side, area_min)
        for floor in sorted({c["floor"] for c in candidates}):
            graph = state.router.graphs[floor]
            members = [c for c in candidates if c["floor"] == floor]
            views = graph.landing_view(np.array([[c["x"], c["y"]] for c in members]), VIEW_MAX_M)
            for candidate, view in zip(members, views, strict=True):
                candidate["view"] = view
                candidate["near"] = graph.label_at(candidate["x"], candidate["y"])
                candidate["affinity"] = None
                if not lifts:
                    continue
                point = Point(candidate["x"], candidate["y"])
                for unit in graph.units:
                    group = unit.attributes.get("category")
                    if (
                        unit.entity_id == candidate.get("entity_id")
                        or _vacant(unit.attributes)
                        or group == focal
                        or lifts.get(group, 0.0) < AFFINITY_MIN_LIFT
                        or unit.geometry.distance(point) > AFFINITY_M
                    ):
                        continue
                    if candidate["affinity"] is None or lifts[group] > candidate["affinity"][0]:
                        candidate["affinity"] = (lifts[group], group, _unit_code(unit))
        peak = max((c["passers"] for c in candidates), default=0.0)
        lift_peak = max((c["affinity"][0] - 1.0 for c in candidates if c.get("affinity")), default=0.0)
        for c in candidates:
            traffic = c["passers"] / peak if peak > 0 else 0.0
            sight = 1.0 - min(c["view"][0], VIEW_MAX_M) / VIEW_MAX_M if c.get("view") else 0.0
            if focal:
                fit = (c["affinity"][0] - 1.0) / lift_peak if c["affinity"] and lift_peak > 0 else 0.0
                c["score"] = traffic * (0.7 + 0.2 * sight + 0.1 * fit)
            else:
                c["score"] = traffic * (0.75 + 0.25 * sight)
        candidates.sort(
            key=lambda c: (-c["score"], -c["passers"], c["floor"], c.get("unit_code") or "", c["x"], c["y"])
        )
        traffic_rank = {
            id(c): rank for rank, c in enumerate(sorted(candidates, key=lambda c: -c["passers"]), 1)
        }
        chosen = candidates[: _top(top, 5)]
        notes = self._flow_notes(query, state)
        if len(chosen) >= 3:
            for kind, text in (("unit", "공실·팝업 매장"), ("spot", "통로 위 공간")):
                if not any(c["kind"] == kind for c in chosen):
                    best = next((c for c in candidates if c["kind"] == kind), None)
                    if best is not None:
                        chosen[-1] = best
                        notes.append(
                            f"{text} 후보가 빠지지 않게 점수가 가장 높은 {text} 하나를 마지막 순위에 넣었어요."
                        )
        window = window_text(query.day_type, query.hours)
        focal_name = _category_name(focal) if focal else ""
        ranked = []
        for rank, c in enumerate(chosen, 1):
            per_day = state.per_day(c["passers"])
            amount = f"{per_day:,.0f}" if per_day >= 10 else f"{per_day:.1f}"
            reasons = [
                f"{window + ' ' if window else ''}하루 평균 약 {amount}명(추정)이 앞을 지나요"
                f"(후보 중 유동 {traffic_rank[id(c)]}위)."
                if c["passers"] > 0
                else "이 조건에서는 앞을 지나는 추정 유동이 없어요."
            ]
            if c.get("view"):
                reasons.append(f"{c['view'][1]} 승강장에서 {c['view'][0]:.0f}m 거리로 가림 없이 보여요.")
            if c["kind"] == "spot":
                reasons.append(
                    f"빈 공간 폭이 {c['clear_width_m']:.1f}m라 {side:.0f}×{side:.0f}m 부스를 두어도 사방으로 "
                    f"{POPUP_CLEAR_M:g}m 이상 지나갈 수 있어요."
                )
            elif c["vacant"]:
                place = "비어 있는 팝업 전용 자리" if c["popup_unit"] else "공실"
                ending = "이라" if _final(place) else "라"
                reasons.append(f"{place}(전용 {c['area_m2']:.0f}㎡){ending} 바로 검토할 수 있어요.")
            else:
                reasons.append(
                    f"팝업 전용 자리(전용 {c['area_m2']:.0f}㎡)예요. 지금은 {c['brand_name']} 매장이 쓰고 있어 "
                    "일정 조율이 필요해요."
                )
            if c.get("affinity"):
                lift, group, code = c["affinity"]
                reasons.append(
                    f"{_josa(focal_name, '과', '와')} 함께 사는 비율이 높은 {_category_name(group)} 매장({code})이 "
                    f"가까워요(lift {lift:.1f})."
                )
            item: dict[str, Any] = {
                "rank": rank,
                "kind": c["kind"],
                "floor": c["floor"],
                "unit_code": c["unit_code"],
                "x": round(c["x"], 2),
                "y": round(c["y"], 2),
                "near": c["near"],
            }
            if c["kind"] == "spot":
                item.update(clear_width_m=c["clear_width_m"], booth_m2=c["booth_m2"])
            else:
                item.update(area_m2=c["area_m2"], vacant=c["vacant"], brand_name=c["brand_name"])
            item.update(
                passers=int(round(c["passers"])),
                passers_per_day=round(per_day, 1),
                score=round(c["score"], 3),
                reasons=reasons,
            )
            ranked.append(item)
        if focal:
            scoring = (
                "점수(0~1) = 추정 유동(후보 중 최댓값=1) × (0.7 + 0.2 × 에스컬레이터 승강장에서 보이는 정도 "
                "+ 0.1 × 주변 카테고리 궁합)이에요. 유동이 없으면 0점이에요."
            )
        else:
            scoring = (
                "점수(0~1) = 추정 유동(후보 중 최댓값=1) × (0.75 + 0.25 × 에스컬레이터 승강장에서 보이는 정도)"
                "예요. 유동이 없으면 0점이에요."
            )
        if focal and not lifts:
            notes.append(f"{focal_name}의 교차구매 lift를 계산할 수 없어 카테고리 궁합은 넣지 않았어요.")
        if focal:
            notes.append(
                f"카테고리 궁합은 같은 기간 전체 고객의 같은 날 교차구매 lift가 {AFFINITY_MIN_LIFT:g} 이상인 "
                f"카테고리 매장이 반경 {AFFINITY_M:g}m 안에 있는지로 봤어요."
            )
        return {
            "rev": query.rev,
            "period": {"start": query.start, "end": query.end, "days": state.days},
            "query": {
                **query.echo(),
                "segment_label": segment_text(query.segment),
                "category": focal,
                "category_name": focal_name or None,
                "booth_m2": round(side * side, 1),
            },
            "label": "추정치",
            "candidates": ranked,
            "scoring": scoring,
            "notes": notes,
            "caveat": POPUP_CAVEAT,
        }

    def spatial_popup_candidates(
        self,
        rev: str | None = None,
        day_type: str = "weekend",
        hours: str | None = None,
        segment: str | dict[str, Any] | None = None,
        floor: str | None = None,
        category: str | None = None,
        min_area_m2: float = 9.0,
        top: int = 5,
    ) -> dict[str, Any]:
        """Popup locations ranked by estimated passing traffic in a time window, view and category fit."""
        query = self._flow_query(rev, segment, floor, day_type, hours, default_day="weekend")
        return _json(self._popup(query, self._flow_state(query), category, min_area_m2, top))

    # Corridor width and escape distance -------------------------------------------------------------
    def _egress_floor(self, rev: str, floor: str) -> dict[str, Any] | None:
        """Corridor graph, width statistics and exit distances of one floor (geometry only, built lazily)."""
        floors = self._memo(("egress", rev, self._model_key(rev)), dict)
        if floor not in floors:
            model = self._model(rev)
            network = self._route_planner(rev).network(floor)
            graph = build_corridor_graph(model, floor, network)
            floors[floor] = (
                None
                if graph is None
                else {
                    "graph": graph,
                    "stats": width_stats(graph),
                    "exits": exit_distances(model, floor, network),
                }
            )
        return floors[floor]

    def _egress_review(
        self, rev: Any, floor: Any, min_width_m: Any, max_travel_m: Any
    ) -> tuple[str, float, float, dict[str, dict[str, Any]]]:
        rev = self._known_rev(rev) if rev else self.latest_sales_rev()
        min_width = _limit(min_width_m, "min_width_m", DEFAULT_MIN_WIDTH_M, MAX_WIDTH_M)
        max_travel = _limit(max_travel_m, "max_travel_m", DEFAULT_MAX_TRAVEL_M, MAX_TRAVEL_M)
        model = self._model(rev)
        codes = floor_codes(model)
        if floor:
            code = _floor_code(floor)
            if code not in codes or self._egress_floor(rev, code) is None:
                raise NotFoundToolError(f"{rev}에 {code} 통로가 없습니다.")
            codes = [code]
        review: dict[str, dict[str, Any]] = {}
        for code in codes:
            data = self._egress_floor(rev, code)
            if data is None:
                continue
            exits = data["exits"]
            reachable = [e for e in exits if math.isfinite(e.travel_m)]
            review[code] = {
                "stats": data["stats"],
                "stretches": narrow_stretches(data["graph"], min_width),
                "over": sorted(
                    (e for e in reachable if e.travel_m > max_travel),
                    key=lambda e: (-e.travel_m, e.unit_code),
                ),
                "unreachable": [e for e in exits if not math.isfinite(e.travel_m)],
                "farthest": max(reachable, key=lambda e: (e.travel_m, e.unit_code), default=None),
            }
        return rev, min_width, max_travel, review

    @staticmethod
    def _egress_text(
        min_width: float,
        max_travel: float,
        stretches: list[dict[str, Any]],
        over: list[dict[str, Any]],
        lost: int,
    ) -> str:
        parts = [f"검토 기준(통로 폭 {min_width:g}m, 걷는 거리 {max_travel:g}m)으로 보면"]
        if stretches:
            floors = "·".join(dict.fromkeys(s["floor"] for s in stretches))
            tight = stretches[0]
            ending = "이에요" if _final(tight["label"]) else "예요"
            parts.append(
                f"기준보다 좁은 통로가 {len(stretches)}곳({floors})이고, 가장 좁은 곳은 {tight['label']}"
                f"(폭 {tight['min_width_m']:.1f}m){ending}."
            )
        else:
            parts.append("기준보다 좁은 통로는 없어요.")
        if over:
            far = over[0]
            parts.append(
                f"계단·출입구까지 {max_travel:g}m를 넘는 매장은 {len(over)}곳이고, 가장 먼 곳은 {far['unit_code']}"
                f"({far['nearest_name']}까지 {far['travel_m']:.0f}m)예요."
            )
        else:
            parts.append(f"계단·출입구까지 {max_travel:g}m를 넘는 매장은 없어요.")
        if lost:
            parts.append(f"통로로 계단·출입구에 이어지지 않는 매장이 {lost}곳 있어요.")
        return " ".join(parts)

    def spatial_egress_check(
        self,
        rev: str | None = None,
        floor: str | None = None,
        min_width_m: float = DEFAULT_MIN_WIDTH_M,
        max_travel_m: float = DEFAULT_MAX_TRAVEL_M,
        top: int = 20,
    ) -> dict[str, Any]:
        """Corridors narrower than ``min_width_m`` and units farther than ``max_travel_m`` from an exit."""
        rev, min_width, max_travel, review = self._egress_review(rev, floor, min_width_m, max_travel_m)
        floors, stretches, over, lost = [], [], [], []
        for code, item in review.items():
            farthest = item["farthest"]
            floors.append(
                {
                    "floor": code,
                    "min_width_m": item["stats"]["min_width_m"],
                    "median_width_m": item["stats"]["median_width_m"],
                    "narrow_stretches": len(item["stretches"]),
                    "max_travel_m": farthest.travel_m if farthest else None,
                    "farthest_unit": farthest.unit_code if farthest else None,
                    "units_over_limit": len(item["over"]),
                }
            )
            stretches.extend(
                {
                    "floor": code,
                    "label": s.label,
                    "min_width_m": s.min_width_m,
                    "length_m": s.length_m,
                    "x": s.x,
                    "y": s.y,
                }
                for s in item["stretches"]
            )
            over.extend(
                {
                    "unit_code": e.unit_code,
                    "floor": code,
                    "brand_name": e.brand_name,
                    "travel_m": e.travel_m,
                    "nearest": e.exit_ref,
                    "nearest_name": e.exit_name,
                }
                for e in item["over"]
            )
            lost.extend(
                {"unit_code": e.unit_code, "floor": code, "brand_name": e.brand_name}
                for e in item["unreachable"]
            )
        stretches.sort(key=lambda s: (s["min_width_m"], -s["length_m"], s["floor"]))
        over.sort(key=lambda u: (-u["travel_m"], u["unit_code"]))
        k = _top(top, 20)
        return _json(
            {
                "rev": rev,
                "criteria": {
                    "min_width_m": min_width,
                    "max_travel_m": max_travel,
                    "kind": "검토용 예시 기준(법규 아님)",
                },
                "floors": floors,
                "totals": {
                    "narrow_stretches": len(stretches),
                    "units_over_limit": len(over),
                    "unreachable_units": len(lost),
                },
                "narrow_stretches": stretches[:k],
                "units_over_limit": over[:k],
                "unreachable_units": lost[:k],
                "summary": self._egress_text(min_width, max_travel, stretches, over, len(lost)),
                "caveats": EGRESS_CAVEATS,
            }
        )

    def _egress_object(self, rev: Any, floor: Any, min_width_m: Any, max_travel_m: Any) -> dict[str, Any]:
        rev, min_width, max_travel, review = self._egress_review(rev, floor, min_width_m, max_travel_m)
        warnings = sorted(
            ((code, e) for code, item in review.items() for e in item["over"]),
            key=lambda pair: (-pair[1].travel_m, pair[1].unit_code),
        )
        markers = []
        for rank, (code, e) in enumerate(warnings, 1):
            who = f"{e.unit_code} {e.brand_name}" if e.brand_name else e.unit_code
            markers.append(
                {
                    "floor": code,
                    "x": e.x,
                    "y": e.y,
                    "rank": rank,
                    "kind": "warning",
                    "label": f"{e.travel_m:.0f}m",
                    "title": f"{who} · {e.exit_name}까지 {e.travel_m:.0f}m(기준 {max_travel:g}m)",
                    "unit_code": e.unit_code,
                }
            )
        for code, item in review.items():
            for e in item["unreachable"]:
                markers.append(
                    {
                        "floor": code,
                        "x": e.x,
                        "y": e.y,
                        "rank": len(markers) + 1,
                        "kind": "warning",
                        "label": "연결 없음",
                        "title": f"{e.unit_code} · 통로로 계단·출입구에 이어지지 않아요",
                        "unit_code": e.unit_code,
                    }
                )
        layers: dict[str, Any] = {}
        stretches: list[dict[str, Any]] = []
        for code, item in review.items():
            segments = [
                {
                    "points": [list(point) for point in s.points],
                    "weight": round(0.5 + 0.5 * (1.0 - s.min_width_m / min_width), 3),
                    "value": s.min_width_m,
                    "color": WARNING_COLOR,
                    "label": f"{s.label} · 폭 {s.min_width_m:.1f}m",
                }
                for s in item["stretches"]
            ]
            stretches.extend(
                {"floor": code, "label": s.label, "min_width_m": s.min_width_m} for s in item["stretches"]
            )
            if segments or floor or any(m["floor"] == code for m in markers):
                layers[code] = {
                    "segments": segments[:MAX_SEGMENTS],
                    "max_value": min_width,
                    "total": len(segments),
                }
        stretches.sort(key=lambda s: (s["min_width_m"], s["floor"]))
        over = [
            {"unit_code": e.unit_code, "travel_m": e.travel_m, "nearest_name": e.exit_name}
            for _, e in warnings
        ]
        lost = sum(len(item["unreachable"]) for item in review.values())
        return _json(
            {
                "mode": "egress",
                "title": f"통로 폭·피난 거리 점검(폭 {min_width:g}m·거리 {max_travel:g}m 기준)",
                "rev": rev,
                "query": {
                    "floor": _floor_code(floor) if floor else None,
                    "min_width_m": min_width,
                    "max_travel_m": max_travel,
                },
                "floors": layers,
                "markers": markers,
                "legend": {
                    "low": "좁음",
                    "high": "기준 폭",
                    "unit": "m",
                    "note": f"빨간 선: 폭 {min_width:g}m 미만 통로 · 경고: 계단·출입구까지 {max_travel:g}m 초과 매장",
                },
                "summary": {
                    "customer_days": None,
                    "trips": None,
                    "floors": list(layers),
                    "narrow_stretches": len(stretches),
                    "units_over_limit": len(warnings),
                    "unreachable_units": lost,
                    "text": self._egress_text(min_width, max_travel, stretches, over, lost),
                },
                "caveat": " ".join(EGRESS_CAVEATS),
            }
        )

    # UI object -------------------------------------------------------------------------------------
    def flow_object(
        self,
        mode: str = "traffic",
        rev: str | None = None,
        floor: str | None = None,
        segment: str | dict[str, Any] | None = None,
        day_type: str | None = None,
        hours: str | None = None,
        category: str | None = None,
        min_width_m: float | None = None,
        max_travel_m: float | None = None,
    ) -> dict[str, Any]:
        """The ``flow`` UI state object (docs/specs/atlas-api.md §3): traffic, popup or egress."""
        mode = str(mode or "traffic").strip().lower()
        if mode not in FLOW_MODES:
            raise ToolError(
                "mode는 traffic(추정 유동), popup(팝업 후보), egress(통로 폭·피난 거리) 중 하나예요."
            )
        if mode == "egress":
            return self._egress_object(rev, floor, min_width_m, max_travel_m)
        query = self._flow_query(
            rev,
            segment,
            floor,
            day_type,
            hours,
            default_day="weekend" if mode == "popup" else "all",
            fallback=True,
        )
        state = self._flow_state(query)
        window = window_text(query.day_type, query.hours)
        suffix = f"({window})" if window else ""
        markers: list[dict[str, Any]] = []
        echo: dict[str, Any] = query.echo()
        notes = self._flow_notes(query, state)
        if mode == "popup":
            popup = self._popup(query, state, category, 9.0, 5)
            notes = popup["notes"]
            markers = [
                {
                    "floor": c["floor"],
                    "x": c["x"],
                    "y": c["y"],
                    "rank": c["rank"],
                    "kind": c["kind"],
                    "label": str(c["rank"]),
                    "title": f"후보 {c['rank']} · "
                    + (
                        f"{c['unit_code']} {'공실' if c['vacant'] else '팝업 자리'}"
                        if c["kind"] == "unit"
                        else c["near"]
                    ),
                    "unit_code": c["unit_code"],
                }
                for c in popup["candidates"]
            ]
            echo["category"] = popup["query"]["category"]
            title = f"{segment_text(query.segment)} 고객 기준 팝업 후보{suffix}"
            note = "번호는 팝업 후보 순위, 선은 같은 조건의 추정 유동(결제 순서를 통로망 최단 경로로 이어 모은 값)"
            caveat = f"{POPUP_CAVEAT} 유동은 결제 순서를 통로망 최단 경로로 이은 추정이에요."
        else:
            title = f"{segment_text(query.segment)} 고객의 추정 유동{suffix}"
            note = TRAFFIC_LEGEND_NOTE
            caveat = TRAFFIC_CAVEAT
        layers = self._traffic_layers(query, state)
        for marker in markers:
            layers.setdefault(marker["floor"], {"segments": [], "max_value": 0, "total": 0})
        summary: dict[str, Any] = {
            "customer_days": int(round(state.customer_days)),
            "trips": int(round(state.trips)),
            "floors": list(layers),
            "period": {"start": query.start, "end": query.end, "days": state.days},
            "notes": notes,
        }
        # The agent explains the drawing from this object alone, so name the busiest corridors and units.
        summary["top_corridors"] = [
            {k: row[k] for k in ("floor", "label", "value", "per_day", "share_of_floor")}
            for row in self._corridors(query, state)[:SUMMARY_TOP]
        ]
        if mode == "traffic":
            summary["top_units"] = self._passing_units(query, state)[:SUMMARY_TOP]
        return _json(
            {
                "mode": mode,
                "title": title,
                "rev": query.rev,
                "query": echo,
                "floors": layers,
                "markers": markers,
                "legend": {"low": "적음", "high": "많음", "unit": "명(추정)", "note": note},
                "summary": summary,
                "caveat": caveat,
            }
        )
