"""Small deterministic regressions for decision preservation and entry assignment."""

import copy
import math
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from cad2anything.layout import LayoutConstraints, check_constraints, exact, fit_scoring_model
from cad2anything.layout.engine import _Search
from cad2anything.layout.exact import assign_entries
from cad2anything.model import Floor, Revision, SpatialModel
from cad2anything.services.errors import DecisionExistsError, ToolError
from cad2anything.services.relayout_tools import RelayoutTools
from cad2anything.workspace import Workspace


@pytest.fixture
def brief():
    return {
        "brief_id": "REGRESSION",
        "title": "가상 개편",
        "base_rev": "R2",
        "season": "FW",
        "season_label": "F/W(9~2월)",
        "year": 2026,
        "effective_date": "2026-10-01",
        "objective": "commission",
        "objective_label": "시즌 가중 수수료 수익",
        "floors": ["1F"],
        "max_moves": 2,
        "downtime_weeks": 2,
        "area_tolerance": 0.0,
        "keep_anchors": True,
        "movable": "expiring",
        "entries": [],
        "exits": [],
        "notes": "",
    }


@pytest.fixture
def decision_tools(tmp_path, monkeypatch, brief):
    tools = RelayoutTools()
    tools.ws = Workspace(tmp_path)
    monkeypatch.setattr(tools, "_brief", lambda _: copy.deepcopy(brief))
    project = tools._project(brief["brief_id"])
    project.update(
        status="planned",
        diagnosis={"as_of": "2026-09-30"},
        plan={
            "brief_id": brief["brief_id"],
            "as_of": "2026-09-30",
            "params": {
                "objective": "commission",
                "season": "FW",
                "max_moves": 2,
                "downtime_weeks": 2,
                "movable": "expiring",
                "floors": ["1F"],
                "extra_entries": [],
                "extra_exits": [],
                "n_alternatives": 3,
                "as_of": "2026-09-30",
                "lease_cutoff": "2026-10-01",
            },
            "alternatives": [
                {
                    "alt_id": f"ALT-{n}",
                    "rev_id": f"REGRESSION-A{n}",
                    "kpi": {"net_krw": n * 100},
                    "changes": [],
                    "constraints": [{"name": "면적", "ok": True}],
                }
                for n in range(3)
            ],
        },
    )
    tools._save_project(project)
    return tools


def test_identical_replan_preserves_decision_check_and_phasing(decision_tools, brief, monkeypatch):
    tools = decision_tools
    tools.relayout_select(brief["brief_id"], "ALT-2", "MD팀", decision_source="api")
    project = tools._project(brief["brief_id"])
    project["check"] = {"rev": "R3", "alt_id": "ALT-2"}
    project["phasing"] = {"alt_id": "ALT-2", "steps": [1]}
    tools._save_project(project)
    path = tools.ws.root / tools._project_rel(brief["brief_id"])
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("A preserved decision must not be recomputed")

    monkeypatch.setattr(tools, "_plan_setup", forbidden)
    result = tools.relayout_plan(brief["brief_id"])
    assert result["reused"] and result["selected_alt_id"] == "ALT-2"
    assert result["decided_by"] == "MD팀"
    assert {k: result[k] for k in project["plan"]} == project["plan"]
    assert path.read_bytes() == before


def test_legacy_plan_defaults_are_reused(decision_tools, brief):
    tools = decision_tools
    project = tools.relayout_select(brief["brief_id"], "ALT-2")
    for key in ("n_alternatives", "as_of", "lease_cutoff"):
        project["plan"]["params"].pop(key)
    tools._save_project(project)
    assert tools.relayout_plan(brief["brief_id"])["reused"]
    assert tools._project(brief["brief_id"])["decision_source"] == "chat"


@pytest.mark.parametrize("protected", ["selected_alt_id", "check", "phasing"])
def test_changed_plan_requires_confirmation_for_any_protected_work(decision_tools, brief, protected):
    tools = decision_tools
    project = tools._project(brief["brief_id"])
    project[protected] = "ALT-2" if protected == "selected_alt_id" else {"alt_id": "ALT-2"}
    project["decided_by"] = "MD팀"
    project["decided_at"] = "2026-10-03T10:00:00+09:00"
    tools._save_project(project)
    before = copy.deepcopy(tools._project(brief["brief_id"]))
    with pytest.raises(DecisionExistsError, match="MD팀.*2026-10-03.*replace_decision=true"):
        tools.relayout_plan(brief["brief_id"], max_moves=1)
    assert tools._project(brief["brief_id"]) == before


