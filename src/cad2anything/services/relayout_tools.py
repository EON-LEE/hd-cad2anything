"""Seasonal relayout workflow: brief -> diagnosis -> alternatives -> human decision -> drawing check -> report.

A brief is human input (``business/relayout_briefs/{brief_id}.yaml``): season, base revision, floors,
objective, entering and exiting brands. Each brief has a project (``projects/relayout/{brief_id}.json``)
that stores the diagnosis, the plan, the decision and the drawing check. Money values are annualized KRW;
everything about future layouts is an estimate of the layout scoring model (``cad2anything.layout``).
"""

from __future__ import annotations

import calendar
import functools
import html
import math
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from cad2anything.analytics.prep import as_list, floor_sort_key
from cad2anything.catalog import CATEGORIES
from cad2anything.model import EntityKind, RevisionKind
from cad2anything.services.analytics_tools import _floor_code, _json
from cad2anything.services.errors import NotFoundToolError, ToolError

SEASON_MONTHS = {"SS": (3, 4, 5, 6, 7, 8), "FW": (9, 10, 11, 12, 1, 2)}
SEASON_LABELS = {"SS": "S/S(3~8월)", "FW": "F/W(9~2월)"}
SEASON_ALIASES = {
    "SS": "SS",
    "S/S": "SS",
    "SPRING": "SS",
    "SUMMER": "SS",
    "봄": "SS",
    "여름": "SS",
    "봄여름": "SS",
    "봄·여름": "SS",
    "FW": "FW",
    "F/W": "FW",
    "FALL": "FW",
    "AUTUMN": "FW",
    "WINTER": "FW",
    "가을": "FW",
    "겨울": "FW",
    "가을겨울": "FW",
    "가을·겨울": "FW",
}
OBJECTIVE_LABELS = {"sales": "시즌 가중 기대 매출", "commission": "시즌 가중 수수료 수익"}
MOVABLE = ("expiring", "all")
STATUS_ORDER = ("draft", "diagnosed", "planned", "selected", "checked")
BRIEF_DIR = Path("business") / "relayout_briefs"
PROJECT_DIR = Path("projects") / "relayout"
BRIEF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
DEFAULT_AREA_TOLERANCE = 0.25
AREA_CHANGE_PCT = 3.0
MAX_ALTERNATIVES = 5
KST = timezone(timedelta(hours=9))
# Project files are read-modify-written by several requests (REST, MCP, upload hook); serialize them.
_PROJECT_LOCK = threading.RLock()
CHANGE_LABELS = {"move": "이전", "entry": "입점", "exit": "퇴점"}
CHECK_LABELS = {
    "match": "일치",
    "different_unit": "다른 매장",
    "missing": "도면에 없음",
    "not_exited": "퇴점 안 됨",
}
PLAN_CAVEATS = (
    "기대 매출과 수수료 수익은 현재 배치 실적으로 맞춘 점수 모델의 추정치이고, 실제 결과를 보장하지 않아요.",
    "시즌 지수는 2년치 카테고리 일평균 매출로 계산했고, 그 사이 배치 변화의 영향이 섞여 있어요.",
    "이전 비용은 공사 기간 동안 해당 매장 기대 매출(목표 가중치 적용)이 없다고 본 단순 추정이에요.",
    "신규 입점 브랜드는 실적이 없어 카테고리 평균과 브랜드 매력도로 추정해 범위가 넓어요.",
)


def _now() -> str:
    return datetime.now(UTC).astimezone(KST).isoformat(timespec="seconds")


def _locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a project-changing method under the project lock (the signature stays inspectable)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _PROJECT_LOCK:
            return fn(*args, **kwargs)

    return wrapper


