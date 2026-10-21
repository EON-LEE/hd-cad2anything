import math

import numpy as np
import pandas as pd
import pytest

from cad2anything.analytics import (
    adjacency_effect,
    before_after,
    cross_shopping,
    driver_decomposition,
    enrich_transactions,
    floor_transitions,
    space_productivity,
    time_patterns,
    top_partners,
)
from cad2anything.analytics.kpi import bottom_share_flags
from cad2anything.analytics.prep import PYEONG_M2
from cad2anything.catalog import AGE_BANDS, CATEGORIES, TIERS

TX_COLUMNS = ["txn_id", "ts", "pos_id", "customer_id", "amount_krw", "is_return", "ref_txn_id"]


def _pos(rows):
    return pd.DataFrame(rows, columns=["pos_id", "brand_id", "unit_code", "valid_from", "valid_to"])


def _tenancies(rows):
    return pd.DataFrame(
        [(b, u, "R0", start, end, rate) for b, u, start, end, rate in rows],
        columns=["brand_id", "unit_code", "rev_id", "valid_from", "valid_to", "commission_rate"],
    )


def _transactions(rows, weight=None):
    tx = pd.DataFrame(rows, columns=TX_COLUMNS)
    tx["items"] = 1
    tx["payment"] = "card"
    if weight is not None:
        tx["weight"] = weight
    return tx


# --- enrichment ---------------------------------------------------------------------------------------


def _relocation_case():
    pos = _pos(
        [
            ("P1", "BR1", "1F-S01", "2024-10-01", "2024-12-31"),
            ("P1", "BR9", "1F-S01", "2025-01-01", None),
            ("P2", "BR2", "1F-S02", "2024-10-01", "2025-02-28"),
            ("P2", "BR2", "2F-S01", "2025-03-01", None),
        ]
    )
    tenancies = _tenancies(
        [
            ("BR1", "1F-S01", "2024-10-01", "2024-12-31", 0.30),
            ("BR9", "1F-S01", "2025-01-01", None, 0.28),
            ("BR2", "1F-S02", "2024-10-01", "2025-02-28", 0.25),
            ("BR2", "2F-S01", "2025-03-01", None, 0.22),
        ]
    )
    brands = pd.DataFrame(
        {
            "brand_id": ["BR1", "BR2", "BR9"],
            "brand_name": ["가온 뷰티", "루미 잡화", "새솔 코스메틱"],
            "category": ["COSMETICS", "ACCESSORIES", "COSMETICS"],
        }
    )
    features = pd.DataFrame(
        {
            "unit_code": ["1F-S01", "1F-S02", "2F-S01"],
            "floor": ["1F", "1F", "2F"],
            "zone_code": ["1F-Z1", "1F-Z1", "2F-Z1"],
            "category": ["COSMETICS", "ACCESSORIES", "ACCESSORIES"],
            "brand_id": ["BR9", None, "BR2"],
            "vacant": [False, True, False],
            "area_m2": [100.0, 50.0, 80.0],
        }
    )
    rows = [
        (1, "2024-12-31 23:00", "P1", "C1", 100_000, False, None),  # last day of a date-only validity
        (2, "2025-01-01 09:00", "P1", "C1", 50_000, False, None),  # same POS, new brand
        (3, "2025-02-28 20:00", "P2", "C2", 70_000, False, None),
        (4, "2025-03-01 10:00", "P2", "C2", 80_000, False, None),  # same POS after the relocation
        (5, "2025-03-05 11:00", "P2", "C2", 70_000, True, 3),  # flagged return, positive amount
        (6, "2025-01-02 12:00", "P1", None, -20_000, "False", None),  # negative amount, flag missing
        (7, "2025-03-02 13:00", "P2", "C3", -30_000, "True", None),
        (8, "2025-01-03 10:00", "P9", "C1", 10_000, False, None),  # unknown POS
        (9, "2024-09-30 10:00", "P1", "C1", 10_000, False, None),  # before the POS existed
    ]
    return rows, pos, tenancies, brands, features


