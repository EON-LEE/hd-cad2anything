"""Analytics and layout tools for the Toolkit, shared by the MCP server and the Atlas API.

Money values are full-scale, annualized KRW: each synthetic sample receipt is multiplied by its sampling
weight, and period totals are scaled by 365 / period days so that revisions whose periods differ in
length compare directly. Estimates carry a label and a caveat; nothing here claims causality.
"""

from __future__ import annotations

import calendar
import json
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.catalog import CATEGORIES
from cad2anything.model import RevisionKind
from cad2anything.services.errors import ToolError

DEFAULT_LIMIT = 50
CACHE_SIZE = 48
LEVELS = ("unit", "zone", "floor", "category")
PAIR_LEVELS = ("brand", "category", "floor", "zone", "unit")
TIME_DIMENSIONS = ("hour", "weekday", "date", "month", "category", "floor", "zone_code", "brand_id")
BUSINESS = ("transactions", "pos_terminals", "tenancies", "brands", "customers")
SEGMENT_KEYS = ("age_band", "gender", "tier", "home_area")
ANNUALIZED = (
    "sales",
    "gross_sales",
    "returns",
    "txns",
    "sales_per_m2",
    "sales_per_pyeong",
    "commission_revenue",
    "gmrof",
)
PRODUCTIVITY_FIELDS = (
    "unit_code",
    "floor",
    "zone_code",
    "brand_id",
    "brand_name",
    "category",
    "category_name",
    "n_units",
    "area_m2",
    "sales",
    "txns",
    "aov",
    "sales_per_m2",
    "sales_per_pyeong",
    "commission_rate",
    "gmrof",
    "percentile",
    "floor_percentile",
    "low_efficiency",
)
RENAMED = {"sales": "sales_krw", "aov": "aov_krw", "txns": "receipts"}
SALES_NOTE = (
    "합성 표본 거래에 표본 가중치를 곱해 전체 규모로 환산하고, 기간 합계를 연 단위로 환산한 값입니다(원). "
    "면적은 전용면적 기준입니다."
)
HYPOTHETICAL_HINT = (
    "가상 개정에는 거래 실적이 없습니다. whatif_score_moves나 layout_generate_alternatives로 추정하세요."
)
PAIR_CAVEAT = "같은 고객의 구매가 함께 나타나는 정도입니다. 매장 간 동선이나 인과를 뜻하지 않습니다."
LAYOUT_NOTE = "검토용 대안입니다. 실제 도면이 아니며, 매출 변화는 모형 추정치입니다."
# Category groups accepted where a filter may cover several categories (aliases are matched without spaces).
CATEGORY_GROUPS = {"F&B": tuple(code for code, cat in CATEGORIES.items() if cat.is_food_beverage)}
GROUP_ALIASES = {
    "F&B": "F&B",
    "FNB": "F&B",
    "F＆B": "F&B",
    "식음료": "F&B",
    "식음": "F&B",
    "식품·음료": "F&B",
}


def _json(value: Any) -> Any:
    """JSON-ready copy: numpy/pandas scalars to Python, non-finite floats to None, KRW-scale floats rounded."""
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json(v) for v in value]
    if isinstance(value, pd.DataFrame):
        return _json(value.to_dict(orient="records"))
    if isinstance(value, pd.Series):
        return _json(value.to_dict())
    if isinstance(value, np.ndarray):
        return _json(value.tolist())
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        number = float(value)
        if not math.isfinite(number):
            return None
        return round(number) if abs(number) >= 1000 else round(number, 4)
    if isinstance(value, pd.Timestamp | datetime | date):
        return value.isoformat()
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    return value


def _parse_date(value: Any, name: str, end: bool = False) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}", text):
            year, month = (int(part) for part in text.split("-"))
            day = calendar.monthrange(year, month)[1] if end else 1
            return date(year, month, day)
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ToolError(f"{name}는 YYYY-MM-DD 또는 YYYY-MM 형식이어야 합니다: {value}") from exc


def _floor_code(text: Any) -> str:
    value = str(text).strip().upper().replace(" ", "")
    match = re.fullmatch(r"(지하|B)(\d+)(층|F)?", value)
    if match:
        return f"B{match.group(2)}"
    match = re.fullmatch(r"(\d+)(층|F)", value)
    return f"{match.group(1)}F" if match else value


def parse_segment(segment: Any) -> dict[str, Any] | None:
    """Customer segment from a mapping or ``"age_band:40s;tier:VIP|VVIP"``; Korean shorthands accepted."""
    if segment in (None, "", {}):
        return None
    if isinstance(segment, str):
        parsed: dict[str, Any] = {}
        for part in filter(None, (p.strip() for p in segment.split(";"))):
            key, _, raw = part.partition(":")
            values = [v.strip() for v in raw.split("|") if v.strip()]
            parsed[key.strip()] = values if len(values) > 1 else (values[0] if values else "")
        segment = parsed
    if not isinstance(segment, dict):
        raise ToolError("segment는 {age_band, gender, tier, home_area} 키를 가진 객체여야 합니다.")
    result: dict[str, Any] = {}
    for key, raw in segment.items():
        if key not in SEGMENT_KEYS:
            raise ToolError(f"segment 키는 {', '.join(SEGMENT_KEYS)} 중 하나여야 합니다: {key}")
        values = [str(v).strip() for v in (raw if isinstance(raw, list | tuple) else [raw])]
        normalized = []
        for value in values:
            if key == "age_band":
                match = re.fullmatch(r"(\d)0(대|s)?(\s*이상|\+)?", value)
                if match:
                    value = f"{match.group(1)}0s" + ("+" if match.group(3) or match.group(1) == "6" else "")
            elif key == "gender":
                value = {"여성": "F", "여": "F", "FEMALE": "F", "남성": "M", "남": "M", "MALE": "M"}.get(
                    value.upper() if value.isascii() else value, value.upper()
                )
            elif key == "tier":
                value = value.upper()
            normalized.append(value)
        result[key] = normalized if len(normalized) > 1 else normalized[0]
    return result