def test_selection_appends_immutable_kpi_snapshot(decision_tools, brief):
    tools = decision_tools
    first = tools.relayout_select(brief["brief_id"], "ALT-2", "MD팀", decision_source="api")
    assert first["decision_snapshot"]["kpi"] == {"net_krw": 200}
    assert first["decision_history"][-1]["kpi_snapshot"] == {"net_krw": 200}
    assert first["decision_history"][-1]["decision_source"] == "api"
    prior = copy.deepcopy(first["decision_history"])
    first["plan"]["alternatives"][2]["kpi"]["net_krw"] = 999
    tools._save_project(first)
    second = tools.relayout_select(brief["brief_id"], "ALT-1", "MD팀")
    assert second["decision_history"][:-1] == prior
    assert "decision_source" not in second["decision_history"][-1]
    with pytest.raises(ToolError, match="decision_source"):
        tools.relayout_select(brief["brief_id"], "ALT-1", decision_source="guessed")


def test_failed_constraints_cannot_be_selected(decision_tools, brief):
    tools = decision_tools
    project = tools._project(brief["brief_id"])
    project["plan"]["alternatives"][1]["constraints"][0]["ok"] = False
    tools._save_project(project)
    with pytest.raises(ToolError, match="제약"):
        tools.relayout_select(brief["brief_id"], "ALT-1")


@pytest.fixture
def setup_tools(decision_tools, monkeypatch):
    tools = decision_tools
    features = pd.DataFrame(
        [
            {"unit_code": "X", "floor": "1F", "area_m2": 100.0, "walk_to_escalator_m": 1.0},
            {"unit_code": "Y", "floor": "1F", "area_m2": 60.0, "walk_to_escalator_m": 20.0},
            {"unit_code": "Z", "floor": "1F", "area_m2": 100.0, "walk_to_escalator_m": 100.0},
        ]
    )
    brands = pd.DataFrame(
        [
            {
                "brand_id": bid,
                "brand_name": f"가상 {bid}",
                "category": category,
                "anchor": False,
                "target_area_min_m2": lo,
                "target_area_max_m2": 100,
                "preferred_floors": "1F",
                "adjacency_conflicts": "",
                "attractiveness": 1.0,
            }
            for bid, category, lo in [
                ("A", "WOMEN_CASUAL", 60),
                ("B", "ACCESSORIES", 100),
                ("INC", "CAFE", 60),
            ]
        ]
    )
    model = fit_scoring_model(
        pd.DataFrame([{"unit_code": "Z", "brand_id": "INC", "sales": 1000.0}]),
        features,
        brands,
    )
    inputs = {
        "features": features,
        "brands": brands,
        "model": model,
        "affinity": None,
        "adjacency": None,
        "fit_rev": "R2",
        "assignment": {"X": None, "Y": None, "Z": "INC"},
    }
    monkeypatch.setattr(tools, "_layout_inputs", lambda rev, as_of=None: inputs, raising=False)
    monkeypatch.setattr(tools, "_catalog", lambda: brands)
    monkeypatch.setattr(tools, "_placements", lambda rev: {"INC": "Z"})
    monkeypatch.setattr(tools, "_season_factors", lambda season, as_of=None: {})
    monkeypatch.setattr(tools, "_commission_rates", lambda rev: {"INC": 0.2})
    monkeypatch.setattr(
        tools,
        "_leases",
        lambda: pd.DataFrame([{"brand_id": "INC", "unit_code": "Z", "lease_end": "2026-09-30"}]),
    )
    return tools, inputs


def _params(tools, brief):
    return copy.deepcopy(tools._project(brief["brief_id"])["plan"]["params"])


def test_entry_assignment_maximizes_cardinality_before_score(setup_tools, brief, monkeypatch):
    tools, inputs = setup_tools
    from cad2anything.layout.whatif import AssignmentScorer

    values = {("A", "X"): 1000.0, ("A", "Y"): 100.0, ("B", "X"): 800.0}
    original = AssignmentScorer.location_value
    monkeypatch.setattr(
        AssignmentScorer,
        "location_value",
        lambda self, b, u: values[(b, u)] if (b, u) in values else original(self, b, u),
    )
    brief["entries"] = [
        {"brand_id": b, "brand_name": f"가상 {b}", "new_brand": False, "commission_rate": 1.0}
        for b in ("A", "B")
    ]
    setup = tools._plan_setup(brief, _params(tools, brief))
    assert setup["start"] == {"X": "B", "Y": "A", "Z": "INC"}
    assert setup["placed_entries"] == ["A", "B"] and not setup["unplaced"]
    assert assign_entries(values) == {"A": "Y", "B": "X"}
    assert assign_entries(values) == assign_entries(dict(reversed(list(values.items()))))
    assert inputs["assignment"] == {"X": None, "Y": None, "Z": "INC"}


