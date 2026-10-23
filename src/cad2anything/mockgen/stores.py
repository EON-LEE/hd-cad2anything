"""Synthetic peer stores for the multi-store comparison (다점포 비교).

The spatial model covers one facility, so peer stores reach Atlas as human or BI metadata: ``business/stores.csv``
(store_id, store_name, region, format, gross_area_m2, opened) and ``business/store_kpis.csv`` (store_id, period,
floor, category, area_m2, sales_krw per month x floor x category). This module writes a deterministic, fully
fictional version calibrated on the flagship's own data (``services.benchmark_tools.flagship_monthly``):

- program: the flagship's floor x category pairs with occupied area and sales in its latest 12 complete months;
- area: the flagship's average occupied area of the pair times the store's size, its category space allocation
  and a deterministic noise, constant over time;
- sales: area x the flagship's annual sales per m2 of the pair x store and category productivity multipliers,
  spread over the flagship's complete months with the flagship's month pattern per category (seasonality), a
  growth trend relative to the flagship and a deterministic monthly noise;
- formats: 가상 중형점 has no 8F; 가상 아울렛점 carries no luxury or jewelry and has no 7F~8F.

Planted signals (``PLANTED``, checked by tests): the flagship's 4F (women's casual and lingerie, its weak east
band) is below the peer median, only 가상 도심점 beats the flagship in cosmetics per m2, and 가상 역사점 leads the
B1 food hall. The flagship itself (``FLAGSHIP``) is not in these tables: its KPIs come from its transactions.

Usage:
    uv run --no-sync python -m cad2anything.mockgen.stores --root data/workspace --samples data/samples/metadata
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pandas as pd

from cad2anything.analytics.prep import floor_sort_key
from cad2anything.catalog import BUILDING_DEPTH_M, BUILDING_WIDTH_M, CATEGORIES
from cad2anything.model import EntityKind
from cad2anything.workspace import Workspace

TABLES = ("stores", "store_kpis")
STORE_COLUMNS = ("store_id", "store_name", "region", "format", "gross_area_m2", "opened")
KPI_COLUMNS = ("store_id", "period", "floor", "category", "area_m2", "sales_krw")
RECENT_MONTHS = 12
AREA_NOISE = 0.08
PRODUCTIVITY_NOISE = 0.05
MONTH_NOISE = 0.04
GROSS_NOISE = 0.03
SEASON_CLIP = (0.25, 4.0)
SAMPLE_LIMIT_BYTES = 300_000
B1_FOOD = ("FOOD_FRESH", "FOOD_DELI", "BAKERY", "WINE", "FOOD_COURT")
# Every peer sells women's casual and lingerie better per m2 than the flagship's weak 4F.
STRONG_4F = {"WOMEN_CASUAL": 1.5, "LINGERIE": 1.5}
PLANTED = (
    "4F(여성 캐주얼·란제리) ㎡당 매출은 다른 점포 중앙값이 대상 점포보다 높아요(대상 점포 4F 동측 저효율 밴드).",
    "화장품 ㎡당 매출은 가상 도심점만 대상 점포보다 높아요.",
    "B1 식품관 ㎡당 매출은 가상 역사점이 가장 높아요.",
    "가상 중형점은 8F가 없고, 가상 아울렛점은 명품·주얼리와 7F~8F가 없어요.",
)


@dataclass(frozen=True)
class PeerSpec:
    """A fictional peer store relative to the flagship (1.0 = the flagship's value)."""

    store_id: str
    store_name: str
    region: str
    format: str
    opened: str
    area_scale: float
    productivity: float
    growth: float
    floors: tuple[str, ...] | None = None
    exclude: tuple[str, ...] = ()
    category_productivity: Mapping[str, float] = field(default_factory=dict)
    category_area: Mapping[str, float] = field(default_factory=dict)


PEERS: tuple[PeerSpec, ...] = (
    PeerSpec(
        "S01",
        "가상 도심점",
        "수도권 도심",
        "백화점(대형)",
        "1998-09-01",
        area_scale=1.12,
        productivity=1.10,
        growth=0.02,
        category_productivity={
            "LUXURY": 1.25,
            "JEWELRY_WATCH": 1.2,
            "COSMETICS": 1.15,
            "GLOBAL_FASHION": 1.15,
            **STRONG_4F,
        },
        category_area={"LUXURY": 1.3, "GLOBAL_FASHION": 1.15},
    ),
    PeerSpec(
        "S02",
        "가상 신도시점",
        "수도권 신도시",
        "백화점",
        "2012-03-01",
        area_scale=0.92,
        productivity=0.85,
        growth=0.05,
        category_productivity={
            "LUXURY": 0.7,
            "COSMETICS": 0.85,
            "KIDS": 1.35,
            "LIVING": 1.3,
            "SPORTS": 1.15,
            **STRONG_4F,
        },
        category_area={"KIDS": 1.3, "LIVING": 1.2, "LUXURY": 0.7},
    ),
    PeerSpec(
        "S03",
        "가상 역사점",
        "광역시 도심",
        "백화점(역사 연결)",
        "2009-12-01",
        area_scale=0.85,
        productivity=0.95,
        growth=0.01,
        category_productivity={**dict.fromkeys(B1_FOOD, 1.35), "CAFE": 1.2, "COSMETICS": 0.92, **STRONG_4F},
        category_area=dict.fromkeys(B1_FOOD, 1.2),
    ),
    PeerSpec(
        "S04",
        "가상 지역거점점",
        "지방 거점도시",
        "백화점",
        "2003-08-01",
        area_scale=0.85,
        productivity=0.70,
        growth=-0.02,
        category_productivity={
            "LUXURY": 0.6,
            "JEWELRY_WATCH": 0.65,
            "OUTDOOR": 1.25,
            "MEN_FORMAL": 1.1,
            **STRONG_4F,
        },
        category_area={"OUTDOOR": 1.2, "LUXURY": 0.6},
    ),
    PeerSpec(
        "S05",
        "가상 중형점",
        "수도권 외곽",
        "백화점(중형)",
        "2016-06-01",
        area_scale=0.62,
        productivity=0.80,
        growth=0.0,
        floors=("B1", "1F", "2F", "3F", "4F", "5F", "6F", "7F"),
        category_productivity={"LUXURY": 0.7, **STRONG_4F},
    ),
    PeerSpec(
        "S06",
        "가상 아울렛점",
        "수도권 외곽",
        "아울렛",
        "2019-11-01",
        area_scale=0.80,
        productivity=0.55,
        growth=0.06,
        floors=("B1", "1F", "2F", "3F", "4F", "5F", "6F"),
        exclude=("LUXURY", "JEWELRY_WATCH"),
        category_productivity={"SPORTS": 1.3, "OUTDOOR": 1.4, "GLOBAL_FASHION": 0.9, **STRONG_4F},
        category_area={"SPORTS": 1.4, "OUTDOOR": 1.4, "GLOBAL_FASHION": 1.2},
    ),
)
_NORMAL = NormalDist()


def _noise(sigma: float, *parts: Any) -> float:
    """Deterministic lognormal factor ``exp(sigma * z)``; ``z ~ N(0, 1)`` comes from a SHA-256 of ``parts``."""
    digest = hashlib.sha256("|".join(("stores", *(str(part) for part in parts))).encode()).digest()
    uniform = (int.from_bytes(digest[:7], "big") + 0.5) / float(1 << 56)
    return math.exp(sigma * _NORMAL.inv_cdf(uniform))


@dataclass
class Baseline:
    """The flagship facts the peers are calibrated on."""

    months: list[str]
    program: pd.DataFrame
    season: dict[tuple[str, str], float]
    gross_area_m2: float


def flagship_baseline(monthly: pd.DataFrame, gross_area_m2: float) -> Baseline:
    """Program (floor, category, monthly average area, annual sales per m2 in the latest 12 complete months) and
    the month pattern per category (mean of the latest 12 months = 1) from ``flagship_monthly``."""
    complete = monthly[monthly["complete"].astype(bool) & monthly["category"].isin(CATEGORIES)]
    months = sorted(set(complete["month"]))
    if len(months) < RECENT_MONTHS:
        raise ValueError(f"needs {RECENT_MONTHS} complete months of flagship data, got {len(months)}")
    recent = months[-RECENT_MONTHS:]
    window = complete[complete["month"].isin(recent)]
    pairs = window.groupby(["floor", "category"], as_index=False)[["sales_krw", "area_m2"]].sum()
    pairs = pairs[(pairs["sales_krw"] > 0) & (pairs["area_m2"] > 0)]
    program = pd.DataFrame(
        {
            "floor": pairs["floor"].astype(str),
            "category": pairs["category"].astype(str),
            "area_m2": pairs["area_m2"] / RECENT_MONTHS,
            "sales_per_m2": pairs["sales_krw"] * RECENT_MONTHS / pairs["area_m2"],
        }
    )
    program = program.assign(_order=program["floor"].map(lambda code: floor_sort_key(code)[0]))
    program = (
        program.sort_values(["_order", "floor", "category"]).drop(columns="_order").reset_index(drop=True)
    )
    totals = complete.groupby(["category", "month"])["sales_krw"].sum()
    season: dict[tuple[str, str], float] = {}
    for category in sorted(set(program["category"])):
        values = (
            totals.loc[category] if category in totals.index.get_level_values(0) else pd.Series(dtype=float)
        )
        base = float(values.reindex(recent).fillna(0.0).mean())
        if base <= 0:
            continue
        for month in months:
            factor = float(values.get(month, 0.0)) / base
            season[(month, category)] = min(max(factor, SEASON_CLIP[0]), SEASON_CLIP[1])
    return Baseline(months=months, program=program, season=season, gross_area_m2=float(gross_area_m2))


def build_store_benchmarks(
    baseline: Baseline, peers: Sequence[PeerSpec] = PEERS
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``stores`` and ``store_kpis`` frames for ``peers`` (deterministic for the same baseline)."""
    months = baseline.months
    center = len(months) - 1 - (RECENT_MONTHS - 1) / 2
    floors = sorted(set(baseline.program["floor"]), key=floor_sort_key)
    stores, kpis = [], []
    for spec in peers:
        carried = [floor for floor in floors if spec.floors is None or floor in spec.floors]
        gross = baseline.gross_area_m2 * spec.area_scale * len(carried) / max(1, len(floors))
        stores.append(
            {
                "store_id": spec.store_id,
                "store_name": spec.store_name,
                "region": spec.region,
                "format": spec.format,
                "gross_area_m2": int(round(gross * _noise(GROSS_NOISE, spec.store_id, "gross"), -2)),
                "opened": spec.opened,
            }
        )
        for pair in baseline.program.itertuples(index=False):
            if pair.floor not in carried or pair.category in spec.exclude:
                continue
            key = (spec.store_id, pair.floor, pair.category)
            area = pair.area_m2 * spec.area_scale * spec.category_area.get(pair.category, 1.0)
            area *= _noise(AREA_NOISE, *key, "area")
            productivity = (
                pair.sales_per_m2 * spec.productivity * spec.category_productivity.get(pair.category, 1.0)
            )
            productivity *= _noise(PRODUCTIVITY_NOISE, *key, "productivity")
            for index, month in enumerate(months):
                season = baseline.season.get((month, pair.category))
                if season is None:
                    continue
                trend = (1.0 + spec.growth) ** ((index - center) / 12.0)
                sales = area * productivity / 12.0 * season * trend * _noise(MONTH_NOISE, *key, month)
                kpis.append(
                    {
                        "store_id": spec.store_id,
                        "period": month,
                        "floor": pair.floor,
                        "category": pair.category,
                        "area_m2": round(area, 1),
                        "sales_krw": int(round(sales, -3)),
                    }
                )
    store_frame = pd.DataFrame(stores, columns=list(STORE_COLUMNS))
    kpi_frame = pd.DataFrame(kpis, columns=list(KPI_COLUMNS))
    return store_frame, kpi_frame.reset_index(drop=True)


def _gross_area(toolkit: Any) -> float:
    model = toolkit._model(toolkit.latest_sales_rev())
    gross = sum(entity.geometry.area for entity in model.by_kind(EntityKind.EXTERIOR))
    return gross or BUILDING_WIDTH_M * BUILDING_DEPTH_M * max(1, len(model.floors))


def _copy_samples(ws: Workspace, samples_dir: Path) -> dict[str, int]:
    samples_dir.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for name in TABLES:
        source = ws.table_path(name)
        if source is None:
            continue
        data = source.read_bytes()
        if source.name.endswith(".gz"):
            data = gzip.decompress(data)
        target = samples_dir / f"{name}.csv"
        target.write_bytes(data)
        sizes[target.as_posix()] = len(data)
    return sizes


def ensure_store_benchmarks(
    ws: Workspace, toolkit: Any | None = None, samples_dir: Path | None = None, overwrite: bool = False
) -> dict[str, Any]:
    """Write ``business/stores.csv`` and ``business/store_kpis.csv`` when neither exists and copy the workspace
    tables to ``samples_dir``. Tables a person uploaded are only replaced with ``overwrite``. Pass a ``toolkit``
    on ``ws`` to reuse its analytics caches."""
    summary: dict[str, Any] = {"written": []}
    present = [name for name in TABLES if ws.has_table(name)]
    if present and not overwrite:
        summary["skipped"] = f"already present: {', '.join(present)}"
    elif not ws.has_table("transactions"):
        summary["skipped"] = "no transactions"
    else:
        if toolkit is None:
            from cad2anything.services.tools import Toolkit

            toolkit = Toolkit(ws, use_postgis=False)
        from cad2anything.services.benchmark_tools import flagship_monthly

        baseline = flagship_baseline(flagship_monthly(toolkit), _gross_area(toolkit))
        stores, kpis = build_store_benchmarks(baseline)
        for name, frame in zip(TABLES, (stores, kpis), strict=True):
            if (ws.root / "business" / f"{name}.csv.gz").is_file():
                ws.delete_file(Path("business") / f"{name}.csv.gz")
            rel = Path("business") / f"{name}.csv"
            ws.write_text(rel, frame.to_csv(index=False, lineterminator="\n"))
            summary["written"].append(rel.as_posix())
        summary.update(
            {
                "stores": len(stores),
                "kpi_rows": len(kpis),
                "periods": [str(kpis["period"].min()), str(kpis["period"].max())],
                "planted": list(PLANTED),
            }
        )
    if samples_dir is not None:
        summary["samples"] = _copy_samples(ws, samples_dir)
        oversized = [path for path, size in summary["samples"].items() if size > SAMPLE_LIMIT_BYTES]
        if oversized:
            summary["samples_warning"] = f"larger than {SAMPLE_LIMIT_BYTES} bytes: {', '.join(oversized)}"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=Path("data/workspace"))
    parser.add_argument("--samples", type=Path, default=None, help="also copy the two CSVs here")
    parser.add_argument("--overwrite", action="store_true", help="replace existing stores/store_kpis tables")
    args = parser.parse_args(argv)
    summary = ensure_store_benchmarks(
        Workspace(args.root), samples_dir=args.samples, overwrite=args.overwrite
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
