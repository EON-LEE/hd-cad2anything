"""Multi-store comparison and relayout performance review (docs/specs/atlas-api.md §1).

``analytics_store_compare`` puts the flagship next to peer stores. The flagship's monthly sales come from its
transactions and its area from the actual revision in effect each month (occupied units, weighted by days);
peer stores come from human or BI metadata (``business/stores.csv`` and ``business/store_kpis.csv``, synthetic in
this PoC: ``cad2anything.mockgen.stores``). ``analytics_relayout_review`` reviews a past relayout (parent revision
-> revision): each changed unit's average daily sales in the same number of days before and after the effective
date, sales per m2 from the parent and revision areas, and the same floor's unchanged units as a control.
``relayout_review_html`` prints the review.

Money values are full-scale net KRW (sampled receipts times their weights). Nothing here claims causality.
"""

from __future__ import annotations

import calendar
import html
import math
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import numpy as np
import pandas as pd

from cad2anything.analytics.prep import PYEONG_M2, floor_sort_key
from cad2anything.cadcore.profile import normalize_floor
from cad2anything.catalog import CATEGORIES, FACILITY_CODE, FACILITY_NAME
from cad2anything.model import RevisionKind
from cad2anything.services.analytics_tools import _floor_code, _json, _parse_date
from cad2anything.services.errors import NotFoundToolError, ToolError
from cad2anything.services.metadata import normalize_category, normalize_period
from cad2anything.services.uploads import CHANGE_LABELS

FLAGSHIP_ID = FACILITY_CODE
UNKNOWN = "UNKNOWN"
LEVELS = ("store", "floor", "category")
METRIC_LABELS: dict[str, str] = {
    "sales_per_m2": "연환산 ㎡당 매출(원)",
    "sales_per_pyeong": "연환산 평당 매출(원)",
    "sales_krw": "연환산 매출(원)",
    "area_m2": "월평균 영업면적(㎡)",
    "growth_pct": "직전 같은 기간 대비 매출 증감률(%)",
}
METRIC_SHORT: dict[str, str] = {
    "sales_per_m2": "㎡당 매출",
    "sales_per_pyeong": "평당 매출",
    "sales_krw": "매출",
    "area_m2": "영업면적",
    "growth_pct": "매출 증감률",
}
KPI_COLUMNS = ("store_id", "period", "floor", "category", "area_m2", "sales_krw")
DEFAULT_MONTHS = 12
MAX_ROWS = 200
MAX_ROW_NOTES = 4
MIN_REVIEW_DAYS = 14
MIN_CONTROL_UNITS = 3
TYPE_ORDER = (
    "move",
    "reassign",
    "resize",
    "split",
    "merge",
    "add",
    "remove",
    "fixture_change",
    "walkway_change",
)
KST = timezone(timedelta(hours=9))
STORE_GUIDE = (
    "Atlas의 개정 검토 → 메타데이터에서 stores(점포 목록: store_id, store_name, region, format, gross_area_m2)와 "
    "store_kpis(점포별 월·층·카테고리 실적: store_id, period(YYYY-MM), floor, category, area_m2, sales_krw)를 "
    "CSV나 XLSX로 올려 주세요."
)
COMPARE_CAVEAT = (
    "다른 점포 실적은 사람이 올린 store_kpis 메타데이터(이 PoC에서는 합성 데이터)이고, 대상 점포 실적은 거래 데이터에서 "
    "계산했어요. 점포마다 면적(영업 매장 면적)과 매출(순매출) 기준이 같다고 가정했어요. 입지·상권·점포 규모를 통제하지 않은 "
    "단순 비교라 차이의 원인을 뜻하지 않아요."
)
REVIEW_CAVEATS = (
    "적용일 앞뒤 같은 일수의 일평균 매출을 비교한 관측 결과예요. 상관관계일 뿐 인과 효과가 아니에요.",
    "같은 층에서 바뀌지 않은 매장의 같은 기간 변화(비교군)로 계절성과 층 공통 변화를 일부만 걸렀어요. "
    "판촉, 브랜드 자체 변화, 공사 기간은 통제하지 않았어요.",
    "합성 데이터라 개편 효과가 심어져 있어요(예: R1의 3층 에스컬레이터 정면 이동, R2의 3층 카테고리 구성 변화). "
    "실제 매장 결과가 아니에요.",
    "매출은 표본 거래에 표본 가중치를 곱한 전체 규모 순매출이에요. ㎡당 매출은 일평균 매출을 연 단위로 환산해 "
    "이전 개정과 해당 개정 도면의 면적으로 나눴어요.",
)
REVIEW_CAVEAT = "상관관계일 뿐 인과 효과가 아니에요. 같은 층 비교군으로 계절성을 일부만 걸렀고, 합성 데이터에는 개편 효과가 심어져 있어요."
FLAGSHIP_AREA_NOTE = (
    "대상 점포 면적은 달마다 그때 적용된 실제 개정 도면에서 공실을 뺀 매장 면적을 적용 일수로 가중해 썼어요."
)
FLOOR_LEVEL_NOTE = "점포마다 층에 넣은 카테고리가 달라 층 비교는 참고용이에요. 같은 카테고리끼리 비교하려면 level=category로 보세요."
REPORT_CSS = (
    "body{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;max-width:1040px;margin:24px auto;padding:0 16px;"
    "color:#222;line-height:1.55}h1{font-size:24px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid #ddd;"
    "padding-bottom:4px}table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}th,td{border:1px solid #ddd;"
    "padding:4px 6px;text-align:left;vertical-align:top}th{background:#f6f6f6}td.num{text-align:right;white-space:nowrap}"
    ".pos{color:#1b5e20}.neg{color:#b71c1c}.muted{color:#666;font-size:13px}.kpis{display:flex;gap:12px;flex-wrap:wrap}"
    ".kpi{border:1px solid #ddd;border-radius:8px;padding:8px 12px;min-width:180px}.kpi b{display:block;font-size:20px}"
    "@media print{body{margin:0;max-width:none}h2{break-after:avoid}tr{break-inside:avoid}}"
)
_DIGIT_BATCHIM = {"0": True, "1": True, "2": False, "3": True, "4": False, "5": False, "6": True, "7": True}
_DIGIT_BATCHIM |= {"8": True, "9": False}


def _alias_key(value: Any) -> str:
    return re.sub(r"[\s_\-·]+", "", str(value or "")).lower()


def _aliases(table: Mapping[str, Sequence[str]]) -> dict[str, str]:
    return {_alias_key(alias): target for target, aliases in table.items() for alias in (target, *aliases)}


_LEVELS = _aliases(
    {
        "store": ("점포", "점포전체", "전체", "total"),
        "floor": ("층", "층별", "floors"),
        "category": ("카테고리", "카테고리별", "품목", "categories"),
    }
)
_METRICS = _aliases(
    {
        "sales_per_m2": ("㎡당매출", "m2당매출", "면적당매출", "효율"),
        "sales_per_pyeong": ("평당매출", "평효율"),
        "sales_krw": ("sales", "매출", "매출액"),
        "area_m2": ("area", "면적", "영업면적"),
        "growth_pct": ("growth", "성장률", "신장률", "증감률", "매출증감률"),
    }
)


