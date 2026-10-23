"""Build a workspace from generated data: copy drawings and business tables, then create spatial models.

Models come from parsing the drawings with the CAD pipeline when it is available. Otherwise the
generator's ground-truth layouts are imported and labeled ``source: truth`` so that nobody mistakes
them for parsed results. Finally the synthetic relayout inputs (current leases and the FW2026 brief,
``cad2anything.mockgen.relayout``) are added when missing.

Usage:
    uv run --no-sync python -m cad2anything.bootstrap --source data/generated --root data/workspace
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from cad2anything.model import ChangeSet, SpatialModel
from cad2anything.workspace import Workspace

REVISION_CHAIN = (("R0", "R1"), ("R1", "R2"), ("R2", "H1"))


def _copy_tree(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    count = 0
    for path in source.rglob("*"):
        if path.is_file():
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            count += 1
    return count


def _parse(workspace: Workspace, rev_id: str, variant: str, brand_registry: Any) -> SpatialModel | None:
    try:
        from cad2anything.cadcore.pipeline import parse_revision
    except ImportError:
        return None
    rev_dir = workspace.drawing_dir(rev_id, variant)
    if not (rev_dir / "manifest.json").exists():
        return None
    model, report = parse_revision(rev_dir, brand_registry=brand_registry, rev_id=rev_id)
    workspace.save_report(rev_id, report.to_dict() if hasattr(report, "to_dict") else dict(report))
    return model


def _diff(before: SpatialModel, after: SpatialModel) -> ChangeSet | None:
    try:
        from cad2anything.revision.diff import diff_models
    except ImportError:
        return None
    return diff_models(before, after)


def bootstrap(source: Path, root: Path, mode: str = "auto", variant: str = "clean") -> dict[str, Any]:
    workspace = Workspace(root)
    summary: dict[str, Any] = {
        "drawings": _copy_tree(source / "drawings", root / "drawings"),
        "business": _copy_tree(source / "business", root / "business"),
        "models": {},
        "changes": {},
    }
    brand_registry = workspace.table("brands") if workspace.has_table("brands") else None
    truth_dir = source / "truth"
    for truth_path in sorted(truth_dir.glob("layout_*.json")):
        rev_id = truth_path.stem.removeprefix("layout_")
        model = _parse(workspace, rev_id, variant, brand_registry) if mode in ("auto", "parse") else None
        if model is not None:
            workspace.save_model(model, {"source": "parsed", "variant": variant})
            summary["models"][rev_id] = "parsed"
            continue
        if mode == "parse":
            raise RuntimeError(f"could not parse drawings for {rev_id}")
        truth = SpatialModel.from_dict(json.loads(truth_path.read_text(encoding="utf-8")))
        workspace.save_model(truth, {"source": "truth"})
        summary["models"][rev_id] = "truth"
    for rev_from, rev_to in REVISION_CHAIN:
        if not (workspace.has_model(rev_from) and workspace.has_model(rev_to)):
            continue
        parsed = (
            workspace.model_meta(rev_from).get("source")
            == workspace.model_meta(rev_to).get("source")
            == "parsed"
        )
        change_set = _diff(workspace.model(rev_from), workspace.model(rev_to)) if parsed else None
        if change_set is not None:
            workspace.save_change_set(change_set, {"source": "diff"})
            summary["changes"][f"{rev_from}->{rev_to}"] = "diff"
            continue
        truth_changes = truth_dir / f"changes_{rev_from}_{rev_to}.json"
        if truth_changes.exists():
            change_set = ChangeSet.from_dict(json.loads(truth_changes.read_text(encoding="utf-8")))
            workspace.save_change_set(change_set, {"source": "truth"})
            summary["changes"][f"{rev_from}->{rev_to}"] = "truth"
    if workspace.has_table("brands") and workspace.has_table("tenancies"):
        from cad2anything.mockgen.relayout import ensure_relayout_metadata

        relayout = ensure_relayout_metadata(workspace)
        summary["relayout"] = {
            k: relayout.get(k) for k in ("written", "skipped", "leases", "entries", "exits")
        }
    if workspace.has_table("transactions"):
        from cad2anything.mockgen.stores import ensure_store_benchmarks

        stores = ensure_store_benchmarks(workspace)
        summary["stores"] = {k: stores.get(k) for k in ("written", "skipped", "stores", "kpi_rows")}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", type=Path, default=Path("data/generated"))
    parser.add_argument("--root", type=Path, default=Path("data/workspace"))
    parser.add_argument("--mode", choices=["auto", "parse", "truth"], default="auto")
    parser.add_argument("--variant", choices=["clean", "messy"], default="clean")
    parser.add_argument(
        "--push",
        action="store_true",
        help="upload the workspace to the configured mirror (WORKSPACE_MIRROR=postgres|blob) afterwards",
    )
    parser.add_argument("--clean", action="store_true", help="delete the workspace root before building")
    parser.add_argument(
        "--prune", action="store_true", help="with --push, delete mirrored files missing from the workspace"
    )
    args = parser.parse_args()
    if args.clean and args.root.exists():
        shutil.rmtree(args.root)
    summary = bootstrap(args.source, args.root, args.mode, args.variant)
    if args.push:
        from cad2anything.store.pgmirror import mirror_from_env

        mirror = mirror_from_env()
        if mirror is None:
            raise SystemExit("No workspace mirror is configured (set WORKSPACE_MIRROR and its settings)")
        summary["uploaded"] = mirror.push_tree(args.root)
        if args.prune:
            summary["pruned"] = mirror.prune(args.root)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
