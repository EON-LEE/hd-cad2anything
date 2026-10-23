"""Corridor width and escape distance review (검토용 기준, not legal criteria)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from cad2anything.services.errors import NotFoundToolError, ToolError
from cad2anything.services.flow_tools import EGRESS_CAVEATS, FlowTools
from cad2anything.services.tools import Toolkit
from cad2anything.spatial.egress import exit_distances, narrow_stretches, width_stats
from cad2anything.spatial.features import build_floor_network
from cad2anything.spatial.flows import build_corridor_graph, clear_widths

# The coordinator adds FlowTools to Toolkit; until then the mixin goes first.
_BASES = (Toolkit,) if issubclass(Toolkit, FlowTools) else (FlowTools, Toolkit)
DISCLAIMER = "법규 인증이 아니라 검토용 기준이에요. 기준값은 바꿀 수 있어요."


class _Toolkit(*_BASES):  # type: ignore[misc]
    pass


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> _Toolkit:
    return _Toolkit(populate(tmp_path_factory.mktemp("egress-ws")), use_postgis=False)


@pytest.fixture(scope="module")
def floors(synthetic_store):
    model = synthetic_store["models"]["R2"]
    result = {}
    for floor in ("1F", "3F", "4F", "5F"):
        network = build_floor_network(model, floor)
        result[floor] = (network, build_corridor_graph(model, floor, network))
    return result


def _width_near(graph, x: float, y: float) -> float:
    k = int(np.argmin(np.hypot(graph.cell_xy[:, 0] - x, graph.cell_xy[:, 1] - y)))
    return float(graph.cell_width[k])


@pytest.mark.parametrize(
    ("floor", "x", "y", "width"),
    [
        ("3F", 60.0, 15.0, 6.0),  # south ring corridor
        ("3F", 15.0, 28.0, 6.0),  # west ring corridor between junctions
        ("3F", 53.0, 31.5, 3.0),  # strip in front of the ESC-A landing
        ("4F", 56.0, 44.5, 2.0),  # squeeze between ESC-A and the popup kiosk 4F-P01
        ("5F", 30.0, 40.0, 7.0),  # central corridor widened in R1
    ],
)
def test_corridor_widths_match_the_synthetic_walkways(floors, floor, x, y, width):
    network, graph = floors[floor]
    assert _width_near(graph, x, y) == pytest.approx(width, abs=0.5)
    across, _ = clear_widths(network.walkable, np.array([[x + 0.3, y + 0.2]]))
    assert across[0] == pytest.approx(width, abs=0.5)


def test_narrow_stretches_find_the_squeeze_beside_the_popup_kiosk(floors):
    _, graph = floors["4F"]
    stretches = narrow_stretches(graph, 2.4)
    assert len(stretches) == 2
    for stretch in stretches:
        assert stretch.min_width_m == pytest.approx(2.0, abs=0.5)
        assert stretch.length_m == pytest.approx(5.0, abs=1.5)
        assert stretch.label.startswith("4F ") and len(stretch.points) >= 2
        assert 55.0 <= stretch.x <= 65.0 and 42.0 <= stretch.y <= 47.0
    assert narrow_stretches(floors["3F"][1], 2.4) == []
    wider = narrow_stretches(floors["3F"][1], 3.5)
    assert wider and all(s.min_width_m == pytest.approx(3.0, abs=0.5) for s in wider)
    stats = width_stats(graph)
    assert stats["min_width_m"] == pytest.approx(2.0, abs=0.5) and stats["median_width_m"] == pytest.approx(
        6.0
    )


def test_exit_distances_reach_stairs_and_entrances(floors, synthetic_store):
    model = synthetic_store["models"]["R2"]
    third = exit_distances(model, "3F", floors["3F"][0])
    assert len(third) == len(model.units("3F"))
    assert all(math.isfinite(r.travel_m) and r.travel_m > 0 for r in third)
    assert {r.exit_ref for r in third} == {"ST-1", "ST-2"} and {r.exit_name for r in third} == {
        "계단 1",
        "계단 2",
    }
    first = exit_distances(model, "1F", floors["1F"][0])
    assert {r.exit_ref for r in first} & {"ENT-1", "ENT-2", "ENT-3"}
    assert max(r.travel_m for r in first) < max(r.travel_m for r in third)


def test_egress_check_flags_narrow_corridors_and_far_units(toolkit):
    result = toolkit.spatial_egress_check()
    assert result["rev"] == "R2" and DISCLAIMER in result["caveats"] and result["caveats"] == EGRESS_CAVEATS
    assert result["criteria"]["min_width_m"] == 2.4 and result["criteria"]["max_travel_m"] == 50.0
    assert len(result["floors"]) == 9 and result["totals"]["narrow_stretches"] == 2
    assert {s["floor"] for s in result["narrow_stretches"]} == {"4F"}
    assert result["summary"].startswith("검토 기준(통로 폭 2.4m, 걷는 거리 50m)으로 보면")
    over = result["units_over_limit"]
    assert over and [u["travel_m"] for u in over] == sorted((u["travel_m"] for u in over), reverse=True)
    assert all(u["travel_m"] > 50 and u["nearest"] for u in over)

    tight = toolkit.spatial_egress_check(floor="3F", max_travel_m=1.0, top=500)
    assert [f["floor"] for f in tight["floors"]] == ["3F"]
    assert tight["totals"]["units_over_limit"] == len(toolkit._model("R2").units("3F"))
    assert toolkit.spatial_egress_check(rev="H1", floor="4F")["rev"] == "H1"  # geometry only: any revision
    with pytest.raises(ToolError):
        toolkit.spatial_egress_check(min_width_m=0)
    with pytest.raises(NotFoundToolError):
        toolkit.spatial_egress_check(rev="R9")


def test_egress_flow_object_draws_warnings(toolkit):
    flow = toolkit.flow_object("egress", max_travel_m=60)
    assert flow["mode"] == "egress" and DISCLAIMER in flow["caveat"]
    assert {"mode", "title", "rev", "query", "floors", "markers", "legend", "summary", "caveat"} <= set(flow)
    narrow = flow["floors"]["4F"]["segments"]
    assert len(narrow) == 2 and all(s["color"] == "#e53935" and s["value"] < 2.4 for s in narrow)
    assert all(0 <= s["weight"] <= 1 and "폭 2.0m" in s["label"] for s in narrow)
    assert flow["markers"] and all(m["kind"] == "warning" for m in flow["markers"])
    assert {m["floor"] for m in flow["markers"]} <= set(flow["floors"])
    assert flow["summary"]["units_over_limit"] == len(flow["markers"])
    assert {"customer_days", "trips", "floors"} <= set(flow["summary"]) and flow["summary"]["trips"] is None