def test_enrich_maps_pos_validity_and_nets_returns():
    rows, pos, tenancies, brands, features = _relocation_case()

    enriched = enrich_transactions(_transactions(rows), pos, tenancies, brands, features).set_index("txn_id")

    def where(txn):
        return [enriched.loc[txn, "unit_code"], enriched.loc[txn, "brand_id"]]

    assert where(1) == ["1F-S01", "BR1"]
    assert where(2) == ["1F-S01", "BR9"]
    assert where(3) == ["1F-S02", "BR2"]
    assert where(4) == ["2F-S01", "BR2"]
    assert where(5) == ["1F-S02", "BR2"] and enriched.loc[5, "return_attributed"]  # back to the original sale
    assert enriched.loc[5, "commission_rate"] == pytest.approx(0.25)
    assert enriched.loc[6, "is_return"] and enriched.loc[6, "net_amount_krw"] == -20_000
    assert enriched.loc[7, "is_return"] and enriched.loc[7, "net_amount_krw"] == -30_000
    assert not enriched.loc[8, "mapped"] and not enriched.loc[9, "mapped"]
    assert enriched.loc[4, "floor"] == "2F" and enriched.loc[4, "category"] == "ACCESSORIES"
    assert enriched.loc[2, "commission_rate"] == pytest.approx(0.28)
    assert enriched.loc[2, "commission_source"] == "tenancy"
    assert (enriched["weight"] == 1.0).all()
    net = enriched.groupby("unit_code")["net_amount_krw"].sum()
    assert net.to_dict() == {"1F-S01": 130_000, "1F-S02": 0, "2F-S01": 50_000}


def test_weights_scale_sales_and_receipts_but_not_customer_ratios():
    rows, pos, tenancies, brands, features = _relocation_case()
    plain = enrich_transactions(_transactions(rows), pos, tenancies, brands, features)
    scaled = enrich_transactions(_transactions(rows, weight=50.0), pos, tenancies, brands, features)

    a = space_productivity(plain, features, tenancies).set_index("unit_code")
    b = space_productivity(scaled, features, tenancies).set_index("unit_code")

    assert b.loc["1F-S01", "sales"] == pytest.approx(50 * a.loc["1F-S01", "sales"])
    assert a.loc["1F-S01", "txns"] == 2 and b.loc["1F-S01", "txns"] == pytest.approx(100)
    assert b.loc["1F-S01", "aov"] == pytest.approx(a.loc["1F-S01", "aov"])
    assert scaled["net_sales_krw"].sum() == pytest.approx(50 * plain["net_amount_krw"].sum())
    pd.testing.assert_frame_equal(
        cross_shopping(plain, level="category", min_customers=1),
        cross_shopping(scaled, level="category", min_customers=1),
    )


# --- productivity ---------------------------------------------------------------------------------------


def _productivity_case():
    features = pd.DataFrame(
        {
            "unit_code": ["1F-S01", "1F-S02", "1F-S03", "2F-S01", "2F-S02"],
            "floor": ["1F", "1F", "1F", "2F", "2F"],
            "zone_code": ["1F-Z1", "1F-Z1", "1F-Z2", "2F-Z1", "2F-Z1"],
            "category": ["COSMETICS", "COSMETICS", "JEWELRY_WATCH", "GLOBAL_FASHION", "ACCESSORIES"],
            "brand_id": ["BR1", "BR2", "BR3", "BR4", None],
            "vacant": [False, False, False, False, True],
            "area_m2": [100.0, 50.0, 200.0, 80.0, 60.0],
        }
    )
    brands = pd.DataFrame(
        {
            "brand_id": ["BR1", "BR2", "BR3", "BR4"],
            "brand_name": ["가온", "나린", "다솜", "라온"],
            "category": ["COSMETICS", "COSMETICS", "JEWELRY_WATCH", "GLOBAL_FASHION"],
        }
    )
    occupied = features.dropna(subset=["brand_id"])
    pos = _pos(
        [
            (f"P{i}", b, u, "2024-10-01", None)
            for i, (b, u) in enumerate(zip(occupied.brand_id, occupied.unit_code, strict=True), 1)
        ]
    )
    tenancies = _tenancies(
        [
            ("BR1", "1F-S01", "2024-10-01", None, 0.30),
            ("BR2", "1F-S02", "2024-10-01", None, 0.25),
            ("BR3", "1F-S03", "2024-10-01", None, 0.20),
            ("BR4", "2F-S01", "2024-10-01", None, 0.10),
        ]
    )
    rows = []

    def add(pos_id, ts, amount, is_return=False, ref=None):
        rows.append((len(rows) + 1, ts, pos_id, f"C{len(rows) % 7}", amount, is_return, ref))

    for day in range(1, 11):
        add("P1", f"2025-01-{day:02d} 11:00", 100_000)
    add("P1", "2025-01-15 12:00", 100_000, True, 1)
    for day in range(1, 5):
        add("P2", f"2025-01-{day:02d} 15:00", 50_000)
    for day in range(10, 13):
        add("P4", f"2025-01-{day:02d} 16:00", 200_000)
    add("P1", "2025-02-01 10:00", 999_000)  # outside the period
    enriched = enrich_transactions(_transactions(rows), pos, tenancies, brands, features)
    return enriched, features, tenancies