def test_entry_conflicts_are_checked_against_actual_pre_entry_layout(setup_tools, brief):
    tools, inputs = setup_tools
    inputs["brands"].loc[inputs["brands"]["brand_id"] == "A", "adjacency_conflicts"] = "CAFE"
    inputs["adjacency"] = pd.DataFrame({"unit_a": ["X"], "unit_b": ["Z"]})
    brief["entries"] = [{"brand_id": "A", "brand_name": "가상 A", "commission_rate": 0.2}]
    setup = tools._plan_setup(brief, _params(tools, brief))
    assert setup["start"]["Y"] == "A" and setup["start"]["X"] is None
    bad = {**setup["start"], "X": "A", "Y": None}
    checks = check_constraints(
        bad,
        bad,
        inputs["features"],
        setup["infos"],
        inputs["adjacency"],
        setup["constraints"],
        free=["A"],
        adjacency_reference=setup["current"],
    ).set_index("constraint")
    assert checks.loc["인접 충돌 금지", "violations"] == 1
    assert not checks.loc["인접 충돌 금지", "satisfied"]


def test_exact_and_heuristic_do_not_grandfather_a_preplaced_entrant(setup_tools):
    from cad2anything.layout.whatif import AssignmentScorer

    _, inputs = setup_tools
    inputs["brands"].loc[inputs["brands"]["brand_id"] == "A", "adjacency_conflicts"] = "CAFE"
    adjacency = pd.DataFrame({"unit_a": ["X"], "unit_b": ["Z"]})
    scorer = AssignmentScorer(inputs["features"], inputs["brands"], inputs["model"], None, adjacency)
    constraints = LayoutConstraints(floors=["1F"], movable_brands=[], max_moves=0, area_tolerance=0.0)
    invalid = {"X": "A", "Y": None, "Z": "INC"}
    search = _Search(scorer, invalid, constraints, free=["A"], adjacency_reference=inputs["assignment"])
    solved = exact.solve(search, hint=search.base)
    assert solved is not None
    final = search.assignment(search.state_of(solved.state, 0.0, {}))
    assert final == {"X": None, "Y": "A", "Z": "INC"}
    valid_search = _Search(scorer, final, constraints, free=["A"], adjacency_reference=inputs["assignment"])
    brand = valid_search.bids.index("A")
    change = valid_search.changes_for(valid_search.initial(), brand, valid_search.index["X"])
    assert change is not None
    assert valid_search.evaluate(valid_search.initial(), change, 0.0, {}) is None


def test_entry_assignment_constrains_conflicts_between_entrants():
    values = {("A", "X"): 1000.0, ("A", "Y"): 100.0, ("B", "Z"): 800.0}
    assigned = assign_entries(values, [(("A", "X"), ("B", "Z"))])
    assert assigned == {"A": "Y", "B": "Z"}


def test_unplaced_entry_explains_required_incumbent_relocation(setup_tools, brief):
    tools, inputs = setup_tools
    inputs["features"].loc[inputs["features"]["unit_code"] == "X", "area_m2"] = 60.0
    brief["entries"] = [{"brand_id": "B", "brand_name": "가상 B", "commission_rate": 0.2}]
    setup = tools._plan_setup(brief, _params(tools, brief))
    assert setup["unplaced"][0]["requires_incumbent_relocation"]
    assert setup["unplaced"][0]["relocation_unit_codes"] == ["Z"]
    assert "계약 만료 기존 브랜드를 먼저 옮겨야" in setup["unplaced"][0]["reason"]


def test_unplaced_entry_reports_a_movable_conflicting_neighbor(setup_tools, brief):
    tools, inputs = setup_tools
    inputs["brands"].loc[inputs["brands"]["brand_id"] == "A", "adjacency_conflicts"] = "CAFE"
    inputs["brands"].loc[inputs["brands"]["brand_id"] == "A", "target_area_min_m2"] = 100
    inputs["features"].loc[inputs["features"]["unit_code"] == "Z", "area_m2"] = 200
    inputs["adjacency"] = pd.DataFrame({"unit_a": ["X"], "unit_b": ["Z"]})
    brief["entries"] = [{"brand_id": "A", "brand_name": "가상 A", "commission_rate": 0.2}]
    setup = tools._plan_setup(brief, _params(tools, brief))
    assert not setup["placed_entries"]
    unplaced = setup["unplaced"][0]
    assert unplaced["requires_incumbent_relocation"]
    assert unplaced["relocation_unit_codes"] == ["Z"]
    assert "계약 만료 이웃 매장(Z)" in unplaced["reason"]


