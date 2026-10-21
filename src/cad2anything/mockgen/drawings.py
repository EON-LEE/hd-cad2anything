"""Generate the synthetic flagship drawings, truth JSON and committed samples.

Usage::

    uv run python -m cad2anything.mockgen.drawings --out data/generated --seed 42 --samples data/samples
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from cad2anything.catalog import FLOOR_CODES
from cad2anything.mockgen.brands import Brand, generate_brands
from cad2anything.mockgen.dxf_writer import write_revision_drawings
from cad2anything.mockgen.layout import PLANTED_LOW_EFFICIENCY, build_r0
from cad2anything.mockgen.revisions import build_revisions, change_type_counts
from cad2anything.model import ChangeSet, EntityKind, SpatialModel

VARIANTS: dict[str, tuple[str, ...]] = {
    "R0": ("clean", "messy"),
    "R1": ("clean", "messy"),
    "R2": ("clean", "messy"),
    "H1": ("clean",),
}
SAMPLE_FLOOR = "3F"


def build_all(seed: int = 42) -> tuple[list[Brand], dict[str, SpatialModel], dict[str, ChangeSet]]:
    """Brands, truth models ``{R0, R1, R2, H1}`` and change sets ``{R0_R1, R1_R2, R2_H1}``."""
    brands = generate_brands(seed)
    models, changes = build_revisions(build_r0(brands, seed), brands, seed)
    return brands, models, changes


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def _write_json(path: Path, data: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(data), encoding="utf-8", newline="\n")
    return path.stat().st_size


def revision_stats(model: SpatialModel) -> dict[str, Any]:
    units = {f: model.units(f) for f in FLOOR_CODES}
    fixtures = {f: len(model.by_kind(EntityKind.FIXTURE, f)) for f in FLOOR_CODES}
    return {
        "kind": str(model.revision.kind),
        "parent": model.revision.parent_rev_id,
        "effective_date": model.revision.effective_date,
        "units_total": sum(len(u) for u in units.values()),
        "units_per_floor": {f: len(u) for f, u in units.items()},
        "vacant_units": sum(1 for us in units.values() for u in us if u.attributes["vacant"]),
        "fixtures_total": sum(fixtures.values()),
        "fixtures_per_floor": fixtures,
        "entities": len(model.entities),
        "relations": len(model.relations),
    }


def generate_all(out_dir: Path, seed: int = 42) -> dict[str, Any]:
    """Build R0..H1, write truth JSON and DXF drawings under ``out_dir``; return a summary."""
    started = time.perf_counter()
    out_dir = Path(out_dir)
    brands, models, changes = build_all(seed)
    files: dict[str, int] = {}
    for rev_id, model in models.items():
        files[f"truth/layout_{rev_id}.json"] = _write_json(
            out_dir / "truth" / f"layout_{rev_id}.json", model.to_dict()
        )
    for change_set in changes.values():
        name = f"changes_{change_set.rev_from}_{change_set.rev_to}.json"
        files[f"truth/{name}"] = _write_json(out_dir / "truth" / name, change_set.to_dict())
    for rev_id, model in models.items():
        for variant in VARIANTS[rev_id]:
            sizes = write_revision_drawings(model, out_dir / "drawings" / rev_id / variant, variant, seed)
            files.update({f"drawings/{rev_id}/{variant}/{name}": size for name, size in sizes.items()})
    planted_floor, planted_band = PLANTED_LOW_EFFICIENCY["floor"], PLANTED_LOW_EFFICIENCY["band"]
    planted_codes = [
        u.attributes["unit_code"]
        for u in models["R0"].units(planted_floor)
        if u.attributes["band"] == planted_band
    ]
    return {
        "seed": seed,
        "out_dir": str(out_dir),
        "brands": len(brands),
        "planted_low_efficiency": {**PLANTED_LOW_EFFICIENCY, "unit_codes": planted_codes},
        "revisions": {rev_id: revision_stats(model) for rev_id, model in models.items()},
        "changes": {
            key: {"items": len(cs.items), "by_type": change_type_counts(cs)} for key, cs in changes.items()
        },
        "files": files,
        "total_bytes": sum(files.values()),
        "elapsed_s": round(time.perf_counter() - started, 2),
    }


def write_samples(
    samples_dir: Path, seed: int = 42, model: SpatialModel | None = None, floor: str = SAMPLE_FLOOR
) -> dict[str, int]:
    """Write the small committed R0 sample (one floor, clean and messy, with manifests)."""
    if model is None:
        model = build_r0(generate_brands(seed), seed)
    files: dict[str, int] = {}
    for variant in ("clean", "messy"):
        folder = Path(samples_dir) / "drawings" / model.revision.rev_id / variant
        sizes = write_revision_drawings(model, folder, variant, seed, floors=[floor])
        files.update(
            {f"drawings/{model.revision.rev_id}/{variant}/{name}": size for name, size in sizes.items()}
        )
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic flagship drawings and ground truth.")
    parser.add_argument("--out", type=Path, default=Path("data/generated"), help="output folder")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument("--samples", type=Path, default=None, help="also write committed samples here")
    args = parser.parse_args(argv)
    summary = generate_all(args.out, args.seed)
    if args.samples is not None:
        summary["samples"] = write_samples(args.samples, args.seed)
    brief = {k: v for k, v in summary.items() if k != "files"}
    brief["file_count"] = len(summary["files"])
    print(json.dumps(brief, ensure_ascii=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
