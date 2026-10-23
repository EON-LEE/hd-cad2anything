"""Synthetic relayout inputs: current leases and a seasonal relayout brief.

In a real store these come from the leasing system and the MD team. Here they are derived
deterministically from a workspace (latest actual revision, tenancies, brands, optional productivity):

- ``business/leases.csv``: one current lease per occupied unit. End dates are staggered by a stable
  hash; anchors get long leases and the brief's exits expire right before its effective date. Four
  discrepancies are planted for the drawing-vs-contract check: three contract areas 5~8% larger than the
  drawing (6F, 2F, 7F) and one 3F lease registered to a different brand than the drawing shows.
- ``business/relayout_briefs/FW2026.yaml``: "2026 F/W MD 개편" for 4F~6F. Exits are the two least
  productive lingerie stores on 4F; entries are unplaced catalog brands that fit the freed units and the
  floors' vacancies (outdoor on 6F for the F/W season, casual and lingerie on 4F, men's casual on 5F).

Usage:
    uv run --no-sync python -m cad2anything.mockgen.relayout --root data/workspace --samples data/samples/metadata
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from cad2anything.analytics.prep import as_list, to_bool
from cad2anything.model import RevisionKind, SpatialModel
from cad2anything.workspace import Workspace

BRIEF_ID = "FW2026"
BRIEF_FLOORS = ("4F", "5F", "6F")
EFFECTIVE_DATE = "2026-10-01"
EXPIRING_END = "2026-09-30"
ANCHOR_END = "2029-09-30"
LEASE_ENDS = (("2026-09-30", 0.30), ("2027-02-28", 0.25), ("2027-09-30", 0.25), ("2028-02-29", 0.20))
AREA_TOLERANCE = 0.25
EXIT_FLOOR, EXIT_CATEGORY, N_EXITS = "4F", "LINGERIE", 2
ENTRY_PLAN = (
    ("6F", "OUTDOOR", "F/W 아웃도어 수요에 맞춘 신규 유치"),
    ("4F", "WOMEN_CASUAL", "저효율 란제리 자리에 캐주얼 브랜드 유치"),
    ("4F", "LINGERIE", "란제리 브랜드 교체"),
    ("5F", "MEN_CASUAL", "5층 공실 해소"),
)
AREA_PLANTS = (("6F", 1.06), ("2F", 1.05), ("7F", 1.08))
BRAND_PLANT_FLOOR = "3F"
LEASE_COLUMNS = (
    "lease_id",
    "brand_id",
    "unit_code",
    "lease_start",
    "lease_end",
    "contract_area_m2",
    "commission_rate",
)


def _stable(*parts: Any) -> float:
    """Deterministic number in [0, 1) from the parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def latest_actual(ws: Workspace) -> str:
    actual = [r for r in ws.revisions() if r.kind == RevisionKind.ACTUAL and r.effective_date]
    if not actual:
        raise ValueError("workspace has no actual revision")
    return sorted(actual, key=lambda r: r.effective_date or "")[-1].rev_id


def _occupied(model: SpatialModel) -> pd.DataFrame:
    rows = [
        {
            "unit_code": str(u.attributes.get("unit_code")),
            "floor": u.floor,
            "brand_id": u.attributes.get("brand_id"),
            "category": u.attributes.get("category"),
            "area_m2": float(u.geometry.area),
        }
        for u in model.units()
    ]
    frame = pd.DataFrame(rows)
    frame["vacant"] = frame["brand_id"].isna()
    return frame


def _area_band(row: pd.Series) -> tuple[float, float]:
    lo = float(row.get("target_area_min_m2", math.nan))
    hi = float(row.get("target_area_max_m2", math.nan))
    lo = lo * (1 - AREA_TOLERANCE) if math.isfinite(lo) else 0.0
    hi = hi * (1 + AREA_TOLERANCE) if math.isfinite(hi) else math.inf
    return lo, hi