def test_expiring_without_leases_fails_closed_but_all_is_explicit(setup_tools, brief, monkeypatch):
    tools, _ = setup_tools
    monkeypatch.setattr(tools, "_leases", lambda: None)
    params = _params(tools, brief)
    closed = tools._plan_setup(brief, params)
    assert not closed["constraints"].is_movable(closed["infos"]["INC"])
    assert closed["movable_count"] == 0
    assert any("기존 브랜드는 이동하지 않아요" in note for note in closed["caveats"])
    opened = tools._plan_setup(brief, {**params, "movable": "all"})
    assert opened["constraints"].is_movable(opened["infos"]["INC"])


def test_lease_cutoff_is_exclusive_and_shared_by_plan(setup_tools, brief, monkeypatch):
    tools, _ = setup_tools
    leases = pd.DataFrame(
        [
            {"brand_id": b, "unit_code": u, "lease_end": end}
            for b, u, end in [
                ("INC", "Z", "2026-10-01"),
                ("A", "X", "2026-09-30"),
                ("B", "Y", "2026-10-02"),
            ]
        ]
    )
    monkeypatch.setattr(tools, "_leases", lambda: leases)
    assert tools._expiring_pairs("2026-10-01") == {("A", "X")}
    setup = tools._plan_setup(brief, _params(tools, brief))
    assert setup["lease_cutoff"] == "2026-10-01" and setup["movable_count"] == 0
    assert not setup["constraints"].is_movable(setup["infos"]["INC"])


@pytest.mark.parametrize("data_end", ["2026-03-10", "2026-03-31"])
def test_season_factors_ignore_transactions_after_as_of(monkeypatch, data_end):
    tools = RelayoutTools()
    frame = pd.DataFrame(
        {"date": pd.date_range("2026-01-01", data_end), "category": "CAFE", "net_sales_krw": 100.0}
    )
    monkeypatch.setattr(tools, "_enriched", lambda: frame, raising=False)
    monkeypatch.setattr(
        tools, "_data_range", lambda: (frame["date"].min().date(), frame["date"].max().date()), raising=False
    )
    monkeypatch.setattr(tools, "_memo", lambda key, compute: compute(), raising=False)
    monkeypatch.setattr(tools, "_data_key", lambda: "current", raising=False)
    before = tools._season_factors("FW", "2026-03-31")
    frame.loc[len(frame)] = [pd.Timestamp("2026-12-01"), "CAFE", 1_000_000.0]
    assert tools._season_factors("FW", "2026-03-31") == before
    assert tools._season_factors("FW") != before


def test_brief_cutoff_default_and_override(brief):
    tools = RelayoutTools()
    assert tools._brief_as_of(brief) == "2026-09-30"
    assert tools._brief_as_of({**brief, "data_cutoff": "2026-03-31"}) == "2026-03-31"
    assert tools._brief_as_of(brief, "2026-02-28") == "2026-02-28"
    with pytest.raises(ToolError, match="as_of"):
        tools._brief_as_of(brief, "tomorrow")


def test_layout_input_cutoff_is_forwarded_but_old_helper_is_supported(monkeypatch):
    tools = RelayoutTools()
    calls = []

    def newer(rev, as_of=None):
        calls.append((rev, as_of))
        return {"as_of": as_of}

    monkeypatch.setattr(tools, "_layout_inputs", newer, raising=False)
    assert tools._relayout_inputs("R2", "2026-03-31") == {"as_of": "2026-03-31"}
    assert calls == [("R2", "2026-03-31")]
    monkeypatch.setattr(tools, "_layout_inputs", lambda rev: {"rev": rev})
    assert tools._relayout_inputs("R2", "2026-03-31") == {"rev": "R2"}


