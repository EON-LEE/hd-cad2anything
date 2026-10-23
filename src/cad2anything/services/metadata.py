"""Metadata uploads (docs/specs/atlas-api.md §1.2) and the drawing-versus-lease check.

Tables (``brands``, ``tenancies``, ``leases``, ``pos_terminals`` and, for the multi-store comparison, ``stores``
and ``store_kpis``) arrive as CSV (UTF-8 or CP949) or XLSX (first sheet) and replace ``business/{name}.csv``
after validation; the previous file is kept under ``business/_backup/``. Relayout briefs and drawing profiles
arrive as YAML or JSON and are stored one file per ID. Any error rejects the upload and leaves the workspace
unchanged; warnings alone are accepted.

Store tables are normalized before they are stored: ``period`` becomes ``YYYY-MM``, ``floor`` a floor code
(``B1``, ``3F``), ``category`` a catalog code when the code or Korean name is known, and thousands separators
are removed from numbers. ``store_kpis`` rows must name a store of the ``stores`` table when that table exists.

``check_leases`` compares a model's units with the leases valid on a date (codes ``unit_missing_in_drawing``,
``brand_mismatch``, ``area_mismatch``, ``unit_without_lease``).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from cad2anything.cadcore.pipeline import normalize_brand
from cad2anything.cadcore.profile import (
    DEFAULT_PROFILE_ID,
    PROFILE_DIR,
    normalize_floor,
    profile_to_dict,
    validate_profile,
)
from cad2anything.catalog import CATEGORIES, FACILITY_CODE
from cad2anything.model import RevisionKind, SpatialModel
from cad2anything.services.errors import ToolError
from cad2anything.services.uploads import KST, remove_file

logger = logging.getLogger("cad2anything.uploads")

BACKUP_DIR = "business/_backup"
BRIEF_DIR = "business/relayout_briefs"
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
AREA_TOLERANCE_PCT = 3.0
LEASE_COLUMNS = ("lease_id", "brand_id", "unit_code", "lease_start", "lease_end")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

NumberRule = tuple[str, Callable[[float], bool], str]


@dataclass(frozen=True)
class TableSpec:
    required: tuple[str, ...]
    keys: tuple[tuple[str, ...], ...] = ()
    dates: tuple[tuple[str, str], ...] = ()
    numbers: tuple[NumberRule, ...] = ()
    optional: tuple[str, ...] = ()


_RATE: NumberRule = ("commission_rate", lambda v: 0.0 <= v <= 1.0, "0~1 사이의 소수여야 합니다(예: 0.27)")
TABLES: dict[str, TableSpec] = {
    "brands": TableSpec(
        required=("brand_id", "brand_name", "category"),
        keys=(("brand_id",),),
        numbers=(("attractiveness", lambda v: v > 0, "0보다 커야 합니다"),),
    ),
    "tenancies": TableSpec(
        required=("brand_id", "unit_code", "rev_id", "valid_from", "valid_to"),
        dates=(("valid_from", "valid_to"),),
        numbers=(_RATE,),
    ),
    "leases": TableSpec(
        required=LEASE_COLUMNS,
        keys=(("lease_id",),),
        dates=(("lease_start", "lease_end"),),
        numbers=(("contract_area_m2", lambda v: v > 0, "0보다 큰 면적(㎡)이어야 합니다"), _RATE),
        optional=("contract_area_m2", "commission_rate"),
    ),
    "pos_terminals": TableSpec(
        required=("pos_id", "brand_id", "unit_code", "valid_from", "valid_to"),
        # One terminal keeps its pos_id when its brand moves, so a pos_id has one row per validity period.
        keys=(("pos_id", "valid_from"),),
        dates=(("valid_from", "valid_to"),),
    ),
    "stores": TableSpec(
        required=("store_id", "store_name", "region", "format", "gross_area_m2"),
        keys=(("store_id",),),
        numbers=(("gross_area_m2", lambda v: v > 0, "0보다 큰 면적(㎡)이어야 합니다"),),
        optional=("opened",),
    ),
    "store_kpis": TableSpec(
        required=("store_id", "period", "floor", "category", "area_m2", "sales_krw"),
        keys=(("store_id", "period", "floor", "category"),),
        numbers=(
            ("area_m2", lambda v: v > 0, "0보다 큰 면적(㎡)이어야 합니다"),
            ("sales_krw", lambda v: v >= 0, "0 이상의 금액(원)이어야 합니다"),
        ),
    ),
}
DOCUMENTS: dict[str, tuple[str, ...]] = {
    "relayout_brief": ("brief_id", "title", "season", "year", "effective_date", "base_rev", "floors"),
    "drawing_profile": ("profile_id", "title"),
}
STORE_TABLES: tuple[str, ...] = ("stores", "store_kpis")
# Order of the API contract: the store tables were added after the documents.
KINDS: tuple[str, ...] = (*(k for k in TABLES if k not in STORE_TABLES), *DOCUMENTS, *STORE_TABLES)
REQUIRED_COLUMNS: dict[str, list[str]] = {
    kind: list(TABLES[kind].required if kind in TABLES else DOCUMENTS[kind]) for kind in KINDS
}
_PERIOD_PATTERNS = (
    re.compile(r"^(\d{4})(\d{2})$"),
    re.compile(r"^(\d{4})\s*[-./]\s*(\d{1,2})(?:\s*[-./]\s*(\d{1,2}))?$"),
    re.compile(r"^(\d{4})\s*년\s*(\d{1,2})\s*월$"),
)
_NAME_KEY_RE = re.compile(r"[\s·・ㆍ/]+")


@dataclass
class TableContext:
    """Reference data for warnings: known brand IDs, revisions and unit codes, and store IDs.

    ``store_ids`` holds the stores table's IDs (None when there is no stores table); ``kpi_store_ids`` holds
    the IDs used by the current store_kpis table (None when there is none)."""

    brand_ids: set[str] | None = None
    rev_ids: set[str] = field(default_factory=set)
    unit_codes: set[str] = field(default_factory=set)
    unit_codes_by_rev: dict[str, set[str]] = field(default_factory=dict)
    store_ids: set[str] | None = None
    kpi_store_ids: set[str] | None = None


def result(kind: str, rows: int = 0, columns: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": "rejected",
        "rows": rows,
        "columns": list(columns),
        "errors": [],
        "warnings": [],
        "backup": None,
        "id": None,
    }


def _rows(index: Iterable[Any], limit: int = 5) -> str:
    rows = [int(i) for i in index]
    text = ", ".join(f"{row}행" for row in rows[:limit])
    return text + (f" 외 {len(rows) - limit}건" if len(rows) > limit else "")


def _examples(values: Iterable[Any], limit: int = 5) -> str:
    items = sorted({str(v) for v in values})
    text = ", ".join(items[:limit])
    return text + (f" 외 {len(items) - limit}개" if len(items) > limit else "")


def _stamp() -> str:
    return datetime.now(KST).strftime("%Y%m%dT%H%M%S")


def _mtime_iso(paths: Iterable[Path]) -> str | None:
    stamps = [path.stat().st_mtime for path in paths if path.is_file()]
    return datetime.fromtimestamp(max(stamps), KST).isoformat(timespec="seconds") if stamps else None


# --- reading ----------------------------------------------------------------------------------------------


def _cell_text(value: Any) -> str:
    """Excel cell as text: dates as YYYY-MM-DD, integral floats without ``.0``, blanks as ``""``."""
    if value is None or (not isinstance(value, str | bool) and pd.isna(value)):
        return ""
    if isinstance(value, datetime):
        midnight = (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)
        return value.date().isoformat() if midnight else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_table(filename: str, data: bytes) -> tuple[pd.DataFrame | None, list[str]]:
    """All-text frame from a CSV (UTF-8, UTF-8 BOM or CP949) or the first XLSX sheet, plus errors."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".xls":
        return None, ["XLS(이전 엑셀 형식)는 받지 않습니다. XLSX나 CSV로 저장해 올려 주세요."]
    if suffix == ".xlsx":
        try:
            frame = pd.read_excel(io.BytesIO(data), sheet_name=0, dtype=object)
        except Exception:
            return None, ["XLSX 파일을 읽을 수 없습니다. 파일이 손상되지 않았는지 확인하세요."]
        return frame.map(_cell_text), []
    if suffix != ".csv":
        return None, ["표 메타데이터는 CSV나 XLSX 파일이어야 합니다."]
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False), []
        except pd.errors.EmptyDataError:
            return None, ["빈 파일입니다."]
        except pd.errors.ParserError:
            return None, ["CSV 형식이 올바르지 않습니다. 열 개수가 다른 행이 있는지 확인하세요."]
    return None, ["CSV 파일의 문자 인코딩을 알 수 없습니다. UTF-8로 저장해 올려 주세요."]