def test_space_productivity_unit_math():
    enriched, features, tenancies = _productivity_case()

    table = space_productivity(enriched, features, tenancies, "2025-01-01", "2025-01-31").set_index(
        "unit_code"
    )

    assert list(table.index) == ["1F-S01", "1F-S02", "1F-S03", "2F-S01"]  # vacant unit without sales dropped
    s01 = table.loc["1F-S01"]
    assert s01["sales"] == 900_000 and s01["gross_sales"] == 1_000_000 and s01["returns"] == 100_000
    assert s01["txns"] == 10 and s01["aov"] == pytest.approx(100_000)
    assert s01["sales_per_m2"] == pytest.approx(9_000)
    assert s01["sales_per_pyeong"] == pytest.approx(9_000 * PYEONG_M2)
    assert s01["commission_revenue"] == pytest.approx(270_000) and s01["gmrof"] == pytest.approx(2_700)
    assert table.loc["1F-S02", "sales_per_m2"] == pytest.approx(4_000)
    assert table.loc["2F-S01", "sales_per_m2"] == pytest.approx(7_500)
    assert table.loc["1F-S03", "sales"] == 0 and table.loc["1F-S03", "txns"] == 0
    assert table["percentile"].to_dict() == {"1F-S01": 100.0, "1F-S02": 50.0, "1F-S03": 25.0, "2F-S01": 75.0}
    assert table["low_efficiency"].to_dict() == {
        "1F-S01": False,
        "1F-S02": False,
        "1F-S03": True,
        "2F-S01": False,
    }


def test_space_productivity_rollups():
    enriched, features, tenancies = _productivity_case()

    floors = space_productivity(enriched, features, tenancies, "2025-01-01", "2025-01-31", level="floor")
    zones = space_productivity(enriched, features, tenancies, "2025-01-01", "2025-01-31", level="zone")
    categories = space_productivity(
        enriched, features, tenancies, "2025-01-01", "2025-01-31", level="category"
    )

    assert floors["floor"].tolist() == ["1F", "2F"]
    assert floors["sales_per_m2"].tolist() == pytest.approx([1_100_000 / 350, 7_500])
    assert floors["low_efficiency"].tolist() == [True, False]
    zone = zones.set_index("zone_code")
    assert zone.loc["1F-Z1", "sales_per_m2"] == pytest.approx(1_100_000 / 150)
    assert zone.loc["1F-Z2", "low_efficiency"] and not zone.loc["2F-Z1", "low_efficiency"]
    category = categories.set_index("category")
    assert category.loc["COSMETICS", "commission_revenue"] == pytest.approx(270_000 + 50_000)
    assert category.loc["COSMETICS", "category_name"] == CATEGORIES["COSMETICS"].name_ko


