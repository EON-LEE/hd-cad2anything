from pathlib import Path

import pytest

from cad2anything.mockgen.drawings import build_all
from cad2anything.services.tools import ToolError, Toolkit
from cad2anything.workspace import Workspace


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory) -> Toolkit:
    root: Path = tmp_path_factory.mktemp("workspace")
    workspace = Workspace(root)
    _, models, changes = build_all(42)
    for model in models.values():
        workspace.save_model(model, {"source": "truth"})
    for change_set in changes.values():
        workspace.save_change_set(change_set, {"source": "truth"})
    return Toolkit(workspace, use_postgis=False)


def test_revisions_are_ordered_with_hypothetical_last(toolkit: Toolkit):
    revisions = [r["rev_id"] for r in toolkit.spatial_list_revisions()["revisions"]]
    assert revisions == ["R0", "R1", "R2", "H1"]


def test_floor_summary_and_find_units(toolkit: Toolkit):
    summary = toolkit.spatial_floor_summary("R0", "3F")
    assert summary["units"] == 43 and summary["floor_name"] == "여성 컨템포러리"
    assert summary["categories"][0]["category"] == "WOMEN_CONTEMPORARY"
    assert {"ESC-A", "ESC-B"} <= set(summary["vertical_links"])

    found = toolkit.spatial_find_units("R0", floor="1F", category="화장품", limit=5)
    assert found["count"] > 5 and found["truncated"] and len(found["units"]) == 5
    assert all(row["category"] == "COSMETICS" for row in found["units"])

    brand = found["units"][0]["brand_name"]
    by_name = toolkit.spatial_find_units("R0", brand_query=brand)
    assert by_name["units"][0]["brand_name"] == brand


def test_unit_details_and_nearby(toolkit: Toolkit):
    unit_code = toolkit.spatial_find_units("R0", floor="3F", vacant=False)["units"][0]["unit_code"]
    details = toolkit.spatial_unit_details("R0", unit_code)
    assert details["neighbors"] and details["fixtures"]
    nearby = toolkit.spatial_nearby_units("R0", unit_code, radius_m=5)
    assert nearby["engine"] == "메모리 계산"
    assert {row["unit_code"] for row in nearby["units"] if row["touches"]} >= {
        n["unit_code"] for n in details["neighbors"]
    }


def test_revision_diff_and_impact(toolkit: Toolkit):
    diff = toolkit.revision_diff("R0", "R1")
    assert diff["source"] == "truth" and len(diff["by_type"]) == 9
    impact = toolkit.registry_impact("R0", "R1")
    generators = [entry["generator"] for entry in impact["plan"]]
    assert generators[0] in ("area_schedule", "pos_space_kpi")
    assert {"fixture_schedule", "floor_directory_svg"} <= set(generators)


def test_registry_run_and_validate(toolkit: Toolkit):
    run = toolkit.registry_run("floor_directory_svg", "R1", floors=["3F"])
    assert run["status"] == "published" and run["files"] == ["floor_directory_3F.svg"]
    area = toolkit.registry_run("area_schedule", "R1")
    assert area["status"] == "awaiting_approval" and area["summary"]["floors"] == 9
    validation = toolkit.registry_validate("R1", "area_schedule")
    assert validation["errors"] == []
    with pytest.raises(ToolError):
        toolkit.registry_validate("R1", "fixture_schedule")


def test_adhoc_table_groups_and_proposes_spec(toolkit: Toolkit):
    result = toolkit.render_adhoc_table(
        "R2", "amenity", ["floor", "amenity_type", "area_m2"], "층별 편의시설 면적표", group_by="floor"
    )
    assert result["status"] == "draft" and "proposed_spec.yaml" in result["files"]
    assert result["columns"] == ["floor", "count", "area_m2"]
    assert len(result["preview"]) == 9
    with pytest.raises(ToolError, match="사용 가능한 필드"):
        toolkit.render_adhoc_table("R2", "zone", ["floor", "zone_label"], "구역표")


def test_hypothetical_move_and_swap(toolkit: Toolkit):
    units = toolkit.spatial_find_units("R2", floor="3F", vacant=False)["units"]
    first, second = units[0], units[1]
    result = toolkit.revision_create_hypothetical(
        "R2", [{"brand_id": first["brand_id"], "to_unit": second["unit_code"]}], rev_id="H9"
    )
    assert result["rev_id"] == "H9" and "교환" in result["changes"][0]
    moved = toolkit.spatial_unit_details("H9", second["unit_code"])
    swapped = toolkit.spatial_unit_details("H9", first["unit_code"])
    assert moved["brand_id"] == first["brand_id"] and swapped["brand_id"] == second["brand_id"]
    assert toolkit.revision_diff("R2", "H9")["by_type"] == {"move": 1}


def test_unknown_revision_is_a_tool_error(toolkit: Toolkit):
    with pytest.raises(ToolError, match="R7"):
        toolkit.spatial_floor_summary("R7", "3F")