def read_document(filename: str, data: bytes) -> tuple[dict[str, Any] | None, list[str]]:
    """Mapping from a YAML (.yaml, .yml) or JSON file, plus errors."""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".yaml", ".yml", ".json"):
        return None, ["YAML(.yaml, .yml)이나 JSON(.json) 파일이어야 합니다."]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, ["UTF-8로 저장된 파일이어야 합니다."]
    try:
        document = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        return None, [f"JSON 형식이 올바르지 않습니다({exc.lineno}행 {exc.colno}열)."]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"({mark.line + 1}행 {mark.column + 1}열)" if mark is not None else ""
        return None, [f"YAML 형식이 올바르지 않습니다{where}."]
    if not isinstance(document, dict):
        return None, ["파일의 최상위는 키-값 객체여야 합니다."]
    return document, []


# --- table validation -------------------------------------------------------------------------------------


def _normalize(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Lower-case trimmed headers, trimmed text cells, blank rows dropped; index = spreadsheet row number."""
    frame = frame.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    unnamed = [c for c in frame.columns if c.startswith("unnamed:")]
    frame = frame.drop(columns=[c for c in unnamed if (frame[c].astype(str).str.strip() == "").all()])
    duplicated = sorted({c for c in frame.columns if list(frame.columns).count(c) > 1})
    if duplicated:
        return frame, [f"같은 이름의 열이 여러 개 있습니다: {', '.join(duplicated)}"]
    frame = frame.astype(str).apply(lambda column: column.str.strip())
    frame.index = range(2, len(frame) + 2)
    return frame[~(frame == "").all(axis=1)].copy(), []


def _dates(frame: pd.DataFrame, column: str, errors: list[str]) -> pd.Series:
    values = frame[column]
    parsed = pd.to_datetime(values.where(values != ""), format="%Y-%m-%d", errors="coerce")
    bad = frame.index[(values != "") & parsed.isna()]
    if len(bad):
        errors.append(f"{column}는 YYYY-MM-DD 형식의 날짜여야 합니다: {_rows(bad)}")
    frame.loc[parsed.notna(), column] = parsed[parsed.notna()].dt.strftime("%Y-%m-%d")
    return parsed


def _numbers(frame: pd.DataFrame, rule: NumberRule, errors: list[str]) -> None:
    column, check, hint = rule
    values = frame[column]
    numbers = pd.to_numeric(values.where(values != ""), errors="coerce")
    bad = frame.index[(values != "") & numbers.isna()]
    if len(bad):
        errors.append(f"{column}는 숫자여야 합니다: {_rows(bad)}")
    valid = numbers.dropna()
    outside = valid.index[~valid.map(lambda v: math.isfinite(v) and check(float(v)))]
    if len(outside):
        errors.append(f"{column}는 {hint}: {_rows(outside)}")


def _overlaps(frame: pd.DataFrame, group: str, start: str, end: str) -> pd.DataFrame:
    """Rows whose validity period starts on or before the latest end of an earlier period of the same group."""
    ordered = frame.sort_values([group, start], kind="stable")
    keys = ordered[group]
    starts = pd.to_datetime(ordered[start], errors="coerce")
    latest = pd.to_datetime(ordered[end], errors="coerce").groupby(keys).cummax().groupby(keys).shift()
    return ordered[starts <= latest]


def _reference_warnings(kind: str, frame: pd.DataFrame, context: TableContext, warnings: list[str]) -> None:
    if context.brand_ids is not None and "brand_id" in frame and kind != "brands":
        unknown = frame.loc[
            ~frame["brand_id"].isin(context.brand_ids) & (frame["brand_id"] != ""), "brand_id"
        ]
        if len(unknown):
            warnings.append(f"brands 표에 없는 brand_id가 {len(unknown)}행 있습니다: {_examples(unknown)}")
    if "unit_code" in frame and context.unit_codes:
        if kind == "tenancies":
            by_rev = context.unit_codes_by_rev
            pairs = zip(frame["rev_id"], frame["unit_code"], strict=True)
            known = pd.Series(
                [code in by_rev.get(rev, context.unit_codes) for rev, code in pairs], index=frame.index
            )
        else:
            known = frame["unit_code"].isin(context.unit_codes)
        unknown = frame.loc[~known & (frame["unit_code"] != ""), "unit_code"]
        if len(unknown):
            warnings.append(f"도면 모델에 없는 매장 코드가 {len(unknown)}행 있습니다: {_examples(unknown)}")
    if kind == "tenancies" and context.rev_ids:
        unknown = frame.loc[~frame["rev_id"].isin(context.rev_ids) & (frame["rev_id"] != ""), "rev_id"]
        if len(unknown):
            warnings.append(f"없는 개정을 가리키는 rev_id가 {len(unknown)}행 있습니다: {_examples(unknown)}")


def normalize_period(value: Any) -> str | None:
    """``YYYY-MM`` from ``2025-03``, ``2025-3``, ``2025/03``, ``202503``, ``2025년 3월`` or a date; None otherwise."""
    text = str(value or "").strip()
    for pattern in _PERIOD_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        day = match.group(3) if match.lastindex and match.lastindex >= 3 else None
        if not (1900 <= year <= 2100 and 1 <= month <= 12):
            return None
        if day is not None:
            try:
                date(year, month, int(day))
            except ValueError:
                return None
        return f"{year:04d}-{month:02d}"
    return None


def normalize_category(value: Any) -> str:
    """Catalog code from a code (any case) or a Korean name (spaces and ``·`` ignored); else the trimmed text."""
    text = str(value or "").strip()
    if text.upper() in CATEGORIES:
        return text.upper()
    key = _NAME_KEY_RE.sub("", text)
    for code, category in CATEGORIES.items():
        if key and _NAME_KEY_RE.sub("", category.name_ko) == key:
            return code
    return text


def _plain_numbers(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    """Drop thousands separators (``1,234``) that spreadsheet exports add to numbers."""
    for column in columns:
        frame[column] = frame[column].str.replace(",", "", regex=False).str.strip()


def _normalize_store_table(kind: str, frame: pd.DataFrame, errors: list[str], warnings: list[str]) -> None:
    """Normalize periods, floors and categories in place (before duplicate keys are checked)."""
    flagship = frame.index[frame["store_id"] == FACILITY_CODE]
    if kind == "stores":
        _plain_numbers(frame, ["gross_area_m2"])
        if "opened" in frame:
            opened = frame["opened"]
            parsed = pd.to_datetime(opened.where(opened != ""), format="%Y-%m-%d", errors="coerce")
            bad = frame.index[(opened != "") & parsed.isna()]
            if len(bad):
                warnings.append(f"opened는 YYYY-MM-DD 형식의 날짜가 아닙니다(정보로만 씁니다): {_rows(bad)}")
        names = frame.loc[frame["store_name"] != ""].groupby("store_name")["store_id"].nunique()
        if (names > 1).any():
            warnings.append(f"점포명이 같은 store_id가 여러 개 있습니다: {_examples(names[names > 1].index)}")
        if len(flagship):
            warnings.append(
                f"store_id {FACILITY_CODE}는 대상 점포입니다. 이 행은 점포 정보(지역·형태)로만 쓰고, "
                "실적은 거래 데이터로 계산합니다."
            )
        return
    _plain_numbers(frame, ["area_m2", "sales_krw"])
    periods = frame["period"].map(normalize_period)
    bad = frame.index[(frame["period"] != "") & periods.isna()]
    if len(bad):
        errors.append(f"period는 YYYY-MM 형식의 월이어야 합니다(예: 2026-03): {_rows(bad)}")
    frame.loc[periods.notna(), "period"] = periods[periods.notna()]
    floors = frame["floor"].map(lambda v: normalize_floor(v, allow_bare=True) if v else None)
    bad = frame.index[(frame["floor"] != "") & floors.isna()]
    if len(bad):
        errors.append(f"floor는 층 코드여야 합니다(예: B1, 1F, 지하1층, 3층): {_rows(bad)}")
    frame.loc[floors.notna(), "floor"] = floors[floors.notna()]
    frame["category"] = frame["category"].map(normalize_category)
    unknown = frame.loc[~frame["category"].isin(CATEGORIES) & (frame["category"] != ""), "category"]
    if len(unknown):
        warnings.append(
            f"카탈로그에 없는 카테고리가 있습니다: {_examples(unknown)}. 비교에서 이름 없이 표시됩니다."
        )
    if len(flagship):
        warnings.append(
            f"store_id {FACILITY_CODE} 행 {len(flagship)}개는 비교에 쓰지 않습니다. 대상 점포 실적은 거래 데이터로 계산합니다."
        )


def _store_references(
    kind: str, frame: pd.DataFrame, context: TableContext, errors: list[str], warnings: list[str]
) -> None:
    ids = frame["store_id"]
    if kind == "store_kpis":
        if context.store_ids is None:
            warnings.append(
                "stores 표가 없어 store_id를 확인하지 못했습니다. 점포 이름·지역·형태를 보이려면 stores도 올리세요."
            )
            return
        unknown = ids[~ids.isin(context.store_ids | {FACILITY_CODE}) & (ids != "")]
        if len(unknown):
            errors.append(
                f"stores 표에 없는 store_id가 {len(unknown)}행 있습니다: {_examples(unknown)}. "
                "stores를 먼저 올리거나 store_id를 고치세요."
            )
    elif kind == "stores" and context.kpi_store_ids:
        orphaned = context.kpi_store_ids - set(ids) - {FACILITY_CODE}
        if orphaned:
            warnings.append(
                f"store_kpis에 실적이 있는 점포가 새 stores 표에 없습니다: {_examples(orphaned)}. "
                "비교 결과에 이름 없이 표시됩니다."
            )


def validate_table(
    kind: str, frame: pd.DataFrame, context: TableContext | None = None
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Normalized frame (dates as YYYY-MM-DD) plus errors and warnings for one table kind."""
    spec = TABLES[kind]
    context = context or TableContext()
    frame, errors = _normalize(frame)
    if errors:
        return frame, errors, []
    missing = [column for column in spec.required if column not in frame.columns]
    if missing:
        return frame, [f"필수 열이 없습니다: {', '.join(missing)}. 필요한 열: {', '.join(spec.required)}"], []
    if frame.empty:
        return frame, ["데이터 행이 없습니다."], []
    warnings: list[str] = []
    for column in spec.required:
        blank = frame.index[frame[column] == ""]
        if len(blank):
            errors.append(f"{column} 값이 비어 있습니다: {_rows(blank)}")
    if kind in STORE_TABLES:
        _normalize_store_table(kind, frame, errors, warnings)
    for key in spec.keys:
        filled = frame[(frame[list(key)] != "").all(axis=1)]
        duplicated = filled[filled.duplicated(list(key), keep=False)]
        if len(duplicated):
            values = duplicated[list(key)].astype(str).agg("/".join, axis=1)
            errors.append(f"{'+'.join(key)} 값이 중복됩니다: {_examples(values)} ({_rows(duplicated.index)})")
    for start, end in spec.dates:
        before = len(errors)
        first, last = _dates(frame, start, errors), _dates(frame, end, errors)
        if len(errors) == before:
            reversed_rows = frame.index[first.notna() & last.notna() & (last < first)]
            if len(reversed_rows):
                errors.append(f"{end}가 {start}보다 앞선 행이 있습니다: {_rows(reversed_rows)}")
    for rule in spec.numbers:
        if rule[0] in frame.columns:
            _numbers(frame, rule, errors)
    if kind == "brands":
        unknown = frame.loc[~frame["category"].isin(CATEGORIES) & (frame["category"] != ""), "category"]
        if len(unknown):
            warnings.append(
                f"카탈로그에 없는 카테고리가 있습니다: {_examples(unknown)}. 분석에서 이름 없이 표시됩니다."
            )
        names = (
            frame.assign(key=frame["brand_name"].map(normalize_brand)).groupby("key")["brand_id"].nunique()
        )
        if (names > 1).any():
            warnings.append(
                f"브랜드명이 같은 brand_id가 여러 개 있습니다(도면 라벨로 구분할 수 없음): {_examples(names[names > 1].index)}"
            )
    if kind == "pos_terminals" and not errors:
        overlaps = _overlaps(frame, "pos_id", "valid_from", "valid_to")
        if len(overlaps):
            errors.append(
                f"같은 pos_id의 유효 기간이 겹칩니다: {_examples(overlaps['pos_id'])} ({_rows(overlaps.index)})"
            )
    if kind == "leases" and not errors:
        overlaps = _overlaps(frame, "unit_code", "lease_start", "lease_end")
        if len(overlaps):
            warnings.append(f"같은 매장에 기간이 겹치는 계약이 있습니다: {_examples(overlaps['unit_code'])}")
    if kind in STORE_TABLES:
        _store_references(kind, frame, context, errors, warnings)
    _reference_warnings(kind, frame, context, warnings)
    return frame, errors, warnings


def _id_set(ws: Any, name: str, column: str) -> set[str] | None:
    """IDs of a business table read as text (``007`` stays ``007``); None without a readable column."""
    path = ws.table_path(name)
    if path is None:
        return None
    try:
        values = pd.read_csv(path, dtype=str, keep_default_na=False, usecols=[column])[column]
    except (OSError, ValueError, pd.errors.ParserError):
        return None
    return {value.strip() for value in values if value.strip()}


def table_context(host: Any, kind: str) -> TableContext:
    """Brand IDs (brands table), revisions and the unit codes of every actual revision (cached models).

    Store tables only need the other store table's IDs, so no model is loaded for them."""
    ws = host.ws
    context = TableContext()
    if kind == "brands":
        return context
    if kind in STORE_TABLES:
        if kind == "store_kpis":
            context.store_ids = _id_set(ws, "stores", "store_id")
        else:
            context.kpi_store_ids = _id_set(ws, "store_kpis", "store_id")
        return context
    if ws.has_table("brands"):
        context.brand_ids = set(ws.table("brands")["brand_id"].astype(str))
    for path in sorted((ws.root / "models").glob("*.json")):
        try:
            model = ws.model(path.stem)
        except (KeyError, OSError, ValueError):
            continue
        context.rev_ids.add(path.stem)
        if model.revision.kind == RevisionKind.ACTUAL:
            codes = {str(u.attributes.get("unit_code")) for u in model.units()}
            context.unit_codes_by_rev[path.stem] = codes
            context.unit_codes |= codes
    return context


# --- storing ----------------------------------------------------------------------------------------------


def _backup(ws: Any, source: Path, rel_dir: str, stem: str, suffix: str) -> str:
    data = source.read_bytes()
    if source.name.endswith(".gz"):
        data = gzip.decompress(data)
    stamp, n = _stamp(), 2
    rel = f"{rel_dir}/{stem}.{stamp}{suffix}"
    while (ws.root / rel).exists():
        rel, n = f"{rel_dir}/{stem}.{stamp}-{n}{suffix}", n + 1
    ws.write_bytes(rel, data)
    return rel


def write_table(ws: Any, name: str, frame: pd.DataFrame) -> str | None:
    """Replace ``business/{name}.csv`` (backing up the previous ``.csv`` or ``.csv.gz``); returns the backup."""
    business = ws.root / "business"
    previous = next(
        (business / f"{name}{s}" for s in (".csv", ".csv.gz") if (business / f"{name}{s}").is_file()), None
    )
    backup = _backup(ws, previous, BACKUP_DIR, name, ".csv") if previous is not None else None
    ws.write_text(f"business/{name}.csv", frame.to_csv(index=False, lineterminator="\n"))
    if (business / f"{name}.csv.gz").is_file():
        remove_file(ws, f"business/{name}.csv.gz")
    return backup


def _write_document(ws: Any, rel_dir: str, item_id: str, document: Mapping[str, Any]) -> str | None:
    path = ws.root / rel_dir / f"{item_id}.yaml"
    backup = None
    if path.is_file():
        backup = _backup(ws, path, f"{BACKUP_DIR}/{Path(rel_dir).name}", item_id, ".yaml")
    plain = json.loads(json.dumps(dict(document), ensure_ascii=False, default=str))
    ws.write_text(f"{rel_dir}/{item_id}.yaml", yaml.safe_dump(plain, allow_unicode=True, sort_keys=False))
    return backup


# --- uploads ----------------------------------------------------------------------------------------------


def _upload_table(host: Any, kind: str, filename: str, data: bytes) -> dict[str, Any]:
    outcome = result(kind)
    frame, errors = read_table(filename, data)
    if frame is None:
        outcome["errors"] = errors
        return outcome
    frame, errors, warnings = validate_table(kind, frame, table_context(host, kind))
    outcome.update(rows=len(frame), columns=list(frame.columns), errors=errors[:20], warnings=warnings[:20])
    if errors:
        return outcome
    outcome.update(status="accepted", backup=write_table(host.ws, kind, frame))
    return outcome


def _upload_brief(host: Any, filename: str, data: bytes) -> dict[str, Any]:
    outcome = result("relayout_brief")
    document, errors = read_document(filename, data)
    if document is None:
        outcome["errors"] = errors
        return outcome
    outcome.update(rows=1, columns=list(document), id=str(document.get("brief_id") or "") or None)
    validator = getattr(host, "relayout_validate_brief", None)
    if not callable(validator):
        outcome["errors"] = ["개편 브리프 검증기를 사용할 수 없습니다."]
        return outcome
    try:
        brief, errors, warnings = validator(document)
    except ToolError as exc:
        brief, errors, warnings = {}, [str(exc)], []
    except Exception:
        logger.exception("relayout brief validation failed")
        brief, errors, warnings = {}, ["개편 브리프를 검증하지 못했습니다."], []
    brief_id = str((brief or {}).get("brief_id") or document.get("brief_id") or "").strip()
    errors = list(errors)
    if not errors and not SAFE_ID_RE.match(brief_id):
        errors.append("brief_id는 영문·숫자·-·_로 1~40자여야 합니다.")
    outcome.update(errors=errors, warnings=list(warnings), id=brief_id or None)
    if errors:
        return outcome
    outcome.update(status="accepted", backup=_write_document(host.ws, BRIEF_DIR, brief_id, brief))
    return outcome


def _upload_profile(host: Any, filename: str, data: bytes) -> dict[str, Any]:
    outcome = result("drawing_profile")
    document, errors = read_document(filename, data)
    if document is None:
        outcome["errors"] = errors
        return outcome
    profile, errors, warnings = validate_profile(document)
    profile_id = profile.profile_id if profile else str(document.get("profile_id") or "").strip()
    if profile_id == DEFAULT_PROFILE_ID:
        errors = [
            *errors,
            "default 프로필은 기본 제공 프로필이라 바꿀 수 없습니다. 다른 profile_id를 쓰세요.",
        ]
    outcome.update(rows=1, columns=list(document), errors=errors, warnings=warnings, id=profile_id or None)
    if errors or profile is None:
        return outcome
    backup = _write_document(host.ws, PROFILE_DIR, profile_id, profile_to_dict(profile))
    outcome.update(status="accepted", backup=backup)
    return outcome


def upload_metadata(host: Any, kind: str, filename: str, data: bytes) -> dict[str, Any]:
    """Validate and store one metadata file; ``kind`` must be one of ``KINDS`` (checked by the caller)."""
    if kind in TABLES:
        return _upload_table(host, kind, filename, data)
    if kind == "relayout_brief":
        return _upload_brief(host, filename, data)
    return _upload_profile(host, filename, data)


def metadata_status(ws: Any) -> list[dict[str, Any]]:
    """Per kind (in ``KINDS`` order): ``{kind, present, rows, updated_at, required_columns, items}``."""
    status: dict[str, dict[str, Any]] = {}
    business = ws.root / "business"
    for kind, spec in TABLES.items():
        path = next(
            (business / f"{kind}{s}" for s in (".csv", ".csv.gz") if (business / f"{kind}{s}").is_file()),
            None,
        )
        status[kind] = {
            "kind": kind,
            "present": path is not None,
            "rows": len(ws.table(kind)) if path is not None else 0,
            "updated_at": _mtime_iso([path]) if path is not None else None,
            "required_columns": list(spec.required),
            "items": [],
        }
    briefs = sorted((ws.root / BRIEF_DIR).glob("*.yaml"))
    status["relayout_brief"] = {
        "kind": "relayout_brief",
        "present": bool(briefs),
        "rows": len(briefs),
        "updated_at": _mtime_iso(briefs),
        "required_columns": list(DOCUMENTS["relayout_brief"]),
        "items": [path.stem for path in briefs],
    }
    profiles = [p for p in sorted((ws.root / PROFILE_DIR).glob("*.yaml")) if p.stem != DEFAULT_PROFILE_ID]
    status["drawing_profile"] = {
        "kind": "drawing_profile",
        "present": True,
        "rows": 1 + len(profiles),
        "updated_at": _mtime_iso(profiles),
        "required_columns": list(DOCUMENTS["drawing_profile"]),
        "items": [DEFAULT_PROFILE_ID, *(path.stem for path in profiles)],
    }
    return [status[kind] for kind in KINDS]


# --- drawing versus leases --------------------------------------------------------------------------------


def _same_brand(lease: Mapping[str, Any], attrs: Mapping[str, Any], names: Mapping[str, str]) -> bool:
    lease_id = str(lease.get("brand_id") or "").strip()
    drawing_id = str(attrs.get("brand_id") or "").strip()
    if lease_id and drawing_id:
        return lease_id == drawing_id
    lease_name = names.get(lease_id) or str(lease.get("brand_name") or "")
    drawing_name = str(attrs.get("brand_name") or "")
    return bool(lease_name and drawing_name) and normalize_brand(lease_name) == normalize_brand(drawing_name)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _natural(code: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", code)]


def check_leases(
    model: SpatialModel,
    effective_date: str | date,
    leases: pd.DataFrame,
    brands: pd.DataFrame | None = None,
    floors: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Drawing units versus the leases valid on ``effective_date`` (``lease_start <= date <= lease_end``)."""
    day = pd.Timestamp(str(effective_date)[:10])
    frame = leases.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    missing = [column for column in LEASE_COLUMNS if column not in frame.columns]
    if missing:
        return {"skipped": True, "message": f"leases 표에 필요한 열이 없습니다: {', '.join(missing)}"}
    start = pd.to_datetime(frame["lease_start"], errors="coerce")
    end = pd.to_datetime(frame["lease_end"], errors="coerce")
    valid = frame[(start <= day) & (end >= day)].to_dict("records")
    checked = list(floors) if floors else [f.code for f in model.floors]
    units = {str(u.attributes.get("unit_code")): u for u in model.units()}
    names: dict[str, str] = {}
    if brands is not None and {"brand_id", "brand_name"} <= set(brands.columns):
        names = dict(zip(brands["brand_id"].astype(str), brands["brand_name"].astype(str), strict=False))
    by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lease in valid:
        code = str(lease.get("unit_code") or "").strip()
        floor = units[code].floor if code in units else normalize_floor(code.split("-")[0])
        if floor in checked:
            by_unit[code].append(lease)
    issues: list[dict[str, Any]] = []

    def add(severity: str, code: str, unit_code: str, message: str, **extra: Any) -> None:
        floor = units[unit_code].floor if unit_code in units else normalize_floor(unit_code.split("-")[0])
        issues.append(
            {
                "severity": severity,
                "code": code,
                "unit_code": unit_code,
                "floor": floor,
                "message": message,
                **extra,
            }
        )

    for unit_code, unit_leases in by_unit.items():
        lease = unit_leases[0]
        lease_brand = names.get(str(lease.get("brand_id"))) or str(lease.get("brand_id"))
        unit = units.get(unit_code)
        if unit is None:
            add(
                "warning",
                "unit_missing_in_drawing",
                unit_code,
                f"계약 {lease.get('lease_id')}({lease_brand})의 매장 {unit_code}가 도면에 없습니다.",
                lease_id=lease.get("lease_id"),
                brand_id=lease.get("brand_id"),
            )
            continue
        attrs = unit.attributes
        matched = [item for item in unit_leases if _same_brand(item, attrs, names)]
        if not matched:
            vacant = bool(attrs.get("vacant")) or not (attrs.get("brand_id") or attrs.get("brand_name"))
            drawing_brand = attrs.get("brand_name") or attrs.get("brand_id")
            message = (
                f"도면은 공실이지만 계약 브랜드는 {lease_brand}입니다."
                if vacant
                else f"도면 브랜드 {drawing_brand}와 계약 브랜드 {lease_brand}가 다릅니다."
            )
            add(
                "warning",
                "brand_mismatch",
                unit_code,
                message,
                lease_id=lease.get("lease_id"),
                brand_id=lease.get("brand_id"),
                drawing_brand_id=attrs.get("brand_id"),
            )
        contract = _number((matched or unit_leases)[0].get("contract_area_m2"))
        if contract and contract > 0:
            drawing = float(unit.geometry.area)
            delta_pct = abs(contract - drawing) / contract * 100
            if delta_pct > AREA_TOLERANCE_PCT:
                add(
                    "warning",
                    "area_mismatch",
                    unit_code,
                    f"계약 면적 {contract:.1f}㎡와 도면 면적 {drawing:.1f}㎡가 {delta_pct:.1f}% 다릅니다.",
                    lease_id=(matched or unit_leases)[0].get("lease_id"),
                    contract_area_m2=round(contract, 2),
                    drawing_area_m2=round(drawing, 2),
                    delta_pct=round(delta_pct, 1),
                )
    for unit_code, unit in units.items():
        attrs = unit.attributes
        occupied = not attrs.get("vacant") and (attrs.get("brand_id") or attrs.get("brand_name"))
        if unit.floor in checked and occupied and unit_code not in by_unit:
            brand = attrs.get("brand_name") or attrs.get("brand_id")
            add(
                "info",
                "unit_without_lease",
                unit_code,
                f"{unit_code} {brand}: 적용일({day.date()})에 유효한 계약이 없습니다. 신규·갱신 계약을 등록해야 합니다.",
                brand_id=attrs.get("brand_id"),
            )
    issues.sort(
        key=lambda item: (SEVERITY_ORDER.get(item["severity"], 9), _natural(item["unit_code"]), item["code"])
    )
    counts = Counter(item["severity"] for item in issues)
    return {
        "effective_date": day.date().isoformat(),
        "floors": checked,
        "leases_checked": sum(len(items) for items in by_unit.values()),
        "units_checked": sum(1 for unit in units.values() if unit.floor in checked),
        "errors": counts.get("error", 0),
        "warnings": counts.get("warning", 0),
        "infos": counts.get("info", 0),
        "by_code": dict(sorted(Counter(item["code"] for item in issues).items())),
        "total": len(issues),
        "issues": issues[:50],
        "truncated": len(issues) > 50,
    }