def _year_shift(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def season_range(season: str, year: int) -> tuple[date, date]:
    """Calendar range of a season: S/S is March to August, F/W is September to February next year."""
    if season == "SS":
        return date(year, 3, 1), date(year, 8, 31)
    return date(year, 9, 1), date(year + 1, 2, calendar.monthrange(year + 1, 2)[1])


def aligned_windows(
    season: str, year: int, first: date, last: date
) -> tuple[tuple[date, date] | None, tuple[date, date] | None]:
    """The latest and the previous same-season windows before ``year``, clipped to the data range and
    aligned so that both cover the same days of the year."""

    def clip(window: tuple[date, date]) -> tuple[date, date] | None:
        start, end = max(window[0], first), min(window[1], last)
        return (start, end) if start <= end else None

    latest = clip(season_range(season, year - 1))
    previous = clip(season_range(season, year - 2))
    if latest is None or previous is None:
        return latest, None
    start = max(latest[0], _year_shift(previous[0], 1))
    end = min(latest[1], _year_shift(previous[1], 1))
    if start > end:
        return latest, None
    return (start, end), (_year_shift(start, -1), _year_shift(end, -1))


def _krw(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(number):
        return "-"
    sign = "-" if number < 0 else ""
    amount = abs(number)
    if amount >= 1e8:
        return f"{sign}{amount / 1e8:,.1f}억 원"
    if amount >= 1e4:
        return f"{sign}{amount / 1e4:,.0f}만 원"
    return f"{sign}{amount:,.0f}원"


def _signed_krw(value: Any) -> str:
    text = _krw(value)
    return text if text.startswith("-") or text == "-" else f"+{text}"


def _more_less(value: Any) -> str:
    """'연 3.2억 원 많아요' or '연 1.1억 원 적어요'."""
    number = float(value or 0.0)
    return f"연 {_krw(abs(number))} {'많아요' if number >= 0 else '적어요'}"


def _name_key(name: Any) -> str:
    return re.sub(r"\s+", "", str(name or "")).lower()


@dataclass
class _Occupancy:
    """Who occupies which unit on some floors of a model (by brand ID and by brand name) and unit areas."""

    by_brand: dict[str, str] = field(default_factory=dict)
    by_unit: dict[str, str] = field(default_factory=dict)
    by_name: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    areas: dict[str, float] = field(default_factory=dict)


def _occupancy(model: Any, floors: set[str]) -> _Occupancy:
    result = _Occupancy()
    for unit in model.units():
        if unit.floor not in floors:
            continue
        code = str(unit.attributes.get("unit_code"))
        result.areas[code] = float(unit.geometry.area)
        if unit.attributes.get("vacant"):
            continue
        brand = unit.attributes.get("brand_id")
        if brand:
            result.by_brand[str(brand)] = code
            result.by_unit[code] = str(brand)
        if unit.attributes.get("brand_name"):
            result.by_name[_name_key(unit.attributes["brand_name"])] = code
            result.names[code] = str(unit.attributes["brand_name"])
    return result


class RelayoutTools:
    """Mixin for Toolkit; needs ``ws``, ``_model``, ``_table``, ``_layout_inputs``, ``productivity_frame``,
    ``unit_table``, ``_enriched``, ``_data_range``, ``_resolve_brand``, ``_resolve_category``, ``_memo``,
    ``_data_key``, ``_has_sales`` and ``latest_sales_rev``."""

    # Briefs ------------------------------------------------------------------------------------------
    def _brief_path(self, brief_id: str) -> Path:
        if not BRIEF_ID_RE.match(str(brief_id or "")):
            raise NotFoundToolError(f"브리프 ID 형식이 올바르지 않아요: {brief_id}")
        return self.ws.root / BRIEF_DIR / f"{brief_id}.yaml"

    def _brief_ids(self) -> list[str]:
        folder = self.ws.root / BRIEF_DIR
        return sorted(p.stem for p in folder.glob("*.yaml")) if folder.exists() else []

    def _raw_brief(self, brief_id: str) -> dict[str, Any]:
        path = self._brief_path(brief_id)
        if not path.is_file():
            known = ", ".join(self._brief_ids()) or "없음"
            raise NotFoundToolError(f"개편 브리프 {brief_id}가 없어요. 등록된 브리프: {known}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ToolError(f"개편 브리프 {brief_id} 파일 형식이 올바르지 않아요.")
        return data

    def _brief(self, brief_id: str) -> dict[str, Any]:
        brief, errors, _ = self.relayout_validate_brief(self._raw_brief(brief_id))
        if errors:
            raise ToolError(f"개편 브리프 {brief_id}에 오류가 있어요: " + " / ".join(errors))
        return brief

    def _catalog(self) -> pd.DataFrame:
        brands = self._table("brands")
        return brands.assign(brand_id=brands["brand_id"].astype(str)).drop_duplicates("brand_id")

    def _placements(self, rev: str) -> dict[str, str]:
        """brand_id -> unit_code for the occupied units of a revision."""
        return {
            str(u.attributes["brand_id"]): str(u.attributes.get("unit_code"))
            for u in self._model(rev).units()
            if u.attributes.get("brand_id") and not u.attributes.get("vacant")
        }

    def _lookup_brand(self, catalog: pd.DataFrame, value: Any) -> pd.Series | None:
        text = str(value or "").strip()
        if not text:
            return None
        hit = catalog[catalog["brand_id"] == text]
        if hit.empty:
            hit = catalog[catalog["brand_name"].astype(str).str.strip() == text]
        return hit.iloc[0] if len(hit) == 1 else None

    def _new_brand(
        self, spec: dict[str, Any], index: int, catalog: pd.DataFrame
    ) -> tuple[dict[str, Any], list[str]]:
        """A brand that is not in the catalog yet, with category defaults for missing fields."""
        errors: list[str] = []
        name = str(spec.get("brand_name") or "").strip()
        try:
            category = self._resolve_category(spec.get("category"))
        except ToolError:
            return {}, [
                f"입점 {index}: 카탈로그에 없는 브랜드 '{name or spec.get('brand_id')}'는 category가 필요해요."
            ]
        peers = catalog[catalog["category"] == category]
        area = spec.get("target_area_m2")
        if isinstance(area, list | tuple) and len(area) == 2:
            lo, hi = float(area[0]), float(area[1])
        elif peers.empty:
            lo, hi = CATEGORIES[category].unit_area_range_m2
        else:
            lo = float(peers["target_area_min_m2"].median())
            hi = float(peers["target_area_max_m2"].median())
        if lo <= 0 or hi < lo:
            errors.append(f"입점 {index}: target_area_m2는 [최소, 최대] 면적이어야 해요.")
        floors = [_floor_code(f) for f in as_list(spec.get("preferred_floors"))]
        if not floors and not peers.empty:
            counts = pd.Series([f for v in peers["preferred_floors"] for f in as_list(v)]).value_counts()
            floors = [str(counts.index[0])] if len(counts) else []
        conflicts = ""
        if not peers.empty and "adjacency_conflicts" in peers:
            common = peers["adjacency_conflicts"].dropna().astype(str)
            conflicts = str(common.mode().iloc[0]) if len(common) else ""
        brand_id = str(spec.get("brand_id") or f"NEW-{index:02d}")
        return {
            "brand_id": brand_id,
            "brand_name": name or brand_id,
            "category": category,
            "category_name": CATEGORIES[category].name_ko,
            "anchor": False,
            "target_area_min_m2": lo,
            "target_area_max_m2": hi,
            "preferred_floors": ";".join(floors),
            "adjacency_conflicts": conflicts,
            "attractiveness": float(spec.get("attractiveness") or 1.0),
            "new_brand": True,
        }, errors

    def relayout_validate_brief(self, data: Any) -> tuple[dict[str, Any], list[str], list[str]]:
        """Normalized brief plus errors and warnings (Korean). Used for uploads and before every step."""
        errors: list[str] = []
        warnings: list[str] = []
        if not isinstance(data, dict):
            return {}, ["브리프는 키-값 객체(YAML 매핑)여야 해요."], []
        brief: dict[str, Any] = {}
        for key in ("brief_id", "title", "season", "year", "effective_date", "base_rev", "floors"):
            if data.get(key) in (None, "", []):
                errors.append(f"필수 항목 {key}가 없어요.")
        if errors:
            return {}, errors, warnings
        brief_id = str(data["brief_id"]).strip()
        if not BRIEF_ID_RE.match(brief_id):
            errors.append("brief_id는 영문·숫자·-·_로 1~32자여야 해요.")
        season = SEASON_ALIASES.get(str(data["season"]).strip().upper()) or SEASON_ALIASES.get(
            str(data["season"]).strip()
        )
        if season is None:
            errors.append("season은 SS(봄·여름) 또는 FW(가을·겨울)여야 해요.")
        try:
            year = int(data["year"])
            if not 2000 <= year <= 2100:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("year는 2000~2100 사이의 연도여야 해요.")
            year = 0
        try:
            effective = date.fromisoformat(str(data["effective_date"])[:10])
        except ValueError:
            errors.append("effective_date는 YYYY-MM-DD 형식이어야 해요.")
            effective = None
        base_rev = str(data["base_rev"]).strip()
        base_model = None
        if not self.ws.has_model(base_rev):
            errors.append(f"기준 개정 {base_rev}가 없어요.")
        else:
            base_model = self._model(base_rev)
            if base_model.revision.kind != RevisionKind.ACTUAL:
                errors.append(f"기준 개정 {base_rev}는 실제 개정이어야 해요.")
            elif (
                effective
                and base_model.revision.effective_date
                and (effective.isoformat() <= base_model.revision.effective_date)
            ):
                errors.append("effective_date는 기준 개정의 적용일보다 뒤여야 해요.")
        floors = [_floor_code(f) for f in as_list(data["floors"])]
        if base_model is not None:
            known = {f.code for f in base_model.floors} | {u.floor for u in base_model.units()}
            bad = [f for f in floors if f not in known]
            if bad:
                errors.append(f"기준 개정에 없는 층이에요: {', '.join(bad)}")
        objective = str(data.get("objective") or "sales").strip().lower()
        if objective not in OBJECTIVE_LABELS:
            errors.append("objective는 sales(매출) 또는 commission(수수료 수익)이어야 해요.")
        movable = str(data.get("movable") or ("expiring" if self.ws.has_table("leases") else "all")).lower()
        if movable not in MOVABLE:
            errors.append("movable은 expiring(계약 만료 매장만) 또는 all(전체)이어야 해요.")
        numbers: dict[str, float] = {}
        for key, default, low, high in (
            ("max_moves", 6, 0, 30),
            ("downtime_weeks", 2, 0, 26),
            ("area_tolerance", DEFAULT_AREA_TOLERANCE, 0, 1),
        ):
            try:
                value = float(data.get(key, default))
                if not low <= value <= high:
                    raise ValueError
                numbers[key] = value
            except (TypeError, ValueError):
                errors.append(f"{key}는 {low}~{high} 사이의 숫자여야 해요.")
        keep_anchors = data.get("keep_anchors", True)
        if errors:
            return {}, errors, warnings
        catalog = self._catalog()
        placements = self._placements(base_rev)
        leases = self._leases()
        lease_end = {}
        if leases is not None:
            lease_end = dict(
                zip(leases["brand_id"].astype(str), leases["lease_end"].astype(str), strict=True)
            )
        exits = []
        for index, item in enumerate(data.get("exits") or [], start=1):
            spec = item if isinstance(item, dict) else {"brand_id": item}
            row = self._lookup_brand(catalog, spec.get("brand_id") or spec.get("brand_name"))
            if row is None:
                errors.append(
                    f"퇴점 {index}: 브랜드 '{spec.get('brand_id') or spec.get('brand_name')}'를 찾을 수 없어요."
                )
                continue
            brand_id = str(row["brand_id"])
            unit = placements.get(brand_id)
            if unit is None:
                errors.append(f"퇴점 {index}: {row['brand_name']}는 {base_rev}에 입점해 있지 않아요.")
                continue
            floor = unit.split("-")[0]
            if floor not in floors:
                errors.append(f"퇴점 {index}: {row['brand_name']}({unit})는 대상 층 밖에 있어요.")
                continue
            end = lease_end.get(brand_id)
            if end and effective and end >= effective.isoformat():
                warnings.append(
                    f"퇴점 {index}: {row['brand_name']} 계약이 {end}까지라 조기 종료 협의가 필요해요."
                )
            exits.append(
                {
                    "brand_id": brand_id,
                    "brand_name": str(row["brand_name"]),
                    "category": row.get("category"),
                    "category_name": CATEGORIES[row["category"]].name_ko
                    if row.get("category") in CATEGORIES
                    else "",
                    "unit_code": unit,
                    "reason": str(spec.get("reason") or "퇴점"),
                }
            )
        entries = []
        new_index = 0
        for index, item in enumerate(data.get("entries") or [], start=1):
            spec = item if isinstance(item, dict) else {"brand_id": item}
            row = self._lookup_brand(catalog, spec.get("brand_id") or spec.get("brand_name"))
            if row is None:
                new_index += 1
                record, problems = self._new_brand(spec, new_index, catalog)
                errors.extend(problems)
                if problems:
                    continue
            else:
                record = row.to_dict()
                record["new_brand"] = False
                if str(record["brand_id"]) in placements:
                    errors.append(
                        f"입점 {index}: {record['brand_name']}는 이미 {placements[str(record['brand_id'])]}에 입점해 있어요."
                    )
                    continue
                if spec.get("brand_name") and str(spec["brand_name"]).strip() != str(record["brand_name"]):
                    warnings.append(f"입점 {index}: 브랜드명이 카탈로그({record['brand_name']})와 달라요.")
            category = record.get("category")
            rate = spec.get("commission_rate")
            if rate is None and category in CATEGORIES:
                rate = sum(CATEGORIES[category].commission_range) / 2
            entries.append(
                {
                    "brand_id": str(record["brand_id"]),
                    "brand_name": str(record["brand_name"]),
                    "category": category,
                    "category_name": CATEGORIES[category].name_ko if category in CATEGORIES else "",
                    "target_area_m2": [
                        round(float(record.get("target_area_min_m2") or 0), 1),
                        round(float(record.get("target_area_max_m2") or 0), 1),
                    ],
                    "preferred_floors": [_floor_code(f) for f in as_list(record.get("preferred_floors"))],
                    "commission_rate": round(float(rate), 3) if rate is not None else None,
                    "attractiveness": float(record.get("attractiveness") or 1.0),
                    "new_brand": bool(record.get("new_brand")),
                    "adjacency_conflicts": record.get("adjacency_conflicts") or "",
                    "note": str(spec.get("note") or ""),
                }
            )
        ids = [e["brand_id"] for e in entries]
        if len(set(ids)) != len(ids):
            errors.append("같은 브랜드가 입점 목록에 두 번 있어요.")
        brief = {
            "brief_id": brief_id,
            "title": str(data["title"]).strip(),
            "season": season,
            "season_label": SEASON_LABELS[season],
            "year": year,
            "effective_date": effective.isoformat() if effective else None,
            "base_rev": base_rev,
            "floors": sorted(set(floors), key=floor_sort_key),
            "objective": objective,
            "objective_label": OBJECTIVE_LABELS[objective],
            "max_moves": int(numbers["max_moves"]),
            "downtime_weeks": numbers["downtime_weeks"],
            "area_tolerance": numbers["area_tolerance"],
            "movable": movable,
            "keep_anchors": bool(keep_anchors),
            "entries": entries,
            "exits": exits,
            "notes": str(data.get("notes") or ""),
        }
        if movable == "expiring" and leases is None:
            warnings.append(
                "계약 정보(leases)가 없어 계약 만료 매장을 알 수 없어요. 모든 매장을 이동 대상으로 봐요."
            )
        return brief, errors, warnings

    # Projects ----------------------------------------------------------------------------------------
    def _project_rel(self, brief_id: str) -> Path:
        return PROJECT_DIR / f"{brief_id}.json"

    def _project(self, brief_id: str) -> dict[str, Any]:
        self._brief_path(brief_id)
        stored = self.ws.read_json(self._project_rel(brief_id))
        return stored or {
            "brief_id": brief_id,
            "status": "draft",
            "diagnosis": None,
            "plan": None,
            "selected_alt_id": None,
            "decided_by": None,
            "decided_at": None,
            "check": None,
            "report_url": None,
            "updated_at": None,
        }

    def _save_project(self, project: dict[str, Any]) -> dict[str, Any]:
        project["updated_at"] = _now()
        self.ws.write_json(self._project_rel(project["brief_id"]), _json(project))
        return project

    @staticmethod
    def _advance(project: dict[str, Any], status: str) -> None:
        current = project.get("status") or "draft"
        if STATUS_ORDER.index(status) > STATUS_ORDER.index(current):
            project["status"] = status

    def relayout_project(self, brief_id: str) -> dict[str, Any]:
        """The project of a brief with the normalized brief (REST). The report renders on demand, so its
        link is always present."""
        project = self._project(brief_id)
        return _json(
            {**project, "brief": self._brief(brief_id), "report_url": f"/api/relayout/{brief_id}/report"}
        )

    def relayout_briefs(self) -> list[dict[str, Any]]:
        """Brief summaries with project status (REST)."""
        rows = []
        for brief_id in self._brief_ids():
            project = self._project(brief_id)
            try:
                brief = self._brief(brief_id)
            except ToolError as exc:
                rows.append({"brief_id": brief_id, "title": brief_id, "status": "invalid", "error": str(exc)})
                continue
            rows.append(
                {
                    "brief_id": brief_id,
                    "title": brief["title"],
                    "season": brief["season"],
                    "year": brief["year"],
                    "effective_date": brief["effective_date"],
                    "base_rev": brief["base_rev"],
                    "floors": brief["floors"],
                    "objective": brief["objective"],
                    "status": project.get("status") or "draft",
                    "entries": len(brief["entries"]),
                    "exits": len(brief["exits"]),
                    "selected_alt_id": project.get("selected_alt_id"),
                    "updated_at": project.get("updated_at"),
                }
            )
        return rows

    def relayout_list_briefs(self) -> dict[str, Any]:
        """MCP: registered relayout briefs and where each project stands."""
        briefs = self.relayout_briefs()
        return {
            "briefs": [
                {
                    k: b.get(k)
                    for k in (
                        "brief_id",
                        "title",
                        "season",
                        "effective_date",
                        "base_rev",
                        "floors",
                        "objective",
                        "status",
                        "entries",
                        "exits",
                        "selected_alt_id",
                        "error",
                    )
                    if b.get(k) is not None
                }
                for b in briefs
            ],
            "note": "브리프는 사람이 올린 개편 계획이에요. 진단 → 대안 → 사람 결정 → 도면 대조 순서로 진행해요.",
        }

    # Data helpers ------------------------------------------------------------------------------------
    def _leases(self) -> pd.DataFrame | None:
        if not self.ws.has_table("leases"):
            return None
        leases = self.ws.table("leases").copy()
        leases["brand_id"] = leases["brand_id"].astype(str)
        leases["unit_code"] = leases["unit_code"].astype(str)
        leases["lease_end"] = leases["lease_end"].astype(str).str[:10]
        return leases

    def _season_factors(self, season: str) -> dict[str, float]:
        """Category average daily sales in the season months divided by the all-year average."""

        def compute() -> dict[str, float]:
            frame = self._enriched()
            frame = frame[frame["category"].notna()]
            first, last = self._data_range()
            days = pd.date_range(first, last, freq="D")
            in_season = days.month.isin(SEASON_MONTHS[season])
            n_all, n_season = len(days), int(in_season.sum())
            if not n_season:
                return {}
            months = frame["date"].dt.month
            total = frame.groupby("category")["net_sales_krw"].sum()
            seasonal = frame[months.isin(SEASON_MONTHS[season])].groupby("category")["net_sales_krw"].sum()
            factors = (seasonal.reindex(total.index, fill_value=0.0) / n_season) / (total / n_all)
            return {str(k): round(float(v), 3) for k, v in factors.items() if np.isfinite(v) and v > 0}

        return self._memo(("season_factors", season, self._data_key()), compute)

    def _commission_rates(self, rev: str) -> dict[str, float]:
        leases = self._leases()
        rates: dict[str, float] = {}
        tenancies = self._table("tenancies", required=False)
        if tenancies is not None and "commission_rate" in tenancies:
            current = tenancies[tenancies["rev_id"].astype(str) == rev]
            rates.update(
                {
                    str(b): float(r)
                    for b, r in zip(current["brand_id"], current["commission_rate"], strict=True)
                    if pd.notna(r)
                }
            )
        if leases is not None and "commission_rate" in leases:
            rates.update(
                {
                    str(b): float(r)
                    for b, r in zip(leases["brand_id"], leases["commission_rate"], strict=True)
                    if pd.notna(r)
                }
            )
        return rates

    def _floor_daily(self, floor: str, window: tuple[date, date] | None) -> float | None:
        if window is None:
            return None
        frame = self._enriched()
        start, end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
        mask = (frame["floor"] == floor) & (frame["date"] >= start) & (frame["date"] <= end)
        days = (window[1] - window[0]).days + 1
        return float(frame.loc[mask, "net_sales_krw"].sum()) / days if days > 0 else None

    # Diagnosis ---------------------------------------------------------------------------------------
    @_locked
    def relayout_diagnose(self, brief_id: str) -> dict[str, Any]:
        """Season, floor trend, renewal candidates, vacancies and entry feasibility for a brief."""
        brief = self._brief(brief_id)
        base_rev = brief["base_rev"]
        first, last = self._data_range()
        latest, previous = aligned_windows(brief["season"], brief["year"], first, last)
        factors = self._season_factors(brief["season"])
        productivity = self.productivity_frame(base_rev, "unit")
        floors = brief["floors"]
        scope = productivity[productivity["floor"].isin(floors)]
        floor_rows = []
        for floor in floors:
            rows = scope[(scope["floor"] == floor) & scope["brand_id"].notna()]
            area = float(rows["area_m2"].sum())
            now, before = self._floor_daily(floor, latest), self._floor_daily(floor, previous)
            floor_rows.append(
                {
                    "floor": floor,
                    "sales_krw": float(rows["sales"].sum()),
                    "sales_per_m2": float(rows["sales"].sum()) / area if area else None,
                    "yoy_pct": (now / before - 1) * 100 if now and before else None,
                    "low_efficiency": int(rows["low_efficiency"].eq(True).sum()),
                    "units": int(len(rows)),
                }
            )
        leases = self._leases()
        lease_end = {}
        if leases is not None:
            lease_end = {
                (b, u): e
                for b, u, e in zip(leases["brand_id"], leases["unit_code"], leases["lease_end"], strict=True)
            }
        effective = brief["effective_date"]
        candidates = []
        for row in scope.itertuples():
            if not isinstance(row.brand_id, str):
                continue
            end = lease_end.get((row.brand_id, row.unit_code))
            reasons = []
            if end and end < effective:
                reasons.append("계약 만료")
            if bool(row.low_efficiency):
                reasons.append("저효율")
            if not reasons:
                continue
            candidates.append(
                {
                    "unit_code": row.unit_code,
                    "floor": row.floor,
                    "brand_id": row.brand_id,
                    "brand_name": row.brand_name,
                    "category_name": row.category_name,
                    "lease_end": end,
                    "sales_per_m2": row.sales_per_m2,
                    "percentile": row.percentile,
                    "low_efficiency": bool(row.low_efficiency),
                    "reasons": reasons,
                }
            )
        candidates.sort(
            key=lambda c: (not c["low_efficiency"], c["percentile"] if c["percentile"] is not None else 100)
        )
        model = self._model(base_rev)
        vacancies = [
            {
                "unit_code": str(u.attributes.get("unit_code")),
                "floor": u.floor,
                "area_m2": round(u.geometry.area, 1),
            }
            for u in model.units()
            if u.floor in floors and (u.attributes.get("vacant") or not u.attributes.get("brand_id"))
        ]
        slots = {v["unit_code"]: (v["floor"], v["area_m2"]) for v in vacancies}
        for item in brief["exits"]:
            unit = model.unit_by_code(item["unit_code"])
            if unit is not None:
                slots[item["unit_code"]] = (unit.floor, round(unit.geometry.area, 1))
        tolerance = brief["area_tolerance"]
        entries = []
        for entry in brief["entries"]:
            lo, hi = entry["target_area_m2"]
            fits = [
                code
                for code, (floor, area) in slots.items()
                if lo * (1 - tolerance) <= area <= hi * (1 + tolerance)
                and (not entry["preferred_floors"] or floor in entry["preferred_floors"])
            ]
            entries.append(
                {
                    "brand_id": entry["brand_id"],
                    "brand_name": entry["brand_name"],
                    "category_name": entry["category_name"],
                    "target_area_m2": entry["target_area_m2"],
                    "preferred_floors": entry["preferred_floors"],
                    "season_factor": factors.get(entry["category"]),
                    "feasible_units": len(fits),
                    "feasible_unit_codes": sorted(fits),
                    "note": entry["note"],
                }
            )
        exits = []
        rates = scope.set_index("unit_code")
        for item in brief["exits"]:
            row = rates.loc[item["unit_code"]] if item["unit_code"] in rates.index else None
            exits.append(
                {
                    **{k: item[k] for k in ("brand_id", "brand_name", "unit_code", "reason")},
                    "lease_end": lease_end.get((item["brand_id"], item["unit_code"])),
                    "sales_per_m2": None if row is None else row["sales_per_m2"],
                    "percentile": None if row is None else row["percentile"],
                }
            )
        categories = {c for c in scope["category"].dropna()} | {e["category"] for e in brief["entries"]}
        season_factors = sorted(
            (
                {
                    "category": c,
                    "category_name": CATEGORIES[c].name_ko if c in CATEGORIES else c,
                    "factor": factors[c],
                }
                for c in categories
                if c in factors
            ),
            key=lambda r: -r["factor"],
        )
        notes = []
        if leases is None:
            notes.append("계약 정보(leases)가 없어 계약 만료 후보를 판단하지 못했어요.")
        for item in entries:
            if not item["feasible_units"]:
                notes.append(
                    f"{item['brand_name']}는 면적·선호 층에 맞는 공실이나 퇴점 매장이 없어요. 면적 허용 오차를 넓히거나 퇴점을 추가해 보세요."
                )
        notes.append("시즌 지수는 2년치 카테고리 일평균 매출로 계산했고 배치 변화의 영향이 섞여 있어요.")
        diagnosis = _json(
            {
                "brief_id": brief_id,
                "season": {
                    "code": brief["season"],
                    "label": brief["season_label"],
                    "year": brief["year"],
                    "last": {"start": latest[0], "end": latest[1]} if latest else None,
                    "previous": {"start": previous[0], "end": previous[1]} if previous else None,
                },
                "period": productivity.attrs.get("period"),
                "season_factors": season_factors,
                "floors": floor_rows,
                "candidates": candidates[:30],
                "candidate_total": len(candidates),
                "vacancies": vacancies,
                "entries": entries,
                "exits": exits,
                "notes": notes,
            }
        )
        project = self._project(brief_id)
        project["diagnosis"] = diagnosis
        self._advance(project, "diagnosed")
        self._save_project(project)
        return diagnosis

    # Plan --------------------------------------------------------------------------------------------
    def _plan_setup(self, brief: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        from cad2anything.layout import AssignmentScorer, LayoutConstraints
        from cad2anything.layout.whatif import brand_info

        base_rev = brief["base_rev"]
        inputs = self._layout_inputs(base_rev)
        catalog = self._catalog()
        entries = list(brief["entries"])
        exits = list(brief["exits"])
        placements = self._placements(base_rev)
        for text in params.get("extra_exits") or []:
            row = self._lookup_brand(catalog, self._resolve_brand(text))
            unit = placements.get(str(row["brand_id"])) if row is not None else None
            if row is None or unit is None or unit.split("-")[0] not in brief["floors"]:
                raise ToolError(f"추가 퇴점 '{text}'는 대상 층에 입점한 브랜드가 아니에요.")
            if all(e["brand_id"] != row["brand_id"] for e in exits):
                exits.append(
                    {
                        "brand_id": str(row["brand_id"]),
                        "brand_name": str(row["brand_name"]),
                        "unit_code": unit,
                        "reason": "추가 퇴점 검토",
                    }
                )
        for text in params.get("extra_entries") or []:
            row = self._lookup_brand(catalog, self._resolve_brand(text))
            if row is None:
                raise ToolError(f"추가 입점 '{text}'를 카탈로그에서 찾을 수 없어요.")
            if str(row["brand_id"]) in placements:
                raise ToolError(
                    f"{row['brand_name']}는 이미 {placements[str(row['brand_id'])]}에 입점해 있어요."
                )
            if all(e["brand_id"] != row["brand_id"] for e in entries):
                category = row.get("category")
                entries.append(
                    {
                        "brand_id": str(row["brand_id"]),
                        "brand_name": str(row["brand_name"]),
                        "category": category,
                        "category_name": CATEGORIES[category].name_ko if category in CATEGORIES else "",
                        "commission_rate": sum(CATEGORIES[category].commission_range) / 2
                        if category in CATEGORIES
                        else None,
                        "new_brand": False,
                        "note": "추가 입점 검토",
                    }
                )
        brands = inputs["brands"]
        new_rows = []
        for entry in entries:
            if entry.get("new_brand"):
                lo, hi = entry["target_area_m2"]
                new_rows.append(
                    {
                        "brand_id": entry["brand_id"],
                        "brand_name": entry["brand_name"],
                        "category": entry["category"],
                        "anchor": False,
                        "target_area_min_m2": lo,
                        "target_area_max_m2": hi,
                        "preferred_floors": ";".join(entry["preferred_floors"]),
                        "adjacency_conflicts": entry.get("adjacency_conflicts", ""),
                        "attractiveness": entry.get("attractiveness", 1.0),
                    }
                )
        if new_rows:
            brands = pd.concat([brands, pd.DataFrame(new_rows)], ignore_index=True)
        infos = brand_info(brands)
        scorer = AssignmentScorer(
            inputs["features"], infos, inputs["model"], inputs["affinity"], inputs["adjacency"]
        )
        factors = self._season_factors(brief["season"])
        rates = self._commission_rates(inputs["fit_rev"])
        median_rate = float(np.median(list(rates.values()))) if rates else 0.25
        for entry in entries:
            if entry.get("commission_rate") is not None:
                rates[entry["brand_id"]] = float(entry["commission_rate"])
        objective = params["objective"]

        def weight(brand_id: str) -> float:
            info = infos.get(brand_id)
            factor = factors.get(info.category, 1.0) if info is not None else 1.0
            return factor * (rates.get(brand_id, median_rate) if objective == "commission" else 1.0)

        floors = brief["floors"]
        current = dict(inputs["assignment"])
        scope_codes = [u for u, info in scorer.units.items() if info.floor in floors]
        start = dict(current)
        exit_units = []
        for item in exits:
            if start.get(item["unit_code"]) == item["brand_id"]:
                start[item["unit_code"]] = None
                exit_units.append(item["unit_code"])
        constraints = LayoutConstraints(
            floors=floors,
            keep_anchors=brief["keep_anchors"],
            area_tolerance=brief["area_tolerance"],
            max_moves=int(params["max_moves"]),
        )
        free_units = [u for u in scope_codes if start.get(u) is None]
        pending = [e for e in entries if e["brand_id"] in infos]
        placed_entries: list[str] = []
        unplaced = [
            {
                "brand_id": e["brand_id"],
                "brand_name": e["brand_name"],
                "reason": "브랜드 정보를 만들 수 없어요.",
            }
            for e in entries
            if e["brand_id"] not in infos
        ]
        while pending:
            best: tuple[float, str, str] | None = None
            for entry in pending:
                info = infos[entry["brand_id"]]
                for unit in free_units:
                    unit_info = scorer.units[unit]
                    if not (
                        constraints.area_ok(info, unit_info.area_m2)
                        and constraints.floor_ok(info, unit_info.floor)
                    ):
                        continue
                    value = weight(entry["brand_id"]) * scorer.location_value(entry["brand_id"], unit)
                    if best is None or value > best[0]:
                        best = (value, entry["brand_id"], unit)
            if best is None:
                break
            _, brand_id, unit = best
            start[unit] = brand_id
            free_units.remove(unit)
            placed_entries.append(brand_id)
            pending = [e for e in pending if e["brand_id"] != brand_id]
        for entry in pending:
            unplaced.append(
                {
                    "brand_id": entry["brand_id"],
                    "brand_name": entry["brand_name"],
                    "reason": "면적·선호 층에 맞는 빈 매장이 없어요. 면적 허용 오차를 넓히거나 퇴점을 추가해 보세요.",
                }
            )
        leases = self._leases()
        movable_ids = None
        caveats = list(PLAN_CAVEATS)
        if params["movable"] == "expiring":
            if leases is None:
                caveats.append("계약 정보가 없어 모든 매장을 이동 대상으로 봤어요.")
            else:
                expiring = leases[leases["lease_end"] < brief["effective_date"]]
                pairs = set(zip(expiring["brand_id"], expiring["unit_code"], strict=True))
                movable_ids = {b for u, b in current.items() if b and (b, u) in pairs and u in scope_codes}
                movable_ids |= set(placed_entries)
        if movable_ids is not None:
            constraints = LayoutConstraints(
                floors=floors,
                movable_brands=movable_ids,
                keep_anchors=brief["keep_anchors"],
                area_tolerance=brief["area_tolerance"],
                max_moves=int(params["max_moves"]),
            )
        current_scope = {u: current.get(u) for u in scope_codes}
        current_score = scorer.score(current_scope)
        base_sales = {
            str(r.brand_id): float(r.expected_sales)
            for r in current_score.units.itertuples()
            if isinstance(r.brand_id, str)
        }
        weeks = float(params["downtime_weeks"])
        weights = {b: weight(b) for b in {*base_sales, *placed_entries}}
        move_cost = {b: weights[b] * sales * weeks / 52.0 for b, sales in base_sales.items()}
        return {
            "inputs": inputs,
            "infos": infos,
            "scorer": scorer,
            "constraints": constraints,
            "current": current,
            "start": start,
            "scope_codes": scope_codes,
            "weights": weights,
            "move_cost": move_cost,
            "entries": entries,
            "exits": exits,
            "placed_entries": placed_entries,
            "unplaced": unplaced,
            "caveats": caveats,
            "factors": factors,
            "rates": rates,
            "movable_count": None if movable_ids is None else len(movable_ids - set(placed_entries)),
        }

    @staticmethod
    def _weighted_total(score: Any, weights: dict[str, float]) -> float:
        return float(
            sum(
                float(r.expected_sales) * weights.get(r.brand_id, 1.0)
                for r in score.units.itertuples()
                if isinstance(r.brand_id, str)
            )
        )

    def _changes(
        self,
        setup: dict[str, Any],
        final: dict[str, str | None],
        moves: list[Any],
        final_score: Any,
        current_score: Any,
    ) -> list[dict[str, Any]]:
        scope = setup["scope_codes"]
        infos = setup["infos"]
        current_pos = {b: u for u in scope if (b := setup["current"].get(u))}
        final_pos = {b: u for u in scope if (b := final.get(u))}
        after = final_score.units.set_index("unit_code")
        before = current_score.units.set_index("unit_code")
        engine_moves = {m.brand_id: m for m in moves}
        floor_of = {u: setup["scorer"].units[u].floor for u in scope}
        reasons = {e["brand_id"]: e.get("reason", "퇴점") for e in setup["exits"]}
        notes = {e["brand_id"]: e.get("note", "") for e in setup["entries"]}
        changes = []
        for brand_id, unit in sorted(
            current_pos.items(), key=lambda kv: (floor_sort_key(floor_of[kv[1]]), kv[1])
        ):
            info = infos.get(brand_id)
            name = info.brand_name if info else brand_id
            category = info.category if info else None
            if brand_id not in final_pos:
                lost = float(before.loc[unit, "expected_sales"])
                changes.append(
                    {
                        "type": "exit",
                        "brand_id": brand_id,
                        "brand_name": name,
                        "category_name": CATEGORIES[category].name_ko if category in CATEGORIES else "",
                        "from_unit": unit,
                        "to_unit": None,
                        "from_floor": floor_of[unit],
                        "to_floor": None,
                        "delta_krw": -lost,
                        "explanation": f"{name}({unit}) 퇴점: {reasons.get(brand_id, '퇴점')}. 지금 기대 매출 {_krw(lost)}/년이 빠져요.",
                    }
                )
            elif final_pos[brand_id] != unit:
                move = engine_moves.get(brand_id)
                target = final_pos[brand_id]
                delta = (
                    move.delta
                    if move is not None
                    else float(after.loc[target, "expected_sales"])
                    - float(before.loc[unit, "expected_sales"])
                )
                changes.append(
                    {
                        "type": "move",
                        "brand_id": brand_id,
                        "brand_name": name,
                        "category_name": CATEGORIES[category].name_ko if category in CATEGORIES else "",
                        "from_unit": unit,
                        "to_unit": target,
                        "from_floor": floor_of[unit],
                        "to_floor": floor_of[target],
                        "delta_krw": delta,
                        "move_cost_krw": setup["move_cost"].get(brand_id, 0.0),
                        "explanation": move.explanation if move is not None else "",
                    }
                )
        for brand_id, unit in sorted(
            final_pos.items(), key=lambda kv: (floor_sort_key(floor_of[kv[1]]), kv[1])
        ):
            if brand_id in current_pos:
                continue
            info = infos.get(brand_id)
            category = info.category if info else None
            expected = float(after.loc[unit, "expected_sales"])
            factor = setup["factors"].get(category, 1.0)
            changes.append(
                {
                    "type": "entry",
                    "brand_id": brand_id,
                    "brand_name": info.brand_name if info else brand_id,
                    "category_name": CATEGORIES[category].name_ko if category in CATEGORIES else "",
                    "from_unit": None,
                    "to_unit": unit,
                    "from_floor": None,
                    "to_floor": floor_of[unit],
                    "delta_krw": expected,
                    "explanation": (
                        f"{info.brand_name if info else brand_id}({unit}) 입점: 기대 매출 {_krw(expected)}/년"
                        f"(범위 {_krw(after.loc[unit, 'low'])}~{_krw(after.loc[unit, 'high'])}), 시즌 지수 {factor:.2f}, "
                        f"수수료율 {setup['rates'].get(brand_id, float('nan')):.1%}."
                        + (f" {notes[brand_id]}" if notes.get(brand_id) else "")
                    ),
                }
            )
        order = {"exit": 0, "entry": 1, "move": 2}
        changes.sort(key=lambda c: (order[c["type"]], floor_sort_key(c["to_floor"] or c["from_floor"] or "")))
        return changes

    def _save_alternative(
        self, brief: dict[str, Any], alt_id: str, rev_id: str, changes: list[dict[str, Any]]
    ) -> None:
        from cad2anything.layout import to_hypothetical_revision

        moves = []
        for change in changes:
            item = {
                "brand_id": change["brand_id"],
                "brand_name": change["brand_name"],
                "from_unit": change["from_unit"],
                "to_unit": change["to_unit"],
                "delta": change.get("delta_krw"),
                "explanation": change.get("explanation"),
            }
            if change["type"] == "entry":
                info = self._catalog()
                row = info[info["brand_id"] == change["brand_id"]]
                category = (
                    row["category"].iloc[0]
                    if len(row)
                    else next(
                        (e["category"] for e in brief["entries"] if e["brand_id"] == change["brand_id"]), None
                    )
                )
                item["category"] = category
            moves.append(item)
        base = self._model(brief["base_rev"])
        model, change_set = to_hypothetical_revision(base, {"alt_id": alt_id, "moves": moves}, rev_id=rev_id)
        model.revision.notes = f"{brief['title']} {alt_id} 가상 개정(추정치 기반, 실제 도면 아님)"
        self.ws.save_model(
            model,
            {
                "source": "hypothetical",
                "base": brief["base_rev"],
                "alt_id": alt_id,
                "origin": "relayout",
                "brief_id": brief["brief_id"],
            },
        )
        self.ws.save_change_set(
            change_set, {"source": "relayout", "alt_id": alt_id, "brief_id": brief["brief_id"]}
        )

    def _remove_stale_alternatives(self, brief: dict[str, Any], keep: set[str]) -> None:
        for revision in list(self.ws.revisions()):
            if revision.kind != RevisionKind.HYPOTHETICAL or revision.rev_id in keep:
                continue
            meta = self.ws.model_meta(revision.rev_id)
            if meta.get("origin") == "relayout" and meta.get("brief_id") == brief["brief_id"]:
                self.ws.delete_file(Path("models") / f"{revision.rev_id}.json")
                self.ws.delete_file(Path("changes") / f"{brief['base_rev']}__{revision.rev_id}.json")

    @_locked
    def relayout_plan(
        self,
        brief_id: str,
        objective: str | None = None,
        n_alternatives: int = 3,
        max_moves: int | None = None,
        downtime_weeks: float | None = None,
        movable: str | None = None,
        extra_exits: list[str] | None = None,
        extra_entries: list[str] | None = None,
    ) -> dict[str, Any]:
        """Alternatives for a brief: exits and entries applied, then expiring brands relocated to raise the
        season-weighted objective net of downtime costs. Each alternative is saved as a hypothetical revision."""
        from cad2anything.layout import check_constraints, generate_alternatives

        brief = self._brief(brief_id)
        params = {
            "objective": str(objective or brief["objective"]).lower(),
            "season": brief["season"],
            "max_moves": brief["max_moves"] if max_moves is None else int(max_moves),
            "downtime_weeks": brief["downtime_weeks"] if downtime_weeks is None else float(downtime_weeks),
            "movable": str(movable or brief["movable"]).lower(),
            "floors": brief["floors"],
            "extra_exits": list(extra_exits or []),
            "extra_entries": list(extra_entries or []),
        }
        if params["objective"] not in OBJECTIVE_LABELS:
            raise ToolError("objective는 sales 또는 commission이어야 해요.")
        if params["movable"] not in MOVABLE:
            raise ToolError("movable은 expiring 또는 all이어야 해요.")
        if not 0 <= params["max_moves"] <= 30 or not 0 <= params["downtime_weeks"] <= 26:
            raise ToolError("max_moves는 0~30, downtime_weeks는 0~26이어야 해요.")
        project = self._project(brief_id)
        if not project.get("diagnosis"):
            self.relayout_diagnose(brief_id)
            project = self._project(brief_id)
        setup = self._plan_setup(brief, params)
        inputs, scorer, weights = setup["inputs"], setup["scorer"], setup["weights"]
        scope = setup["scope_codes"]
        current_score = scorer.score({u: setup["current"].get(u) for u in scope})
        baseline_score = scorer.score({u: setup["start"].get(u) for u in scope})
        current_obj = self._weighted_total(current_score, weights)
        baseline_obj = self._weighted_total(baseline_score, weights)
        engine = generate_alternatives(
            setup["start"],
            inputs["features"],
            setup["infos"],
            inputs["adjacency"],
            inputs["model"],
            setup["constraints"],
            n_alternatives=max(1, min(int(n_alternatives), MAX_ALTERNATIVES)),
            seed=7,
            affinity_pairs=inputs["affinity"],
            weights=weights,
            move_cost=setup["move_cost"],
            free=setup["placed_entries"],
            objective_label=OBJECTIVE_LABELS[params["objective"]],
        )
        results = []
        candidates: list[tuple[str, dict[str, str | None], Any]] = [("ALT-0", setup["start"], None)]
        candidates += [(alt.alt_id, alt.assignment, alt) for alt in engine]
        keep: set[str] = set()
        for alt_id, final, alt in candidates:
            final_score = baseline_score if alt is None else scorer.score({u: final.get(u) for u in scope})
            changes = self._changes(setup, final, alt.moves if alt else [], final_score, current_score)
            sales_after = float(final_score.total)
            objective_after = self._weighted_total(final_score, weights)
            cost = alt.move_cost_krw if alt else 0.0
            n_moves = sum(1 for c in changes if c["type"] == "move")
            kpi = {
                "sales_delta_krw": sales_after - baseline_score.total,
                # The baseline compared with itself has no range.
                "sales_low_krw": alt.sales_low - baseline_score.total if alt else 0.0,
                "sales_high_krw": alt.sales_high - baseline_score.total if alt else 0.0,
                "objective_delta_krw": objective_after - baseline_obj,
                "move_cost_krw": cost,
                "net_krw": objective_after - baseline_obj - cost,
                "vs_current_sales_krw": sales_after - current_score.total,
                "vs_current_objective_krw": objective_after - cost - current_obj,
                "moves": n_moves,
            }
            checks = check_constraints(
                setup["start"],
                final,
                inputs["features"],
                setup["infos"],
                inputs["adjacency"],
                setup["constraints"],
                setup["placed_entries"],
            )
            n_entries = sum(1 for c in changes if c["type"] == "entry")
            n_exits = sum(1 for c in changes if c["type"] == "exit")
            if alt is None:
                summary = (
                    f"기준안: 퇴점 {n_exits}건과 입점 {n_entries}건만 반영하고 기존 브랜드는 옮기지 않아요. "
                    f"기대 매출은 지금보다 {_more_less(kpi['vs_current_sales_krw'])}."
                )
            else:
                objective_delta = kpi["objective_delta_krw"]
                summary = (
                    f"{alt_id}: 기준안에서 기존 브랜드 {n_moves}곳을 옮겨요. {OBJECTIVE_LABELS[params['objective']]}은 "
                    f"연 {_krw(abs(objective_delta))} {'늘고' if objective_delta >= 0 else '줄고'}, 이전 공사 손실 "
                    f"{_krw(cost)}을 빼면 순효과는 연 {_signed_krw(kpi['net_krw'])}이에요. 기대 매출은 기준안보다 "
                    f"{_more_less(kpi['sales_delta_krw'])}, 지금보다 {_more_less(kpi['vs_current_sales_krw'])}."
                )
            rev_id = f"{brief_id}-A{alt_id.split('-')[-1]}"
            self._save_alternative(brief, alt_id, rev_id, changes)
            keep.add(rev_id)
            results.append(
                {
                    "alt_id": alt_id,
                    "rev_id": rev_id,
                    "summary": summary,
                    "kpi": kpi,
                    "changes": changes,
                    "constraints": [
                        {"name": r.constraint, "ok": bool(r.satisfied), "detail": r.detail}
                        for r in checks.itertuples()
                    ],
                    # Per-move explanations are already in ``changes``; keep only the extra notes
                    # (new-entry shifts and the neighbors' affinity spillover).
                    "explanations": alt.explanations[len(alt.moves) :] if alt else [],
                }
            )
        self._remove_stale_alternatives(brief, keep)
        caveats = list(setup["caveats"])
        if not engine:
            caveats.append("계약 만료 브랜드를 옮겨서 순효과가 커지는 배치를 찾지 못해 기준안만 만들었어요.")
        if setup["movable_count"] is not None:
            caveats.append(f"이동 대상은 적용일 전에 계약이 끝나는 브랜드 {setup['movable_count']}곳이에요.")
        plan = _json(
            {
                "brief_id": brief_id,
                "params": params,
                "objective_label": OBJECTIVE_LABELS[params["objective"]],
                "current": {"sales_krw": current_score.total, "objective_krw": current_obj},
                "baseline": {
                    "sales_krw": baseline_score.total,
                    "objective_krw": baseline_obj,
                    "note": "퇴점·입점만 반영한 기준안",
                },
                "alternatives": results,
                "unplaced": setup["unplaced"],
                "caveats": caveats,
                "label": "추정치",
                "created_at": _now(),
            }
        )
        project["plan"] = plan
        project["selected_alt_id"] = None
        project["decided_by"] = None
        project["decided_at"] = None
        project["check"] = None
        project["status"] = "planned"
        self._save_project(project)
        return plan

    # Decision and drawing check ----------------------------------------------------------------------
    @_locked
    def relayout_select(self, brief_id: str, alt_id: str, decided_by: str = "채팅 사용자") -> dict[str, Any]:
        """Record the human decision: which alternative the CAD team should draw."""
        project = self._project(brief_id)
        plan = project.get("plan")
        if not plan:
            raise ToolError("먼저 대안을 만들어야 해요(relayout_plan).")
        alt_id = str(alt_id).strip().upper()
        if re.fullmatch(r"\d+", alt_id):
            alt_id = f"ALT-{alt_id}"
        if alt_id not in {a["alt_id"] for a in plan["alternatives"]}:
            raise ToolError(
                f"{alt_id}는 이 브리프의 대안이 아니에요. 대안: {', '.join(a['alt_id'] for a in plan['alternatives'])}"
            )
        project.update(
            {
                "selected_alt_id": alt_id,
                "decided_by": str(decided_by or "사용자")[:60],
                "decided_at": _now(),
                "check": None,
            }
        )
        project["status"] = "selected"
        self._save_project(project)
        return self.relayout_project(brief_id)

    def _selected(self, project: dict[str, Any], alt_id: str | None = None) -> dict[str, Any]:
        plan = project.get("plan") or {}
        wanted = alt_id or project.get("selected_alt_id")
        if not wanted:
            raise ToolError("선택한 대안이 없어요. 사람이 대안을 고른 뒤 대조할 수 있어요.")
        for alternative in plan.get("alternatives", []):
            if alternative["alt_id"] == wanted:
                return alternative
        raise ToolError(f"대안 {wanted}를 찾을 수 없어요.")

    @_locked
    def relayout_check_drawing(self, brief_id: str, rev: str, alt_id: str | None = None) -> dict[str, Any]:
        """Compare an uploaded actual revision with the selected alternative (plan vs drawing)."""
        brief = self._brief(brief_id)
        project = self._project(brief_id)
        alternative = self._selected(project, alt_id)
        model = self._model(rev)
        if model.revision.kind != RevisionKind.ACTUAL:
            raise ToolError("도면 대조는 실제 개정(업로드한 도면)으로만 할 수 있어요.")
        if rev == brief["base_rev"]:
            raise ToolError("기준 개정과는 대조할 수 없어요. CAD팀이 그린 새 개정을 고르세요.")
        floors = set(brief["floors"])
        base = self._model(brief["base_rev"])
        drawn = _occupancy(model, floors)
        original = _occupancy(base, floors)

        def where(change: dict[str, Any]) -> str | None:
            unit = drawn.by_brand.get(change["brand_id"])
            if unit is None:
                unit = drawn.by_name.get(_name_key(change["brand_name"]))
            return unit

        items = []
        touched: set[str] = set()
        for change in alternative["changes"]:
            found = where(change)
            touched.update(u for u in (change["from_unit"], change["to_unit"]) if u)
            if change["type"] == "exit":
                status = "match" if found is None else "not_exited"
            elif found is None:
                status = "missing"
            else:
                status = "match" if found == change["to_unit"] else "different_unit"
            items.append(
                {
                    "type": change["type"],
                    "type_label": CHANGE_LABELS[change["type"]],
                    "brand_id": change["brand_id"],
                    "brand_name": change["brand_name"],
                    "planned_unit": change["to_unit"],
                    "from_unit": change["from_unit"],
                    "actual_unit": found,
                    "status": status,
                    "status_label": CHECK_LABELS[status],
                }
            )
        unexpected = []
        for code in sorted(
            set(original.by_unit) | set(drawn.by_unit) | set(drawn.areas),
            key=lambda c: (floor_sort_key(c.split("-")[0]), c),
        ):
            if code in touched:
                continue
            before, after = original.by_unit.get(code), drawn.by_unit.get(code)
            if before != after:
                unexpected.append(
                    {
                        "unit_code": code,
                        "before_brand": before,
                        "before_brand_name": original.names.get(code),
                        "after_brand": after,
                        "after_brand_name": drawn.names.get(code),
                    }
                )
        area_changes = []
        for code in sorted(
            set(original.areas) | set(drawn.areas), key=lambda c: (floor_sort_key(c.split("-")[0]), c)
        ):
            before_m2, after_m2 = original.areas.get(code), drawn.areas.get(code)
            if before_m2 is None or after_m2 is None:
                area_changes.append(
                    {
                        "unit_code": code,
                        "before_m2": before_m2,
                        "after_m2": after_m2,
                        "delta_pct": None,
                        "note": "신설" if before_m2 is None else "삭제",
                    }
                )
                continue
            pct = (after_m2 / before_m2 - 1) * 100 if before_m2 else None
            if pct is not None and abs(pct) > AREA_CHANGE_PCT:
                area_changes.append(
                    {
                        "unit_code": code,
                        "before_m2": round(before_m2, 1),
                        "after_m2": round(after_m2, 1),
                        "delta_pct": round(pct, 1),
                    }
                )
        matches = sum(1 for i in items if i["status"] == "match")
        conformance = round(matches / len(items) * 100, 1) if items else 100.0
        counts = {s: sum(1 for i in items if i["status"] == s) for s in CHECK_LABELS}
        summary = (
            f"계획한 변경 {len(items)}건 중 {matches}건이 도면과 일치해요(일치율 {conformance:.0f}%). "
            f"다른 매장 {counts['different_unit']}건, 도면에 없음 {counts['missing']}건, 퇴점 안 됨 {counts['not_exited']}건, "
            f"계획에 없는 변경 {len(unexpected)}건, 면적 변화 {len(area_changes)}건이에요."
        )
        check = _json(
            {
                "brief_id": brief_id,
                "rev": rev,
                "plan_rev": alternative["rev_id"],
                "alt_id": alternative["alt_id"],
                "conformance_pct": conformance,
                "items": items,
                "unexpected": unexpected[:50],
                "area_changes": area_changes[:50],
                "summary": summary,
                "checked_at": _now(),
            }
        )
        project["check"] = check
        project["status"] = "checked"
        self._save_project(project)
        return check

    def relayout_after_upload(self, rev: str, parent_rev: str | None) -> dict[str, Any] | None:
        """Hook for drawing uploads: check the new revision against the selected plan of a brief with the
        same base revision and the same effective date (other uploads, such as redrawn copies, are skipped)."""
        if not parent_rev:
            return None
        effective = self._model(rev).revision.effective_date
        latest: tuple[str, str] | None = None
        for brief_id in self._brief_ids():
            project = self._project(brief_id)
            if not project.get("selected_alt_id"):
                continue
            try:
                brief = self._brief(brief_id)
            except ToolError:
                continue
            if brief["base_rev"] != parent_rev or brief["effective_date"] != effective:
                continue
            stamp = project.get("decided_at") or ""
            if latest is None or stamp > latest[0]:
                latest = (stamp, brief_id)
        if latest is None:
            return None
        check = self.relayout_check_drawing(latest[1], rev)
        return {k: check[k] for k in ("brief_id", "alt_id", "conformance_pct", "summary")}

    # Entry options -----------------------------------------------------------------------------------
    def relayout_entry_options(
        self,
        brand: str | None = None,
        category: str | None = None,
        rev: str | None = None,
        floors: list[str] | None = None,
        season: str | None = None,
        include_expiring: bool = True,
        top: int = 5,
    ) -> dict[str, Any]:
        """Where a new brand (catalog brand or a category's typical brand) would sell best: vacant units and,
        optionally, units whose lease ends within six months after the data period (estimates)."""
        from cad2anything.layout import AssignmentScorer, LayoutConstraints
        from cad2anything.layout.whatif import BrandInfo, brand_info

        rev = rev or self.latest_sales_rev()
        inputs = self._layout_inputs(rev)
        infos = dict(brand_info(inputs["brands"]))
        catalog = self._catalog()
        placements = self._placements(rev)
        if brand:
            brand_id = self._resolve_brand(brand)
            if brand_id not in infos:
                raise ToolError(f"브랜드 '{brand}'의 정보가 카탈로그에 없어요.")
            if brand_id in placements:
                raise ToolError(
                    f"{infos[brand_id].brand_name}는 이미 {placements[brand_id]}에 있어요. 이동 효과는 whatif_score_moves로 보세요."
                )
            info = infos[brand_id]
            prior = "카테고리 평균과 브랜드 매력도 기반 추정"
        elif category:
            code = self._resolve_category(category)
            peers = catalog[catalog["category"] == code]
            counts = pd.Series([f for v in peers["preferred_floors"] for f in as_list(v)]).value_counts()
            info = BrandInfo(
                brand_id=f"NEW-{code}",
                brand_name=f"신규 {CATEGORIES[code].name_ko} 브랜드",
                category=code,
                target_area_min_m2=float(peers["target_area_min_m2"].median()) if len(peers) else math.nan,
                target_area_max_m2=float(peers["target_area_max_m2"].median()) if len(peers) else math.nan,
                preferred_floors=(str(counts.index[0]),) if len(counts) else (),
            )
            infos[info.brand_id] = info
            prior = "카테고리 평균 브랜드 기준 추정"
        else:
            raise ToolError("brand(브랜드 ID·이름) 또는 category(카테고리)가 필요해요.")
        scorer = AssignmentScorer(
            inputs["features"], infos, inputs["model"], inputs["affinity"], inputs["adjacency"]
        )
        constraints = LayoutConstraints(area_tolerance=DEFAULT_AREA_TOLERANCE)
        wanted = [_floor_code(f) for f in floors] if floors else list(info.preferred_floors)
        assignment = dict(inputs["assignment"])
        _, data_end = self._data_range()
        horizon = (data_end + timedelta(days=183)).isoformat()
        leases = self._leases()
        expiring: dict[str, str] = {}
        if include_expiring and leases is not None:
            for b, u, e in zip(leases["brand_id"], leases["unit_code"], leases["lease_end"], strict=True):
                if assignment.get(u) == b and e <= horizon:
                    expiring[u] = e
        factor = 1.0
        if season:
            code = SEASON_ALIASES.get(str(season).strip().upper()) or SEASON_ALIASES.get(str(season).strip())
            if code is None:
                raise ToolError("season은 SS 또는 FW여야 해요.")
            factor = self._season_factors(code).get(info.category, 1.0)
        table = self.unit_table(rev).set_index("unit_code")
        rows = []
        for code, unit in scorer.units.items():
            if wanted and unit.floor not in wanted:
                continue
            occupant = assignment.get(code)
            if occupant is not None and code not in expiring:
                continue
            if not (
                constraints.area_ok(info, unit.area_m2) and (floors or constraints.floor_ok(info, unit.floor))
            ):
                continue
            trial = dict(assignment)
            trial[code] = info.brand_id
            value = scorer.location_value(info.brand_id, code) * (
                1 + scorer.bonus(info.brand_id, code, trial)
            )
            half = scorer.half_width(info.brand_id, code)
            features = []
            if code in table.index:
                row = table.loc[code]
                if bool(row.get("escalator_front")):
                    features.append("에스컬레이터 정면")
                if bool(row.get("corner")):
                    features.append("코너")
                if bool(row.get("main_walkway")):
                    features.append("메인 동선")
            current = infos[occupant].brand_name if occupant in infos else occupant
            rows.append(
                {
                    "unit_code": code,
                    "floor": unit.floor,
                    "area_m2": round(unit.area_m2, 1),
                    "status": "계약 만료 예정" if occupant else "공실",
                    "current_brand": current,
                    "lease_end": expiring.get(code),
                    "expected_sales_krw": value * factor,
                    "low_krw": value * factor * math.exp(-half),
                    "high_krw": value * factor * math.exp(half),
                    "walk_to_escalator_m": round(unit.walk_m, 1),
                    "features": features,
                }
            )
        rows.sort(key=lambda r: -r["expected_sales_krw"])
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        lo, hi = constraints.area_band(info)
        result = {
            "rev": rev,
            "brand": {
                "brand_id": info.brand_id,
                "brand_name": info.brand_name,
                "category": info.category,
                "category_name": CATEGORIES[info.category].name_ko if info.category in CATEGORIES else "",
                "area_range_m2": [round(lo, 1), round(hi, 1) if math.isfinite(hi) else None],
                "preferred_floors": list(info.preferred_floors),
                "basis": prior,
            },
            "season_factor": factor if season else None,
            "options": rows[: max(1, min(int(top), 20))],
            "total_candidates": len(rows),
            "label": "추정치",
            "caveat": (
                "실적이 없는 브랜드라 기대 매출 범위가 넓어요. 계약 만료 예정 매장은 기존 브랜드가 나간다는 가정이에요."
            ),
        }
        if not rows:
            result["note"] = (
                "면적과 선호 층에 맞는 공실이나 계약 만료 예정 매장이 없어요. floors를 넓혀 보세요."
            )
        return _json(result)

    # Report ------------------------------------------------------------------------------------------
    def _floor_svg(self, rev: str, floor: str, marks: dict[str, tuple[str, str]]) -> str:
        model = self._model(rev)
        shapes = [
            e
            for e in model.entities.values()
            if e.floor == floor and e.kind in (EntityKind.UNIT, EntityKind.WALKWAY)
        ]
        if not shapes:
            return ""
        xs0, ys0, xs1, ys1 = zip(*(e.geometry.bounds for e in shapes), strict=True)
        x0, y0, x1, y1 = min(xs0), min(ys0), max(xs1), max(ys1)
        scale = 640.0 / max(x1 - x0, 1.0)
        height = (y1 - y0) * scale

        def path(geometry: Any) -> str:
            parts = []
            for polygon in getattr(geometry, "geoms", [geometry]):
                coords = list(polygon.exterior.coords)
                points = " L ".join(f"{(x - x0) * scale:.1f},{(y1 - y) * scale:.1f}" for x, y in coords)
                parts.append(f"M {points} Z")
            return " ".join(parts)

        body = []
        for entity in shapes:
            if entity.kind == EntityKind.WALKWAY:
                body.append(f'<path d="{path(entity.geometry)}" fill="#f4f4f4" stroke="none"/>')
        for entity in shapes:
            if entity.kind != EntityKind.UNIT:
                continue
            code = str(entity.attributes.get("unit_code"))
            color, label = marks.get(code, ("#ffffff", ""))
            body.append(
                f'<path d="{path(entity.geometry)}" fill="{color}" stroke="#888" stroke-width="0.6"/>'
            )
            if label:
                cx, cy = entity.geometry.representative_point().coords[0]
                body.append(
                    f'<text x="{(cx - x0) * scale:.1f}" y="{(y1 - cy) * scale:.1f}" font-size="9" text-anchor="middle">'
                    f"{html.escape(label)}</text>"
                )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="{height:.0f}" viewBox="0 0 640 {height:.0f}" '
            f'role="img" aria-label="{html.escape(floor)} 변경 도면">' + "".join(body) + "</svg>"
        )

    def relayout_report_html(self, brief_id: str) -> str:
        """Proposal document (HTML) for a brief: diagnosis, alternatives, decision, drawing check, caveats."""
        brief = self._brief(brief_id)
        project = self._project(brief_id)
        diagnosis, plan, check = project.get("diagnosis"), project.get("plan"), project.get("check")
        esc = html.escape
        out = [
            "<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
            f"<title>{esc(brief['title'])} 제안서</title>",
            "<style>body{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;max-width:960px;margin:24px auto;"
            "padding:0 16px;color:#222;line-height:1.55}h1{font-size:24px}h2{font-size:18px;margin-top:28px;"
            "border-bottom:1px solid #ddd;padding-bottom:4px}table{border-collapse:collapse;width:100%;font-size:13px;"
            "margin:8px 0}th,td{border:1px solid #ddd;padding:4px 6px;text-align:left}th{background:#f6f6f6}"
            "td.num{text-align:right}.muted{color:#666;font-size:13px}.chip{display:inline-block;padding:0 6px;"
            "border-radius:8px;background:#eef;font-size:12px;margin-right:4px}.legend span{display:inline-block;"
            "width:12px;height:12px;margin:0 4px 0 12px;vertical-align:middle}</style></head><body>",
            f"<h1>{esc(brief['title'])} 제안서</h1>",
            f"<p class='muted'>기준 개정 {esc(brief['base_rev'])} · 적용일 {esc(str(brief['effective_date']))} · "
            f"대상 {esc(', '.join(brief['floors']))} · 목표 {esc(brief['objective_label'])} · 작성 {esc(_now())} · 모든 금액은 연 단위 추정치</p>",
            "<h2>1. 개편 조건</h2><table><tbody>",
            f"<tr><th>시즌</th><td>{esc(brief['season_label'])} {brief['year']}</td></tr>",
            f"<tr><th>이동 대상</th><td>{'계약 만료 매장만' if brief['movable'] == 'expiring' else '전체'}, 최대 {brief['max_moves']}곳, "
            f"이전 공사 {brief['downtime_weeks']:g}주</td></tr>",
            "<tr><th>입점</th><td>"
            + (
                ", ".join(f"{esc(e['brand_name'])}({esc(e['category_name'])})" for e in brief["entries"])
                or "없음"
            )
            + "</td></tr><tr><th>퇴점</th><td>"
            + (
                ", ".join(
                    f"{esc(e['brand_name'])}({esc(e['unit_code'])}, {esc(e['reason'])})"
                    for e in brief["exits"]
                )
                or "없음"
            )
            + "</td></tr>",
            f"<tr><th>메모</th><td>{esc(brief['notes'])}</td></tr></tbody></table>",
        ]
        if diagnosis:
            out.append("<h2>2. 진단</h2>")
            factors = diagnosis.get("season_factors") or []
            if factors:
                out.append(
                    "<p>시즌 지수(1보다 크면 이 시즌에 강함)</p><table><tr><th>카테고리</th><th>시즌 지수</th></tr>"
                )
                out += [
                    f"<tr><td>{esc(f['category_name'])}</td><td class='num'>{f['factor']:.2f}</td></tr>"
                    for f in factors
                ]
                out.append("</table>")
            out.append(
                "<table><tr><th>층</th><th>㎡당 매출</th><th>1년 전 같은 시즌 대비</th><th>저효율 매장</th></tr>"
            )
            for row in diagnosis.get("floors", []):
                yoy = row.get("yoy_pct")
                out.append(
                    f"<tr><td>{esc(row['floor'])}</td><td class='num'>{_krw(row.get('sales_per_m2'))}</td>"
                    f"<td class='num'>{'-' if yoy is None else f'{yoy:+.1f}%'}</td><td class='num'>{row['low_efficiency']}</td></tr>"
                )
            out.append(
                "</table><p>개편 후보(계약 만료·저효율)</p><table><tr><th>매장</th><th>브랜드</th><th>계약 만료</th><th>효율 백분위</th><th>사유</th></tr>"
            )
            for row in diagnosis.get("candidates", [])[:12]:
                pct = row.get("percentile")
                out.append(
                    f"<tr><td>{esc(row['unit_code'])}</td><td>{esc(str(row['brand_name']))}({esc(str(row['category_name']))})</td>"
                    f"<td>{esc(str(row.get('lease_end') or '-'))}</td><td class='num'>{'-' if pct is None else f'{pct:.0f}'}</td>"
                    f"<td>{''.join(f'<span class=chip>{esc(r)}</span>' for r in row['reasons'])}</td></tr>"
                )
            out.append("</table>")
        if plan:
            out.append(
                "<h2>3. 대안 비교</h2><table><tr><th>대안</th><th>이전</th><th>순효과</th><th>목표 변화</th><th>이전 손실</th><th>지금 대비 매출</th></tr>"
            )
            for alt in plan["alternatives"]:
                k = alt["kpi"]
                mark = " (선택)" if alt["alt_id"] == project.get("selected_alt_id") else ""
                out.append(
                    f"<tr><td>{esc(alt['alt_id'])}{mark}</td><td class='num'>{k['moves']}</td><td class='num'>{_signed_krw(k['net_krw'])}</td>"
                    f"<td class='num'>{_signed_krw(k['objective_delta_krw'])}</td><td class='num'>{_krw(k['move_cost_krw'])}</td>"
                    f"<td class='num'>{_signed_krw(k['vs_current_sales_krw'])}</td></tr>"
                )
            out.append("</table>")
            if plan.get("unplaced"):
                out.append(
                    "<p>자리를 찾지 못한 입점: "
                    + ", ".join(esc(u["brand_name"]) for u in plan["unplaced"])
                    + "</p>"
                )
            chosen = next(
                (a for a in plan["alternatives"] if a["alt_id"] == project.get("selected_alt_id")),
                max(plan["alternatives"], key=lambda a: a["kpi"]["net_krw"]),
            )
            decided = (
                f"{esc(project['decided_by'])}님이 {esc(str(project['decided_at']))}에 결정"
                if project.get("selected_alt_id")
                else "아직 결정 전이라 순효과가 가장 큰 대안을 보여 줘요"
            )
            out.append(
                f"<h2>4. {esc(chosen['alt_id'])} 상세</h2><p class='muted'>{decided}. {esc(chosen['summary'])}</p>"
            )
            out.append(
                "<table><tr><th>구분</th><th>브랜드</th><th>매장</th><th>기대 매출 변화</th><th>설명</th></tr>"
            )
            for change in chosen["changes"]:
                where = (
                    f"{change['from_unit']} → {change['to_unit']}"
                    if change["type"] == "move"
                    else change["to_unit"] or change["from_unit"]
                )
                out.append(
                    f"<tr><td>{CHANGE_LABELS[change['type']]}</td><td>{esc(change['brand_name'])}({esc(change['category_name'])})</td>"
                    f"<td>{esc(str(where))}</td><td class='num'>{_signed_krw(change['delta_krw'])}</td><td>{esc(change.get('explanation') or '')}</td></tr>"
                )
            out.append("</table>")
            marks: dict[str, dict[str, tuple[str, str]]] = {}
            for change in chosen["changes"]:
                if change["type"] == "entry":
                    marks.setdefault(change["to_floor"], {})[change["to_unit"]] = ("#c8e6c9", "입점")
                elif change["type"] == "move":
                    marks.setdefault(change["to_floor"], {})[change["to_unit"]] = ("#bbdefb", "이전")
                    marks.setdefault(change["from_floor"], {}).setdefault(
                        change["from_unit"], ("#eeeeee", "이전 전")
                    )
                else:
                    marks.setdefault(change["from_floor"], {}).setdefault(
                        change["from_unit"], ("#ffcdd2", "퇴점")
                    )
            if marks and self.ws.has_model(chosen["rev_id"]):
                out.append(
                    "<p class='legend'>범례<span style='background:#c8e6c9'></span>입점<span style='background:#bbdefb'></span>"
                    "이전 후<span style='background:#eeeeee'></span>이전 전<span style='background:#ffcdd2'></span>퇴점 후 빈 자리</p>"
                )
                for floor in sorted(marks, key=floor_sort_key):
                    out.append(
                        f"<p><b>{esc(floor)}</b></p>{self._floor_svg(chosen['rev_id'], floor, marks[floor])}"
                    )
        if check:
            out.append(f"<h2>5. 도면 대조({esc(check['rev'])})</h2><p>{esc(check['summary'])}</p>")
            out.append("<table><tr><th>구분</th><th>브랜드</th><th>계획</th><th>도면</th><th>결과</th></tr>")
            for item in check["items"]:
                out.append(
                    f"<tr><td>{esc(item['type_label'])}</td><td>{esc(item['brand_name'])}</td><td>{esc(str(item['planned_unit'] or '-'))}</td>"
                    f"<td>{esc(str(item['actual_unit'] or '-'))}</td><td>{esc(item['status_label'])}</td></tr>"
                )
            out.append("</table>")
            if check.get("area_changes"):
                out.append(
                    "<p>면적 변화(3% 초과): "
                    + ", ".join(
                        f"{esc(a['unit_code'])} {a['delta_pct'] if a['delta_pct'] is not None else a.get('note')}%"
                        for a in check["area_changes"][:10]
                    )
                    + "</p>"
                )
        out.append("<h2>가정과 한계</h2><ul>")
        caveats = (plan or {}).get("caveats") or list(PLAN_CAVEATS)
        out += [f"<li>{esc(c)}</li>" for c in caveats]
        out.append("<li>최종 배치는 현장 검토와 브랜드 협의 후 사람이 결정해요.</li></ul></body></html>")
        return "".join(out)

    @_locked
    def relayout_report(self, brief_id: str) -> dict[str, Any]:
        """MCP: write the proposal document and return its link."""
        document = self.relayout_report_html(brief_id)
        rel = PROJECT_DIR / f"{brief_id}.html"
        self.ws.write_text(rel, document)
        project = self._project(brief_id)
        project["report_url"] = f"/api/relayout/{brief_id}/report"
        self._save_project(project)
        sections = ["개편 조건"]
        if project.get("diagnosis"):
            sections.append("진단")
        if project.get("plan"):
            sections += ["대안 비교", "선택안 상세"]
        if project.get("check"):
            sections.append("도면 대조")
        return {
            "brief_id": brief_id,
            "url": project["report_url"],
            "status": project.get("status"),
            "sections": sections + ["가정과 한계"],
            "note": "제안서는 검토용 초안이에요. 링크를 열어 확인하세요.",
        }
