"""Seasonal relayout workflow: metadata generation, brief validation, engine extensions, tools and REST."""

import copy

import pytest
from fastapi.testclient import TestClient
from shapely import affinity

from cad2anything.layout import LayoutConstraints, check_constraints, generate_alternatives
from cad2anything.mockgen.relayout import BRIEF_ID, EFFECTIVE_DATE, build_leases, ensure_relayout_metadata
from cad2anything.model import Revision, RevisionKind
from cad2anything.services.atlas_api import create_api
from cad2anything.services.errors import NotFoundToolError
from cad2anything.services.tools import ToolError, Toolkit


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> Toolkit:
    ws = populate(tmp_path_factory.mktemp("relayout-ws"))
    toolkit = Toolkit(ws, use_postgis=False)
    summary = ensure_relayout_metadata(ws, toolkit=toolkit)
    assert summary["written"] and summary["entries"] and summary["exits"]
    return toolkit


@pytest.fixture(scope="module")
def plan(toolkit) -> dict:
    return toolkit.relayout_plan(BRIEF_ID)


def _raw_brief(toolkit) -> dict:
    return copy.deepcopy(toolkit._raw_brief(BRIEF_ID))


def test_generated_leases_and_brief(toolkit):
    leases = toolkit._leases()
    model = toolkit._model("R2")
    occupied = {u.attributes["unit_code"] for u in model.units() if u.attributes.get("brand_id")}
    assert set(leases["unit_code"]) == occupied and leases["lease_id"].is_unique
    assert (leases["lease_end"] >= leases["lease_start"]).all()
    brief = toolkit._brief(BRIEF_ID)
    exits = {e["brand_id"] for e in brief["exits"]}
    expiring = set(leases.loc[leases["lease_end"] < EFFECTIVE_DATE, "brand_id"])
    assert exits <= expiring
    assert {e["unit_code"].split("-")[0] for e in brief["exits"]} <= set(brief["floors"])
    placed = toolkit._placements("R2")
    assert all(e["brand_id"] not in placed for e in brief["entries"])
    _, planted = build_leases(model, toolkit._table("tenancies"), toolkit._table("brands"))
    assert {p["type"] for p in planted} == {"area_mismatch", "brand_mismatch"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda b: b.pop("base_rev"), "base_rev"),
        (lambda b: b.update(season="winter-ish"), "season"),
        (lambda b: b.update(effective_date="2025-01-01"), "effective_date"),
        (lambda b: b.update(floors=["9F"]), "층"),
        (lambda b: b.update(objective="profit"), "objective"),
    ],
)
def test_brief_validation_errors(toolkit, change, message):
    raw = _raw_brief(toolkit)
    change(raw)
    _, errors, _ = toolkit.relayout_validate_brief(raw)
    assert errors and any(message in e for e in errors)


def test_brief_validation_brands(toolkit):
    raw = _raw_brief(toolkit)
    placed_brand = next(iter(toolkit._placements("R2")))
    raw["entries"].append({"brand_id": placed_brand})
    _, errors, _ = toolkit.relayout_validate_brief(raw)
    assert any("이미" in e for e in errors)
    raw = _raw_brief(toolkit)
    raw["entries"] = [{"brand_name": "Brand New Label", "category": "아웃도어", "target_area_m2": [120, 200]}]
    brief, errors, _ = toolkit.relayout_validate_brief(raw)
    assert not errors and brief["entries"][0]["new_brand"] and brief["entries"][0]["preferred_floors"]
    raw = _raw_brief(toolkit)
    raw["exits"] = [{"brand_id": "BR9999"}]
    _, errors, _ = toolkit.relayout_validate_brief(raw)
    assert errors
    with pytest.raises(NotFoundToolError):
        toolkit.relayout_project("NOPE")