def test_driver_decomposition_recovers_planted_exposure_effect():
    rows = []
    categories = {"COSMETICS": 0.3, "ACCESSORIES": 0.0, "LINGERIE": -0.2}
    for i in range(36):
        walk = 5 + (i * 37) % 86
        frontage = 4 + (i * 7) % 12
        area = 50 + (i * 13) % 100
        category = list(categories)[i % 3]
        log_spm = (
            11
            - 0.5 * math.log1p(walk)
            + 0.03 * frontage
            - 0.25 * math.log(area)
            + categories[category]
            + 0.02 * math.sin(i)
        )
        rows.append(
            {
                "unit_code": f"{1 + i // 12}F-U{i:02d}",
                "floor": f"{1 + i // 12}F",
                "category": category,
                "sales_per_m2": math.exp(log_spm),
                "walk_to_escalator_m": walk,
                "frontage_m": frontage,
                "area_m2": area,
            }
        )
    productivity = pd.DataFrame(rows)
    productivity["low_efficiency"] = bottom_share_flags(
        productivity, "sales_per_m2", ["floor"], ["unit_code"]
    )

    result = driver_decomposition(productivity)

    coef = result.coefficients.set_index("term")["coef"]
    assert coef["log_walk_escalator"] == pytest.approx(-0.5, abs=0.05)
    assert coef["frontage_m"] == pytest.approx(0.03, abs=0.01)
    assert coef["log_area"] == pytest.approx(-0.25, abs=0.05)
    assert result.r2 > 0.95 and result.n_obs == 36 and result.label == "추정치"
    parts = result.contributions[["exposure", "frontage", "area", "category", "residual"]].sum(axis=1)
    centered = result.contributions["log_sales_per_m2"] - result.contributions["log_sales_per_m2"].mean()
    assert np.allclose(parts, centered)
    flagged = set(productivity.loc[productivity["low_efficiency"], "unit_code"])
    assert set(result.explanations) == flagged and len(flagged) == 6
    for unit_code, text in result.explanations.items():
        assert unit_code in text and "추정치" in text and "인과" in text
    exposure_led = result.contributions.query("low_efficiency and main_driver == 'exposure'")
    assert len(exposure_led) >= 1
    assert all("에스컬레이터" in result.explanations[u] for u in exposure_led["unit_code"])


def test_time_patterns_shares():
    enriched, _, _ = _productivity_case()

    by_hour = time_patterns(enriched, by=("hour", "category"), start="2025-01-01", end="2025-01-31")
    by_weekday = time_patterns(enriched, by=("weekday",))

    assert by_hour["share"].sum() == pytest.approx(1.0)
    assert by_hour.groupby("category")["share_in_group"].sum().tolist() == pytest.approx([1.0, 1.0])
    assert set(by_weekday["weekday_name"]) <= {"월", "화", "수", "목", "금", "토", "일"}
    with pytest.raises(ValueError):
        time_patterns(enriched, by=("nonexistent",))


# --- cross-shopping and flows ----------------------------------------------------------------------------


def _cross_shopping_case():
    pos = _pos(
        [
            ("PA", "BRA", "1F-S01", "2024-10-01", None),
            ("PB", "BRB", "1F-S02", "2024-10-01", None),
            ("PC", "BRC", "2F-S01", "2024-10-01", None),
        ]
    )
    brands = pd.DataFrame(
        {
            "brand_id": ["BRA", "BRB", "BRC"],
            "brand_name": ["알파", "베타", "감마"],
            "category": ["LUXURY", "JEWELRY_WATCH", "COSMETICS"],
        }
    )
    buyers = {"PA": range(1, 7), "PB": range(4, 10), "PC": (1, 10)}
    rows = []
    for pos_id, members in buyers.items():
        for c in members:
            rows.append((len(rows) + 1, f"2025-01-{c:02d} 12:00", pos_id, f"C{c:02d}", 100_000, False, None))
    rows.append((len(rows) + 1, "2025-01-20 12:00", "PB", None, 100_000, False, None))  # anonymous
    customers = pd.DataFrame(
        {
            "pseudo_id": [f"C{c:02d}" for c in range(1, 11)],
            "age_band": ["40s"] * 5 + ["30s"] * 5,
            "gender": ["F"] * 10,
            "tier": ["VIP", "VIP", "VVIP", "VIP", "GOLD", "GOLD", "GOLD", "MEMBER", "MEMBER", "MEMBER"],
            "join_date": ["2020-01-01"] * 10,
            "home_area": ["가상구"] * 10,
        }
    )
    return enrich_transactions(_transactions(rows), pos, None, brands, None), customers