MOVE_FIELDS = (
    "brand_id",
    "brand_name",
    "category",
    "from_unit",
    "to_unit",
    "from_floor",
    "to_floor",
    "delta",
    "delta_low",
    "delta_high",
    "explanation",
)


def _alternative_summary(alternative: Any) -> dict[str, Any]:
    """Compact alternative: moves without component columns; label and caveats are reported once."""
    data = alternative.to_dict()
    for key in ("assignment", "label", "caveats", "seed"):
        data.pop(key, None)
    data["moves"] = [{k: move.get(k) for k in MOVE_FIELDS} for move in data["moves"]]
    data["delta_krw"] = alternative.delta
    data["all_constraints_satisfied"] = alternative.all_constraints_satisfied
    return data


def _pct(log_points: Any) -> float | None:
    value = float(log_points)
    return (math.exp(value) - 1.0) * 100.0 if math.isfinite(value) else None


DRIVER_COMPONENTS = {
    "exposure": "노출(에스컬레이터 보행거리)",
    "frontage": "통로 접면",
    "area": "면적",
    "category": "카테고리 특성",
    "residual": "모형 밖 요인",
}


class AnalyticsTools:
    """Mixin for Toolkit; needs ``ws``, ``_model(rev)``, ``unit_features(rev)``, ``_next_hypothetical_id()``."""

    ws: Any

    # Cache -------------------------------------------------------------------------------------------
    def _init_analytics(self) -> None:
        self._analytics_lock = threading.RLock()
        self._analytics_cache: OrderedDict[tuple[Any, ...], Any] = OrderedDict()

    def _memo(self, key: tuple[Any, ...], compute: Callable[[], Any]) -> Any:
        with self._analytics_lock:
            if key in self._analytics_cache:
                self._analytics_cache.move_to_end(key)
                return self._analytics_cache[key]
            value = compute()
            self._analytics_cache[key] = value
            while len(self._analytics_cache) > CACHE_SIZE:
                self._analytics_cache.popitem(last=False)
            return value

    def _data_key(self) -> tuple[Any, ...]:
        return tuple(
            self.ws.table(n).attrs.get("fingerprint") if self.ws.has_table(n) else None for n in BUSINESS
        )

    def _model_key(self, rev: str) -> float:
        path = self.ws.root / "models" / f"{rev}.json"
        return path.stat().st_mtime if path.exists() else 0.0

    def _table(self, name: str, required: bool = True) -> pd.DataFrame | None:
        if self.ws.has_table(name):
            return self.ws.table(name)
        if required:
            raise ToolError(
                f"업무 데이터 '{name}'이 작업공간에 없습니다. 업무 데이터를 적재한 뒤 다시 시도하세요."
            )
        return None

    # Revisions and periods ---------------------------------------------------------------------------
    def _actual_revisions(self) -> list[Any]:
        return [r for r in self.ws.revisions() if r.kind == RevisionKind.ACTUAL and r.effective_date]

    def _fit_revision(self, rev: str) -> str:
        """The revision itself when actual, else its nearest actual ancestor."""
        current = self._model(rev).revision
        seen: set[str] = set()
        while current.kind != RevisionKind.ACTUAL:
            parent = current.parent_rev_id
            if not parent or parent in seen:
                raise ToolError(f"{rev}의 기준이 되는 실제 개정을 찾을 수 없습니다.")
            seen.add(current.rev_id)
            current = self._model(parent).revision
        return current.rev_id

    def _enriched(self) -> pd.DataFrame:
        actual = self._actual_revisions()
        key = ("enriched", self._data_key(), tuple((r.rev_id, self._model_key(r.rev_id)) for r in actual))

        def compute() -> pd.DataFrame:
            from cad2anything.analytics import enrich_transactions

            frames = [self.unit_features(r.rev_id) for r in reversed(actual)]
            frames = [frame for frame in frames if frame is not None and len(frame)]
            features = pd.concat(frames, ignore_index=True).drop_duplicates("unit_code") if frames else None
            return enrich_transactions(
                self._table("transactions"),
                self._table("pos_terminals"),
                self._table("tenancies", required=False),
                self._table("brands", required=False),
                features,
            )

        return self._memo(key, compute)

    def _data_range(self) -> tuple[date, date]:
        ts = self._enriched()["ts"]
        return ts.min().date(), ts.max().date()

    def _range(self, start: Any = None, end: Any = None) -> tuple[str, str, int]:
        first, last = self._data_range()
        s = max(_parse_date(start, "start") or first, first)
        e = min(_parse_date(end, "end", end=True) or last, last)
        if s > e:
            raise ToolError(f"요청한 기간에 거래 데이터가 없습니다(데이터 기간 {first}~{last}).")
        return s.isoformat(), e.isoformat(), (e - s).days + 1

    def revision_period(self, rev: str, start: Any = None, end: Any = None) -> tuple[str, str, int]:
        """Inclusive analysis period of an actual revision: its effective date to the next one's eve."""
        revision = self._model(rev).revision
        if revision.kind != RevisionKind.ACTUAL:
            raise ToolError(HYPOTHETICAL_HINT)
        first, last = self._data_range()
        actual = self._actual_revisions()
        ids = [r.rev_id for r in actual]
        rev_start, rev_end = first, last
        if rev in ids:
            index = ids.index(rev)
            rev_start = date.fromisoformat(actual[index].effective_date)
            if index + 1 < len(actual):
                rev_end = date.fromisoformat(actual[index + 1].effective_date) - timedelta(days=1)
        s = max(_parse_date(start, "start") or rev_start, first)
        e = min(_parse_date(end, "end", end=True) or rev_end, last)
        if s > e:
            raise ToolError(f"{rev}의 분석 기간에 거래 데이터가 없습니다(데이터 기간 {first}~{last}).")
        return s.isoformat(), e.isoformat(), (e - s).days + 1

    # Resolution helpers ------------------------------------------------------------------------------
    def _resolve_category(self, text: Any) -> str:
        value = str(text or "").strip()
        if value.upper() in CATEGORIES:
            return value.upper()
        exact = [code for code, cat in CATEGORIES.items() if cat.name_ko == value]
        partial = [code for code, cat in CATEGORIES.items() if value and value in cat.name_ko]
        matches = exact or partial
        if len(matches) == 1:
            return matches[0]
        options = ", ".join(f"{code}({cat.name_ko})" for code, cat in CATEGORIES.items())
        raise ToolError(f"카테고리 '{text}'를 특정할 수 없습니다. 사용 가능한 값: {options}")

    def _resolve_categories(self, text: Any) -> list[str]:
        """One category, or every category of a group such as F&B (식음료)."""
        group = GROUP_ALIASES.get(str(text or "").strip().upper().replace(" ", ""))
        return list(CATEGORY_GROUPS[group]) if group else [self._resolve_category(text)]

    def _resolve_brand(self, text: Any) -> str:
        value = str(text or "").strip()
        brands = self._table("brands", required=False)
        if brands is None or not value:
            if not value:
                raise ToolError("브랜드 ID나 이름이 필요합니다.")
            return value
        ids = brands["brand_id"].astype(str)
        if (ids == value).any():
            return value
        names = brands["brand_name"].astype(str)
        exact = brands[names == value]
        partial = brands[names.str.contains(re.escape(value), case=False, regex=True)]
        matches = exact if len(exact) else partial
        if len(matches) == 1:
            return str(matches["brand_id"].iloc[0])
        if len(matches) > 1:
            listed = ", ".join(f"{r.brand_id}({r.brand_name})" for r in matches.head(5).itertuples())
            raise ToolError(f"브랜드 '{text}' 후보가 여러 개입니다: {listed}")
        raise ToolError(f"브랜드 '{text}'를 찾을 수 없습니다.")

    def _resolve_item(self, level: str, text: Any) -> str:
        if level == "category":
            return self._resolve_category(text)
        if level == "brand":
            return self._resolve_brand(text)
        if level == "floor":
            return _floor_code(text)
        return str(text).strip().upper()

    def _item_name(self, level: str, item: str) -> str:
        if level == "category":
            return CATEGORIES[item].name_ko if item in CATEGORIES else item
        brands = self._table("brands", required=False) if level == "brand" else None
        if brands is not None:
            match = brands.loc[brands["brand_id"].astype(str) == item, "brand_name"]
            if len(match):
                return str(match.iloc[0])
        return item

    # Cached computations -----------------------------------------------------------------------------
    def productivity_frame(
        self, rev: str, level: str = "unit", start: Any = None, end: Any = None
    ) -> pd.DataFrame:
        """Annualized space productivity for an actual revision's period (``attrs['period']``)."""
        if level not in LEVELS:
            raise ToolError(f"level은 {', '.join(LEVELS)} 중 하나여야 합니다.")
        s, e, days = self.revision_period(rev, start, end)
        key = ("productivity", rev, level, s, e, self._data_key(), self._model_key(rev))

        def compute() -> pd.DataFrame:
            from cad2anything.analytics import space_productivity

            frame = space_productivity(
                self._enriched(),
                self.unit_features(rev),
                self._table("tenancies", required=False),
                s,
                e,
                level=level,
            )
            factor = 365.0 / days
            for column in ANNUALIZED:
                if column in frame:
                    frame[column] = frame[column] * factor
            frame.attrs["period"] = {"start": s, "end": e, "days": days}
            return frame

        return self._memo(key, compute)

    def _drivers(self, rev: str) -> Any:
        s, e, _ = self.revision_period(rev)
        key = ("drivers", rev, s, e, self._data_key(), self._model_key(rev))

        def compute() -> Any:
            from cad2anything.analytics import driver_decomposition

            return driver_decomposition(self.productivity_frame(rev, "unit"), self.unit_features(rev))

        return self._memo(key, compute)

    def _driver_texts(self, rev: str) -> dict[str, str]:
        try:
            return self._drivers(rev).explanations
        except ValueError:  # too few units with sales and features to fit the regression
            return {}

    def _adjacency(self, rev: str) -> pd.DataFrame:
        def compute() -> pd.DataFrame:
            from cad2anything.spatial.features import adjacency_pairs

            return adjacency_pairs(self._model(rev))

        return self._memo(("adjacency", rev, self._model_key(rev)), compute)

    def _pairs(
        self, level: str, segment: dict[str, Any] | None, s: str, e: str, basket: str, min_customers: int
    ) -> pd.DataFrame:
        key = (
            "pairs",
            level,
            json.dumps(segment, sort_keys=True),
            s,
            e,
            basket,
            min_customers,
            self._data_key(),
        )

        def compute() -> pd.DataFrame:
            from cad2anything.analytics import cross_shopping

            return cross_shopping(
                self._enriched(),
                self._table("customers", required=False),
                level=level,
                segment=segment,
                start=s,
                end=e,
                min_customers=min_customers,
                basket=basket,
            )

        try:
            return self._memo(key, compute)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _layout_inputs(self, rev: str) -> dict[str, Any]:
        fit_rev = self._fit_revision(rev)
        s, e, days = self.revision_period(fit_rev)
        key = ("layout", rev, fit_rev, s, e, self._data_key(), self._model_key(rev), self._model_key(fit_rev))

        def compute() -> dict[str, Any]:
            from cad2anything.layout import assignment_from_features, fit_scoring_model

            brands = self._table("brands")
            model = fit_scoring_model(
                self.productivity_frame(fit_rev, "unit"), self.unit_features(fit_rev), brands
            )
            features = self.unit_features(rev)
            return {
                "fit_rev": fit_rev,
                "period": {"start": s, "end": e, "days": days},
                "model": model,
                "brands": brands,
                "features": features,
                "assignment": assignment_from_features(features),
                "adjacency": self._adjacency(rev),
                "affinity": self._pairs("category", None, s, e, "customer_day", 20),
            }

        return self._memo(key, compute)

    def expected_unit_sales(self, rev: str) -> pd.DataFrame:
        """Expected annual sales per unit from the layout scoring model (estimates; works for any revision)."""

        def compute() -> pd.DataFrame:
            from cad2anything.layout import score_assignment

            inputs = self._layout_inputs(rev)
            score = score_assignment(
                inputs["assignment"],
                inputs["features"],
                inputs["brands"],
                inputs["model"],
                affinity_pairs=inputs["affinity"],
                adjacency_pairs=inputs["adjacency"],
            )
            units = score.units.copy()
            units["expected_sales_per_m2"] = units["expected_sales"] / units["area_m2"].where(
                units["area_m2"] > 0
            )
            return units

        return self._memo(("expected", rev, self._data_key(), self._model_key(rev)), compute)

    # Productivity ------------------------------------------------------------------------------------
    @staticmethod
    def _productivity_row(record: dict[str, Any], drivers: dict[str, str]) -> dict[str, Any]:
        row = {RENAMED.get(k, k): record[k] for k in PRODUCTIVITY_FIELDS if k in record}
        if record.get("low_efficiency") and str(record.get("unit_code")) in drivers:
            row["driver"] = drivers[str(record["unit_code"])]
        return row

    def analytics_productivity(
        self,
        rev: str,
        floor: str | None = None,
        level: str = "unit",
        start: str | None = None,
        end: str | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        frame = self.productivity_frame(rev, level, start, end)
        period = frame.attrs.get("period")
        if floor:
            if "floor" not in frame:
                raise ToolError("level=category에서는 floor로 거를 수 없습니다.")
            floor = _floor_code(floor)
            frame = frame[frame["floor"] == floor]
            if frame.empty:
                raise ToolError(f"{rev}에 {floor}층 실적이 없습니다.")
        frame = frame.sort_values("sales_per_m2", ascending=False, na_position="last")
        drivers = self._driver_texts(rev) if level == "unit" and not (start or end) else {}
        rows = [self._productivity_row(r, drivers) for r in frame.to_dict(orient="records")]
        low = [row for row in rows if row.get("low_efficiency")]
        limit = max(1, min(int(top or DEFAULT_LIMIT), 500))
        values = frame["sales_per_m2"].replace([np.inf, -np.inf], np.nan).dropna()
        sales, area = float(frame["sales"].sum()), float(frame["area_m2"].sum())
        return _json(
            {
                "rev": rev,
                "level": level,
                "floor": floor,
                "period": period,
                "summary": {
                    "rows": len(rows),
                    "sales_krw": sales,
                    "area_m2": area,
                    "sales_per_m2": sales / area if area else None,
                    "low_efficiency": len(low),
                },
                "metric": {
                    "name": "sales_per_m2",
                    "label": "연환산 ㎡당 매출(원)",
                    "min": values.min() if len(values) else None,
                    "max": values.max() if len(values) else None,
                },
                "low_efficiency": low[:DEFAULT_LIMIT],
                "rows": rows[:limit],
                "truncated": len(rows) > limit,
                "note": SALES_NOTE,
            }
        )

    def analytics_drivers(self, rev: str, floor: str | None = None) -> dict[str, Any]:
        try:
            decomposition = self._drivers(rev)
        except ValueError as exc:
            raise ToolError(f"요인 분해를 계산할 수 없습니다: {exc}") from exc
        contributions = decomposition.contributions
        if floor:
            contributions = contributions[contributions["floor"] == _floor_code(floor)]
        low = contributions[contributions["low_efficiency"].astype(bool)]
        units = [
            {
                "unit_code": r["unit_code"],
                "floor": r["floor"],
                "category": CATEGORIES[r["category_code"]].name_ko
                if r["category_code"] in CATEGORIES
                else r["category_code"],
                "main_driver": r["main_driver_ko"],
                "main_driver_effect_pct": r["main_driver_effect_pct"],
                "effects_pct": {label: _pct(r[key]) for key, label in DRIVER_COMPONENTS.items() if key in r},
                "explanation": r["explanation"],
            }
            for r in low.to_dict(orient="records")
        ]
        coefficients = decomposition.coefficients.to_dict(orient="records")
        return _json(
            {
                "rev": rev,
                "floor": floor,
                "period": self.productivity_frame(rev, "unit").attrs.get("period"),
                "label": decomposition.label,
                "r2": decomposition.r2,
                "adj_r2": decomposition.adj_r2,
                "n_obs": decomposition.n_obs,
                "reference_category": decomposition.reference_category,
                "coefficients": coefficients[:30],
                "driver_counts": low["main_driver_ko"].value_counts().to_dict(),
                "low_efficiency_units": units[:DEFAULT_LIMIT],
                "reading_guide": "effects_pct는 매장 전체 평균 대비 각 요인의 영향(%)입니다. 음수가 클수록 하락 요인입니다.",
                "caveat": decomposition.caveat,
                "notes": decomposition.notes,
            }
        )

    # Customers and flows -----------------------------------------------------------------------------
    def analytics_cross_shopping(
        self,
        level: str = "category",
        focal: str | None = None,
        segment: dict[str, Any] | str | None = None,
        start: str | None = None,
        end: str | None = None,
        top: int = 10,
        basket: str = "customer_day",
        min_customers: int = 20,
    ) -> dict[str, Any]:
        if level not in PAIR_LEVELS:
            raise ToolError(f"level은 {', '.join(PAIR_LEVELS)} 중 하나여야 합니다.")
        parsed = parse_segment(segment)
        s, e, _ = self._range(start, end)
        pairs = self._pairs(level, parsed, s, e, basket, int(min_customers))
        k = max(1, min(int(top), 50))
        result: dict[str, Any] = {
            "level": level,
            "segment": parsed,
            "basket": basket,
            "period": {"start": s, "end": e},
            "n_baskets": pairs.attrs.get("n_total"),
            "n_customers": pairs.attrs.get("n_customers"),
            "label": "관측 지표",
            "caveat": PAIR_CAVEAT,
        }
        if focal:
            from cad2anything.analytics import top_partners

            focal_id = self._resolve_item(level, focal)
            partners = top_partners(pairs, focal_id, k=k)
            result["focal"] = focal_id
            result["focal_name"] = self._item_name(level, focal_id)
            result["partners"] = partners.to_dict(orient="records")
            if partners.empty:
                result["note"] = f"{focal_id}와 함께 구매한 고객이 {min_customers}명 이상인 조합이 없습니다."
        else:
            columns = [
                "item_a",
                "item_b",
                "name_a",
                "name_b",
                "lift",
                "jaccard",
                "confidence_a_b",
                "confidence_b_a",
                "support",
                "n_customers",
            ]
            result["pairs"] = pairs[columns].head(k).to_dict(orient="records")
            result["truncated"] = len(pairs) > k
        return _json(result)

    def analytics_floor_transitions(
        self, segment: dict[str, Any] | str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        from cad2anything.analytics import floor_transitions

        parsed = parse_segment(segment)
        s, e, _ = self._range(start, end)
        key = ("flows", json.dumps(parsed, sort_keys=True), s, e, self._data_key())
        flows = self._memo(
            key,
            lambda: floor_transitions(
                self._enriched(), self._table("customers", required=False), segment=parsed, start=s, end=e
            ),
        )
        floors = list(flows.floors)
        matrix = flows.probabilities.reindex(index=floors, columns=floors)
        counts = flows.counts.reindex(index=floors, columns=floors).fillna(0)
        top = [
            {"from": a, "to": b, "count": counts.loc[a, b], "probability": matrix.loc[a, b]}
            for a in floors
            for b in floors
            if a != b and counts.loc[a, b] > 0
        ]
        top.sort(key=lambda row: -row["count"])
        return _json(
            {
                "segment": parsed,
                "period": {"start": s, "end": e},
                "floors": floors,
                "matrix": matrix.round(4).to_numpy().tolist(),
                "top_transitions": top[:10],
                "fountain": {
                    "base_floor": flows.base_floor,
                    "share": flows.fountain_share,
                    "customer_days": flows.fountain_customer_days,
                },
                "shower": {
                    "top_floor": flows.top_floor,
                    "share": flows.shower_share,
                    "customer_days": flows.shower_customer_days,
                },
                "n_customer_days": flows.n_customer_days,
                "n_transitions": flows.n_transitions,
                "notes": flows.notes,
                "caveat": flows.caveat,
            }
        )

    # Effects -----------------------------------------------------------------------------------------
    def _near_pairs(self, rev: str, radius_m: float) -> pd.DataFrame:
        """Same-floor unit pairs whose boundaries are within ``radius_m`` (across an aisle counts)."""

        def compute() -> pd.DataFrame:
            by_floor: dict[str, list[Any]] = {}
            for unit in self._model(rev).units():
                by_floor.setdefault(unit.floor or "", []).append(unit)
            rows = []
            for floor, units in by_floor.items():
                for i, a in enumerate(units):
                    for b in units[i + 1 :]:
                        distance = a.geometry.distance(b.geometry)
                        if distance <= radius_m:
                            rows.append(
                                {
                                    "unit_a": a.attributes.get("unit_code"),
                                    "unit_b": b.attributes.get("unit_code"),
                                    "floor": floor,
                                    "distance_m": distance,
                                }
                            )
            return pd.DataFrame(rows, columns=["unit_a", "unit_b", "floor", "distance_m"])

        return self._memo(("near", rev, float(radius_m), self._model_key(rev)), compute)

    def analytics_adjacency_effect(
        self, rev: str, neighbor_category: str, neighborhood: str = "near", radius_m: float = 8.0
    ) -> dict[str, Any]:
        from cad2anything.analytics import adjacency_effect

        if neighborhood not in ("near", "wall"):
            raise ToolError("neighborhood는 near(통로 건너편 포함 반경) 또는 wall(벽 공유) 중 하나입니다.")
        category = self._resolve_category(neighbor_category)
        pairs = self._near_pairs(rev, float(radius_m)) if neighborhood == "near" else self._adjacency(rev)
        effect = adjacency_effect(
            self.productivity_frame(rev, "unit"), pairs, self.unit_features(rev), category
        )
        data = effect.to_dict()
        table = data.pop("table", None) or []
        data["table"] = table[:20]
        data["rev"] = rev
        data["neighborhood"] = (
            f"경계 사이 {float(radius_m):g}m 이내(통로 건너편 포함)" if neighborhood == "near" else "벽 공유"
        )
        data["period"] = self.productivity_frame(rev, "unit").attrs.get("period")
        return _json(data)

    def analytics_before_after(
        self, change_date: str, unit_codes: list[str] | str, window_days: int = 90, key: str = "unit_code"
    ) -> dict[str, Any]:
        from cad2anything.analytics import before_after

        if key not in ("unit_code", "brand_id"):
            raise ToolError("key는 unit_code 또는 brand_id여야 합니다.")
        codes = (
            [c.strip() for c in unit_codes.split(",")] if isinstance(unit_codes, str) else list(unit_codes)
        )
        if key == "brand_id":
            codes = [self._resolve_brand(code) for code in codes]
        else:
            codes = [code.upper() for code in codes]
        if not codes:
            raise ToolError("비교할 매장 코드가 필요합니다.")
        day = _parse_date(change_date, "change_date")
        if day is None:
            raise ToolError("change_date(YYYY-MM-DD)가 필요합니다.")
        result = before_after(self._enriched(), day.isoformat(), codes, window_days=int(window_days), key=key)
        data = result.to_dict()
        units = pd.DataFrame(data.pop("units"))
        if len(units):
            treated = units[units["treated"].astype(bool)].drop(columns=["treated", "control"])
            data["treated"] = treated
            data["n_control"] = int(units["control"].astype(bool).sum())
            if key == "unit_code" and (treated["pre_sales"] <= 0).any():
                data["hint"] = (
                    "변경 전 매출이 없는 매장이 있습니다(신규 입점·공실). 브랜드 이전 효과를 보려면 "
                    "key=brand_id로 브랜드를 지정하세요."
                )
        return _json(data)

    def analytics_time_patterns(
        self,
        by: list[str] | str = "hour",
        floor: str | None = None,
        category: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]:
        from cad2anything.analytics import time_patterns

        dims = [by] if isinstance(by, str) else list(by)
        unknown = [d for d in dims if d not in TIME_DIMENSIONS]
        if unknown or not dims:
            raise ToolError(f"by는 {', '.join(TIME_DIMENSIONS)} 중에서 고릅니다: {unknown}")
        s, e, _ = self._range(start, end)
        frame = self._enriched()
        if floor:
            frame = frame[frame["floor"] == _floor_code(floor)]
        if category:
            frame = frame[frame["category"].isin(self._resolve_categories(category))]
        if frame.empty:
            raise ToolError("조건에 맞는 거래가 없습니다.")
        table = time_patterns(frame, by=dims, start=s, end=e)
        k = max(1, min(int(limit), 300))
        return _json(
            {
                "by": dims,
                "floor": floor,
                "category": category,
                "period": {"start": s, "end": e},
                "rows": table.head(k).to_dict(orient="records"),
                "truncated": len(table) > k,
                "note": "매출은 기간 합계(전체 규모 환산)입니다. share는 전체 대비, share_in_group은 그룹 안 비중입니다.",
            }
        )

    # Customer journeys --------------------------------------------------------------------------------
    def _journey_rows(self) -> pd.DataFrame:
        enriched = self._enriched()

        def compute() -> pd.DataFrame:
            from cad2anything.analytics.journey import purchase_rows

            return purchase_rows(enriched)

        return self._memo(("journey_rows", id(enriched)), compute)

    def _revision_on(self, day: date) -> str:
        actual = sorted(self._actual_revisions(), key=lambda r: r.effective_date)
        if not actual:
            raise ToolError("실제 개정이 없어 동선을 그릴 도면을 정할 수 없습니다.")
        chosen = actual[0].rev_id
        for revision in actual:
            if date.fromisoformat(revision.effective_date) <= day:
                chosen = revision.rev_id
        return chosen

    def _route_planner(self, rev: str) -> Any:
        from cad2anything.spatial.routes import RoutePlanner

        return self._memo(("routes", rev, self._model_key(rev)), lambda: RoutePlanner(self._model(rev)))

    def _customer_profile(self, customer_id: str) -> dict[str, Any]:
        customers = self._table("customers", required=False)
        if customers is None:
            return {}
        match = customers.loc[customers["pseudo_id"].astype(str) == customer_id]
        if match.empty:
            return {}
        row = match.iloc[0]
        return {
            key: str(row[key]) for key in ("age_band", "gender", "tier") if key in row and pd.notna(row[key])
        }

    def customer_journey(
        self,
        customer_id: str | None = None,
        date: str | None = None,
        segment: dict[str, Any] | str | None = None,
        rank: int = 1,
        min_stops: int = 3,
        start: str | None = None,
        end: str | None = None,
        with_routes: bool = True,
    ) -> dict[str, Any]:
        """One customer's purchase sequence on one day, optionally with estimated walkway routes."""
        from cad2anything.analytics.journey import (
            JOURNEY_CAVEAT,
            filter_rows,
            journey_candidates,
            journey_stops,
            segment_label,
            weekday_ko,
        )

        rows = self._journey_rows()
        parsed = parse_segment(segment)
        day = _parse_date(date, "date")
        rank = max(1, int(rank or 1))
        alternatives: list[dict[str, Any]] = []
        if customer_id:
            customer_id = str(customer_id).strip().upper()
            own = rows.loc[rows["customer_key"] == customer_id]
            if day is not None:
                own = own.loc[own["day"] == day]
            if own.empty:
                when = f" {day.isoformat()}에" if day else ""
                raise ToolError(f"고객 {customer_id}의{when} 결제 기록이 없습니다.")
            if day is None:
                day = journey_candidates(own, min_stops=1).iloc[0]["date"]
            query: dict[str, Any] = {"customer_id": customer_id, "date": day.isoformat()}
        else:
            first, last = (
                (day, day) if day else (_parse_date(start, "start"), _parse_date(end, "end", end=True))
            )
            customers = self._table("customers", required=False)
            key = (
                "journeys",
                id(rows),
                json.dumps(parsed, sort_keys=True),
                str(first),
                str(last),
                int(min_stops),
            )
            ranked = self._memo(
                key,
                lambda: journey_candidates(filter_rows(rows, customers, parsed, first, last), int(min_stops)),
            )
            if len(ranked) < rank:
                if ranked.empty:
                    raise ToolError(
                        "조건에 맞는 고객 동선이 없습니다. 조건을 넓히거나 min_stops를 줄여 보세요."
                    )
                raise ToolError(
                    f"조건에 맞는 고객 동선은 {len(ranked)}개뿐입니다. 더 보여 줄 고객이 없습니다."
                )
            chosen = ranked.iloc[rank - 1]
            customer_id, day = str(chosen["customer_id"]), chosen["date"]
            query = {"segment": parsed, "rank": rank}
            alternatives = [
                {
                    "rank": rank + i + 1,
                    "customer_id": str(r.customer_id),
                    "date": r.date.isoformat(),
                    "stops": int(r.stops),
                }
                for i, r in enumerate(ranked.iloc[rank : rank + 3].itertuples(index=False))
            ]
        stops, duration = journey_stops(rows, customer_id, day)
        if not stops:
            raise ToolError(f"고객 {customer_id}의 {day.isoformat()} 결제 기록이 없습니다.")
        profile = self._customer_profile(customer_id)
        label = segment_label(profile)
        rev = self._revision_on(day)
        journey: dict[str, Any] = {
            "title": f"{label} 고객의 쇼핑 동선",
            "customer": {"id": customer_id, "segment": label, **profile},
            "date": day.isoformat(),
            "weekday": weekday_ko(day),
            "rev": rev,
            "query": query,
            "stops": stops,
            "summary": {
                "stops": len(stops),
                "floors": list(dict.fromkeys(s["floor"] for s in stops if s["floor"])),
                "total_krw": sum(s["amount_krw"] for s in stops),
                "duration_min": duration,
            },
            "caveat": JOURNEY_CAVEAT,
        }
        if alternatives:
            journey["alternatives"] = alternatives
        if with_routes:
            self._add_routes(journey)
        return _json(journey)

    def _add_routes(self, journey: dict[str, Any]) -> None:
        planner = self._route_planner(journey["rev"])
        stops = journey["stops"]
        for stop in stops:
            if planner.has_unit(stop["unit_code"]):
                stop["x"], stop["y"] = planner.access_xy(stop["unit_code"])
        segments, transitions, walk = [], [], 0.0
        for a, b in zip(stops, stops[1:], strict=False):
            if not (planner.has_unit(a["unit_code"]) and planner.has_unit(b["unit_code"])):
                continue
            route = planner.route(a["unit_code"], b["unit_code"])
            walk += route.length_m
            for leg in route.legs:
                if len(leg.points) >= 2:
                    points = [list(point) for point in leg.points]
                    segments.append(
                        {
                            "floor": leg.floor,
                            "points": points,
                            "from_order": a["order"],
                            "to_order": b["order"],
                        }
                    )
            if route.via:
                transitions.append(
                    {
                        "from_order": a["order"],
                        "to_order": b["order"],
                        "from_floor": a["floor"],
                        "to_floor": b["floor"],
                        "via": route.via,
                    }
                )
        journey["segments"] = segments
        journey["transitions"] = transitions
        journey["summary"]["walk_m"] = int(round(walk / 10.0) * 10)

    def analytics_customer_journey(
        self,
        customer_id: str | None = None,
        date: str | None = None,
        segment: dict[str, Any] | str | None = None,
        rank: int = 1,
        min_stops: int = 3,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        return self.customer_journey(
            customer_id, date, segment, rank, min_stops, start, end, with_routes=False
        )

    # Layout ------------------------------------------------------------------------------------------
    def layout_generate_alternatives(
        self,
        rev: str,
        floors: list[str] | None = None,
        keep_anchors: bool = True,
        area_tolerance: float | None = None,
        max_moves: int | None = None,
        n_alternatives: int = 3,
        movable_categories: list[str] | None = None,
        allow_cross_floor: bool = True,
        save: bool = True,
    ) -> dict[str, Any]:
        from cad2anything.layout import LayoutConstraints, generate_alternatives, to_hypothetical_revision

        inputs = self._layout_inputs(rev)
        options: dict[str, Any] = {
            "floors": [_floor_code(f) for f in floors] if floors else None,
            "keep_anchors": bool(keep_anchors),
            "allow_cross_floor": bool(allow_cross_floor),
        }
        if area_tolerance is not None:
            options["area_tolerance"] = float(area_tolerance)
        if max_moves is not None:
            options["max_moves"] = int(max_moves)
        if movable_categories:
            options["movable_categories"] = [self._resolve_category(c) for c in movable_categories]
        try:
            constraints = LayoutConstraints(**options)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"제약 조건이 올바르지 않습니다: {exc}") from exc
        alternatives = generate_alternatives(
            inputs["assignment"],
            inputs["features"],
            inputs["brands"],
            inputs["adjacency"],
            inputs["model"],
            constraints,
            n_alternatives=max(1, min(int(n_alternatives), 5)),
            seed=7,
            affinity_pairs=inputs["affinity"],
        )
        base = self._model(rev)
        results = []
        for alternative in alternatives:
            entry = _alternative_summary(alternative)
            if save and alternative.moves:
                rev_id = self._next_hypothetical_id()
                model, change_set = to_hypothetical_revision(base, alternative, rev_id=rev_id)
                meta = {
                    "source": "hypothetical",
                    "base": rev,
                    "alt_id": alternative.alt_id,
                    "origin": "layout",
                }
                self.ws.save_model(model, meta)
                self.ws.save_change_set(change_set, {"source": "layout", "alt_id": alternative.alt_id})
                entry["rev_id"] = rev_id
            results.append(entry)
        result: dict[str, Any] = {
            "rev": rev,
            "fit_rev": inputs["fit_rev"],
            "period": inputs["period"],
            "constraints": constraints.to_dict(),
            "label": "추정치",
            "alternatives": results,
            "caveats": alternatives[0].caveats if alternatives else [],
            "note": LAYOUT_NOTE,
        }
        if not results:
            result["note"] = (
                "제약을 지키면서 기대 매출을 높이는 대안을 찾지 못했습니다. 제약을 완화해 보세요."
            )
        return _json(result)

    def whatif_score_moves(self, rev: str, moves: list[dict[str, str]]) -> dict[str, Any]:
        from cad2anything.layout import LayoutConstraints, check_constraints, score_assignment

        if not moves:
            raise ToolError("moves에 {brand_id, to_unit} 항목이 하나 이상 필요합니다.")
        inputs = self._layout_inputs(rev)
        base = dict(inputs["assignment"])
        proposed = dict(base)
        applied = []
        for move in moves:
            brand_id = self._resolve_brand(
                move.get("brand_id") or move.get("brand") or move.get("brand_name")
            )
            target = str(move.get("to_unit") or "").strip().upper()
            if target not in proposed:
                raise ToolError(f"{rev}에 매장 {target}가 없습니다.")
            source = next((unit for unit, brand in proposed.items() if brand == brand_id), None)
            if source is None:
                raise ToolError(f"{rev}에 브랜드 {brand_id}가 입점해 있지 않습니다.")
            if source == target:
                continue
            displaced = proposed[target]
            proposed[target], proposed[source] = brand_id, displaced
            applied.append(
                {"brand_id": brand_id, "from_unit": source, "to_unit": target, "swapped_with": displaced}
            )
        if not applied:
            raise ToolError("적용할 이동이 없습니다(이미 해당 매장에 있음).")
        common = {"affinity_pairs": inputs["affinity"], "adjacency_pairs": inputs["adjacency"]}
        before = score_assignment(base, inputs["features"], inputs["brands"], inputs["model"], **common)
        after = score_assignment(proposed, inputs["features"], inputs["brands"], inputs["model"], **common)
        units_before = before.units.set_index("unit_code")
        units_after = after.units.set_index("unit_code")
        changed = sorted(unit for unit in proposed if proposed[unit] != base[unit])
        unit_rows, half_widths = [], []
        for unit in changed:
            b, a = units_before.loc[unit], units_after.loc[unit]
            half_widths.append(max(float(a["high"]) - float(a["expected_sales"]), 0.0))
            unit_rows.append(
                {
                    "unit_code": unit,
                    "floor": a.get("floor"),
                    "brand_before": base[unit],
                    "brand_after": proposed[unit],
                    "expected_before": b["expected_sales"],
                    "expected_after": a["expected_sales"],
                    "delta": float(a["expected_sales"]) - float(b["expected_sales"]),
                }
            )
        delta = after.total - before.total
        half_width = math.sqrt(sum(w * w for w in half_widths))
        floors_before = before.by_floor().set_index("floor")["expected_sales"]
        floors_after = after.by_floor().set_index("floor")["expected_sales"]
        by_floor = [
            {"floor": f, "delta": float(floors_after.get(f, 0.0)) - float(floors_before.get(f, 0.0))}
            for f in floors_after.index
            if abs(float(floors_after.get(f, 0.0)) - float(floors_before.get(f, 0.0))) > 0.5
        ]
        checks = check_constraints(
            base,
            proposed,
            inputs["features"],
            inputs["brands"],
            inputs["adjacency"],
            LayoutConstraints(max_moves=max(6, 2 * len(applied))),
        )
        return _json(
            {
                "rev": rev,
                "fit_rev": inputs["fit_rev"],
                "label": "추정치",
                "moves": applied,
                "total_before": before.total,
                "total_after": after.total,
                "delta_krw": delta,
                "delta_range_krw": [delta - half_width, delta + half_width],
                "units": unit_rows,
                "by_floor": by_floor,
                "constraint_checks": checks.to_dict(orient="records"),
                "caveats": after.caveats,
                "note": "연간 기대 매출(전체 규모 환산) 기준 추정치입니다. 가상 개정을 만들려면 revision_create_hypothetical을 쓰세요.",
            }
        )
