import math

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from cad2anything.catalog import CATEGORIES
from cad2anything.generators import load_specs, plan_impact
from cad2anything.layout import (
    LayoutConstraints,
    check_constraints,
    fit_scoring_model,
    generate_alternatives,
    score_assignment,
    to_hypothetical_revision,
)
from cad2anything.model import (
    ChangeSet,
    ChangeType,
    Entity,
    EntityKind,
    Floor,
    Revision,
    RevisionKind,
    SpatialModel,
)
from cad2anything.spatial import adjacency_pairs, unit_features

GAMMA = 0.6
FLOORS = ("3F", "4F")
UNIT_BOXES = {
    "S01": (0, 0, 12, 8),
    "S02": (12, 0, 24, 8),
    "S03": (24, 0, 36, 8),
    "S04": (36, 0, 48, 8),
    "C01": (4, 12, 14, 20),
    "C02": (14, 12, 22, 20),
    "C03": (26, 12, 34, 20),
    "C04": (34, 12, 44, 20),
    "N01": (0, 24, 12, 32),
    "N02": (12, 24, 24, 32),
    "N03": (24, 24, 36, 32),
    "N04": (36, 24, 48, 32),
}
# brand_id, category, strength, anchor, target min/max (m2), preferred floors. Strengths are balanced across
# exposure tiers (so the cross-section can identify the exposure elasticity) but not sorted by exposure.
BRANDS = [
    ("B301", "WOMEN_CONTEMPORARY", 1.30, False, 60, 100, "3F"),
    ("B302", "CAFE", 0.80, False, 50, 90, "3F;5F"),
    ("B303", "ACCESSORIES", 1.10, False, 50, 90, "3F;4F"),
    ("B304", "WOMEN_CONTEMPORARY", 1.50, False, 60, 100, "3F"),
    ("B305", "WOMEN_CONTEMPORARY", 0.70, False, 60, 100, "3F"),
    ("B306", "WOMEN_CONTEMPORARY", 1.20, False, 60, 100, "3F"),
    ("B307", "ACCESSORIES", 0.70, False, 40, 60, "3F;4F"),
    ("B308", "WOMEN_CONTEMPORARY", 1.00, True, 60, 100, "3F"),
    ("B309", "WOMEN_CONTEMPORARY", 0.65, False, 60, 100, "3F"),
    ("B310", "WOMEN_CONTEMPORARY", 1.40, False, 90, 120, "3F"),
    ("B311", "ACCESSORIES", 0.90, False, 50, 90, "3F;4F"),
    ("B401", "WOMEN_CASUAL", 0.90, False, 50, 100, "4F"),
    ("B402", "LINGERIE", 1.00, False, 50, 90, "4F"),
    ("B403", "WOMEN_CASUAL", 1.40, False, 50, 100, "4F"),
    ("B404", "ACCESSORIES", 0.60, False, 50, 90, "3F;4F"),
    ("B405", "LINGERIE", 1.50, False, 50, 90, "4F"),
    ("B406", "WOMEN_CASUAL", 0.80, False, 50, 100, "4F"),
    ("B407", "WOMEN_CASUAL", 0.60, False, 50, 100, "4F"),
    ("B408", "WOMEN_CASUAL", 1.00, True, 50, 100, "4F"),
    ("B409", "LINGERIE", 1.30, False, 50, 90, "4F"),
    ("B410", "ACCESSORIES", 0.75, False, 50, 90, "3F;4F"),
    ("B411", "WOMEN_CASUAL", 1.20, False, 50, 100, "4F"),
]
CONFLICTS = {"ACCESSORIES": "CAFE", "CAFE": "LINGERIE;JEWELRY_WATCH", "LINGERIE": "CAFE;KIDS;MEN_FORMAL"}
BASE = {
    "3F-C02": "B301", "3F-C03": "B302", "3F-C04": None, "3F-C01": "B303", "3F-S01": "B304", "3F-S02": "B305",
    "3F-S03": "B306", "3F-S04": "B307", "3F-N01": "B308", "3F-N02": "B309", "3F-N03": "B310", "3F-N04": "B311",
    "4F-C02": "B401", "4F-C03": "B402", "4F-C01": "B403", "4F-C04": "B404", "4F-S01": "B405", "4F-S02": "B406",
    "4F-S03": None, "4F-S04": "B407", "4F-N01": "B408", "4F-N02": "B409", "4F-N03": "B410", "4F-N04": "B411",
}  # fmt: skip