def test_cross_shopping_lift_and_jaccard_hand_checked():
    enriched, customers = _cross_shopping_case()

    pairs = cross_shopping(enriched, customers, level="brand", min_customers=1)

    keyed = pairs.set_index(["item_a", "item_b"])
    assert set(keyed.index) == {("BRA", "BRB"), ("BRA", "BRC")}  # B and C were never bought together
    ab = keyed.loc[("BRA", "BRB")]
    assert ab["n_customers"] == 3 and ab["n_total"] == 10
    assert ab["support"] == pytest.approx(0.3)
    assert ab["confidence_a_b"] == pytest.approx(0.5) and ab["confidence_b_a"] == pytest.approx(0.5)
    assert ab["lift"] == pytest.approx(3 * 10 / (6 * 6))
    assert ab["jaccard"] == pytest.approx(3 / (6 + 6 - 3))
    ac = keyed.loc[("BRA", "BRC")]
    assert ac["lift"] == pytest.approx(1 * 10 / (6 * 2)) and ac["jaccard"] == pytest.approx(1 / 7)
    assert ac["name_a"] == "알파"
    assert len(cross_shopping(enriched, customers, level="brand")) == 0  # default min_customers=20
    partners = top_partners(pairs, "BRA", k=1)
    assert partners["partner"].tolist() == ["BRB"]  # lift tie broken by more shared customers
    assert partners.loc[0, "confidence"] == pytest.approx(0.5)


def test_cross_shopping_segment_filter():
    enriched, customers = _cross_shopping_case()

    pairs = cross_shopping(
        enriched, customers, segment={"age_band": "40s", "tier": ["VIP", "VVIP"]}, min_customers=1
    )

    assert pairs.attrs["n_total"] == 4  # C01-C04
    keyed = pairs.set_index(["item_a", "item_b"])
    assert keyed.loc[("BRA", "BRB"), "n_customers"] == 1  # only C04
    assert keyed.loc[("BRA", "BRB"), "lift"] == pytest.approx(1 * 4 / (4 * 1))
    with pytest.raises(ValueError):
        cross_shopping(enriched, customers, segment={"income": "high"})
    with pytest.raises(ValueError):
        cross_shopping(enriched, None, segment={"age_band": "40s"})


def test_cross_shopping_customer_day_baskets():
    pos = _pos(
        [
            (p, b, u, "2024-10-01", None)
            for p, b, u in (("PA", "BRA", "1F-S01"), ("PB", "BRB", "1F-S02"), ("PC", "BRC", "2F-S01"))
        ]
    )
    visits = [
        ("C1", 1, "PA"), ("C1", 1, "PB"), ("C2", 1, "PA"), ("C2", 2, "PB"), ("C3", 1, "PA"), ("C4", 3, "PB"),
        ("C5", 4, "PA"), ("C5", 4, "PB"), ("C5", 5, "PC"), ("C6", 6, "PC"), ("C7", 7, "PC"),
    ]  # fmt: skip
    rows = [
        (i, f"2025-01-{d:02d} 1{i % 10}:00", p, c, 10_000, False, None)
        for i, (c, d, p) in enumerate(visits, 1)
    ]
    enriched = enrich_transactions(_transactions(rows), pos)

    per_customer = cross_shopping(enriched, level="brand", min_customers=1).set_index(["item_a", "item_b"])
    per_day = cross_shopping(enriched, level="brand", min_customers=1, basket="customer_day")

    assert per_customer.loc[("BRA", "BRB"), "lift"] == pytest.approx(
        3 * 7 / (4 * 4)
    )  # C1, C2, C5 of 7 customers
    assert per_day.attrs["n_total"] == 9 and per_day.attrs["n_customers"] == 7
    ab = per_day.set_index(["item_a", "item_b"]).loc[("BRA", "BRB")]
    assert ab["n_baskets"] == 2 and ab["n_customers"] == 2  # same-day only: C1 day 1, C5 day 4
    assert ab["lift"] == pytest.approx(2 * 9 / (4 * 4))
    with pytest.raises(ValueError):
        cross_shopping(enriched, basket="weekly")