def test_engine_weights_free_brands_and_move_costs(toolkit):
    inputs = toolkit._layout_inputs("R2")
    base = dict(inputs["assignment"])
    floor_units = [u for u in toolkit._model("R2").units("4F")]
    vacated = next(u for u in floor_units if u.attributes.get("brand_id"))
    code = vacated.attributes["unit_code"]
    newcomer = next(
        b
        for b, row in toolkit._catalog().set_index("brand_id").iterrows()
        if b not in toolkit._placements("R2") and "4F" in str(row["preferred_floors"])
    )
    base[code] = newcomer
    constraints = LayoutConstraints(floors=["4F"], max_moves=2, area_tolerance=0.5)
    common = (inputs["features"], inputs["brands"], inputs["adjacency"], inputs["model"], constraints, 1)
    plain = generate_alternatives(base, *common, max_iter=300)
    costly = generate_alternatives(
        base, *common, max_iter=300, move_cost={b: 1e15 for b in base.values() if b}, free=[newcomer]
    )
    assert plain and plain[0].move_cost_krw == 0 and plain[0].net_delta == plain[0].delta
    for alternative in costly:
        assert not alternative.moves and alternative.move_cost_krw == 0
    weighted = generate_alternatives(
        base, *common, max_iter=300, weights={b: 2.0 for b in base.values() if b}, free=[newcomer]
    )
    if weighted:
        assert weighted[0].objective_before == pytest.approx(2 * weighted[0].sales_before, rel=1e-6)
        assert len(weighted[0].moves) <= 2
        checks = check_constraints(
            base,
            weighted[0].assignment,
            inputs["features"],
            inputs["brands"],
            inputs["adjacency"],
            constraints,
            [newcomer],
        )
        assert checks["satisfied"].all()


def test_diagnose(toolkit):
    diagnosis = toolkit.relayout_diagnose(BRIEF_ID)
    brief = toolkit._brief(BRIEF_ID)
    assert diagnosis["season"]["code"] == "FW" and diagnosis["season"]["last"]["start"] == "2025-10-01"
    assert [f["floor"] for f in diagnosis["floors"]] == brief["floors"]
    assert diagnosis["season_factors"] and all(f["factor"] > 0 for f in diagnosis["season_factors"])
    candidates = {c["brand_id"] for c in diagnosis["candidates"]}
    assert {e["brand_id"] for e in brief["exits"]} <= candidates
    assert all(e["feasible_units"] >= 1 for e in diagnosis["entries"])
    assert toolkit._project(BRIEF_ID)["status"] in ("diagnosed", "planned", "selected", "checked")


def test_plan_alternatives(toolkit, plan):
    brief = toolkit._brief(BRIEF_ID)
    assert plan["alternatives"][0]["alt_id"] == "ALT-0"
    base_alt = plan["alternatives"][0]
    types = {c["type"] for c in base_alt["changes"]}
    assert types == {"entry", "exit"} and base_alt["kpi"]["net_krw"] == 0
    assert base_alt["kpi"]["sales_low_krw"] == base_alt["kpi"]["sales_high_krw"] == 0
    leases = toolkit._leases()
    expiring = set(leases.loc[leases["lease_end"] < brief["effective_date"], "brand_id"])
    for alternative in plan["alternatives"]:
        assert all(c["ok"] for c in alternative["constraints"]), alternative["constraints"]
        moves = [c for c in alternative["changes"] if c["type"] == "move"]
        assert len(moves) <= brief["max_moves"]
        assert {m["brand_id"] for m in moves} <= expiring
        model = toolkit._model(alternative["rev_id"])
        assert model.revision.kind == RevisionKind.HYPOTHETICAL
        assert toolkit.ws.model_meta(alternative["rev_id"])["origin"] == "relayout"
        for change in alternative["changes"]:
            if change["type"] == "entry":
                assert model.unit_by_code(change["to_unit"]).attributes["brand_id"] == change["brand_id"]
        if alternative["alt_id"] != "ALT-0":
            assert alternative["kpi"]["net_krw"] > 0
    assert len(plan["alternatives"]) >= 1