def brands_table() -> pd.DataFrame:
    rows = [
        {
            "brand_id": b,
            "brand_name": f"가상 {b}",
            "category": c,
            "price_tier": 2,
            "anchor": anchor,
            "target_area_min_m2": lo,
            "target_area_max_m2": hi,
            "preferred_floors": floors,
            "adjacency_likes": "",
            "adjacency_conflicts": CONFLICTS.get(c, ""),
            "min_frontage_m": 3.0,
            "attractiveness": strength,
        }
        for b, c, strength, anchor, lo, hi, floors in BRANDS
    ]
    return pd.DataFrame(rows)


def layout_model() -> SpatialModel:
    model = SpatialModel(
        revision=Revision("R2", effective_date="2025-09-01"),
        floors=[Floor("3F", 2, elevation_m=10.0), Floor("4F", 3, elevation_m=15.0)],
    )
    info = brands_table().set_index("brand_id")
    for floor in FLOORS:
        for key, geometry in (
            ("S", box(0, 8, 48, 12)),
            ("N", box(0, 20, 48, 24)),
            ("W", box(0, 12, 4, 20)),
            ("E", box(44, 12, 48, 20)),
        ):
            model.add(Entity(f"W-{floor}-{key}", EntityKind.WALKWAY, floor, geometry))
        model.add(
            Entity(
                f"V-{floor}-ESC-A",
                EntityKind.VERTICAL_LINK,
                floor,
                box(22, 12, 26, 20),
                attributes={"link_id": "ESC-A", "link_type": "escalator"},
            )
        )
        for suffix, bounds in UNIT_BOXES.items():
            code = f"{floor}-{suffix}"
            brand = BASE[code]
            model.add(
                Entity(
                    f"U-{code}",
                    EntityKind.UNIT,
                    floor,
                    box(*bounds),
                    label=code,
                    attributes={
                        "unit_code": code,
                        "brand_id": brand,
                        "brand_name": info.loc[brand, "brand_name"] if brand else None,
                        "category": info.loc[brand, "category"] if brand else "WOMEN_CONTEMPORARY",
                        "vacant": brand is None,
                        "zone_code": f"{floor}-Z1",
                    },
                )
            )
    return model