def test_floor_transitions_hand_checked():
    pos = _pos(
        [
            ("PB1", "BR1", "B1-S01", "2024-10-01", None),
            ("P1F", "BR2", "1F-S01", "2024-10-01", None),
            ("P3F", "BR3", "3F-S01", "2024-10-01", None),
            ("P8F", "BR4", "8F-S01", "2024-10-01", None),
        ]
    )
    visits = [
        ("X", "2025-01-01 10:00", "PB1"),
        ("X", "2025-01-01 11:00", "P1F"),
        ("X", "2025-01-01 12:00", "P3F"),
        ("Y", "2025-01-01 10:00", "PB1"),
        ("Y", "2025-01-01 10:30", "PB1"),
        ("Z", "2025-01-01 12:00", "P8F"),
        ("Z", "2025-01-01 14:00", "P1F"),
        ("W", "2025-01-01 18:00", "P8F"),
        ("X", "2025-01-02 15:00", "P3F"),
        ("X", "2025-01-02 16:00", "PB1"),
    ]
    rows = [(i + 1, ts, pos_id, c, 10_000, False, None) for i, (c, ts, pos_id) in enumerate(visits)]
    rows.append((99, "2025-01-01 13:00", "P8F", "X", 10_000, True, None))  # returns are not purchases
    enriched = enrich_transactions(_transactions(rows), pos)

    flows = floor_transitions(enriched)

    assert flows.floors == ["B1", "1F", "3F", "8F"]
    assert flows.counts.loc["B1", "1F"] == 1 and flows.counts.loc["B1", "B1"] == 1
    assert flows.counts.loc["8F", "1F"] == 1 and flows.counts.loc["3F", "B1"] == 1
    assert flows.n_transitions == 5
    sums = flows.probabilities.sum(axis=1)
    assert sums.tolist() == pytest.approx([1.0, 1.0, 1.0, 1.0])
    assert flows.probabilities.loc["B1", "1F"] == pytest.approx(0.5)
    assert flows.fountain_share == pytest.approx(2 / 3)  # X day 1 and X day 2 of three B1 customer-days
    assert flows.shower_share == pytest.approx(1 / 2)  # Z goes down after 8F, W does not
    assert flows.top_floor == "8F" and "구매 순서" in flows.caveat
    no_self = floor_transitions(enriched, include_same_floor=False)
    assert no_self.probabilities.loc["B1", "1F"] == pytest.approx(1.0)


# --- effects --------------------------------------------------------------------------------------------


def _adjacency_case(treated_rule, true_effect: float):
    features, productivity, pairs = [], [], []
    for i in range(28):
        code = f"1F-R{i:02d}"
        walk = 5 + 2.5 * i
        treated = treated_rule(i)
        log_spm = (
            12
            - 0.8 * math.log1p(walk)
            + (math.log(true_effect) if treated else 0.0)
            + 0.02 * math.cos(1.7 * i)
        )
        features.append((code, "1F", "WOMEN_CASUAL", f"BR{i:02d}", False, walk, 10.0, 100.0))
        productivity.append((code, "1F", "WOMEN_CASUAL", math.exp(log_spm)))
        if treated:
            pairs.append((code, f"1F-K{i:02d}", "1F", 6.0))
            features.append((f"1F-K{i:02d}", "1F", "CAFE", f"BK{i:02d}", False, walk, 8.0, 60.0))
    features = pd.DataFrame(
        features,
        columns=[
            "unit_code",
            "floor",
            "category",
            "brand_id",
            "vacant",
            "walk_to_escalator_m",
            "frontage_m",
            "area_m2",
        ],
    )
    productivity = pd.DataFrame(productivity, columns=["unit_code", "floor", "category", "sales_per_m2"])
    pairs = pd.DataFrame(pairs, columns=["unit_a", "unit_b", "floor", "shared_length_m"])
    return productivity, pairs, features


def test_adjacency_effect_flags_planted_confounder():
    # cafes sit only next to the units closest to the escalator; the true cafe effect is zero
    productivity, pairs, features = _adjacency_case(lambda i: i < 8, true_effect=1.0)

    effect = adjacency_effect(productivity, pairs, features, "CAFE")

    assert effect.n_treated == 8 and effect.n_control == 20
    assert effect.naive_uplift_pct > 50
    assert abs(effect.controlled_uplift_pct) < 5
    assert effect.exposure_smd < -1
    assert effect.confounding_suspected
    assert "인과 효과가 아닙니다" in effect.caveat and "추정치" in effect.caveat


def test_adjacency_effect_balanced_design_is_not_flagged():
    productivity, pairs, features = _adjacency_case(lambda i: i % 4 in (1, 2), true_effect=1.2)

    effect = adjacency_effect(productivity, pairs, features, ["CAFE"])

    assert effect.controlled_uplift_pct == pytest.approx(20.0, abs=2.0)
    assert not effect.confounding_suspected
    assert "인과 효과가 아닙니다" in effect.caveat