def test_select_check_and_report(toolkit, plan, monkeypatch):
    chosen = plan["alternatives"][-1]
    with pytest.raises(ToolError):
        toolkit.relayout_select(BRIEF_ID, "ALT-9")
    project = toolkit.relayout_select(BRIEF_ID, chosen["alt_id"], "MD팀")
    assert project["status"] == "selected" and project["decided_by"] == "MD팀"
    drawn = copy.deepcopy(toolkit._model(chosen["rev_id"]))
    drawn.revision = Revision("R3", RevisionKind.ACTUAL, "R2", "2026-10-01", source="test")
    entry = next(c for c in chosen["changes"] if c["type"] == "entry")
    planned = drawn.unit_by_code(entry["to_unit"])
    neighbor = next(
        u
        for u in drawn.units(planned.floor)
        if u.attributes.get("brand_id") and u.entity_id != planned.entity_id
    )
    keys = ("brand_id", "brand_name", "category", "category_name", "vacant")
    a = {k: planned.attributes.get(k) for k in keys}
    b = {k: neighbor.attributes.get(k) for k in keys}
    planned.attributes.update(b)
    neighbor.attributes.update(a)
    resized = next(
        u for u in drawn.units(planned.floor) if u.entity_id not in (planned.entity_id, neighbor.entity_id)
    )
    resized.geometry = affinity.scale(resized.geometry, 0.96, 0.96)
    toolkit.ws.save_model(drawn, {"source": "uploaded", "parent": "R2"})
    assert (
        not toolkit._has_sales("R3")
        and toolkit.latest_sales_rev() == "R2"
        and toolkit._fit_revision("R3") == "R2"
    )
    vocabulary = toolkit._vocabulary  # same geometry as R2: reuse its vocabulary to keep the test fast
    monkeypatch.setattr(toolkit, "_vocabulary", lambda rev: vocabulary("R2" if rev == "R3" else rev))
    table = toolkit.unit_table("R3")
    assert "expected_sales_krw" in table and table.attrs["estimated"] and "sales_krw" not in table
    check = toolkit.relayout_check_drawing(BRIEF_ID, "R3")
    statuses = {i["brand_id"]: i["status"] for i in check["items"]}
    assert statuses[entry["brand_id"]] == "different_unit"
    assert check["conformance_pct"] < 100
    assert any(a["unit_code"] == resized.attributes["unit_code"] for a in check["area_changes"])
    hook = toolkit.relayout_after_upload("R3", "R2")
    assert hook["brief_id"] == BRIEF_ID and hook["alt_id"] == chosen["alt_id"]
    assert toolkit.relayout_after_upload("R3", "R0") is None
    redrawn = copy.deepcopy(drawn)
    redrawn.revision = Revision("R2X", RevisionKind.ACTUAL, "R2", "2026-11-01", source="test")
    toolkit.ws.save_model(redrawn, {"source": "uploaded", "parent": "R2"})
    assert (
        toolkit.relayout_after_upload("R2X", "R2") is None
    )  # other effective date: not the relayout drawing
    toolkit.ws.delete_file("models/R2X.json")
    with pytest.raises(ToolError):
        toolkit.relayout_check_drawing(BRIEF_ID, "R2")
    report = toolkit.relayout_report(BRIEF_ID)
    assert report["url"] == f"/api/relayout/{BRIEF_ID}/report" and "도면 대조" in report["sections"]
    html = toolkit.relayout_report_html(BRIEF_ID)
    assert "<svg" in html and "가정과 한계" in html and chosen["alt_id"] in html
    toolkit.ws.delete_file("models/R3.json")