def test_phasing_discloses_fallback_and_explicit_selection(decision_tools, brief, monkeypatch):
    tools = decision_tools
    model = SpatialModel(Revision("R2", effective_date="2025-01-01"), floors=[Floor("1F", 0)])
    monkeypatch.setattr(tools, "_model", lambda _: model, raising=False)
    monkeypatch.setattr(tools, "_has_sales", lambda _: True, raising=False)
    monkeypatch.setattr(tools, "revision_period", lambda _: ("2025-01-01", "2026-09-30", 638), raising=False)
    monkeypatch.setattr(
        tools,
        "productivity_frame",
        lambda *args, **kwargs: pd.DataFrame(columns=["unit_code", "sales"]),
        raising=False,
    )
    fallback = tools.relayout_phasing(brief["brief_id"])
    assert fallback["alt_id"] == "ALT-2" and fallback["basis"] == "best_net_fallback"
    assert "사람이 결정한 대안은 아니에요" in fallback["basis_note"]
    assert tools._project(brief["brief_id"])["selected_alt_id"] is None
    explicit = tools.relayout_phasing(brief["brief_id"], alt_id="ALT-1")
    assert explicit["basis"] == "selected" and "요청에 지정한" in explicit["basis_note"]
    tools.relayout_select(brief["brief_id"], "ALT-1", "MD팀")
    selected = tools.relayout_phasing(brief["brief_id"])
    assert selected["basis"] == "selected" and "사람이 선택한" in selected["basis_note"]


def test_auto_check_explains_no_parent_no_brief_and_no_selection(decision_tools, brief, monkeypatch):
    tools = decision_tools
    model = SpatialModel(Revision("R3", effective_date=brief["effective_date"]))
    monkeypatch.setattr(tools, "_model", lambda _: model, raising=False)
    monkeypatch.setattr(tools, "_brief_ids", lambda: [brief["brief_id"]])
    assert "부모 개정" in tools.relayout_after_upload("R3", None)["reason"]
    assert "기준 개정" in tools.relayout_after_upload("R3", "R0")["reason"]
    result = tools.relayout_after_upload("R3", "R2")
    assert result["skipped"] and "선택한 대안이 없어요" in result["reason"]
    model.revision.effective_date = "2026-11-01"
    assert "적용일" in tools.relayout_after_upload("R3", "R2")["reason"]


def test_financial_horizon_fields_and_replacement_history(setup_tools, brief, monkeypatch):
    tools, _ = setup_tools
    project = tools.relayout_select(brief["brief_id"], "ALT-2", "MD팀", decision_source="api")
    before_history = copy.deepcopy(project["decision_history"])
    old_snapshot = copy.deepcopy(project["decision_snapshot"])
    monkeypatch.setattr(tools, "_save_alternative", lambda *args: None)
    monkeypatch.setattr(tools, "_remove_stale_alternatives", lambda *args: None)
    alternate = SimpleNamespace(
        alt_id="ALT-1",
        assignment={"X": "INC", "Y": None, "Z": None},
        moves=[],
        move_cost_krw=100.0,
        sales_low=500.0,
        sales_high=3000.0,
        search_status="heuristic",
        objective_rounding_krw=None,
        net_upper_bound_krw=math.nan,
        gap_krw=math.nan,
        bound_krw=math.nan,
        explanations=[],
    )
    monkeypatch.setattr("cad2anything.layout.generate_alternatives", lambda *args, **kwargs: [alternate])
    result = tools.relayout_plan(brief["brief_id"], max_moves=1, replace_decision=True)
    horizon = result["season_horizon"]
    assert horizon["days"] == (date(2027, 2, 28) - date(2026, 10, 1)).days + 1
    kpi = result["alternatives"][1]["kpi"]
    assert kpi["annual_uplift_krw"] > 0
    assert kpi["one_off_loss_krw"] == 100
    assert kpi["net_krw"] == pytest.approx(kpi["annual_uplift_krw"] - 100)
    assert kpi["season_net_krw"] == pytest.approx(kpi["annual_uplift_krw"] * horizon["days"] / 365 - 100)
    assert "첫 시즌 기준 순효과" in result["alternatives"][1]["summary"]
    assert "순효과는 연" not in result["alternatives"][1]["summary"]
    assert result["as_of"] == result["params"]["as_of"] == "2026-09-30"
    page = tools.relayout_report_html(brief["brief_id"])
    for label in ("첫 시즌 기준 순효과", "연간 증가 효과", "1회성 공사 손실", "투자 회수 기간"):
        assert label in page
    assert "모든 금액은 연 단위" not in page
    current = tools._project(brief["brief_id"])
    assert current["selected_alt_id"] is None and current["decision_snapshot"] is None
    assert current["decision_history"][:-1] == before_history
    replaced = current["decision_history"][-1]
    assert replaced["kpi_snapshot"] == old_snapshot["kpi"] and replaced["replaced_at"]