def _choice(value: Any, table: Mapping[str, str], name: str, allowed: Iterable[str]) -> str:
    choice = table.get(_alias_key(value))
    if choice is None:
        raise ToolError(f"{name}는 {', '.join(allowed)} 중 하나여야 해요: {value}")
    return choice


def _has_batchim(word: Any) -> bool:
    """Whether the last syllable of ``word`` (Hangul, digits or Latin letters read in Korean) ends in a consonant."""
    text = re.sub(r"[\s)\]}'\"]+$", "", str(word or ""))
    if not text:
        return False
    last = text[-1]
    if "가" <= last <= "힣":
        return (ord(last) - 0xAC00) % 28 != 0
    if last.isdigit():
        return _DIGIT_BATCHIM.get(last, False)
    return last.upper() in "LMNR"


def _josa(word: Any, with_batchim: str, without: str) -> str:
    return with_batchim if _has_batchim(word) else without


def _floor_key(value: Any) -> str:
    return normalize_floor(value, allow_bare=True) or _floor_code(value)


def _is_all(value: Any) -> bool:
    """True for a missing filter or one that means every floor or category (``전체``, ``all``)."""
    return _alias_key(value) in ("", "전체", "all", "*")


def _month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _month_add(month: str, delta: int) -> str:
    return _month_label(int(month[:4]) * 12 + int(month[5:7]) - 1 + delta)


def _month_span(month: str) -> tuple[date, date]:
    year, number = int(month[:4]), int(month[5:7])
    return date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])


def _pct(ratio: float | None) -> float | None:
    """``(ratio - 1) * 100`` rounded to 0.1, None for missing or non-finite ratios."""
    if ratio is None or not math.isfinite(ratio):
        return None
    return round((ratio - 1.0) * 100.0, 1)


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name}는 정수여야 해요: {value}") from exc


def _fingerprint(path: Path | None) -> str | None:
    if path is None:
        return None
    stat = path.stat()
    return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


# --- flagship and peer data -------------------------------------------------------------------------------


def _occupied_areas(model: Any) -> pd.DataFrame:
    """Occupied unit area per floor and category of one revision (vacant units excluded)."""
    rows = []
    for unit in model.units():
        attrs = unit.attributes
        vacant = attrs.get("vacant")
        vacant = not attrs.get("brand_id") if vacant is None else bool(vacant)
        if vacant or not unit.floor:
            continue
        rows.append((str(unit.floor), str(attrs.get("category") or UNKNOWN), float(unit.geometry.area)))
    frame = pd.DataFrame(rows, columns=["floor", "category", "area_m2"])
    return frame.groupby(["floor", "category"], as_index=False)["area_m2"].sum()


def flagship_monthly(host: Any) -> pd.DataFrame:
    """Flagship sales and occupied area per month x floor x category (cached per data and model version).

    Columns: ``month`` (YYYY-MM), ``floor``, ``category``, ``sales_krw`` (full-scale net sales of transactions
    mapped to a unit), ``area_m2`` (occupied area of the actual revision in effect, weighted by its days in the
    month), ``days`` (days with data in the month) and ``complete`` (the whole month has data). ``host`` is a
    Toolkit (``AnalyticsTools`` methods: ``_actual_revisions``, ``_has_sales``, ``_enriched``, ``_data_range``,
    ``_model``, ``_memo``, ``_data_key``, ``_model_key``).
    """
    revisions = sorted(
        (r for r in host._actual_revisions() if host._has_sales(r.rev_id)),
        key=lambda r: r.effective_date or "",
    )
    if not revisions:
        raise ToolError("거래 실적이 있는 실제 개정이 없어 대상 점포 실적을 계산할 수 없어요.")
    key = (
        "bench_monthly",
        host._data_key(),
        tuple((r.rev_id, r.effective_date, host._model_key(r.rev_id)) for r in revisions),
    )
    return host._memo(key, lambda: _flagship_monthly(host, revisions))


def _flagship_monthly(host: Any, revisions: Sequence[Any]) -> pd.DataFrame:
    enriched = host._enriched()
    first, last = host._data_range()
    rows = enriched.loc[enriched["mapped"].astype(bool) & enriched["floor"].notna()]
    stamps = rows["ts"]
    floor_codes, floor_values = pd.factorize(rows["floor"].astype(str))
    category_codes, category_values = pd.factorize(rows["category"].fillna(UNKNOWN).astype(str))
    sales = (
        pd.DataFrame(
            {
                "m": (stamps.dt.year * 12 + stamps.dt.month - 1).to_numpy(),
                "f": floor_codes,
                "c": category_codes,
                "sales_krw": rows["net_sales_krw"].to_numpy(dtype=float),
            }
        )
        .groupby(["m", "f", "c"], as_index=False)["sales_krw"]
        .sum()
    )
    sales["month"] = [_month_label(int(index)) for index in sales["m"]]
    sales["floor"] = np.asarray(floor_values, dtype=object)[sales["f"].to_numpy()]
    sales["category"] = np.asarray(category_values, dtype=object)[sales["c"].to_numpy()]

    months: dict[str, tuple[date, date, int, bool]] = {}
    cursor = date(first.year, first.month, 1)
    while cursor <= last:
        label = f"{cursor.year:04d}-{cursor.month:02d}"
        month_start, month_end = _month_span(label)
        lo, hi = max(month_start, first), min(month_end, last)
        months[label] = (lo, hi, (hi - lo).days + 1, lo == month_start and hi == month_end)
        cursor = month_end + timedelta(days=1)
    pieces = []
    for index, revision in enumerate(revisions):
        start = date.fromisoformat(revision.effective_date)
        start = min(start, first) if index == 0 else start
        following = revisions[index + 1].effective_date if index + 1 < len(revisions) else None
        end = date.fromisoformat(following) - timedelta(days=1) if following else last
        areas = _occupied_areas(host._model(revision.rev_id))
        for label, (lo, hi, days, _complete) in months.items():
            overlap = (min(hi, end) - max(lo, start)).days + 1
            if overlap > 0:
                pieces.append(areas.assign(month=label, area_m2=areas["area_m2"] * overlap / days))
    keys = ["month", "floor", "category"]
    area = (
        pd.concat(pieces, ignore_index=True).groupby(keys, as_index=False)["area_m2"].sum()
        if pieces
        else pd.DataFrame(columns=[*keys, "area_m2"])
    )
    table = sales[[*keys, "sales_krw"]].merge(area, on=keys, how="outer")
    table = table[table["month"].isin(list(months))].fillna({"sales_krw": 0.0, "area_m2": 0.0})
    table["days"] = table["month"].map(lambda m: months[m][2]).astype(int)
    table["complete"] = table["month"].map(lambda m: months[m][3]).astype(bool)
    return table.sort_values(keys, kind="mergesort").reset_index(drop=True)