def test_entry_options(toolkit):
    placed = toolkit._placements("R2")
    catalog = toolkit._catalog()
    outdoor = next(b for b in catalog.loc[catalog["category"] == "OUTDOOR", "brand_id"] if b not in placed)
    result = toolkit.relayout_entry_options(brand=outdoor, season="FW")
    values = [o["expected_sales_krw"] for o in result["options"]]
    assert values == sorted(values, reverse=True) and result["season_factor"]
    assert all(o["status"] in ("공실", "계약 만료 예정") for o in result["options"])
    assert all(o["low_krw"] <= o["expected_sales_krw"] <= o["high_krw"] for o in result["options"])
    by_category = toolkit.relayout_entry_options(category="카페", floors=["8F"], include_expiring=True, top=3)
    assert len(by_category["options"]) <= 3 and all(o["floor"] == "8F" for o in by_category["options"])
    with pytest.raises(ToolError):
        toolkit.relayout_entry_options(brand=next(iter(placed)))
    with pytest.raises(ToolError):
        toolkit.relayout_entry_options()


def test_unit_options(toolkit):
    brief = toolkit._brief(BRIEF_ID)
    leaving = brief["exits"][0]
    placed = toolkit._placements("R2")
    result = toolkit.relayout_unit_options(leaving["unit_code"], season="FW", top=4)
    unit = result["unit"]
    assert unit["unit_code"] == leaving["unit_code"] and unit["status"] == "입점"
    assert unit["current_brand"] == leaving["brand_name"] and unit["current_expected_krw"] > 0
    values = [o["objective_krw"] for o in result["options"]]
    assert 1 <= len(values) <= 4 and values == sorted(values, reverse=True)
    assert [o["rank"] for o in result["options"]] == list(range(1, len(values) + 1))
    for option in result["options"]:
        assert option["low_krw"] <= option["expected_sales_krw"] <= option["high_krw"]
        assert option["season_factor"] is not None and "vs_current_pct" in option
        assert all(b["brand_id"] not in placed for b in option["brands"])
    commission = toolkit.relayout_unit_options(leaving["unit_code"], objective="commission")
    assert commission["objective"] == "commission" and commission["season"] is None
    vacant = next(
        (u.attributes["unit_code"] for u in toolkit._model("R2").units() if u.attributes.get("vacant")), None
    )
    if vacant:
        empty = toolkit.relayout_unit_options(vacant)
        assert empty["unit"]["status"] == "공실" and empty["unit"]["current_brand"] is None
        assert all("vs_current_pct" not in o for o in empty["options"])
    for kwargs in ({"unit_code": "9F-Z99"}, {"objective": "profit"}, {"season": "winter-ish"}):
        with pytest.raises(ToolError):
            toolkit.relayout_unit_options(**{"unit_code": leaving["unit_code"], **kwargs})


def _assert_valid_phasing(toolkit, alternative, result):
    weeks, crews = result["params"]["fitout_weeks"], result["params"]["crews"]
    steps = {s["step"]: s for s in result["steps"]}
    works = [c for c in alternative["changes"] if c["type"] in ("move", "entry")]
    jobs = {s["brand_id"]: s for s in result["steps"] if s["action"] in ("move", "entry")}
    assert set(jobs) == {c["brand_id"] for c in works}
    assert sum(s["action"] == "exit" for s in result["steps"]) == sum(
        c["type"] == "exit" for c in alternative["changes"]
    )
    assert sorted(steps) == list(range(1, len(steps) + 1))
    for job in jobs.values():
        assert job["end_week"] - job["start_week"] == weeks
        assert all(steps[n]["end_week"] <= job["start_week"] for n in job["waits_for"])
    for week in range(int(result["total_weeks"])):
        assert sum(j["start_week"] <= week < j["end_week"] for j in jobs.values()) <= crews
    leaves = {s["brand_id"]: s for s in result["steps"] if s["action"] in ("temporary", "close")}
    for brand_id, leave in leaves.items():
        assert leave["end_week"] == jobs[brand_id]["end_week"]
    occupant = {u: b for b, u in toolkit._placements(result["base_rev"]).items()}
    for change in works:
        brand = occupant.get(change["to_unit"])
        if brand in jobs and brand != change["brand_id"]:
            vacated = leaves[brand]["start_week"] if brand in leaves else jobs[brand]["end_week"]
            assert vacated <= jobs[change["brand_id"]]["start_week"]
    assert result["total_weeks"] == max((j["end_week"] for j in jobs.values()), default=0)
    assert result["lost_krw"] == pytest.approx(sum(s["lost_krw"] for s in result["steps"]))
    assert result["saved_krw"] == pytest.approx(result["naive_lost_krw"] - result["lost_krw"])
    assert {e["brand_id"] for e in result["entries"]} == {
        c["brand_id"] for c in works if c["type"] == "entry"
    }