def test_before_after_with_comparison_group():
    pos = _pos(
        [
            ("P1", "BR1", "1F-S01", "2024-10-01", None),
            ("P2", "BR2", "1F-S02", "2024-10-01", None),
            ("P3", "BR3", "2F-S01", "2024-10-01", None),
        ]
    )
    rows = []
    for day in pd.date_range("2025-02-01", "2025-03-28"):
        after = day >= pd.Timestamp("2025-03-01")
        for pos_id, before_amt, after_amt in (
            ("P1", 100_000, 150_000),
            ("P2", 100_000, 110_000),
            ("P3", 100_000, 300_000),
        ):
            rows.append(
                (
                    len(rows) + 1,
                    day + pd.Timedelta(hours=12),
                    pos_id,
                    None,
                    after_amt if after else before_amt,
                    False,
                    None,
                )
            )
    enriched = enrich_transactions(_transactions(rows, weight=50.0), pos)

    same_floor = before_after(enriched, "2025-03-01", ["1F-S01"], window_days=28)
    store = before_after(enriched, "2025-03-01", ["1F-S01"], window_days=28, control="all")

    assert same_floor.pre_days == 28 and same_floor.post_days == 28
    assert same_floor.treated_change_pct == pytest.approx(50.0)
    assert same_floor.control_change_pct == pytest.approx(10.0)
    assert same_floor.difference_pct == pytest.approx(40.0)
    assert store.control_change_pct == pytest.approx(105.0)
    units = same_floor.units.set_index("unit_code")
    assert units.loc["1F-S01", "pre_daily"] == pytest.approx(100_000 * 50)
    assert "인과 효과로 해석하면 안 됩니다" in same_floor.caveat and same_floor.label == "추정치"


# --- synthetic store with planted patterns ----------------------------------------------------------------

STORE_UNITS = [
    ("B1-S01", "B1", "FOOD_FRESH", "BR01", 80.0),
    ("B1-S02", "B1", "FOOD_DELI", "BR02", 60.0),
    ("B1-S03", "B1", "BAKERY", "BR03", 50.0),
    ("B1-S04", "B1", "CAFE", "BR04", 70.0),
    ("1F-S01", "1F", "LUXURY", "BR05", 300.0),
    ("1F-S02", "1F", "JEWELRY_WATCH", "BR06", 90.0),
    ("1F-S03", "1F", "COSMETICS", "BR07", 60.0),
    ("1F-S04", "1F", "COSMETICS", "BR08", 55.0),
    ("2F-S01", "2F", "GLOBAL_FASHION", "BR09", 180.0),
    ("2F-S02", "2F", "ACCESSORIES", "BR10", 80.0),
    ("2F-S03", "2F", "GLOBAL_FASHION", "BR11", 150.0),
    ("2F-S04", "2F", "ACCESSORIES", "BR12", 70.0),
]