def _text_table(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    return frame.astype(str).apply(lambda column: column.str.strip())


def _numbers(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values.str.replace(",", "", regex=False), errors="coerce")


def load_store_kpis(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Peer KPIs (``store_id, month, floor, category, area_m2, sales_krw``) normalized like the metadata upload,
    without the flagship's own rows, plus notes on dropped rows."""
    raw = _text_table(path)
    missing = [column for column in KPI_COLUMNS if column not in raw.columns]
    if missing:
        raise ToolError(f"store_kpis 표에 필요한 열이 없어요: {', '.join(missing)}. {STORE_GUIDE}")
    frame = pd.DataFrame(
        {
            "store_id": raw["store_id"],
            "month": raw["period"].map(normalize_period),
            "floor": raw["floor"].map(lambda value: normalize_floor(value, allow_bare=True) or value),
            "category": raw["category"].map(normalize_category),
            "area_m2": _numbers(raw["area_m2"]),
            "sales_krw": _numbers(raw["sales_krw"]),
        }
    )
    valid = (
        (frame["store_id"] != "")
        & frame["month"].notna()
        & (frame["floor"] != "")
        & (frame["area_m2"] > 0)
        & (frame["sales_krw"] >= 0)
    )
    notes: list[str] = []
    if not valid.all():
        notes.append(f"store_kpis에서 값이 비었거나 형식이 맞지 않는 {int((~valid).sum())}행은 뺐어요.")
    frame = frame[valid]
    own = frame["store_id"] == FLAGSHIP_ID
    if own.any():
        notes.append(
            f"store_kpis의 {FLAGSHIP_ID} 행은 쓰지 않았어요. 대상 점포 실적은 거래 데이터로 계산해요."
        )
    keys = ["store_id", "month", "floor", "category"]
    frame = frame[~own].groupby(keys, as_index=False)[["area_m2", "sales_krw"]].sum()
    return frame.astype({"store_id": object, "month": object, "floor": object, "category": object}), notes


def load_stores(path: Path | None) -> dict[str, dict[str, Any]]:
    """Store metadata by ``store_id`` (first row wins); empty without a readable stores table."""
    if path is None:
        return {}
    raw = _text_table(path)
    if "store_id" not in raw.columns:
        return {}
    stores: dict[str, dict[str, Any]] = {}
    for row in raw.to_dict(orient="records"):
        store_id = str(row.get("store_id") or "")
        if not store_id or store_id in stores:
            continue
        gross = pd.to_numeric(str(row.get("gross_area_m2") or "").replace(",", ""), errors="coerce")
        stores[store_id] = {
            "store_name": str(row.get("store_name") or "") or store_id,
            "region": str(row.get("region") or ""),
            "format": str(row.get("format") or ""),
            "gross_area_m2": float(gross) if pd.notna(gross) and gross > 0 else None,
        }
    return stores


# --- store comparison -------------------------------------------------------------------------------------


def _ordered_keys(level: str, keys: Iterable[Any]) -> list[str]:
    values = [str(key) for key in keys]
    if level == "floor":
        return sorted(values, key=floor_sort_key)
    if level == "category":
        order = {code: index for index, code in enumerate(CATEGORIES)}
        return sorted(values, key=lambda key: (order.get(key, len(order)), key))
    return values


def _row_label(level: str, key: str, floor: str | None, categories: Sequence[str] | None, text: Any) -> str:
    if level == "floor":
        return key
    if level == "category":
        return CATEGORIES[key].name_ko if key in CATEGORIES else key
    parts = [floor] if floor else []
    if categories:
        parts.append(CATEGORIES[categories[0]].name_ko if len(categories) == 1 else str(text))
    return " ".join(parts) + " 합계" if parts else "점포 전체"


def _metric_value(
    metric: str, sales: float, area: float, months: int, prior: float | None, full: bool
) -> float | None:
    """One store's value for a row: annualized sales or sales per area over ``months`` months, average area,
    or growth against the previous window (only when both windows are fully covered)."""
    if months <= 0:
        return None
    if metric == "sales_krw":
        return sales * 12.0 / months
    if metric == "area_m2":
        return area / months
    if metric in ("sales_per_m2", "sales_per_pyeong"):
        if area <= 0:
            return None
        value = sales * 12.0 / area
        return value * PYEONG_M2 if metric == "sales_per_pyeong" else value
    if not full or prior is None or prior <= 0:
        return None
    return (sales / prior - 1.0) * 100.0


def _rounded(metric: str, value: float | None) -> float | int | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 1) if metric in ("growth_pct", "area_m2") else round(value)


def _compare_row(
    key: str, label: str, values: Mapping[str, float | None], peers: Sequence[str], metric: str
) -> dict[str, Any]:
    flagship = values.get(FLAGSHIP_ID)
    peer_values = [value for sid in peers if (value := values.get(sid)) is not None]
    median = statistics.median(peer_values) if peer_values else None
    present = [value for value in values.values() if value is not None]
    gap = None
    if flagship is not None and median is not None:
        if metric == "growth_pct":
            gap = round(flagship - median, 1)
        elif median > 0:
            gap = round((flagship / median - 1.0) * 100.0, 1)
    return {
        "key": key,
        "label": label,
        "flagship": _rounded(metric, flagship),
        "peer_median": _rounded(metric, median),
        "rank": 1 + sum(value > flagship for value in present) if flagship is not None else None,
        "n_stores": len(present),
        "gap_pct": gap,
        "values": {sid: _rounded(metric, value) for sid, value in values.items()},
    }


def _missing_notes(
    rows: Sequence[Mapping[str, Any]], stores: Sequence[str], names: Mapping[str, str], level: str
) -> list[str]:
    messages = []
    for row in rows:
        absent = [names[sid] for sid in stores if row["values"].get(sid) is None]
        if absent:
            messages.append(f"{row['label']}: {', '.join(absent)}에는 실적이 없어 비교에서 빠졌어요.")
    if len(messages) > MAX_ROW_NOTES:
        extra = len(messages) - (MAX_ROW_NOTES - 1)
        noun = "층" if level == "floor" else "항목"
        messages = [
            *messages[: MAX_ROW_NOTES - 1],
            f"그 밖에 {extra}개 {noun}도 일부 점포에 실적이 없어 n_stores가 적어요.",
        ]
    return messages


def _compare_summary(
    level: str, metric: str, rows: Sequence[Mapping[str, Any]], months: Sequence[str]
) -> str:
    basis = f"{months[0]}~{months[-1]} {METRIC_SHORT[metric]} 기준으로"
    unit = "%p" if metric == "growth_pct" else "%"
    ranked = [row for row in rows if row["gap_pct"] is not None]
    if not ranked:
        return (
            f"{basis} {FACILITY_NAME}{_josa(FACILITY_NAME, '과', '와')} 견줄 수 있는 다른 점포 실적이 없어요."
        )
    subject = f"{basis} {FACILITY_NAME}{_josa(FACILITY_NAME, '은', '는')}"
    if len(ranked) == 1:
        row = ranked[0]
        direction = "높아요" if row["gap_pct"] >= 0 else "낮아요"
        return (
            f"{subject} {row['label']}에서 {row['n_stores']}개 점포 중 {row['rank']}위이고, "
            f"다른 점포 중앙값보다 {abs(row['gap_pct']):.1f}{unit} {direction}."
        )
    above = sum(row["gap_pct"] > 0 for row in ranked)
    below = sum(row["gap_pct"] < 0 for row in ranked)
    best = max(ranked, key=lambda row: row["gap_pct"])
    worst = min(ranked, key=lambda row: row["gap_pct"])
    noun = "층" if level == "floor" else "카테고리"

    def describe(row: Mapping[str, Any]) -> str:
        return f"{row['label']}({row['gap_pct']:+.1f}{unit}, {row['n_stores']}개 점포 중 {row['rank']}위)"

    return (
        f"{subject} 비교한 {len(ranked)}개 {noun} 중 {above}개가 다른 점포 중앙값보다 높고 {below}개가 낮아요. "
        f"가장 앞선 곳은 {describe(best)}{_josa(best['label'], '이고', '고')}, "
        f"가장 뒤처진 곳은 {describe(worst)}{_josa(worst['label'], '이에요', '예요')}."
    )


# --- relayout review ----------------------------------------------------------------------------------------


def _unit_index(model: Any) -> dict[str, dict[str, Any]]:
    units: dict[str, dict[str, Any]] = {}
    for unit in model.units():
        attrs = unit.attributes
        vacant = attrs.get("vacant")
        units[str(attrs.get("unit_code") or unit.entity_id)] = {
            "floor": unit.floor,
            "brand_id": attrs.get("brand_id"),
            "brand_name": attrs.get("brand_name"),
            "area": float(unit.geometry.area),
            "vacant": not attrs.get("brand_id") if vacant is None else bool(vacant),
        }
    return units


def _control_groups(
    old: Mapping[str, Mapping[str, Any]],
    new: Mapping[str, Mapping[str, Any]],
    changed: set[str],
    before: Mapping[str, float],
    after: Mapping[str, float],
) -> dict[str | None, dict[str, Any]]:
    """Unchanged units (same code, floor and brand in both layouts, occupied, with sales before the change) per
    floor and for the whole store (key None): unit count, summed sales before and after, and their ratio."""
    groups: dict[str | None, dict[str, Any]] = {}
    for code, unit in old.items():
        other = new.get(code)
        if code in changed or other is None or unit["vacant"] or other["vacant"]:
            continue
        if unit["floor"] != other["floor"] or unit["brand_id"] != other["brand_id"]:
            continue
        sales_before = before.get(code, 0.0)
        if sales_before <= 0:
            continue
        for key in (unit["floor"], None):
            group = groups.setdefault(key, {"units": 0, "before": 0.0, "after": 0.0})
            group["units"] += 1
            group["before"] += sales_before
            group["after"] += after.get(code, 0.0)
    for group in groups.values():
        group["ratio"] = group["after"] / group["before"] if group["before"] > 0 else None
    return groups


def _control_for(
    floor: Any, groups: Mapping[str | None, Mapping[str, Any]]
) -> tuple[float | None, str | None]:
    """The floor's control ratio with enough units, else the store-wide one; plus its scope."""
    group = groups.get(floor)
    if group and group["units"] >= MIN_CONTROL_UNITS and group["ratio"]:
        return group["ratio"], "floor"
    store = groups.get(None)
    if store and store["ratio"]:
        return store["ratio"], "store"
    return None, None


def _brand_list(
    codes: Sequence[str],
    units: Mapping[str, Mapping[str, Any]],
    brand_ids: Iterable[Any],
    names: Mapping[str, str],
) -> str | None:
    found: list[str] = []
    for code in codes:
        unit = units.get(code)
        if unit and not unit["vacant"]:
            name = unit["brand_name"] or names.get(str(unit["brand_id"] or ""))
            if name and str(name) not in found:
                found.append(str(name))
    if not found:
        for brand_id in brand_ids:
            name = names.get(str(brand_id)) or str(brand_id)
            if name not in found:
                found.append(name)
    return ", ".join(found) or None


def _units_label(before: Sequence[str], after: Sequence[str]) -> str:
    left, right = ", ".join(before), ", ".join(after)
    if not left or not right or left == right:
        return left or right
    return f"{left} → {right}"


def _review_item(
    item: Any,
    old: Mapping[str, Mapping[str, Any]],
    new: Mapping[str, Mapping[str, Any]],
    before: Mapping[str, float],
    after: Mapping[str, float],
    days: int,
    groups: Mapping[str | None, Mapping[str, Any]],
    names: Mapping[str, str],
) -> dict[str, Any]:
    """One change: daily sales of its units before (parent layout) and after (revision layout) and the control."""
    kind = str(item.change_type)
    codes_before = [str(code) for code in item.unit_codes_before]
    codes_after = [str(code) for code in item.unit_codes_after]
    before_daily = round(sum(before.get(c, 0.0) for c in codes_before) / days) if codes_before else None
    after_daily = round(sum(after.get(c, 0.0) for c in codes_after) / days) if codes_after else None
    area_before = sum(old[c]["area"] for c in codes_before if c in old) or None
    area_after = sum(new[c]["area"] for c in codes_after if c in new) or None
    ratio, scope = _control_for(item.floor, groups)
    comparable = bool(before_daily and before_daily > 0 and after_daily is not None)
    change = after_daily / before_daily if comparable else None
    per_m2_before = before_daily * 365 / area_before if before_daily is not None and area_before else None
    per_m2_after = after_daily * 365 / area_after if after_daily is not None and area_after else None
    if not codes_before:
        note = "신설 매장이라 적용 전 실적이 없어요."
    elif not codes_after:
        note = "없어진 매장이라 적용 후 실적이 없어요."
    elif not comparable:
        note = "적용 전 실적이 없어(공실 등) 증감률을 계산하지 않았어요."
    elif scope == "store":
        note = "같은 층 비교군이 적어 점포 전체 비교군을 썼어요."
    else:
        note = None
    names_before = _brand_list(codes_before, old, item.brand_ids_before, names)
    names_after = _brand_list(codes_after, new, item.brand_ids_after, names)
    return {
        "change_type": kind,
        "change_label": CHANGE_LABELS.get(kind, kind),
        "unit_code": (codes_after or codes_before)[0],
        "units_before": codes_before,
        "units_after": codes_after,
        "units_label": _units_label(codes_before, codes_after),
        "floor": item.floor,
        "brand_name": names_after or names_before,
        "brand_name_before": names_before,
        "brand_name_after": names_after,
        "before_daily_krw": before_daily,
        "after_daily_krw": after_daily,
        "change_pct": _pct(change),
        "control_pct": _pct(ratio),
        "vs_control_pct": _pct(change / ratio) if change is not None and ratio else None,
        "control_scope": scope,
        "before_area_m2": round(area_before, 1) if area_before else None,
        "after_area_m2": round(area_after, 1) if area_after else None,
        "before_sales_per_m2": round(per_m2_before) if per_m2_before is not None else None,
        "after_sales_per_m2": round(per_m2_after) if per_m2_after is not None else None,
        "sales_per_m2_change_pct": _pct(per_m2_after / per_m2_before)
        if per_m2_after is not None and per_m2_before
        else None,
        "comparable": comparable,
        "note": note,
        "_ratio": ratio if comparable else None,
    }


def _review_group(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Sums over the items (a unit in two changes counts in both) and ratios over the comparable ones."""
    comparable = [item for item in items if item["comparable"]]
    controlled = [item for item in comparable if item["_ratio"]]
    base = sum(item["before_daily_krw"] for item in comparable)
    controlled_base = sum(item["before_daily_krw"] for item in controlled)
    expected = sum(item["before_daily_krw"] * item["_ratio"] for item in controlled)
    return {
        "items": len(items),
        "comparable": len(comparable),
        "before_daily_krw": sum(item["before_daily_krw"] or 0 for item in items),
        "after_daily_krw": sum(item["after_daily_krw"] or 0 for item in items),
        "change_pct": _pct(sum(item["after_daily_krw"] for item in comparable) / base) if base > 0 else None,
        "control_pct": _pct(expected / controlled_base) if controlled_base > 0 else None,
        "vs_control_pct": _pct(sum(item["after_daily_krw"] for item in controlled) / expected)
        if expected > 0
        else None,
    }


def _review_total(
    items: Sequence[Mapping[str, Any]],
    old: Mapping[str, Mapping[str, Any]],
    before: Mapping[str, float],
    after: Mapping[str, float],
    days: int,
    groups: Mapping[str | None, Mapping[str, Any]],
) -> dict[str, Any]:
    """Comparable changes with every unit counted once, against the control of each unit's floor."""
    comparable = [item for item in items if item["comparable"]]
    codes_before = sorted({code for item in comparable for code in item["units_before"]})
    codes_after = sorted({code for item in comparable for code in item["units_after"]})
    base = sum(before.get(code, 0.0) for code in codes_before) / days
    actual = sum(after.get(code, 0.0) for code in codes_after) / days
    ratios = [_control_for((old.get(code) or {}).get("floor"), groups)[0] for code in codes_before]
    expected = (
        sum(before.get(code, 0.0) / days * ratio for code, ratio in zip(codes_before, ratios, strict=True))
        if ratios and all(ratios)
        else None
    )
    return {
        "items": len(items),
        "comparable": len(comparable),
        "units_before": len(codes_before),
        "units_after": len(codes_after),
        "before_daily_krw": round(base),
        "after_daily_krw": round(actual),
        "change_pct": _pct(actual / base) if base > 0 else None,
        "control_pct": _pct(expected / base) if expected and base > 0 else None,
        "vs_control_pct": _pct(actual / expected) if expected else None,
    }


def _describe_item(item: Mapping[str, Any]) -> str:
    parts = [item.get("brand_name"), item["change_label"], f"비교군 대비 {item['vs_control_pct']:+.1f}%"]
    return f"{item['units_label']}({', '.join(str(part) for part in parts if part)})"


def _review_summary(
    rev: str,
    parent: str,
    effective: date,
    days: int,
    total: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
) -> str:
    head = f"{rev} 개편({parent}→{rev}, {effective} 적용)"
    comparable = [item for item in items if item["comparable"]]
    if not comparable or total["change_pct"] is None:
        return f"{head}의 변경 {len(items)}건 중 적용 전후 실적이 모두 있는 변경이 없어 증감률을 계산하지 못했어요."
    first = (
        f"{head} 앞뒤 {days}일씩 비교하면, 바뀐 매장(변경 {len(comparable)}건)의 일평균 매출은 "
        f"{total['change_pct']:+.1f}%"
    )
    if total["control_pct"] is not None and total["vs_control_pct"] is not None:
        first += (
            f", 같은 층의 바뀌지 않은 매장(비교군)은 {total['control_pct']:+.1f}% 달라져 "
            f"비교군 대비 {total['vs_control_pct']:+.1f}%예요."
        )
    else:
        first += " 달라졌어요. 비교군이 없어 비교군 대비 값은 계산하지 못했어요."
    sentences = [first]
    ranked = [item for item in comparable if item["vs_control_pct"] is not None]
    if len(ranked) >= 2:
        best = max(ranked, key=lambda item: item["vs_control_pct"])
        worst = min(ranked, key=lambda item: item["vs_control_pct"])
        sentences.append(
            f"가장 좋아진 곳은 {_describe_item(best)}{_josa(best['units_label'], '이고', '고')}, "
            f"가장 나빠진 곳은 {_describe_item(worst)}{_josa(worst['units_label'], '이에요', '예요')}."
        )
    elif ranked:
        sentences.append(f"비교군 대비 값이 있는 변경은 {_describe_item(ranked[0])} 하나예요.")
    missing = len(items) - len(comparable)
    if missing:
        sentences.append(f"신설·철거처럼 적용 전후 중 한쪽 실적이 없는 {missing}건은 증감률 계산에서 뺐어요.")
    return " ".join(sentences)


def _esc(value: Any) -> str:
    return html.escape("-" if value is None else str(value))


def _won(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(number):
        return "-"
    sign, amount = ("-" if number < 0 else ""), abs(number)
    if amount >= 1e8:
        return f"{sign}{amount / 1e8:,.1f}억 원"
    if amount >= 1e4:
        return f"{sign}{amount / 1e4:,.0f}만 원"
    return f"{sign}{amount:,.0f}원"


def _num_cell(text: Any) -> str:
    return f"<td class='num'>{_esc(text)}</td>"


def _pct_cell(value: Any) -> str:
    if value is None:
        return "<td class='num'>-</td>"
    number = float(value)
    css = "pos" if number > 0 else "neg" if number < 0 else ""
    return f"<td class='num {css}'>{_esc(f'{number:+.1f}%')}</td>"


def _group_cells(row: Mapping[str, Any]) -> str:
    return (
        _num_cell(f"{row['items']}건")
        + _num_cell(f"{row['comparable']}건")
        + _num_cell(_won(row["before_daily_krw"]))
        + _num_cell(_won(row["after_daily_krw"]))
        + _pct_cell(row["change_pct"])
        + _pct_cell(row["control_pct"])
        + _pct_cell(row["vs_control_pct"])
    )


def render_relayout_review(review: Mapping[str, Any]) -> str:
    """Printable Korean HTML report of ``analytics_relayout_review``; every interpolated value is escaped."""
    window = review["window"]
    total = review.get("total") or {}
    title = f"{review['rev']} 개편 성과 추적 보고"
    period = (
        f"{review['parent_rev']} → {review['rev']} · 적용일 {review['effective_date']} · "
        f"적용 전 {window['before']['start']}~{window['before']['end']} · "
        f"적용 후 {window['after']['start']}~{window['after']['end']}({window['days']}일씩) · 작성 {_now()}"
    )
    out = [
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
        f"<title>{_esc(title)}</title><style>{REPORT_CSS}</style></head><body>",
        f"<h1>{_esc(title)}</h1><p class='muted'>{_esc(period)}</p>",
        f"<h2>1. 요약</h2><p>{_esc(review['summary'])}</p><div class='kpis'>",
    ]
    for label, value in (
        ("바뀐 매장 일평균 매출", total.get("change_pct")),
        ("같은 층 비교군", total.get("control_pct")),
        ("비교군 대비", total.get("vs_control_pct")),
    ):
        shown = "-" if value is None else f"{value:+.1f}%"
        out.append(f"<div class='kpi'>{_esc(label)}<b>{_esc(shown)}</b></div>")
    out.append("</div>")
    columns = "<th>건수</th><th>비교 가능</th><th>적용 전 일평균</th><th>적용 후 일평균</th><th>증감</th><th>비교군</th><th>비교군 대비</th>"
    out.append(f"<h2>2. 변경 유형별</h2><table><tr><th>유형</th>{columns}</tr>")
    for row in review["by_type"]:
        out.append(f"<tr><td>{_esc(row['change_label'])}</td>{_group_cells(row)}</tr>")
    out.append(f"</table><h2>3. 층별</h2><table><tr><th>층</th>{columns}<th>비교군 매장</th></tr>")
    for row in review["by_floor"]:
        out.append(
            f"<tr><td>{_esc(row['floor'])}</td>{_group_cells(row)}{_num_cell(str(row['control_units']) + '곳')}</tr>"
        )
    out.append(
        "</table><h2>4. 변경 매장</h2><table><tr><th>유형</th><th>층</th><th>매장</th><th>브랜드</th>"
        "<th>적용 전 일평균</th><th>적용 후 일평균</th><th>증감</th><th>비교군</th><th>비교군 대비</th>"
        "<th>㎡당 매출(연환산, 전 → 후)</th><th>메모</th></tr>"
    )
    for item in review["items"]:
        before_name, after_name = item.get("brand_name_before"), item.get("brand_name_after")
        brand = (
            f"{before_name} → {after_name}"
            if before_name and after_name and before_name != after_name
            else after_name or before_name
        )
        per_m2 = f"{_won(item.get('before_sales_per_m2'))} → {_won(item.get('after_sales_per_m2'))}"
        out.append(
            f"<tr><td>{_esc(item['change_label'])}</td><td>{_esc(item['floor'])}</td><td>{_esc(item['units_label'])}</td>"
            f"<td>{_esc(brand)}</td>{_num_cell(_won(item['before_daily_krw']))}{_num_cell(_won(item['after_daily_krw']))}"
            f"{_pct_cell(item['change_pct'])}{_pct_cell(item['control_pct'])}{_pct_cell(item['vs_control_pct'])}"
            f"{_num_cell(per_m2)}<td>{_esc(item.get('note') or '')}</td></tr>"
        )
    out.append("</table>")
    if review.get("notes"):
        out.append(
            "<h2>참고</h2><ul>" + "".join(f"<li>{_esc(note)}</li>" for note in review["notes"]) + "</ul>"
        )
    caveats = review.get("caveats") or [review.get("caveat")]
    out.append("<h2>가정과 한계</h2><ul>" + "".join(f"<li>{_esc(c)}</li>" for c in caveats if c) + "</ul>")
    out.append("</body></html>")
    return "".join(out)


class BenchmarkTools:
    """Mixin for Toolkit: multi-store comparison and relayout performance review.

    Needs ``ws``, ``_model``, ``_memo``, ``_data_key``, ``_model_key``, ``_enriched``, ``_data_range``,
    ``_actual_revisions``, ``_has_sales``, ``_table`` and ``_resolve_categories`` (AnalyticsTools) and
    ``_change_set`` (Toolkit)."""

    ws: Any

    # Store comparison ------------------------------------------------------------------------------------
    def _peer_tables(self) -> tuple[pd.DataFrame, list[str], dict[str, dict[str, Any]], bool]:
        """Peer KPIs, loader notes, store metadata and whether a stores table exists (cached per file)."""
        kpi_path = self.ws.table_path("store_kpis")
        if kpi_path is None:
            raise ToolError(
                f"다른 점포 실적(store_kpis) 메타데이터가 없어 점포를 비교할 수 없어요. {STORE_GUIDE}"
            )
        stores_path = self.ws.table_path("stores")
        key = ("bench_peers", _fingerprint(kpi_path), _fingerprint(stores_path))
        kpis, notes, stores = self._memo(key, lambda: (*load_store_kpis(kpi_path), load_stores(stores_path)))
        if kpis.empty:
            raise ToolError(f"store_kpis에 비교할 다른 점포 실적이 없어요. {STORE_GUIDE}")
        return kpis, list(notes), stores, stores_path is not None

    @staticmethod
    def _compare_months(flagship: pd.DataFrame, kpis: pd.DataFrame, start: Any, end: Any) -> list[str]:
        """Complete flagship months of the comparison period (default: the latest 12 covered by both sources)."""
        complete = sorted(set(flagship.loc[flagship["complete"], "month"]))
        if not complete:
            raise ToolError("대상 점포 거래 데이터에 한 달을 다 채운 달이 없어 비교할 수 없어요.")
        first_day, last_day = _parse_date(start, "start"), _parse_date(end, "end", end=True)
        if first_day is None and last_day is None:
            peer_last = str(kpis["month"].max())
            stop = min(complete[-1], peer_last) if peer_last >= complete[0] else complete[-1]
            begin = _month_add(stop, 1 - DEFAULT_MONTHS)
        else:
            begin = f"{first_day:%Y-%m}" if first_day else _month_add(f"{last_day:%Y-%m}", 1 - DEFAULT_MONTHS)
            stop = f"{last_day:%Y-%m}" if last_day else _month_add(begin, DEFAULT_MONTHS - 1)
        months = [month for month in complete if begin <= month <= stop]
        if not months:
            raise ToolError(
                f"요청한 기간({begin}~{stop})에 대상 점포의 한 달 치 거래 데이터가 없어요"
                f"(데이터 {complete[0]}~{complete[-1]})."
            )
        return months

    def analytics_store_compare(
        self,
        level: str = "floor",
        metric: str = "sales_per_m2",
        floor: str | None = None,
        category: str | None = None,
        start: str | None = None,
        end: str | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        """Flagship versus peer stores for the whole store (``level=store``), per floor or per category.

        The flagship comes from its transactions and the layout in effect each month; peers come from the
        ``store_kpis`` metadata. The default period is the latest 12 complete months of the flagship's data (ending
        earlier when the peers' KPIs do). ``floor`` and ``category`` (code, Korean name or ``F&B``) filter the data
        before aggregation. Metrics: ``sales_per_m2``, ``sales_per_pyeong``, ``sales_krw`` (annualized),
        ``area_m2`` (monthly average) and ``growth_pct`` (versus the previous window of the same length).
        ``rank`` counts every store including the flagship (1 = largest value) and ``gap_pct`` compares the
        flagship with the peer median (percentage points for ``growth_pct``). With ``top`` only the rows with the
        largest gaps are returned.
        """
        level = _choice(level, _LEVELS, "level", LEVELS)
        metric = _choice(metric, _METRICS, "metric", METRIC_LABELS)
        kpis, notes, info, has_stores = self._peer_tables()
        flagship = flagship_monthly(self)
        months = self._compare_months(flagship, kpis, start, end)
        n_months = len(months)
        previous = [_month_add(month, -n_months) for month in months]
        own = flagship.loc[flagship["complete"], ["month", "floor", "category", "area_m2", "sales_krw"]]
        data = pd.concat([own.assign(store_id=FLAGSHIP_ID), kpis], ignore_index=True)
        coverage = data[data["month"].isin(months)].groupby("store_id")["month"].nunique().to_dict()
        prior_coverage = data[data["month"].isin(previous)].groupby("store_id")["month"].nunique().to_dict()
        listed = [sid for sid in info if sid != FLAGSHIP_ID]
        peers = [
            sid for sid in [*listed, *sorted(set(kpis["store_id"]) - set(listed))] if coverage.get(sid, 0) > 0
        ]
        if not peers:
            raise ToolError(
                f"{months[0]}~{months[-1]}에 다른 점포 실적이 없어요. store_kpis의 period를 확인하세요. {STORE_GUIDE}"
            )
        names = {FLAGSHIP_ID: FACILITY_NAME} | {
            sid: (info.get(sid) or {}).get("store_name") or sid for sid in peers
        }
        stores = [FLAGSHIP_ID, *peers]

        floor_code = None if _is_all(floor) else _floor_key(floor)
        categories = None if _is_all(category) else self._resolve_categories(category)
        if floor_code:
            data = data[data["floor"] == floor_code]
        if categories:
            data = data[data["category"].isin(categories)]
        if level == "category":
            data = data[data["category"] != UNKNOWN]
        current_rows = data[data["month"].isin(months)]
        if current_rows.empty:
            raise ToolError(
                f"{months[0]}~{months[-1]}에 조건(층 {floor_code or '전체'}, 카테고리 {category or '전체'})에 맞는 "
                "실적이 어느 점포에도 없어요."
            )
        column = {"store": None, "floor": "floor", "category": "category"}[level]

        def grouped(frame: pd.DataFrame) -> pd.DataFrame:
            keyed = frame.assign(key=frame[column] if column else "ALL")
            return keyed.groupby(["key", "store_id"])[["sales_krw", "area_m2"]].sum()

        current = grouped(current_rows)
        cells = {
            (str(k), str(s)): (float(sales), float(area))
            for (k, s), sales, area in zip(
                current.index, current["sales_krw"], current["area_m2"], strict=True
            )
        }
        prior: dict[tuple[str, str], float] = {}
        if metric == "growth_pct":
            before = grouped(data[data["month"].isin(previous)])["sales_krw"]
            prior = {(str(k), str(s)): float(v) for (k, s), v in before.items()}
        rows = []
        for key in _ordered_keys(level, current.index.get_level_values("key").unique()):
            values: dict[str, float | None] = {}
            for sid in stores:
                cell = cells.get((key, sid))
                full = coverage.get(sid, 0) >= n_months and prior_coverage.get(sid, 0) >= n_months
                values[sid] = (
                    None
                    if cell is None
                    else _metric_value(
                        metric, cell[0], cell[1], coverage.get(sid, 0), prior.get((key, sid)), full
                    )
                )
            label = _row_label(level, key, floor_code, categories, category)
            rows.append(_compare_row(key, label, values, peers, metric))
        summary = _compare_summary(level, metric, rows, months)

        if level == "floor":
            notes.insert(0, FLOOR_LEVEL_NOTE)
        notes.append(FLAGSHIP_AREA_NOTE)
        if not has_stores:
            notes.append("stores 표가 없어 점포 이름·지역·형태 대신 store_id를 보여 줘요.")
        partial = [f"{names[sid]} {coverage[sid]}개월" for sid in peers if coverage[sid] < n_months]
        if partial:
            notes.append(
                f"기간 {n_months}개월 중 일부 달만 실적이 있는 점포는 있는 달로 연환산했어요: {', '.join(partial)}."
            )
        idle = [str(info[sid]["store_name"]) for sid in listed if coverage.get(sid, 0) == 0]
        if idle:
            notes.append(f"기간에 실적이 없어 뺀 점포: {', '.join(idle)}.")
        if metric == "growth_pct":
            short = [
                names[sid]
                for sid in stores
                if coverage.get(sid, 0) < n_months or prior_coverage.get(sid, 0) < n_months
            ]
            if short:
                notes.append(f"직전 {n_months}개월 실적이 모자라 증감률을 비운 점포: {', '.join(short)}.")
        notes += _missing_notes(rows, stores, names, level)
        truncated = False
        if top is not None:
            limit = max(1, min(_int(top, "top"), MAX_ROWS))
            if len(rows) > limit:
                rows = sorted(rows, key=lambda row: (row["gap_pct"] is None, -abs(row["gap_pct"] or 0.0)))
                rows, truncated = rows[:limit], True

        own_info = info.get(FLAGSHIP_ID) or {}
        listing = [
            {
                "store_id": FLAGSHIP_ID,
                "store_name": FACILITY_NAME,
                "region": own_info.get("region", ""),
                "format": own_info.get("format") or "백화점",
                "flagship": True,
                "months": n_months,
            }
        ]
        for sid in peers:
            listing.append(
                {
                    "store_id": sid,
                    "store_name": names[sid],
                    "region": (info.get(sid) or {}).get("region", ""),
                    "format": (info.get(sid) or {}).get("format", ""),
                    "flagship": False,
                    "months": int(coverage[sid]),
                }
            )
        gap_label = (
            "gap_pct는 대상 점포 증감률에서 다른 점포 중앙값을 뺀 값(%p)이에요."
            if metric == "growth_pct"
            else "gap_pct는 대상 점포 값이 다른 점포 중앙값보다 몇 % 높은지예요(음수면 낮아요)."
        )
        return _json(
            {
                "level": level,
                "metric": metric,
                "metric_label": METRIC_LABELS[metric],
                "period": {
                    "start": _month_span(months[0])[0].isoformat(),
                    "end": _month_span(months[-1])[1].isoformat(),
                    "months": n_months,
                },
                "filters": {"floor": floor_code, "category": categories},
                "stores": listing,
                "rows": rows,
                "truncated": truncated,
                "summary": summary,
                "gap_label": gap_label,
                "notes": notes,
                "caveat": COMPARE_CAVEAT,
            }
        )

    # Relayout review -------------------------------------------------------------------------------------
    def _unit_sales(self, start: date, end: date) -> dict[str, float]:
        """Full-scale net sales per unit code in ``[start, end]`` (inclusive dates; cached per data version)."""
        key = ("bench_unit_sales", start.isoformat(), end.isoformat(), self._data_key())

        def compute() -> dict[str, float]:
            enriched = self._enriched()
            stamps = enriched["ts"]
            mask = (
                (stamps >= pd.Timestamp(start))
                & (stamps < pd.Timestamp(end + timedelta(days=1)))
                & enriched["unit_code"].notna()
            )
            part = enriched.loc[mask, ["unit_code", "net_sales_krw"]]
            sums = part.groupby(part["unit_code"].astype(str))["net_sales_krw"].sum()
            return {str(code): float(value) for code, value in sums.items()}

        return self._memo(key, compute)

    def _brand_names(self) -> dict[str, str]:
        brands = self._table("brands", required=False)
        if brands is None or not {"brand_id", "brand_name"} <= set(brands.columns):
            return {}
        return dict(zip(brands["brand_id"].astype(str), brands["brand_name"].astype(str), strict=False))

    def _review_windows(self, rev: str, window_days: Any) -> tuple[Any, date, int, int]:
        """Revision, effective date, compared days per side and requested days (validated)."""
        actual = sorted(self._actual_revisions(), key=lambda r: r.effective_date or "")
        known = ", ".join(r.rev_id for r in actual) or "없음"
        if not self.ws.has_model(rev):
            raise NotFoundToolError(f"개정 {rev}가 없어요. 실제 개정: {known}")
        requested = _int(window_days, "window_days")
        if requested < MIN_REVIEW_DAYS:
            raise ToolError(
                f"window_days는 {MIN_REVIEW_DAYS}일 이상이어야 해요. 기간이 짧으면 요일·행사 영향이 너무 커져요."
            )
        revision = self._model(rev).revision
        if revision.kind != RevisionKind.ACTUAL or not revision.effective_date:
            raise ToolError(
                f"{rev}는 가상 개정이라 실제로 적용된 적이 없어 성과를 볼 수 없어요. 실제 개정({known})을 지정하세요."
            )
        if not revision.parent_rev_id:
            raise ToolError(f"{rev}는 첫 개정이라 비교할 이전 개정이 없어요.")
        if not self.ws.has_model(revision.parent_rev_id):
            raise ToolError(f"{rev}의 이전 개정 {revision.parent_rev_id} 모델이 없어요.")
        effective = date.fromisoformat(revision.effective_date)
        first, last = self._data_range()
        if effective > last:
            raise ToolError(
                f"{rev}는 {effective}부터 적용되는 개정이라 아직 적용 후 실적이 없어요(거래 데이터 {last}까지)."
            )
        earlier = [
            date.fromisoformat(r.effective_date) for r in actual if r.effective_date < revision.effective_date
        ]
        later = [
            date.fromisoformat(r.effective_date) for r in actual if r.effective_date > revision.effective_date
        ]
        lower = max([first, *earlier[-1:]])
        upper = min([last, *(day - timedelta(days=1) for day in later[:1])])
        available_before = max(0, (effective - lower).days)
        available_after = max(0, (upper - effective).days + 1)
        days = min(requested, available_before, available_after)
        if days < MIN_REVIEW_DAYS:
            raise ToolError(
                f"{rev} 적용일({effective}) 앞뒤로 비교할 실적이 {MIN_REVIEW_DAYS}일 이상씩 있어야 해요"
                f"(쓸 수 있는 기간: 적용 전 {available_before}일, 적용 후 {available_after}일)."
            )
        return revision, effective, days, requested

    def analytics_relayout_review(self, rev: str, window_days: int = 90) -> dict[str, Any]:
        """Performance of a past relayout (parent revision -> ``rev``) per changed unit.

        Compares each change's average daily sales (full-scale, net) in ``window_days`` before the effective date
        (parent layout) with the same number of days after it (``rev`` layout). Windows are clipped to the data and
        to the periods of the neighbouring actual revisions, and both sides keep the same length (at least 14
        days). The control is the same floor's unchanged units (store-wide when a floor has fewer than three);
        ``vs_control_pct`` = (change ratio / control ratio - 1) x 100. ``by_type``/``by_floor`` sum the items;
        ``total`` counts every unit once. Observational, not causal.
        """
        rev = str(rev or "").strip()
        if not rev:
            raise ToolError("성과를 볼 개정(rev)을 알려 주세요. 예: R2")
        revision, effective, days, requested = self._review_windows(rev, window_days)
        parent = str(revision.parent_rev_id)
        before = (effective - timedelta(days=days), effective - timedelta(days=1))
        after = (effective, effective + timedelta(days=days - 1))
        change_set, _source = self._change_set(parent, rev)
        old, new = _unit_index(self._model(parent)), _unit_index(self._model(rev))
        sales_before, sales_after = self._unit_sales(*before), self._unit_sales(*after)
        names = self._brand_names()
        unit_changes = [item for item in change_set.items if item.unit_codes_before or item.unit_codes_after]
        changed = {
            str(code) for item in unit_changes for code in (*item.unit_codes_before, *item.unit_codes_after)
        }
        groups = _control_groups(old, new, changed, sales_before, sales_after)
        order = {kind: index for index, kind in enumerate(TYPE_ORDER)}
        items = sorted(
            (_review_item(i, old, new, sales_before, sales_after, days, groups, names) for i in unit_changes),
            key=lambda item: (
                order.get(item["change_type"], len(order)),
                floor_sort_key(item["floor"]),
                item["unit_code"],
            ),
        )
        kinds = sorted(
            {item["change_type"] for item in items}, key=lambda kind: (order.get(kind, len(order)), kind)
        )
        by_type = [
            {
                "change_type": kind,
                "change_label": CHANGE_LABELS.get(kind, kind),
                **_review_group([item for item in items if item["change_type"] == kind]),
            }
            for kind in kinds
        ]
        by_floor = []
        for floor in sorted({item["floor"] for item in items}, key=floor_sort_key):
            group = groups.get(floor) or {}
            by_floor.append(
                {
                    "floor": floor,
                    **_review_group([item for item in items if item["floor"] == floor]),
                    "control_units": int(group.get("units", 0)),
                }
            )
        total = _review_total(items, old, sales_before, sales_after, days, groups)

        notes = []
        if days < requested:
            notes.append(
                f"데이터와 개정 적용 기간에 맞춰 요청한 {requested}일 대신 앞뒤 {days}일씩 비교했어요."
            )
        skipped = Counter(
            str(item.change_type)
            for item in change_set.items
            if not (item.unit_codes_before or item.unit_codes_after)
        )
        for kind, count in sorted(skipped.items()):
            notes.append(
                f"{CHANGE_LABELS.get(kind, kind)} {count}건은 매장 실적과 연결되지 않아 항목에서 뺐어요."
            )
        uses = Counter(code for item in items for code in {*item["units_before"], *item["units_after"]})
        if any(count > 1 for count in uses.values()):
            notes.append(
                "같은 매장이 여러 변경에 걸려 있어 유형별·층별 합계에는 중복으로 들어갔어요. 전체(total)는 한 번씩만 셌어요."
            )
        fallback = sorted(
            {item["floor"] for item in items if item["control_scope"] == "store"}, key=floor_sort_key
        )
        if fallback:
            notes.append(
                f"{', '.join(fallback)}는 같은 층 비교군이 {MIN_CONTROL_UNITS}곳보다 적어 점포 전체 비교군을 썼어요."
            )
        control_floors = sorted((key for key in groups if key is not None), key=floor_sort_key)
        store_group = groups.get(None) or {}
        result = {
            "rev": rev,
            "parent_rev": parent,
            "effective_date": effective.isoformat(),
            "label": "관측 비교",
            "window": {
                "before": {"start": before[0].isoformat(), "end": before[1].isoformat()},
                "after": {"start": after[0].isoformat(), "end": after[1].isoformat()},
                "days": days,
                "requested_days": requested,
            },
            "items": [{k: v for k, v in item.items() if not k.startswith("_")} for item in items],
            "by_type": by_type,
            "by_floor": by_floor,
            "total": total,
            "control": {
                "min_units": MIN_CONTROL_UNITS,
                "floors": [
                    {"floor": key, "units": groups[key]["units"], "change_pct": _pct(groups[key]["ratio"])}
                    for key in control_floors
                ],
                "store": {
                    "units": int(store_group.get("units", 0)),
                    "change_pct": _pct(store_group.get("ratio")),
                },
            },
            "summary": _review_summary(rev, parent, effective, days, total, items),
            "notes": notes,
            "caveat": REVIEW_CAVEAT,
            "caveats": list(REVIEW_CAVEATS),
            "report_url": f"/api/reports/relayout-review/{quote(rev, safe='')}?window_days={requested}",
        }
        return _json(result)

    def relayout_review_html(self, rev: str, window_days: int = 90) -> str:
        """Printable Korean HTML report of ``analytics_relayout_review``."""
        return render_relayout_review(self.analytics_relayout_review(rev, window_days))