def test_phasing_of_plan(toolkit, plan):
    result = toolkit.relayout_phasing(BRIEF_ID, crews=2)
    project = toolkit._project(BRIEF_ID)
    alternative = next(a for a in project["plan"]["alternatives"] if a["alt_id"] == result["alt_id"])
    if project.get("selected_alt_id"):
        assert result["alt_id"] == project["selected_alt_id"]
    _assert_valid_phasing(toolkit, alternative, result)
    assert project["phasing"]["alt_id"] == result["alt_id"] and result["summary"]
    wider = toolkit.relayout_phasing(BRIEF_ID, alt_id=result["alt_id"], crews=6)
    assert wider["total_weeks"] <= result["total_weeks"]
    assert wider["entry_delay_krw"] <= result["entry_delay_krw"] + 1
    assert "공사 순서" in toolkit.relayout_report(BRIEF_ID)["sections"]
    assert "공사 순서(" in toolkit.relayout_report_html(BRIEF_ID)
    for kwargs in ({"crews": 0}, {"fitout_weeks": 20}, {"temporary_sales_ratio": 2}, {"alt_id": "ALT-8"}):
        with pytest.raises(ToolError):
            toolkit.relayout_phasing(BRIEF_ID, **kwargs)


def test_phasing_resolves_swaps(toolkit, plan):
    saved = copy.deepcopy(toolkit._project(BRIEF_ID))
    brief = toolkit._brief(BRIEF_ID)
    floor = brief["floors"][0]
    units = [
        u
        for u in toolkit._model(brief["base_rev"]).units(floor)
        if u.attributes.get("brand_id") and not u.attributes.get("vacant")
    ]
    first, second, third = units[:3]

    def change(kind, unit, target=None):
        return {
            "type": kind,
            "brand_id": unit.attributes["brand_id"],
            "brand_name": unit.attributes.get("brand_name") or unit.attributes["brand_id"],
            "category_name": "",
            "from_unit": unit.attributes["unit_code"],
            "to_unit": target.attributes["unit_code"] if target else None,
            "from_floor": floor,
            "to_floor": floor if target else None,
            "delta_krw": 0.0,
            "explanation": "",
        }

    swap = {
        "alt_id": "ALT-9",
        "rev_id": "ALT9",
        "summary": "",
        "kpi": {"net_krw": 0.0},
        "changes": [change("exit", third), change("move", first, second), change("move", second, first)],
        "constraints": [],
        "explanations": [],
    }
    try:
        project = toolkit._project(BRIEF_ID)
        project["plan"]["alternatives"].append(swap)
        toolkit._save_project(project)
        closed = toolkit.relayout_phasing(BRIEF_ID, alt_id="9", crews=1, fitout_weeks=3, use_temporary=False)
        _assert_valid_phasing(toolkit, swap, closed)
        assert len(closed["cycles"]) == 1 and closed["cycles"][0]["resolved_by"] == "close"
        stop = next(s for s in closed["steps"] if s["action"] == "close")
        assert stop["closed_weeks"] == 6 and stop["lost_krw"] > 0 and closed["total_weeks"] == 6
        assert sum(bool(s["waits_for"]) for s in closed["steps"]) == 1
        assert closed["naive_lost_krw"] >= closed["lost_krw"]
        moved = toolkit.relayout_phasing(BRIEF_ID, alt_id="ALT-9", crews=1, fitout_weeks=3)
        _assert_valid_phasing(toolkit, swap, moved)
        stay = next(s for s in moved["steps"] if s["action"] == "temporary")
        assert stay["brand_id"] == stop["brand_id"] and stay["temporary_unit"] and stay["closed_weeks"] == 0
        assert moved["lost_krw"] == pytest.approx(stop["lost_krw"] * 0.4)
        assert moved["cycles"][0]["unit_code"] == stay["temporary_unit"]
        selected = saved.get("selected_alt_id")
        if selected:
            toolkit.relayout_select(BRIEF_ID, "ALT-9", "MD팀")
            assert toolkit._project(BRIEF_ID)["phasing"]["alt_id"] == "ALT-9"
            toolkit.relayout_select(BRIEF_ID, selected, "MD팀")
            assert toolkit._project(BRIEF_ID)["phasing"] is None
    finally:
        toolkit._save_project(saved)