def build_brief(
    model: SpatialModel, brands: pd.DataFrame, productivity: pd.DataFrame | None = None
) -> dict[str, Any]:
    """The FW2026 brief for ``model`` (exits by productivity when given, else by attractiveness)."""
    units = _occupied(model)
    catalog = brands.drop_duplicates("brand_id").set_index("brand_id")
    lingerie = units[(units["floor"] == EXIT_FLOOR) & (units["category"] == EXIT_CATEGORY) & ~units["vacant"]]
    if productivity is not None and len(productivity):
        rate = productivity.set_index("unit_code")["sales_per_m2"]
        ranked = lingerie.assign(score=lingerie["unit_code"].map(rate))
    else:
        ranked = lingerie.assign(score=lingerie["brand_id"].map(catalog["attractiveness"]))
    ranked = ranked.sort_values(["score", "unit_code"]).head(N_EXITS)
    exits = [
        {
            "brand_id": str(row.brand_id),
            "brand_name": str(catalog.loc[row.brand_id, "brand_name"]),
            "unit_code": str(row.unit_code),
            "reason": "계약 만료·저효율",
        }
        for row in ranked.itertuples()
    ]
    slots = [
        (str(row.unit_code), str(row.floor), float(row.area_m2))
        for row in units.itertuples()
        if row.floor in BRIEF_FLOORS and (row.vacant or row.unit_code in set(ranked["unit_code"]))
    ]
    placed = set(units["brand_id"].dropna().astype(str))
    anchors = to_bool(catalog["anchor"], catalog.index) if "anchor" in catalog else None
    entries = []
    for floor, category, note in ENTRY_PLAN:
        free = [s for s in slots if s[1] == floor]
        pool = catalog[(catalog["category"] == category) & ~catalog.index.isin(placed)]
        if anchors is not None:
            pool = pool[~anchors.reindex(pool.index, fill_value=False).astype(bool)]
        best: tuple[float, str, tuple[str, str, float]] | None = None
        for brand_id, row in pool.iterrows():
            if floor not in as_list(row.get("preferred_floors")):
                continue
            lo, hi = _area_band(row)
            fits = [s for s in free if lo <= s[2] <= hi]
            if not fits:
                continue
            attractiveness = float(row.get("attractiveness") or 1.0)
            candidate = (attractiveness, str(brand_id), fits[0])
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is None:
            continue
        _, brand_id, slot = best
        slots.remove(slot)
        placed.add(brand_id)
        entries.append(
            {
                "brand_id": brand_id,
                "brand_name": str(catalog.loc[brand_id, "brand_name"]),
                "note": note,
            }
        )
    return {
        "brief_id": BRIEF_ID,
        "title": "2026 F/W MD 개편",
        "season": "FW",
        "year": 2026,
        "effective_date": EFFECTIVE_DATE,
        "base_rev": model.revision.rev_id,
        "floors": list(BRIEF_FLOORS),
        "objective": "commission",
        "max_moves": 6,
        "downtime_weeks": 2,
        "movable": "expiring",
        "keep_anchors": True,
        "entries": entries,
        "exits": exits,
        "notes": (
            "F/W 시즌 가중 수수료 수익을 높인다. 계약 기간이 남은 브랜드는 옮기지 않는다. "
            "이전 공사 기간은 매장당 2주로 본다."
        ),
    }


