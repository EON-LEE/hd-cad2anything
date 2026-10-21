"""Business generators: POS-to-space mapping and space productivity baseline per revision."""

from __future__ import annotations

import pandas as pd

from cad2anything.generators.registry import GeneratorContext, GeneratorResult, Issue, generator
from cad2anything.generators.spatial import _write_tables
from cad2anything.model import EntityKind

PYEONG_M2 = 3.3058


def _window(ctx: GeneratorContext, tenancies: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    rows = tenancies[tenancies["rev_id"] == ctx.model.revision.rev_id]
    if rows.empty:
        return None
    start = pd.to_datetime(rows["valid_from"]).min()
    end = pd.to_datetime(rows["valid_to"]).max()
    return start, end


@generator("pos_space_kpi")
def pos_space_kpi(ctx: GeneratorContext) -> GeneratorResult:
    issues: list[Issue] = []
    tenancies = ctx.data.get("tenancies")
    terminals = ctx.data.get("pos_terminals")
    transactions = ctx.data.get("transactions")
    if tenancies is None or terminals is None or transactions is None:
        issues.append(Issue("error", "tenancies, pos_terminals, transactions 테이블이 필요합니다."))
        return GeneratorResult([], {}, issues)
    window = _window(ctx, tenancies)
    if window is None:
        issues.append(
            Issue(
                "error",
                f"{ctx.model.revision.rev_id}: 임차 이력이 없어 실적 기준선을 만들 수 없습니다(가상 개정일 수 있음).",
            )
        )
        return GeneratorResult([], {}, issues)
    start, end = window

    units = {
        u.attributes.get("unit_code"): u
        for u in ctx.model.by_kind(EntityKind.UNIT)
        if ctx.floors is None or u.floor in ctx.floors
    }
    pos = terminals.copy()
    pos["valid_from"] = pd.to_datetime(pos["valid_from"])
    pos["valid_to"] = pd.to_datetime(pos["valid_to"]).fillna(pd.Timestamp.max)
    active = pos[(pos["valid_from"] <= end) & (pos["valid_to"] >= start)]
    # The mapping in force at the start of the revision window is the one the drawing must match.
    in_force = active[(active["valid_from"] <= start) & (active["valid_to"] >= start)]

    mapping_rows: list[list] = []
    for row in in_force.sort_values(["unit_code", "pos_id"]).itertuples(index=False):
        unit = units.get(row.unit_code)
        if unit is None:
            if ctx.floors is None:
                issues.append(
                    Issue("error", f"POS {row.pos_id}가 도면에 없는 매장 {row.unit_code}에 매핑돼 있습니다.")
                )
            continue
        drawn_brand = unit.attributes.get("brand_id")
        status = "일치" if drawn_brand == row.brand_id else "불일치"
        if status == "불일치":
            drawn = unit.attributes.get("brand_name") or "공실"
            issues.append(
                Issue(
                    "error",
                    f"POS {row.pos_id}가 매핑된 매장 {row.unit_code}의 도면 브랜드({drawn})가 "
                    f"매핑 브랜드({row.brand_id})와 다릅니다.",
                    [unit.entity_id],
                )
            )
        mapping_rows.append(
            [
                unit.floor,
                row.unit_code,
                row.pos_id,
                row.brand_id,
                unit.attributes.get("brand_name") or "",
                status,
            ]
        )
    mapped_units = {row[1] for row in mapping_rows}
    for code, unit in sorted(units.items()):
        if not unit.attributes.get("vacant") and code not in mapped_units:
            issues.append(Issue("warning", f"영업 매장 {code}에 연결된 POS가 없습니다.", [unit.entity_id]))

    txns = transactions.copy()
    txns["ts"] = pd.to_datetime(txns["ts"])
    txns = txns[(txns["ts"] >= start) & (txns["ts"] <= end + pd.Timedelta(days=1))]
    if "weight" not in txns.columns:
        txns["weight"] = 1.0
    joined = txns.merge(active[["pos_id", "unit_code", "valid_from", "valid_to"]], on="pos_id", how="inner")
    joined = joined[
        (joined["ts"] >= joined["valid_from"]) & (joined["ts"] <= joined["valid_to"] + pd.Timedelta(days=1))
    ]
    joined["sales"] = joined["amount_krw"] * joined["weight"]
    sales = joined.groupby("unit_code").agg(sales_krw=("sales", "sum"), txns=("weight", "sum"))

    days = max((end - start).days + 1, 1)
    kpi_rows: list[list] = []
    for code, unit in sorted(units.items()):
        area = unit.geometry.area
        unit_sales = float(sales["sales_krw"].get(code, 0.0))
        annual = unit_sales * 365.0 / days
        kpi_rows.append(
            [
                unit.floor,
                code,
                unit.attributes.get("brand_name") or "",
                unit.attributes.get("category") or "",
                round(area, 2),
                round(unit_sales),
                round(float(sales["txns"].get(code, 0.0))),
                round(annual / area) if area else 0,
                round(annual / (area / PYEONG_M2)) if area else 0,
            ]
        )
    kpi = pd.DataFrame(
        kpi_rows,
        columns=["floor", "unit_code", "brand", "category", "area", "sales", "txns", "per_m2", "per_pyeong"],
    )
    kpi["floor_percentile"] = kpi.groupby("floor")["per_m2"].rank(pct=True).round(3)
    kpi_table = [[v.item() if hasattr(v, "item") else v for v in row] for row in kpi.itertuples(index=False)]

    mapping_header = ["층", "매장코드", "POS", "매핑브랜드ID", "도면브랜드", "도면일치"]
    kpi_header = [
        "층",
        "매장코드",
        "브랜드",
        "카테고리",
        "면적(㎡)",
        "기간매출(원)",
        "거래수",
        "연환산 ㎡당 매출(원)",
        "연환산 평당 매출(원)",
        "층내 백분위",
    ]
    files = _write_tables(
        ctx.out_dir / "pos_space_kpi.xlsx",
        ctx.out_dir / "pos_space_kpi.csv",
        {"POS매핑": (mapping_header, mapping_rows), "매장KPI": (kpi_header, kpi_table)},
        csv_sheet="매장KPI",
    )
    summary = {
        "window": [start.date().isoformat(), end.date().isoformat()],
        "pos_mapped": len(mapping_rows),
        "pos_mismatched": sum(1 for row in mapping_rows if row[5] == "불일치"),
        "units": len(units),
        "sales_krw": round(float(kpi["sales"].sum())),
        "note": "거래 가중치(weight)로 전체 규모 환산. 합성 데이터 기준.",
    }
    return GeneratorResult(files, summary, issues)