def test_relayout_rest(toolkit):
    client = TestClient(create_api(toolkit))
    meta = client.get("/api/meta").json()
    assert meta["data_through"] == "2026-09-30"
    assert {r["rev_id"]: r["has_sales"] for r in meta["revisions"]}["R2"] is True
    briefs = client.get("/api/relayout/briefs").json()
    assert briefs[0]["brief_id"] == BRIEF_ID and briefs[0]["entries"] >= 1
    assert client.get("/api/relayout/NOPE").status_code == 404
    assert client.post("/api/relayout/NOPE/diagnose").status_code == 404
    diagnosis = client.post(f"/api/relayout/{BRIEF_ID}/diagnose").json()
    assert diagnosis["season"]["code"] == "FW"
    assert client.post(f"/api/relayout/{BRIEF_ID}/plan", json={"objective": "profit"}).status_code == 422
    plan = client.post(
        f"/api/relayout/{BRIEF_ID}/plan", json={"objective": "sales", "n_alternatives": 1}
    ).json()
    assert plan["params"]["objective"] == "sales" and plan["alternatives"][0]["alt_id"] == "ALT-0"
    assert client.post(f"/api/relayout/{BRIEF_ID}/check", json={"rev": "R2"}).status_code == 400
    selected = client.post(f"/api/relayout/{BRIEF_ID}/select", json={"alt_id": "ALT-0", "decided_by": "MD팀"})
    assert selected.status_code == 200 and selected.json()["selected_alt_id"] == "ALT-0"
    assert client.post(f"/api/relayout/{BRIEF_ID}/phasing", json={"crews": 9}).status_code == 422
    assert client.post("/api/relayout/NOPE/phasing").status_code == 404
    phasing = client.post(f"/api/relayout/{BRIEF_ID}/phasing", json={"crews": 3, "fitout_weeks": 2})
    assert phasing.status_code == 200
    body = phasing.json()
    assert body["alt_id"] == "ALT-0" and body["lost_krw"] == 0 and not body["cycles"]
    assert body["entries"] and body["params"] == {
        "crews": 3,
        "fitout_weeks": 2,
        "temporary_sales_ratio": 0.6,
        "use_temporary": True,
    }
    project = client.get(f"/api/relayout/{BRIEF_ID}").json()
    assert project["brief"]["brief_id"] == BRIEF_ID and project["plan"]["alternatives"]
    assert project["phasing"]["alt_id"] == "ALT-0"
    report = client.get(f"/api/relayout/{BRIEF_ID}/report")
    assert report.status_code == 200 and report.headers["content-type"].startswith("text/html")


def test_replan_removes_stale_revisions(toolkit, plan):
    smaller = toolkit.relayout_plan(BRIEF_ID, n_alternatives=1, max_moves=2)
    ids = {a["rev_id"] for a in smaller["alternatives"]}
    stale = {a["rev_id"] for a in plan["alternatives"]} - ids
    for rev_id in stale:
        assert not toolkit.ws.has_model(rev_id)
        assert rev_id not in {r.rev_id for r in toolkit.ws.revisions()}
    assert all(sum(1 for c in a["changes"] if c["type"] == "move") <= 2 for a in smaller["alternatives"])
    assert toolkit._project(BRIEF_ID)["selected_alt_id"] is None