def build_leases(
    model: SpatialModel,
    tenancies: pd.DataFrame | None,
    brands: pd.DataFrame,
    expiring: Collection[str] = (),
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Current leases for the occupied units of ``model`` and the planted discrepancies."""
    units = _occupied(model)
    occupied = units[~units["vacant"]].copy()
    catalog = brands.drop_duplicates("brand_id").set_index("brand_id")
    anchors = to_bool(catalog["anchor"], catalog.index) if "anchor" in catalog else pd.Series(dtype=bool)
    starts: dict[tuple[str, str], str] = {}
    rates: dict[tuple[str, str], float] = {}
    if tenancies is not None and len(tenancies):
        ten = tenancies.assign(brand_id=tenancies["brand_id"].astype(str), unit_code=tenancies["unit_code"])
        for (brand_id, unit_code), group in ten.groupby(["brand_id", "unit_code"]):
            starts[(brand_id, unit_code)] = str(group["valid_from"].min())
            latest = group.sort_values("valid_from").iloc[-1]
            if pd.notna(latest.get("commission_rate")):
                rates[(brand_id, unit_code)] = float(latest["commission_rate"])
    median_rate = float(pd.Series(list(rates.values())).median()) if rates else 0.25
    effective = model.revision.effective_date or "2025-09-01"
    expiring = set(expiring)
    rows = []
    for row in occupied.sort_values("unit_code").itertuples():
        key = (str(row.brand_id), str(row.unit_code))
        if bool(anchors.get(row.brand_id, False)):
            end = ANCHOR_END
        elif row.brand_id in expiring:
            end = EXPIRING_END
        else:
            draw, cumulative = _stable("lease-end", *key), 0.0
            end = LEASE_ENDS[-1][0]
            for date, share in LEASE_ENDS:
                cumulative += share
                if draw < cumulative:
                    end = date
                    break
        rows.append(
            {
                "lease_id": f"L-{row.unit_code}",
                "brand_id": key[0],
                "unit_code": key[1],
                "lease_start": starts.get(key, effective),
                "lease_end": end,
                "contract_area_m2": round(float(row.area_m2), 1),
                "commission_rate": round(rates.get(key, median_rate), 3),
                "floor": row.floor,
                "category": row.category,
            }
        )
    leases = pd.DataFrame(rows)
    planted: list[dict[str, Any]] = []
    long_lease = (leases["lease_end"] >= "2027-02-28") & (leases["lease_end"] != ANCHOR_END)
    for floor, factor in AREA_PLANTS:
        pool = leases[long_lease & (leases["floor"] == floor)]
        if pool.empty:
            continue
        index = min(pool.index, key=lambda i: _stable("area-plant", leases.at[i, "unit_code"]))
        drawing = float(leases.at[index, "contract_area_m2"])
        leases.at[index, "contract_area_m2"] = round(drawing * factor, 1)
        planted.append(
            {
                "type": "area_mismatch",
                "unit_code": leases.at[index, "unit_code"],
                "drawing_m2": drawing,
                "contract_m2": leases.at[index, "contract_area_m2"],
            }
        )
    pool = leases[long_lease & (leases["floor"] == BRAND_PLANT_FLOOR)]
    if not pool.empty:
        index = min(pool.index, key=lambda i: _stable("brand-plant", leases.at[i, "unit_code"]))
        category = leases.at[index, "category"]
        taken = set(occupied["brand_id"].astype(str))
        spare = sorted(
            b for b in catalog.index[catalog["category"] == category].astype(str) if b not in taken
        )
        if spare:
            drawing_brand = leases.at[index, "brand_id"]
            leases.at[index, "brand_id"] = spare[0]
            planted.append(
                {
                    "type": "brand_mismatch",
                    "unit_code": leases.at[index, "unit_code"],
                    "drawing_brand_id": drawing_brand,
                    "lease_brand_id": spare[0],
                }
            )
    return leases[list(LEASE_COLUMNS)], planted


def write_relayout_metadata(
    ws: Workspace,
    productivity: pd.DataFrame | None = None,
    overwrite: bool = False,
    samples_dir: Path | None = None,
) -> dict[str, Any]:
    """Write ``leases.csv`` and the FW2026 brief into the workspace (and optional sample copies)."""
    rev = latest_actual(ws)
    model = ws.model(rev)
    brands = ws.table("brands")
    tenancies = ws.table("tenancies") if ws.has_table("tenancies") else None
    brief_path = Path("business") / "relayout_briefs" / f"{BRIEF_ID}.yaml"
    leases_path = Path("business") / "leases.csv"
    summary: dict[str, Any] = {"rev": rev, "written": []}
    if not overwrite and (ws.root / brief_path).exists() and (ws.root / leases_path).exists():
        summary["skipped"] = "already present"
        return summary
    brief = build_brief(model, brands, productivity)
    leases, planted = build_leases(model, tenancies, brands, [e["brand_id"] for e in brief["exits"]])
    brief_text = yaml.safe_dump(brief, allow_unicode=True, sort_keys=False)
    leases_text = leases.to_csv(index=False)
    ws.write_text(brief_path, brief_text)
    ws.write_text(leases_path, leases_text)
    summary["written"] = [brief_path.as_posix(), leases_path.as_posix()]
    if samples_dir is not None:
        samples_dir.mkdir(parents=True, exist_ok=True)
        (samples_dir / f"{BRIEF_ID}.yaml").write_text(brief_text, encoding="utf-8")
        (samples_dir / "leases.csv").write_text(leases_text, encoding="utf-8")
        summary["samples"] = str(samples_dir)
    summary.update(
        {
            "leases": len(leases),
            "expiring": int((leases["lease_end"] < EFFECTIVE_DATE).sum()),
            "entries": [e["brand_id"] for e in brief["entries"]],
            "exits": [e["brand_id"] for e in brief["exits"]],
            "planted": planted,
        }
    )
    return summary


def ensure_relayout_metadata(
    ws: Workspace, samples_dir: Path | None = None, toolkit: Any | None = None
) -> dict[str, Any]:
    """Create the relayout inputs when missing, ranking exits by measured productivity when possible.

    Pass an existing ``toolkit`` on ``ws`` to reuse its analytics caches."""
    productivity = None
    try:
        if toolkit is None:
            from cad2anything.services.tools import Toolkit

            toolkit = Toolkit(ws, use_postgis=False)
        productivity = toolkit.productivity_frame(latest_actual(ws), "unit")
    except Exception:  # productivity needs transactions; attractiveness is the fallback ranking
        productivity = None
    return write_relayout_metadata(ws, productivity, samples_dir=samples_dir)


def write_conformance_sample(ws: Workspace, out_dir: Path, toolkit: Any | None = None) -> dict[str, Any]:
    """Zip of the brief floors as the CAD team might draw the best FW2026 alternative, with two deviations.

    Deviation 1: the first planned move lands one unit off (its brand swaps with the nearest untouched
    occupied unit on the same floor). Deviation 2: one untouched unit on the last brief floor is drawn
    about 5% smaller. Upload it as R3 (parent R2, effective 2026-10-01) after selecting the alternative.
    """
    import copy
    import tempfile
    import zipfile

    from shapely import affinity

    from cad2anything.mockgen.dxf_writer import write_clean_floor

    if toolkit is None:
        from cad2anything.services.tools import Toolkit

        toolkit = Toolkit(ws, use_postgis=False)
    plan = toolkit.relayout_plan(BRIEF_ID)  # brief defaults, so the sample matches a fresh plan anywhere
    candidates = [a for a in plan["alternatives"] if a["alt_id"] != "ALT-0"] or plan["alternatives"]
    best = max(candidates, key=lambda a: a["kpi"]["net_krw"])
    model = copy.deepcopy(toolkit._model(best["rev_id"]))
    touched = {u for c in best["changes"] for u in (c["from_unit"], c["to_unit"]) if u}
    keys = ("brand_id", "brand_name", "category", "category_name", "vacant")
    deviations: list[dict[str, Any]] = []
    move = next((c for c in best["changes"] if c["type"] == "move"), None)
    if move is not None:
        target = model.unit_by_code(move["to_unit"])
        others = [
            u
            for u in model.units(target.floor)
            if u.attributes.get("brand_id") and u.attributes["unit_code"] not in touched
        ]
        neighbor = min(others, key=lambda u: u.geometry.centroid.distance(target.geometry.centroid))
        a = {k: target.attributes.get(k) for k in keys}
        b = {k: neighbor.attributes.get(k) for k in keys}
        target.attributes.update(b)
        neighbor.attributes.update(a)
        deviations.append(
            {
                "type": "different_unit",
                "brand_name": move["brand_name"],
                "planned_unit": move["to_unit"],
                "drawn_unit": neighbor.attributes["unit_code"],
            }
        )
    floors = list(toolkit._brief(BRIEF_ID)["floors"])
    last = floors[-1]
    resizable = sorted(
        (
            u
            for u in model.units(last)
            if u.attributes.get("brand_id") and u.attributes["unit_code"] not in touched
        ),
        key=lambda u: u.attributes["unit_code"],
    )
    if resizable:
        unit = resizable[len(resizable) // 2]
        before = unit.geometry.area
        unit.geometry = affinity.scale(unit.geometry, 0.975, 0.975)
        deviations.append(
            {
                "type": "area_change",
                "unit_code": unit.attributes["unit_code"],
                "before_m2": round(before, 1),
                "after_m2": round(unit.geometry.area, 1),
            }
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"R3_{BRIEF_ID}_{floors[0]}-{last}.zip"
    with (
        tempfile.TemporaryDirectory() as tmp,
        zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        for floor in floors:
            path = Path(tmp) / f"R3_{floor}.dxf"
            write_clean_floor(model, floor, path)
            archive.write(path, path.name)
    return {
        "zip": zip_path.as_posix(),
        "bytes": zip_path.stat().st_size,
        "alt_id": best["alt_id"],
        "plan_rev": best["rev_id"],
        "floors": floors,
        "deviations": deviations,
        "upload": {
            "rev_id": "R3",
            "parent_rev": "R2",
            "effective_date": EFFECTIVE_DATE,
            "profile": "default",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=Path("data/workspace"))
    parser.add_argument("--samples", type=Path, default=None, help="also write sample copies here")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--r3-sample",
        type=Path,
        default=None,
        help="also write the R3 drawing sample (best FW2026 alternative with two deviations) here",
    )
    args = parser.parse_args(argv)
    ws = Workspace(args.root)
    if args.overwrite:
        for rel in (
            Path("business") / "leases.csv",
            Path("business") / "relayout_briefs" / f"{BRIEF_ID}.yaml",
        ):
            (ws.root / rel).unlink(missing_ok=True)
    summary = ensure_relayout_metadata(ws, args.samples)
    if args.r3_sample is not None:
        summary["r3_sample"] = write_conformance_sample(ws, args.r3_sample)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