def synthetic_store(seed: int = 11, n_customers: int = 1_500, days: int = 60, visitors: int = 18):
    """About 3,000 deterministic transactions with two planted patterns: visits that start on B1 continue
    upstairs (fountain), and 40s VIP/VVIP customers who buy the luxury brand BR05 also buy the otherwise
    rare jewelry brand BR06."""
    rng = np.random.default_rng(seed)
    features = pd.DataFrame(STORE_UNITS, columns=["unit_code", "floor", "category", "brand_id", "area_m2"])
    features["zone_code"] = features["floor"] + "-Z1"
    features["vacant"] = False
    brands = features[["brand_id", "category"]].assign(brand_name=[f"가상브랜드{i}" for i in range(1, 13)])
    pairs = list(zip(features.brand_id, features.unit_code, strict=True))
    pos = _pos([(f"P{b}", b, u, "2024-10-01", None) for b, u in pairs])
    tenancies = _tenancies([(b, u, "2024-10-01", None, 0.25) for b, u in pairs])
    customers = pd.DataFrame(
        {
            "pseudo_id": [f"C{i:04d}" for i in range(n_customers)],
            "age_band": rng.choice(AGE_BANDS, n_customers, p=[0.15, 0.25, 0.3, 0.2, 0.1]),
            "gender": rng.choice(["F", "M"], n_customers),
            "tier": rng.choice(TIERS, n_customers, p=[0.1, 0.2, 0.3, 0.4]),
            "join_date": "2020-01-01",
            "home_area": "가상구",
        }
    )
    planted = set(
        customers.loc[(customers.age_band == "40s") & customers.tier.isin(["VIP", "VVIP"]), "pseudo_id"]
    )
    category_of = dict(zip(features.brand_id, features.category, strict=True))
    basement = features.loc[features.floor == "B1", "brand_id"].tolist()
    upper = features.loc[(features.floor != "B1") & (features.brand_id != "BR06"), "brand_id"].tolist()
    rows = []
    for day in range(days):
        date = pd.Timestamp("2025-01-01") + pd.Timedelta(days=day)
        for c in rng.choice(n_customers, size=visitors, replace=False):
            customer = f"C{c:04d}"
            ts = date + pd.Timedelta(hours=10, minutes=int(rng.integers(0, 120)))
            basket = [str(rng.choice(basement))] if rng.random() < 0.6 else []
            basket += [str(b) for b in rng.choice(upper, size=int(rng.integers(1, 4)), replace=False)]
            planted_pair = "BR05" in basket and rng.random() < (0.8 if customer in planted else 0.05)
            if planted_pair or rng.random() < 0.03:
                basket.append("BR06")
            for brand in basket:
                ts += pd.Timedelta(minutes=int(rng.integers(15, 60)))
                mean_ticket = CATEGORIES[category_of[brand]].mean_ticket_krw
                amount = round(float(rng.lognormal(math.log(mean_ticket), 0.3)), -2)
                anonymous = rng.random() < 0.1
                rows.append(
                    (len(rows) + 1, ts, f"P{brand}", None if anonymous else customer, amount, False, None)
                )
    sales = list(rows)
    for txn_id, ts, pos_id, customer, amount, _, _ in sales:
        if rng.random() < 0.03:
            rows.append((len(rows) + 1, ts + pd.Timedelta(days=2), pos_id, customer, amount, True, txn_id))
    transactions = _transactions(rows, weight=50.0)
    enriched = enrich_transactions(transactions, pos, tenancies, brands, features)
    return enriched, transactions, features, customers, tenancies


@pytest.fixture(scope="module")
def store():
    return synthetic_store()


def test_synthetic_store_net_sales_and_productivity(store):
    enriched, transactions, features, _, tenancies = store

    assert 2_500 <= len(transactions) <= 4_000
    assert enriched["mapped"].all()
    returns = transactions["is_return"].astype(bool)
    expected = 50.0 * (
        transactions.loc[~returns, "amount_krw"].sum() - transactions.loc[returns, "amount_krw"].sum()
    )
    table = space_productivity(enriched, features, tenancies)
    assert table["sales"].sum() == pytest.approx(expected)
    assert table["txns"].sum() == pytest.approx(50.0 * (~returns).sum())
    assert table["low_efficiency"].sum() == 3  # one per floor (4 units each)


def test_synthetic_store_planted_patterns(store):
    enriched, _, _, customers, _ = store
    segment = {"age_band": "40s", "tier": ["VIP", "VVIP"]}

    overall = cross_shopping(enriched, customers, level="brand", min_customers=5)
    focused = cross_shopping(enriched, customers, level="brand", segment=segment, min_customers=5)
    partners = top_partners(focused, "BR05", k=3)
    overall_partners = top_partners(overall, "BR05", k=20).set_index("partner")
    flows = floor_transitions(enriched, customers)

    assert focused.attrs["n_total"] < overall.attrs["n_total"]
    assert partners.loc[0, "partner"] == "BR06" and partners.loc[0, "lift"] > 2.0
    assert partners.loc[0, "confidence"] > 0.6  # P(BR06 | BR05) within the planted segment
    assert partners.loc[0, "confidence"] > 3 * overall_partners.loc["BR06", "confidence"]
    assert flows.probabilities.sum(axis=1).dropna().tolist() == pytest.approx([1.0] * 3)
    assert flows.fountain_share > 0.9  # B1-first baskets almost always continue upstairs
    assert flows.probabilities.loc["B1", ["1F", "2F"]].sum() > 0.9
