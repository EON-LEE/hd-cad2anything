"""General spatial tools: query units by place, relation and performance; walking routes; floor map cards.

Scenario tools (productivity, layout, journeys) answer fixed questions. These tools let an agent compose
free questions instead: "코너 매장 중 효율이 낮은 곳", "화장품 매장과 벽을 맞댄 매장들의 평균 평효율",
"정문에서 8층 식당가까지", "4층은 어떻게 생겼어". Every value comes from the spatial model, the spatial
vocabulary (``cad2anything.spatial.vocabulary``) or the analytics frames, never from the language model.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.catalog import CATEGORIES, FLOOR_HEIGHT_M, floor_program
from cad2anything.model import Entity, EntityKind, RevisionKind
from cad2anything.services.analytics_tools import (
    CATEGORY_GROUPS,
    GROUP_ALIASES,
    SALES_NOTE,
    _floor_code,
    _json,
)
from cad2anything.services.errors import ToolError

QUERY_LIMIT = 50
MAX_QUERY_LIMIT = 200
CANDIDATE_LIMIT = 40
BASE_FIELDS = (
    "entity_id",
    "unit_code",
    "floor",
    "zone_code",
    "brand_id",
    "brand_name",
    "category",
    "vacant",
    "area_m2",
    "frontage_m",
    "walk_to_escalator_m",
    "nearest_escalator_id",
    "walk_to_entrance_m",
    "nearest_entrance_id",
)
KPI_COLUMNS = {
    "sales": "sales_krw",
    "txns": "receipts",
    "aov": "aov_krw",
    "sales_per_m2": "sales_per_m2",
    "sales_per_pyeong": "sales_per_pyeong",
    "gmrof": "gmrof",
    "commission_rate": "commission_rate",
    "commission_revenue": "commission_revenue",
    "percentile": "percentile",
    "floor_percentile": "floor_percentile",
    "low_efficiency": "low_efficiency",
}
DEFAULT_FIELDS = ("unit_code", "floor", "brand_name", "category_name", "area_m2")
HIDDEN_FIELDS = ("entity_id",)
BOOL_FIELDS = (
    "vacant",
    "anchor",
    "corner",
    "escalator_front",
    "entrance_front",
    "main_walkway",
    "low_efficiency",
)
AGGREGATES = ("count", "sum", "mean", "median", "min", "max", "rate")
OP_ALIASES = {
    "eq": "eq",
    "=": "eq",
    "==": "eq",
    "is": "eq",
    "ne": "ne",
    "!=": "ne",
    "not": "ne",
    "in": "in",
    "not_in": "not_in",
    "nin": "not_in",
    "gt": "gt",
    ">": "gt",
    "gte": "gte",
    ">=": "gte",
    "min": "gte",
    "lt": "lt",
    "<": "lt",
    "lte": "lte",
    "<=": "lte",
    "max": "lte",
    "contains": "contains",
    "like": "contains",
}
OP_TEXT = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "ne": "≠", "not_in": "제외", "contains": "포함"}
TRUE_WORDS = {"true", "1", "yes", "y", "예", "네", "참", "o"}
FALSE_WORDS = {"false", "0", "no", "n", "아니오", "아니요", "거짓", "x"}
DIRECTION_WORDS = {
    "동": "동",
    "서": "서",
    "남": "남",
    "북": "북",
    "가운데": "중앙",
    "중앙": "중앙",
    "센터": "중앙",
}
FIELD_NOTES = {
    "side": "side는 도면 기준 방향이에요(도면 위쪽=북). 층 외곽을 가로세로 3등분한 9개 구역입니다.",
    "corner": "corner는 서로 직각인 두 면 이상이 통로에 2m 이상 접한 매장이에요.",
    "frontage_sides": "frontage_sides는 통로에 2m 이상 접한 면의 수예요.",
    "escalator_front": (
        "escalator_front는 에스컬레이터 승하차 지점에서 내리는 방향 45° 안, 20m 안에 가림 없이 보이는 매장이에요."
    ),
    "escalator_view_m": "escalator_view_m는 에스컬레이터 승하차 지점에서 가림 없이 보이는 직선거리예요.",
    "entrance_front": "entrance_front는 건물 출입구에서 들어오는 방향 45° 안, 25m 안에 가림 없이 보이는 매장이에요.",
    "traffic_potential": (
        "traffic_potential은 에스컬레이터·엘리베이터·계단·출입구 사이 최단 경로가 매장 앞을 지나는 정도예요"
        "(층별 최댓값=1). 도면 구조로 계산한 값이며 관측한 유동이 아니에요."
    ),
    "main_walkway": "main_walkway는 traffic_potential 0.5 이상인 매장이에요.",
    "distance_m": "distance_m는 통로망을 따라 걷는 거리예요(매장은 통로에 접한 면의 가운데 기준).",
    "adjacent_to": "adjacent_to는 벽을 맞댄 매장이에요(대상 매장 자신은 뺍니다).",
}
ROUTE_CAVEAT = {
    "escalator": "통로망 최단 경로로 계산한 걷는 거리예요. 에스컬레이터를 타는 구간과 층마다 갈아타는 거리는 넣지 않았어요.",
    "elevator": "통로망 최단 경로로 계산한 걷는 거리예요. 엘리베이터를 기다리고 타는 구간은 넣지 않았어요.",
    "stairs": "통로망 최단 경로로 계산한 걷는 거리예요. 계단을 오르내리는 구간은 넣지 않았어요.",
}
STRAIGHT_NOTE = "통로로 이어지지 않아 직선거리로 계산했어요."
VIA_ALIASES = {
    "escalator": "escalator",
    "에스컬레이터": "escalator",
    "elevator": "elevator",
    "엘리베이터": "elevator",
    "stairs": "stairs",
    "stair": "stairs",
    "계단": "stairs",
}
PLACE_PREFIX = re.compile(r"^\s*(B\d+F?|\d+F|지하\s*\d+\s*층|\d+\s*층)\s*[:：\s]\s*(.+?)\s*$", re.IGNORECASE)
LINK_WORDS = {
    "escalator": ("ESCALATOR", "에스컬레이터"),
    "elevator": ("ELEVATOR", "엘리베이터", "승강기"),
    "stair": ("STAIRS", "STAIR", "계단"),
}
ENTRANCE_WORDS = ("ENTRANCE", "출입구", "입구")
AMENITY_WORDS = {
    "WC": "RESTROOM",
    "TOILET": "RESTROOM",
    "안내": "INFO",
    "안내데스크": "INFO",
    "수유": "NURSING",
}
ZONE_SIDE_LIMIT = 2
DESCRIBE_TOP = 6


def _norm(text: Any) -> str:
    return re.sub(r"[\s\-_·]", "", str(text or "")).upper()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise ToolError(f"참/거짓 값이 필요합니다: {value}")


def _round5(meters: float) -> int:
    return int(5 * round(float(meters) / 5.0))


def _floor_ko(code: str | None) -> str:
    text = str(code or "")
    if match := re.fullmatch(r"B(\d+)", text):
        return f"지하 {match.group(1)}층"
    if match := re.fullmatch(r"(\d+)F", text):
        return f"{match.group(1)}층"
    return text


class SpatialTools:
    """Mixin for Toolkit; needs ``_model``, ``_model_key``, ``_memo``, ``unit_features``, ``productivity_frame``,
    ``expected_unit_sales``, ``_adjacency``, ``_route_planner``, ``_table``, ``_resolve_category``."""

    # Data --------------------------------------------------------------------------------------------
    def _vocabulary(self, rev: str) -> pd.DataFrame:
        def compute() -> pd.DataFrame:
            from cad2anything.spatial.features import floor_codes
            from cad2anything.spatial.vocabulary import unit_vocabulary

            model = self._model(rev)
            planner = self._route_planner(rev)
            networks = {floor: planner.network(floor) for floor in floor_codes(model) if model.units(floor)}
            return unit_vocabulary(model, networks)

        return self._memo(("vocabulary", rev, self._model_key(rev)), compute)

    def unit_table(self, rev: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """One row per unit: attributes, walking features, spatial vocabulary and performance.

        Actual revisions carry annualized results for their period (``attrs['period']``); hypothetical
        revisions carry the layout model's expected sales instead (estimates).
        """
        model = self._model(rev)
        key = ("unit_table", rev, str(start or ""), str(end or ""), self._data_key(), self._model_key(rev))

        def compute() -> pd.DataFrame:
            features = self.unit_features(rev)
            if features is None or features.empty:
                raise ToolError(f"{rev}에 매장 공간 정보가 없습니다.")
            frame = features[[c for c in BASE_FIELDS if c in features]].copy()
            frame["category_name"] = frame["category"].map(
                lambda c: CATEGORIES[c].name_ko if c in CATEGORIES else (c or "")
            )
            frame = frame.merge(self._vocabulary(rev).drop(columns=["floor"]), on="unit_code", how="left")
            brands = self._table("brands", required=False)
            if brands is not None and "anchor" in brands:
                anchor = brands.assign(brand_id=brands["brand_id"].astype(str)).set_index("brand_id")[
                    "anchor"
                ]
                frame["anchor"] = frame["brand_id"].astype(str).map(anchor).eq(True) & ~frame[
                    "vacant"
                ].astype(bool)
            period = None
            if model.revision.kind == RevisionKind.ACTUAL:
                try:
                    kpi = self.productivity_frame(rev, "unit", start, end)
                except (ToolError, KeyError, ValueError):
                    kpi = None
                if kpi is not None and len(kpi):
                    period = kpi.attrs.get("period")
                    columns = [c for c in KPI_COLUMNS if c in kpi]
                    kpi = kpi[["unit_code", *columns]].rename(columns=KPI_COLUMNS)
                    frame = frame.merge(kpi, on="unit_code", how="left")
                    frame["low_efficiency"] = frame["low_efficiency"].eq(True)
            else:
                try:
                    expected = self.expected_unit_sales(rev)[
                        ["unit_code", "expected_sales", "expected_sales_per_m2"]
                    ]
                    frame = frame.merge(
                        expected.rename(columns={"expected_sales": "expected_sales_krw"}),
                        on="unit_code",
                        how="left",
                    )
                except (ToolError, KeyError, ValueError):
                    pass
            frame.attrs["period"] = period
            frame.attrs["kind"] = model.revision.kind.value
            return frame

        return self._memo(key, compute)

    # Values and filters ------------------------------------------------------------------------------
    def _category_codes(self, value: Any) -> list[str]:
        text = str(value).strip()
        group = GROUP_ALIASES.get(text.replace(" ", "").upper()) or GROUP_ALIASES.get(text.replace(" ", ""))
        if group:
            return list(CATEGORY_GROUPS[group])
        return [self._resolve_category(text)]

    def _values(self, field: str, value: Any) -> list[Any]:
        values = value if isinstance(value, list | tuple | set) else [value]
        result: list[Any] = []
        for item in values:
            if field == "floor":
                result.append(_floor_code(item))
            elif field == "category":
                result.extend(self._category_codes(item))
            elif field == "side":
                text = str(item).strip().replace("쪽", "")
                result.append(DIRECTION_WORDS.get(text, text))
            elif field in BOOL_FIELDS:
                result.append(_bool(item))
            else:
                result.append(item)
        return result

    @staticmethod
    def _matches(series: pd.Series, field: str, values: list[Any]) -> pd.Series:
        mask = pd.Series(False, index=series.index)
        for value in values:
            if field == "side" and value in ("동", "서", "남", "북"):
                mask |= series.astype(str).str.contains(value, regex=False)
            elif isinstance(value, bool):
                mask |= series.fillna(False).astype(bool) == value
            elif isinstance(value, int | float) and not isinstance(value, bool):
                mask |= pd.to_numeric(series, errors="coerce") == float(value)
            else:
                mask |= series.astype(str).str.upper() == str(value).strip().upper()
        return mask

    def _field_mask(self, frame: pd.DataFrame, field: str, condition: Any) -> tuple[pd.Series, str]:
        if field not in frame.columns or field in HIDDEN_FIELDS:
            raise ToolError(
                f"알 수 없는 필드 {field}입니다. 사용 가능한 필드: {', '.join(self._fields(frame))}"
            )
        if isinstance(condition, dict):
            ops = condition
        elif isinstance(condition, list | tuple | set):
            ops = {"in": list(condition)}
        else:
            ops = {"eq": condition}
        series = frame[field]
        mask = pd.Series(True, index=frame.index)
        texts = []
        for raw_op, value in ops.items():
            op = OP_ALIASES.get(str(raw_op).strip().lower())
            if op is None:
                raise ToolError(
                    f"알 수 없는 연산자 {raw_op}입니다. 사용 가능: eq, ne, in, not_in, gt, gte, lt, lte, contains"
                )
            if op in ("eq", "in", "ne", "not_in"):
                values = self._values(field, value)
                matched = self._matches(series, field, values)
                mask &= ~matched if op in ("ne", "not_in") else matched
                shown = ", ".join(str(v) for v in values)
                texts.append(f"{field} {OP_TEXT.get(op, '=')} {shown}")
            elif op == "contains":
                mask &= series.astype(str).str.contains(str(value), case=False, regex=False, na=False)
                texts.append(f"{field} {OP_TEXT[op]} {value}")
            else:
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ToolError(f"{field} {raw_op}에는 숫자가 필요합니다: {value}") from exc
                numeric = pd.to_numeric(series, errors="coerce")
                compare = {"gt": numeric > number, "gte": numeric >= number, "lt": numeric < number}
                mask &= compare.get(op, numeric <= number).fillna(False)
                texts.append(
                    f"{field} {OP_TEXT[op]} {value:g}"
                    if isinstance(value, int | float)
                    else f"{field} {OP_TEXT[op]} {value}"
                )
        return mask.fillna(False).astype(bool), " · ".join(texts)

    def _target_units(self, frame: pd.DataFrame, value: Any) -> tuple[set[str], str]:
        """Units named by a relation target: unit code, category (code, name, group) or brand."""
        if isinstance(value, dict):
            if value.get("unit_code"):
                code = str(value["unit_code"]).strip().upper()
                return {code}, code
            if value.get("category"):
                codes = self._category_codes(value["category"])
                units = set(
                    frame.loc[frame["category"].isin(codes) & ~frame["vacant"].astype(bool), "unit_code"]
                )
                return units, "·".join(CATEGORIES[c].name_ko if c in CATEGORIES else c for c in codes)
            if value.get("brand"):
                value = value["brand"]
            else:
                raise ToolError("관계 대상은 unit_code, category, brand 중 하나로 지정하세요.")
        text = str(value).strip()
        if text.upper() in set(frame["unit_code"].astype(str)):
            return {text.upper()}, text.upper()
        try:
            codes = self._category_codes(text)
        except ToolError:
            codes = []
        if codes:
            units = set(frame.loc[frame["category"].isin(codes) & ~frame["vacant"].astype(bool), "unit_code"])
            return units, "·".join(CATEGORIES[c].name_ko if c in CATEGORIES else c for c in codes)
        brand = frame["brand_name"].astype(str).str.lower() == text.lower()
        brand |= frame["brand_id"].astype(str).str.upper() == text.upper()
        if brand.any():
            return set(frame.loc[brand, "unit_code"]), text
        raise ToolError(f"'{text}'을(를) 매장 코드, 카테고리, 브랜드에서 찾지 못했습니다.")

    def _adjacent_mask(self, frame: pd.DataFrame, rev: str, value: Any) -> tuple[pd.Series, str]:
        targets, label = self._target_units(frame, value)
        pairs = self._adjacency(rev)
        neighbors = set(pairs.loc[pairs["unit_a"].isin(targets), "unit_b"]) | set(
            pairs.loc[pairs["unit_b"].isin(targets), "unit_a"]
        )
        return frame["unit_code"].isin(neighbors - targets), f"{label} 매장과 벽을 맞댄 매장"

    def _near(self, frame: pd.DataFrame, rev: str, value: Any) -> tuple[pd.Series, pd.Series, str]:
        if not isinstance(value, dict) or not value.get("to"):
            raise ToolError(
                'near는 {"to": "ESC-A" 또는 매장 코드, "within_m": 30, "floor": "3F"(선택)} 형식이에요.'
            )
        within = float(value.get("within_m", value.get("max_m", 30)))
        places = self._resolve_places(rev, value["to"], value.get("floor"))
        planner = self._route_planner(rev)
        distances: dict[str, float] = {}
        for place in places[:CANDIDATE_LIMIT]:
            for entity_id, meters in planner.facade_distances(place).items():
                if meters < distances.get(entity_id, np.inf):
                    distances[entity_id] = meters
        series = frame["entity_id"].map(distances).astype(float)
        place = self._place(places[0])
        target = place["label"] if len(places) == 1 else f"{value['to']}({len(places)}곳 중 가까운 곳)"
        return (series <= within).fillna(False), series.round(1), f"{target}에서 걸어서 {within:g}m 이내"

    @staticmethod
    def _fields(frame: pd.DataFrame) -> list[str]:
        return [c for c in frame.columns if c not in HIDDEN_FIELDS]

    @staticmethod
    def _parse_metric(text: Any) -> tuple[str, str, str | None, str]:
        """``count`` | ``mean:sales_per_m2`` | ``rate:sales_krw/area_m2`` -> (name, agg, field, divisor)."""
        if isinstance(text, dict):
            text = f"{text.get('agg', '')}:{text.get('field', '')}".rstrip(":")
        raw = str(text).strip()
        agg, _, field = raw.partition(":")
        agg = agg.strip().lower()
        if agg not in AGGREGATES:
            raise ToolError(f"집계는 {', '.join(AGGREGATES)} 중 하나예요: {raw}")
        if agg == "count":
            return "count", "count", None, ""
        if not field:
            raise ToolError(f"{agg}에는 필드가 필요합니다. 예: {agg}:area_m2")
        if agg == "rate":
            numerator, _, divisor = field.partition("/")
            if not divisor:
                raise ToolError("rate는 rate:분자/분모 형식이에요. 예: rate:sales_krw/area_m2")
            return f"rate_{numerator}_per_{divisor}", agg, numerator.strip(), divisor.strip()
        return f"{agg}_{field.strip()}", agg, field.strip(), ""

    @staticmethod
    def _aggregate(frame: pd.DataFrame, metric: tuple[str, str, str | None, str]) -> Any:
        name, agg, field, divisor = metric
        if agg == "count":
            return int(len(frame))
        if field not in frame.columns or (divisor and divisor not in frame.columns):
            missing = field if field not in frame.columns else divisor
            raise ToolError(f"집계할 필드 {missing}가 없습니다.")
        values = pd.to_numeric(frame[field], errors="coerce")
        if agg == "rate":
            base = pd.to_numeric(frame[divisor], errors="coerce")
            both = values.notna() & base.notna()
            total = float(base[both].sum())
            return float(values[both].sum()) / total if total else None
        values = values.dropna()
        if values.empty:
            return None
        return float(getattr(values, agg)())

    # Query -------------------------------------------------------------------------------------------
    def spatial_query(
        self,
        rev: str,
        filters: dict[str, Any] | None = None,
        group_by: list[str] | str | None = None,
        metrics: list[str] | str | None = None,
        sort_by: str | None = None,
        fields: list[str] | None = None,
        limit: int = QUERY_LIMIT,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Filter, relate, aggregate and sort the units of a revision (see TOOL_DESCRIPTIONS)."""
        table = self.unit_table(rev, start, end)
        frame = table.copy()
        mask = pd.Series(True, index=frame.index)
        applied: list[str] = []
        used: set[str] = set()
        for field, condition in (filters or {}).items():
            if field == "adjacent_to":
                part, text = self._adjacent_mask(frame, rev, condition)
                used.add("adjacent_to")
            elif field == "near":
                part, distance, text = self._near(frame, rev, condition)
                frame["distance_m"] = distance
                used.add("distance_m")
            else:
                part, text = self._field_mask(frame, field, condition)
                used.add(field)
            mask &= part
            applied.append(text)
        matched = frame[mask]
        groups = [group_by] if isinstance(group_by, str) else list(group_by or [])
        for group in groups:
            if group not in frame.columns or group in HIDDEN_FIELDS:
                raise ToolError(
                    f"group_by 필드 {group}가 없습니다. 사용 가능한 필드: {', '.join(self._fields(frame))}"
                )
        metric_specs = [
            self._parse_metric(m) for m in ([metrics] if isinstance(metrics, str) else metrics or [])
        ]
        if groups and not metric_specs:
            metric_specs = [self._parse_metric("count"), self._parse_metric("sum:area_m2")]
        limit = max(1, min(int(limit or QUERY_LIMIT), MAX_QUERY_LIMIT))
        descending = bool(sort_by) and str(sort_by).startswith("-")
        sort_key = str(sort_by or "").lstrip("-+") or None
        result: dict[str, Any] = {
            "rev": rev,
            "kind": table.attrs.get("kind"),
            "matched": int(len(matched)),
            "filters": applied,
        }
        if table.attrs.get("period"):
            result["period"] = table.attrs["period"]
        if metric_specs:
            result["totals"] = {spec[0]: self._aggregate(matched, spec) for spec in metric_specs}
        if groups:
            rows = []
            for keys, part in matched.groupby(groups, dropna=False, sort=True):
                keys = keys if isinstance(keys, tuple) else (keys,)
                row = {g: k for g, k in zip(groups, keys, strict=True)}
                row.update({spec[0]: self._aggregate(part, spec) for spec in metric_specs})
                rows.append(row)
            if sort_key:
                if rows and sort_key not in rows[0]:
                    raise ToolError(f"sort_by는 {', '.join(rows[0])} 중 하나예요.")
                present = [r for r in rows if r.get(sort_key) is not None]
                missing = [r for r in rows if r.get(sort_key) is None]
                present.sort(key=lambda r: r[sort_key], reverse=descending)
                rows = present + missing
            result["groups"] = rows[:limit]
            result["truncated"] = len(rows) > limit
        else:
            columns = list(dict.fromkeys([*(fields or DEFAULT_FIELDS), *sorted(used - {"adjacent_to"})]))
            if sort_key:
                columns = list(dict.fromkeys([*columns, sort_key]))
            unknown = [c for c in columns if c not in matched.columns or c in HIDDEN_FIELDS]
            if unknown:
                raise ToolError(
                    f"없는 필드입니다: {unknown}. 사용 가능한 필드: {', '.join(self._fields(frame))}"
                )
            ordered = matched
            if sort_key:
                ordered = matched.sort_values(
                    sort_key, ascending=not descending, na_position="last", kind="mergesort"
                )
            result["rows"] = ordered[columns].head(limit).to_dict(orient="records")
            result["truncated"] = len(ordered) > limit
        notes = [FIELD_NOTES[f] for f in [*sorted(used), *groups, sort_key or ""] if f in FIELD_NOTES]
        if any(
            c in KPI_COLUMNS.values()
            for c in [*used, *groups, sort_key or ""] + [s[2] or "" for s in metric_specs]
        ):
            notes.append(SALES_NOTE)
        if table.attrs.get("kind") == RevisionKind.HYPOTHETICAL.value:
            notes.append(
                "가상 개정이라 실적 대신 레이아웃 모형의 기대 매출(expected_sales_krw, 추정치)을 씁니다."
            )
        result["notes"] = list(dict.fromkeys(notes))
        return _json(result)

    # Places and routes -------------------------------------------------------------------------------
    def _place(self, entity: Entity) -> dict[str, Any]:
        from cad2anything.spatial.features import link_type

        attrs = entity.attributes
        if entity.kind == EntityKind.UNIT:
            code = str(attrs.get("unit_code") or entity.entity_id)
            brand = attrs.get("brand_name")
            label = f"{brand}({code})" if brand and not attrs.get("vacant") else f"공실({code})"
            return {"ref": code, "kind": "unit", "label": label, "floor": entity.floor, "unit_code": code}
        if entity.kind == EntityKind.VERTICAL_LINK:
            kind = {"stair": "stairs"}.get(link_type(entity) or "", link_type(entity) or "vertical_link")
            ref = str(attrs.get("link_id") or entity.entity_id)
            label = f"{_floor_ko(entity.floor)} {ref} {attrs.get('name_ko') or ''}".strip()
            return {"ref": ref, "kind": kind, "label": label, "floor": entity.floor, "unit_code": None}
        if entity.kind == EntityKind.OPENING:
            ref = str(attrs.get("opening_id") or entity.entity_id)
            label = f"{ref} {attrs.get('name_ko') or ''}".strip()
            return {"ref": ref, "kind": "entrance", "label": label, "floor": entity.floor, "unit_code": None}
        ref = str(attrs.get("amenity_type") or entity.entity_id)
        label = f"{_floor_ko(entity.floor)} {attrs.get('name_ko') or ref}"
        return {"ref": ref, "kind": "amenity", "label": label, "floor": entity.floor, "unit_code": None}

    def _resolve_places(self, rev: str, ref: Any, floor: Any = None) -> list[Entity]:
        """Entities named by a place reference: unit code, brand, entrance, or a floor's link or amenity."""
        from cad2anything.spatial.features import link_type

        model = self._model(rev)
        text = str(ref or "").strip()
        if not text:
            raise ToolError("장소가 비어 있습니다.")
        prefix = PLACE_PREFIX.match(text)
        if prefix and model.unit_by_code(text.upper()) is None:
            floor, text = prefix.group(1), prefix.group(2)
        floor = _floor_code(floor) if floor else None
        unit = model.unit_by_code(text.upper())
        if unit is not None:
            return [unit]
        key = _norm(text)
        brand_units = [
            u
            for u in model.units(floor)
            if not u.attributes.get("vacant")
            and (_norm(u.attributes.get("brand_name")) == key or _norm(u.attributes.get("brand_id")) == key)
        ]
        if brand_units:
            return brand_units
        exact: list[Entity] = []
        partial: list[Entity] = []
        for entity in sorted(model.entities.values(), key=lambda e: e.entity_id):
            if floor and entity.floor != floor:
                continue
            attrs = entity.attributes
            if entity.kind == EntityKind.VERTICAL_LINK:
                kind = link_type(entity) or ""
                names = {_norm(attrs.get("link_id")), *(_norm(w) for w in LINK_WORDS.get(kind, ()))}
                names.add(_norm(attrs.get("name_ko")))
            elif entity.kind == EntityKind.OPENING and attrs.get("opening_type", "entrance") == "entrance":
                names = {
                    _norm(attrs.get("opening_id")),
                    _norm(attrs.get("name_ko")),
                    *map(_norm, ENTRANCE_WORDS),
                }
            elif entity.kind == EntityKind.AMENITY:
                names = {_norm(attrs.get("amenity_type")), _norm(attrs.get("name_ko"))}
                names |= {
                    _norm(word) for word, code in AMENITY_WORDS.items() if code == attrs.get("amenity_type")
                }
            else:
                continue
            names.discard("")
            if key in names:
                exact.append(entity)
            elif len(key) >= 2 and any(key in name or name in key for name in names if len(name) >= 2):
                partial.append(entity)
        found = exact or partial
        if not found:
            where = f"{floor}에서 " if floor else ""
            raise ToolError(
                f"{where}'{ref}'을(를) 찾지 못했습니다. 매장 코드(3F-C05), 브랜드명, 출입구(ENT-1, 정문), "
                "층을 붙인 수직동선·편의시설(1F:ESC-A, 8F:화장실)로 지정하세요."
            )
        return found

    @staticmethod
    def _via(via: Any) -> str:
        key = str(via or "escalator").strip().lower()
        if key not in VIA_ALIASES:
            raise ToolError("via는 escalator, elevator, stairs 중 하나예요.")
        return VIA_ALIASES[key]

    def _vertical(self, rev: str, from_floor: str, to_floor: str) -> tuple[int, float]:
        floors = {f.code: f for f in self._model(rev).floors}
        a, b = floors.get(from_floor), floors.get(to_floor)
        if a is None or b is None:
            return 0, 0.0
        count = abs(int(b.ordinal) - int(a.ordinal))
        height = abs(float(b.elevation_m) - float(a.elevation_m))
        return count, round(height if height > 0 else count * FLOOR_HEIGHT_M, 1)

    def route_object(
        self,
        rev: str,
        origin: Any,
        destination: Any = None,
        destination_category: Any = None,
        destination_floor: Any = None,
        via: Any = "escalator",
        with_geometry: bool = True,
    ) -> dict[str, Any]:
        """Shortest walking route between two places (``route`` UI state when ``with_geometry``)."""
        via = self._via(via)
        planner = self._route_planner(rev)
        origins = self._resolve_places(rev, origin)
        if destination:
            targets = self._resolve_places(rev, destination, destination_floor)
        elif destination_category and destination_floor:
            codes = self._category_codes(destination_category)
            floor = _floor_code(destination_floor)
            targets = [
                u
                for u in self._model(rev).units(floor)
                if u.attributes.get("category") in codes and not u.attributes.get("vacant")
            ]
            if not targets:
                raise ToolError(f"{rev} {floor}에 {destination_category} 매장이 없습니다.")
        else:
            raise ToolError("destination 또는 destination_category와 destination_floor가 필요합니다.")
        target_floors = {t.floor for t in targets}
        if len({o.floor for o in origins}) > 1:
            same = [o for o in origins if o.floor in target_floors]
            if not same:
                raise ToolError(
                    f"출발지 '{origin}'이(가) 여러 층에 있습니다. 층을 붙여 주세요. 예: 1F:{origin}"
                )
            origins = same
        if len(target_floors) > 1:
            same = [t for t in targets if t.floor in {o.floor for o in origins}]
            if same:
                targets = same
            elif not destination_floor:
                raise ToolError(
                    f"도착지 '{destination}'이(가) 여러 층에 있습니다. 층을 붙여 주세요. 예: 8F:{destination}"
                )
        options = []
        for a in origins[:4]:
            for b in targets[:CANDIDATE_LIMIT]:
                if a.entity_id == b.entity_id:
                    continue
                route = planner.route_between(a, b, via)
                options.append((route.straight, route.length_m, b.entity_id, a, b, route))
        if not options:
            raise ToolError("출발지와 도착지가 같습니다.")
        options.sort(key=lambda o: (o[0], o[1], o[2]))
        _, _, _, start, finish, route = options[0]
        origin_place, destination_place = self._place(start), self._place(finish)
        for place, entity in ((origin_place, start), (destination_place, finish)):
            place["x"], place["y"] = planner.entity_xy(entity)
        transitions = []
        if route.via and start.floor != finish.floor:
            floors, height = self._vertical(rev, start.floor or "", finish.floor or "")
            transitions.append(
                {
                    "from_floor": start.floor,
                    "to_floor": finish.floor,
                    "via": route.via,
                    "floors": floors,
                    "height_m": height,
                }
            )
        legs = [{"floor": leg.floor, "walk_m": _round5(leg.length_m)} for leg in route.legs]
        result: dict[str, Any] = {
            "title": f"{origin_place['label']} → {destination_place['label']}",
            "rev": rev,
            "via_type": via,
            "origin": origin_place,
            "destination": destination_place,
            "walk_m": _round5(route.length_m),
            "legs": legs,
            "transitions": transitions,
            "caveat": ROUTE_CAVEAT[via] + (f" {STRAIGHT_NOTE}" if route.straight else ""),
        }
        if route.straight:
            result["straight"] = True
        others = [o for o in options[1:] if o[4].entity_id != finish.entity_id]
        if destination_category or len(targets) > 1:
            seen: set[str] = set()
            alternatives = []
            for _, length, entity_id, _, entity, _ in others:
                if entity_id in seen:
                    continue
                seen.add(entity_id)
                alternatives.append({**self._place(entity), "walk_m": _round5(length)})
                if len(alternatives) == 3:
                    break
            result["alternatives"] = alternatives
        if with_geometry:
            result["segments"] = [
                {"floor": leg.floor, "points": [list(p) for p in leg.points]}
                for leg in route.legs
                if len(leg.points) >= 2
            ]
        return _json(result)

    def spatial_route(
        self,
        rev: str,
        origin: str,
        destination: str | None = None,
        destination_category: str | None = None,
        destination_floor: str | None = None,
        via: str = "escalator",
    ) -> dict[str, Any]:
        return self.route_object(
            rev, origin, destination, destination_category, destination_floor, via, with_geometry=False
        )

    # Floor map card ----------------------------------------------------------------------------------
    def spatial_describe_floor(self, rev: str, floor: str) -> dict[str, Any]:
        """Landmarks, zones, anchors and notable units of a floor, with a short Korean description."""
        from cad2anything.spatial.vocabulary import floor_bounds, floor_landmarks

        floor = _floor_code(floor)
        model = self._model(rev)
        if not model.units(floor):
            raise ToolError(f"{rev} {floor}층에 매장이 없습니다.")
        table = self.unit_table(rev)
        units = table[table["floor"] == floor]
        bounds = floor_bounds(model, floor)
        planner = self._route_planner(rev)
        landmarks = floor_landmarks(model, floor, planner.network(floor))
        try:
            floor_name = floor_program(floor).name_ko
        except KeyError:
            floor_name = ""
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for mark in landmarks:
            by_kind.setdefault(mark.kind, []).append({"ref": mark.ref, "name": mark.name, "side": mark.side})
        zones = []
        for code, part in units.groupby("zone_code", dropna=True, sort=True):
            sides = part["side"].value_counts().head(ZONE_SIDE_LIMIT).index.tolist()
            zones.append(
                {
                    "zone_code": code,
                    "zone_name": str(part["zone_name"].iloc[0] or ""),
                    "units": int(len(part)),
                    "area_m2": float(part["area_m2"].sum()),
                    "sides": sides,
                }
            )

        def brief(row: pd.Series, *extra: str) -> dict[str, Any]:
            item = {
                "unit_code": row["unit_code"],
                "brand_name": None if row["vacant"] else row["brand_name"],
                "category_name": row["category_name"],
                "side": row["side"],
            }
            item.update({k: row[k] for k in extra})
            return item

        occupied = units[~units["vacant"].astype(bool)]
        anchor_mask = (
            occupied["anchor"].eq(True) if "anchor" in occupied else pd.Series(False, index=occupied.index)
        )
        anchors = [brief(r) for _, r in occupied[anchor_mask].iterrows()]
        front = units[units["escalator_front"].fillna(False).astype(bool)].sort_values("escalator_view_m")
        main = units.sort_values("traffic_potential", ascending=False).head(DESCRIBE_TOP)
        vacant = units[units["vacant"].astype(bool)]
        width = round(bounds[2] - bounds[0], 1) if bounds else None
        depth = round(bounds[3] - bounds[1], 1) if bounds else None
        result = {
            "rev": rev,
            "floor": floor,
            "floor_name": floor_name,
            "size_m": {"width": width, "depth": depth},
            "units": int(len(units)),
            "vacant_units": [brief(r, "area_m2") for _, r in vacant.iterrows()],
            "landmarks": by_kind,
            "zones": zones,
            "anchors": anchors,
            "escalator_front": [brief(r, "front_escalator", "escalator_view_m") for _, r in front.iterrows()],
            "main_walkway": [brief(r, "traffic_potential") for _, r in main.iterrows()],
            "corner_units": int(units["corner"].fillna(False).astype(bool).sum()),
            "summary": self._describe_text(
                floor, floor_name, width, depth, units, by_kind, zones, front, anchors
            ),
            "notes": [FIELD_NOTES["side"], FIELD_NOTES["escalator_front"], FIELD_NOTES["traffic_potential"]],
        }
        return _json(result)

    @staticmethod
    def _describe_text(
        floor: str,
        floor_name: str,
        width: float | None,
        depth: float | None,
        units: pd.DataFrame,
        landmarks: dict[str, list[dict[str, Any]]],
        zones: list[dict[str, Any]],
        front: pd.DataFrame,
        anchors: list[dict[str, Any]],
    ) -> str:
        def places(kind: str) -> str:
            items = landmarks.get(kind, [])
            by_side: dict[str, list[str]] = {}
            for item in items:
                by_side.setdefault(item["side"], []).append(
                    item["ref"] if kind != "amenity" else item["name"]
                )
            return ", ".join(f"{'·'.join(dict.fromkeys(refs))}({side})" for side, refs in by_side.items())

        vacant = int(units["vacant"].astype(bool).sum())
        title = f"{_floor_ko(floor)}" + (f"({floor_name})" if floor_name else "")
        size = f" 가로 {width:g}m, 세로 {depth:g}m 크기이고," if width and depth else ""
        parts = [f"{title}은{size} 매장 {len(units)}개(공실 {vacant}개)가 있어요."]
        links = [
            f"{name} {places(kind)}"
            for kind, name in (("escalator", "에스컬레이터"), ("elevator", "엘리베이터"), ("stairs", "계단"))
            if landmarks.get(kind)
        ]
        if links:
            parts.append("수직동선은 " + ", ".join(links) + "에 있어요.")
        if landmarks.get("entrance"):
            parts.append("출입구는 " + places("entrance") + "에 있어요.")
        if landmarks.get("amenity"):
            parts.append("편의시설은 " + places("amenity") + "에 있어요.")
        if zones:
            zone_text = ", ".join(
                f"{z['zone_name'] or z['zone_code']}({'·'.join(z['sides'])}, {z['units']}개)" for z in zones
            )
            parts.append(f"구역은 {zone_text}로 나뉘어요.")
        if len(front):
            parts.append("에스컬레이터 정면 매장은 " + ", ".join(front["unit_code"].astype(str)) + "이에요.")
        if anchors:
            parts.append(
                "앵커 매장은 "
                + ", ".join(f"{a['brand_name']}({a['unit_code']})" for a in anchors)
                + "이에요."
            )
        parts.append("방향은 도면 기준(위쪽=북)이에요.")
        return " ".join(parts)