def planted_productivity(
    features: pd.DataFrame, brands: pd.DataFrame, strength: dict[str, float]
) -> pd.DataFrame:
    """Sales = 1e8 * strength * (area / 80) ** space_beta * ((walk + 1) / 10) ** -GAMMA."""
    feats = features.set_index("unit_code")
    category_of = brands.set_index("brand_id")["category"]
    rows = []
    for unit_code, brand in BASE.items():
        if brand is None:
            continue
        category = category_of[brand]
        area, walk = feats.loc[unit_code, "area_m2"], feats.loc[unit_code, "walk_to_escalator_m"]
        sales = (
            1e8
            * strength[brand]
            * (area / 80) ** CATEGORIES[category].space_beta
            * ((walk + 1) / 10) ** -GAMMA
        )
        rows.append(
            {
                "unit_code": unit_code,
                "brand_id": brand,
                "category": category,
                "floor": unit_code[:2],
                "sales": sales,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def setup():
    model = layout_model()
    brands = brands_table()
    features = unit_features(model)
    pairs = adjacency_pairs(model)
    productivity = planted_productivity(
        features, brands, brands.set_index("brand_id")["attractiveness"].to_dict()
    )
    params = fit_scoring_model(productivity, features, brands)
    return model, brands, features, pairs, productivity, params


def test_fit_scoring_model_recovers_planted_elasticities(setup):
    _, brands, features, _, productivity, params = setup
    equal = planted_productivity(features, brands, dict.fromkeys(brands["brand_id"], 1.0))

    clean = fit_scoring_model(equal, features, brands)

    assert clean.gamma == pytest.approx(GAMMA, abs=0.05)
    assert clean.beta_data["WOMEN_CONTEMPORARY"] == pytest.approx(
        CATEGORIES["WOMEN_CONTEMPORARY"].space_beta, abs=0.05
    )
    assert params.gamma > 0.3  # brand strengths are balanced across exposure tiers
    assert params.n_obs == len(productivity) == 22 and params.label == "추정치"
    n_wc = int((productivity["category"] == "WOMEN_CONTEMPORARY").sum())
    weight = params.beta_weight["WOMEN_CONTEMPORARY"]
    assert n_wc == 7 and 0.0 < weight < clean.beta_weight["WOMEN_CONTEMPORARY"]  # noisy slope, less weight
    assert params.beta["WOMEN_CONTEMPORARY"] == pytest.approx(
        np.clip(
            weight * params.beta_data["WOMEN_CONTEMPORARY"]
            + (1 - weight) * CATEGORIES["WOMEN_CONTEMPORARY"].space_beta,
            0.0,
            1.0,
        )
    )
    assert clean.beta_weight["WOMEN_CONTEMPORARY"] > 0.9
    assert params.beta_weight["CAFE"] == 0.0 and params.beta["CAFE"] == CATEGORIES["CAFE"].space_beta
    assert all(0.0 <= value <= 1.0 for value in params.beta.values())
    assert params.reference_assignment["3F-C04"] is None and params.reference_assignment["3F-C02"] == "B301"


def test_score_assignment_reproduces_observed_and_scores_moves(setup):
    _, brands, features, pairs, productivity, params = setup
    observed = productivity.set_index("unit_code")["sales"]

    base = score_assignment(BASE, features, brands, params, adjacency_pairs=pairs)

    units = base.units.set_index("unit_code")
    assert base.total == pytest.approx(observed.sum(), rel=1e-9)
    assert (units["half_width_log"] == 0).all() and base.total_low == pytest.approx(base.total)
    assert base.label == "추정치" and any("인과" in c for c in base.caveats)

    moved = dict(BASE, **{"3F-S01": None, "3F-C04": "B304"})
    score = score_assignment(moved, features, brands, params, adjacency_pairs=pairs).units.set_index(
        "unit_code"
    )
    feats = features.set_index("unit_code")
    beta = params.beta["WOMEN_CONTEMPORARY"]
    ratio = (feats.loc["3F-C04", "area_m2"] / feats.loc["3F-S01", "area_m2"]) ** beta * (
        (feats.loc["3F-C04", "walk_to_escalator_m"] + 1) / (feats.loc["3F-S01", "walk_to_escalator_m"] + 1)
    ) ** -params.gamma
    assert score.loc["3F-C04", "expected_sales"] == pytest.approx(observed["3F-S01"] * ratio)
    assert score.loc["3F-C04", "low"] < score.loc["3F-C04", "expected_sales"] < score.loc["3F-C04", "high"]
    assert score.loc["3F-S01", "expected_sales"] == 0


def test_affinity_bonus_is_netted_at_the_reference_layout(setup):
    _, brands, features, pairs, productivity, params = setup
    affinity = pd.DataFrame({"level": ["brand"], "item_a": ["B304"], "item_b": ["B305"], "lift": [3.0]})

    base = score_assignment(BASE, features, brands, params, affinity_pairs=affinity, adjacency_pairs=pairs)
    apart = dict(BASE, **{"3F-S02": "B306", "3F-S03": "B305"})
    split = score_assignment(apart, features, brands, params, affinity_pairs=affinity, adjacency_pairs=pairs)

    observed = productivity.set_index("unit_code")["sales"]
    assert base.total == pytest.approx(observed.sum(), rel=1e-9)  # neighbors S01-S02 keep their bonus
    s01 = split.units.set_index("unit_code").loc["3F-S01"]
    bonus = min(params.affinity_cap, params.affinity_lambda * (3.0 - 1.0))
    assert s01["affinity_bonus"] == 0.0
    assert s01["expected_sales"] == pytest.approx(observed["3F-S01"] / (1 + bonus))


def _verify_constraints(alt, brands, features, pairs, constraints):
    info = brands.set_index("brand_id")
    feats = features.set_index("unit_code")
    new_pos = {b: u for u, b in alt.assignment.items() if b is not None}
    base_pos = {b: u for u, b in BASE.items() if b is not None}
    assert sorted(new_pos) == sorted(base_pos)  # every brand placed exactly once
    assert len(new_pos) == sum(b is not None for b in alt.assignment.values())
    moved = [b for b in base_pos if new_pos[b] != base_pos[b]]
    assert 0 < len(moved) <= constraints.max_moves
    assert {m.brand_id for m in alt.moves} == set(moved)
    for brand in moved:
        row, unit = info.loc[brand], new_pos[brand]
        assert not row["anchor"], f"anchor {brand} moved"
        area = feats.loc[unit, "area_m2"]
        assert row["target_area_min_m2"] * 0.75 - 1e-9 <= area <= row["target_area_max_m2"] * 1.25 + 1e-9
        assert feats.loc[unit, "floor"] in row["preferred_floors"].split(";")
    for a, b in zip(pairs["unit_a"], pairs["unit_b"], strict=True):
        brand_a, brand_b = alt.assignment.get(a), alt.assignment.get(b)
        if brand_a is None or brand_b is None or (BASE[a] == brand_a and BASE[b] == brand_b):
            continue
        cat_a, cat_b = info.loc[brand_a, "category"], info.loc[brand_b, "category"]
        assert cat_b not in info.loc[brand_a, "adjacency_conflicts"].split(";"), (a, b)
        assert cat_a not in info.loc[brand_b, "adjacency_conflicts"].split(";"), (a, b)


def test_generate_alternatives_satisfies_constraints_and_improves(setup):
    _, brands, features, pairs, _, params = setup
    constraints = LayoutConstraints(floors=FLOORS, max_moves=4, area_tolerance=0.25)

    alternatives = generate_alternatives(
        BASE, features, brands, pairs, params, constraints, n_alternatives=3, seed=7
    )

    assert len(alternatives) == 3
    move_sets = [frozenset((m.brand_id, m.to_unit) for m in alt.moves) for alt in alternatives]
    assert len(set(move_sets)) == 3  # distinct alternatives
    for alt in alternatives:
        assert alt.objective_after > alt.objective_before
        assert alt.objective_low <= alt.objective_after <= alt.objective_high
        assert alt.constraint_checks["satisfied"].all(), alt.constraint_checks
        _verify_constraints(alt, brands, features, pairs, constraints)
        assert alt.assignment["3F-N01"] == "B308" and alt.assignment["4F-N01"] == "B408"  # anchors stay
        rescored = score_assignment(alt.assignment, features, brands, params, adjacency_pairs=pairs)
        assert rescored.total == pytest.approx(alt.objective_after, rel=1e-9)
        total_row = alt.kpi_deltas.set_index("floor").loc["합계"]
        assert total_row["delta"] == pytest.approx(alt.delta)
        assert total_row["delta_low"] <= total_row["delta"] <= total_row["delta_high"]
        assert sum(m.delta for m in alt.moves) + alt.spillover_krw == pytest.approx(alt.delta)
        for move in alt.moves:
            assert move.exposure_krw + move.area_krw + move.affinity_krw == pytest.approx(move.delta)
            assert move.from_unit in move.explanation and "추정치" in move.explanation
        assert alt.label == "추정치" and "추정치" in alt.summary


def test_conflict_rule_blocks_accessories_next_to_cafe(setup):
    _, brands, features, pairs, _, params = setup
    constraints = LayoutConstraints(floors=("3F",), max_moves=6)

    alternatives = generate_alternatives(
        BASE, features, brands, pairs, params, constraints, n_alternatives=2, seed=3
    )

    assert alternatives
    for alt in alternatives:
        _verify_constraints(
            alt, brands, features, pairs, constraints
        )  # includes no new ACCESSORIES-CAFE pair
        assert all(u.startswith("3F") for m in alt.moves for u in (m.from_unit, m.to_unit))
    bad = dict(BASE, **{"3F-C04": "B303", "3F-C01": None})
    checks = check_constraints(BASE, bad, features, brands, pairs, constraints).set_index("constraint")
    assert not checks.loc["인접 충돌 금지", "satisfied"]
    anchor_moved = dict(BASE, **{"3F-N01": None, "3F-C04": "B308"})
    checks = check_constraints(BASE, anchor_moved, features, brands, pairs, constraints).set_index(
        "constraint"
    )
    assert not checks.loc["앵커 고정", "satisfied"]


def test_generate_alternatives_is_deterministic(setup):
    _, brands, features, pairs, _, params = setup
    affinity = pd.DataFrame(
        {"level": ["brand"] * 2, "item_a": ["B304", "B403"], "item_b": ["B307", "B405"], "lift": [3.0, 2.5]}
    )
    constraints = LayoutConstraints(floors=FLOORS, max_moves=4)

    def run(seed):
        alts = generate_alternatives(
            BASE,
            features,
            brands,
            pairs,
            params,
            constraints,
            n_alternatives=3,
            seed=seed,
            affinity_pairs=affinity,
        )
        return [([(m.brand_id, m.from_unit, m.to_unit) for m in a.moves], a.objective_after) for a in alts]

    first, second = run(11), run(11)
    assert first == second and len(first) >= 1


def test_no_alternative_when_nothing_can_move(setup):
    _, brands, features, pairs, _, params = setup
    constraints = LayoutConstraints(floors=FLOORS, movable_brands=["B308"])  # only an anchor is "movable"

    assert generate_alternatives(BASE, features, brands, pairs, params, constraints) == []


def test_hypothetical_revision_matches_moves(setup):
    model, brands, features, pairs, _, params = setup
    constraints = LayoutConstraints(floors=FLOORS, max_moves=4)
    alt = generate_alternatives(BASE, features, brands, pairs, params, constraints, n_alternatives=1, seed=7)[
        0
    ]

    hypothetical, changes = to_hypothetical_revision(model, alt)

    assert hypothetical.revision.rev_id == "H-1"
    assert hypothetical.revision.kind == RevisionKind.HYPOTHETICAL
    assert hypothetical.revision.parent_rev_id == "R2"
    assert (changes.rev_from, changes.rev_to) == ("R2", "H-1")
    assert len(changes.items) == len(alt.moves)
    for move, item in zip(alt.moves, changes.items, strict=True):
        assert item.change_type == ChangeType.MOVE
        assert item.unit_codes_before == [move.from_unit] and item.unit_codes_after == [move.to_unit]
        assert item.brand_ids_before == [move.brand_id] == item.brand_ids_after
        assert item.entity_ids_after == [f"U-{move.to_unit}"]
        assert "이전" in item.summary and "추정치" in item.summary
        assert hypothetical.unit_by_code(move.to_unit).attributes["brand_id"] == move.brand_id
    for unit in hypothetical.units():
        code = unit.attributes["unit_code"]
        assert unit.attributes["brand_id"] == alt.assignment[code]
        assert unit.attributes["vacant"] == (alt.assignment[code] is None)
        assert unit.geometry.equals(model.entities[unit.entity_id].geometry)
    assert model.unit_by_code("3F-S01").attributes["brand_id"] == "B304"  # base model untouched
    assert set(hypothetical.entities) == set(model.entities)
    assert ChangeSet.from_dict(changes.to_dict()).items[0].change_type == ChangeType.MOVE
    assert SpatialModel.from_dict(hypothetical.to_dict()).revision.kind == RevisionKind.HYPOTHETICAL
    triggered = {entry.generator for entry in plan_impact(changes, load_specs())}
    assert {"area_schedule", "floor_directory_svg"} <= triggered


def test_hypothetical_revision_reassign_items_and_validation():
    model = layout_model()
    entering = {
        "alt_id": "ALT-5",
        "moves": [
            {
                "brand_id": "B999",
                "from_unit": None,
                "to_unit": "3F-C04",
                "brand_name": "신규 가상",
                "category": "CAFE",
            }
        ],
    }

    hypothetical, changes = to_hypothetical_revision(model, entering)

    item = changes.items[0]
    assert hypothetical.revision.rev_id == "H-5"
    assert item.change_type == ChangeType.REASSIGN and item.brand_ids_after == ["B999"]
    assert hypothetical.unit_by_code("3F-C04").attributes["category_name"] == CATEGORIES["CAFE"].name_ko
    with pytest.raises(ValueError):
        to_hypothetical_revision(
            model, {"moves": [{"brand_id": "B301", "from_unit": "3F-S01", "to_unit": "3F-C04"}]}
        )
    assert math.isclose(model.unit_by_code("3F-C04").geometry.area, 80.0)
