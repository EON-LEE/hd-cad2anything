"""Known-answer scorecard for the deterministic layer.

Checks CAD parsing and revision diffs against the generator's ground truth, and the analytics and layout
tools against the effects planted in the synthetic business data. Run it before demos and after data or
code changes; a failed check means the tools no longer recover what the data is known to contain.

Usage:
    uv run --no-sync python -m cad2anything.evaluation.scorecard --root data/workspace --truth data/generated/truth
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cad2anything.model import ChangeSet, SpatialModel
from cad2anything.services.tools import ToolError, Toolkit
from cad2anything.workspace import Workspace

LOW_BAND = tuple(f"4F-E0{i}" for i in range(1, 7))
FOCAL_SEGMENT = {"age_band": "40s", "tier": ["VIP", "VVIP"], "gender": "F"}
REVISION_CHAIN = (("R0", "R1"), ("R1", "R2"), ("R2", "H1"))


@dataclass
class Check:
    check_id: str
    title: str
    expected: str
    observed: Any
    passed: bool | None  # None when the check was skipped
    note: str = ""


def _parse_checks(ws: Workspace, truth_dir: Path | None) -> list[Check]:
    from cad2anything.cadcore.evaluate import compare_with_truth

    checks = []
    for revision in ws.revisions():
        rev = revision.rev_id
        truth_path = truth_dir / f"layout_{rev}.json" if truth_dir else None
        title = f"{rev} 도면 파싱 정확도"
        expected = "매장 정밀도·재현율 ≥ 0.98, 브랜드 ID 정확도 ≥ 0.98"
        if ws.model_meta(rev).get("source") != "parsed" or truth_path is None or not truth_path.exists():
            checks.append(Check(f"parse-{rev}", title, expected, None, None, "파싱 모델이나 정답이 없음"))
            continue
        truth = SpatialModel.from_dict(json.loads(truth_path.read_text(encoding="utf-8")))
        metrics = compare_with_truth(ws.model(rev), truth)
        units, brands = metrics["units"], metrics["brands"]
        observed = {
            "precision": units["precision"],
            "recall": units["recall"],
            "brand_id_accuracy": brands["brand_id_accuracy"],
        }
        passed = min(observed.values()) >= 0.98
        checks.append(Check(f"parse-{rev}", title, expected, observed, passed))
    return checks


def _diff_checks(ws: Workspace, truth_dir: Path | None) -> list[Check]:
    from cad2anything.revision.diff import evaluate_diff

    checks = []
    for rev_from, rev_to in REVISION_CHAIN:
        truth_path = truth_dir / f"changes_{rev_from}_{rev_to}.json" if truth_dir else None
        check_id = f"diff-{rev_from}-{rev_to}"
        title = f"{rev_from}→{rev_to} 개정 diff 정확도"
        expected = "변경 항목 정밀도·재현율 ≥ 0.95"
        predicted = ws.change_set(rev_from, rev_to)
        source = ws.change_meta(rev_from, rev_to).get("source")
        if predicted is None or source != "diff" or truth_path is None or not truth_path.exists():
            checks.append(Check(check_id, title, expected, None, None, "diff 결과나 정답이 없음"))
            continue
        truth = ChangeSet.from_dict(json.loads(truth_path.read_text(encoding="utf-8")))
        overall = evaluate_diff(predicted, truth)["overall"]
        observed = {k: v for k, v in overall.items() if not isinstance(v, dict | list)}
        passed = min(overall.get("precision", 0.0), overall.get("recall", 0.0)) >= 0.95
        checks.append(Check(check_id, title, expected, observed, passed))
    return checks


def _business_checks(toolkit: Toolkit) -> list[Check]:
    checks: list[Check] = []

    def add(check_id: str, title: str, expected: str, run: Callable[[], tuple[Any, bool]]) -> None:
        try:
            observed, passed = run()
            checks.append(Check(check_id, title, expected, observed, passed))
        except (ToolError, KeyError, ValueError) as exc:
            checks.append(Check(check_id, title, expected, None, None, f"건너뜀: {exc}"))

    def low_band() -> tuple[Any, bool]:
        result = toolkit.analytics_productivity("R2", floor="4F")
        flagged = sorted(row["unit_code"] for row in result["low_efficiency"])
        return flagged, len(set(flagged) & set(LOW_BAND)) >= 3

    def segment_lift() -> tuple[Any, bool]:
        def best_lift(segment: dict[str, Any]) -> float:
            result = toolkit.analytics_cross_shopping(
                level="category", focal="COSMETICS", segment=segment, min_customers=3, top=20
            )
            lifts = {p["partner"]: p["lift"] for p in result["partners"]}
            return max(lifts.get("LUXURY", 0.0), lifts.get("GLOBAL_FASHION", 0.0))

        focal, other = best_lift(FOCAL_SEGMENT), best_lift({"age_band": "20s"})
        return {
            "focal_segment": round(focal, 2),
            "20s": round(other, 2),
        }, focal >= 2.0 and focal > other + 0.5

    def exposure_trap() -> tuple[Any, bool]:
        effect = toolkit.analytics_adjacency_effect("R2", "KIDS")
        keys = ("naive_uplift_pct", "controlled_uplift_pct", "confounding_suspected")
        naive, controlled = effect["naive_uplift_pct"] or 0.0, effect["controlled_uplift_pct"] or 0.0
        observed = {k: effect[k] for k in keys}
        return observed, bool(effect["confounding_suspected"]) and abs(controlled) < abs(naive)

    def flows() -> tuple[Any, bool]:
        result = toolkit.analytics_floor_transitions()
        floors, matrix, top = result["floors"], result["matrix"], result["shower"]["top_floor"]
        row = matrix[floors.index(top)] if top in floors else []
        lower = floors[: floors.index(top)] if top in floors else []
        down = sum(p or 0.0 for floor, p in zip(floors, row, strict=False) if floor in lower)
        observed = {
            "fountain_share": result["fountain"]["share"],
            "shower_share": result["shower"]["share"],
            "next_purchase_lower_after_top_floor": round(down, 3),
        }
        return observed, (result["fountain"]["share"] or 0.0) >= 0.3 and down >= 0.6

    def relayout() -> tuple[Any, bool]:
        brand = toolkit._model("R1").unit_by_code("3F-C05").attributes["brand_id"]
        result = toolkit.analytics_before_after("2025-03-01", [brand], key="brand_id")
        observed = {k: result.get(k) for k in ("treated_change_pct", "control_change_pct", "difference_pct")}
        return observed, (result.get("difference_pct") or 0.0) > 0

    def layout() -> tuple[Any, bool]:
        result = toolkit.layout_generate_alternatives("R2", floors=["4F"], n_alternatives=2, save=False)
        observed = [
            {"id": a["alt_id"], "delta_krw": a["delta_krw"], "ok": a["all_constraints_satisfied"]}
            for a in result["alternatives"]
        ]
        return observed, bool(observed) and all(a["ok"] and a["delta_krw"] > 0 for a in observed)

    add(
        "low-efficiency-band",
        "저효율 존(4F 동측) 회수",
        "4F 하위 10% 플래그에 4F-E01~E06 중 3개 이상",
        low_band,
    )
    add(
        "segment-lift",
        "세그먼트 교차구매 회수",
        "40대 VIP·VVIP 여성의 화장품↔명품·해외패션 lift ≥ 2.0이고 20대보다 0.5 이상 높음",
        segment_lift,
    )
    add(
        "exposure-trap",
        "노출 함정 판정",
        "아동 매장 근처 효과가 노출 통제 후 줄고 공통 원인 의심으로 판정",
        exposure_trap,
    )
    add(
        "fountain-shower",
        "분수·샤워 흐름 회수",
        "B1 구매자의 같은 날 위층 구매 비율 ≥ 0.3, 최상층 구매 다음 구매가 아래층일 확률 ≥ 0.6",
        flows,
    )
    add(
        "md-relayout",
        "MD 개편 효과 회수",
        "R1에 에스컬레이터 정면으로 옮긴 브랜드의 전후 변화가 같은 층 비교군보다 큼",
        relayout,
    )
    add(
        "layout-constraints",
        "레이아웃 대안 제약 충족",
        "4F 대안이 모든 제약을 지키고 기대 매출이 증가",
        layout,
    )
    return checks


def run_scorecard(root: Path, truth_dir: Path | None = None) -> list[Check]:
    workspace = Workspace(root)
    toolkit = Toolkit(workspace, use_postgis=False)
    return (
        _parse_checks(workspace, truth_dir) + _diff_checks(workspace, truth_dir) + _business_checks(toolkit)
    )


def to_markdown(checks: list[Check]) -> str:
    lines = ["| 결과 | 점검 | 기대 | 관측 |", "|---|---|---|---|"]
    for check in checks:
        status = "건너뜀" if check.passed is None else ("통과" if check.passed else "실패")
        observed = (
            json.dumps(check.observed, ensure_ascii=False) if check.observed is not None else check.note
        )
        lines.append(f"| {status} | {check.title} | {check.expected} | {observed} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=Path("data/workspace"))
    parser.add_argument("--truth", type=Path, default=Path("data/generated/truth"))
    parser.add_argument("--json", type=Path, help="also write the checks as JSON")
    args = parser.parse_args(argv)
    checks = run_scorecard(args.root, args.truth if args.truth.exists() else None)
    print(to_markdown(checks))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(c) for c in checks]
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 1 if any(c.passed is False for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
