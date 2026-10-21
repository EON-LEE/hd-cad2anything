"""Tests for the synthetic business data generator (``cad2anything.mockgen.business``)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cad2anything.mockgen.business import TX_COLUMNS, generate_business, simulate_business
from cad2anything.mockgen.calibration import load_calibration
from cad2anything.mockgen.drawings import build_all

SEED = 42
SCALE = 0.005
CSV_TABLES = ("categories", "brands", "tenancies", "pos_terminals", "customers", "calendar")


@pytest.fixture(scope="module")
def inputs() -> tuple[list, dict]:
    brands, models, _ = build_all(SEED)
    return brands, models


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory, inputs: tuple[list, dict]) -> tuple[Path, Path, dict]:
    brands, models = inputs
    out = tmp_path_factory.mktemp("business")
    summary = generate_business(
        out, seed=SEED, scale=SCALE, models=models, brands=brands, samples_dir=out / "samples"
    )
    return out / "business", out / "samples" / "business", summary


@pytest.fixture(scope="module")
def tables(generated: tuple[Path, Path, dict]) -> dict[str, pd.DataFrame]:
    folder, _, _ = generated
    frames = {
        name: pd.read_csv(folder / f"{name}.csv", dtype=str, keep_default_na=False) for name in CSV_TABLES
    }
    frames["transactions"] = pd.read_csv(folder / "transactions.csv.gz", dtype=str, keep_default_na=False)
    return frames


def _effect(generated: tuple[Path, Path, dict], effect_id: str) -> dict:
    return generated[2]["effects"][effect_id]["observed"]


def test_files_and_schema(generated: tuple[Path, Path, dict], tables: dict[str, pd.DataFrame]) -> None:
    folder, _, _ = generated
    for name in ("footfall_zone_hourly.csv.gz", "planted_effects.json", "data_card.md"):
        assert (folder / name).is_file()
    assert list(tables["tenancies"].columns) == [
        "brand_id",
        "unit_code",
        "rev_id",
        "valid_from",
        "valid_to",
        "commission_rate",
    ]
    assert list(tables["pos_terminals"].columns) == [
        "pos_id",
        "brand_id",
        "unit_code",
        "valid_from",
        "valid_to",
    ]
    assert list(tables["customers"].columns) == [
        "pseudo_id",
        "age_band",
        "gender",
        "tier",
        "join_date",
        "home_area",
    ]
    assert list(tables["transactions"].columns) == TX_COLUMNS
    assert set(tables["tenancies"]["rev_id"]) == {"R0", "R1", "R2"}
    assert (
        tables["calendar"]["date"].iloc[0] == "2024-10-01"
        and tables["calendar"]["date"].iloc[-1] == "2026-09-30"
    )
    assert set(tables["transactions"]["weight"].astype(float)) == {1 / SCALE}
    card = (folder / "data_card.md").read_text(encoding="utf-8")
    assert "합성" in card and "가상" in card
    effects = json.loads((folder / "planted_effects.json").read_text(encoding="utf-8"))
    assert [e["effect_id"][0] for e in effects["effects"]] == list("abcdefgh")
    assert all(e["parameters"] and e["expected_signal"] for e in effects["effects"])


def test_calibration_parameters_have_sources(inputs: tuple[list, dict]) -> None:
    cal = load_calibration()
    params = cal.parameters()
    assert len(params) > 50 and all(str(p["source"]).strip() for p in params.values())
    _, models = inputs
    dates = cal.value("period.revision_effective_dates")
    assert all(models[rev].revision.effective_date == day for rev, day in dates.items())


def test_validity_windows_do_not_overlap(tables: dict[str, pd.DataFrame]) -> None:
    for name, key in (("tenancies", "unit_code"), ("pos_terminals", "pos_id"), ("tenancies", "brand_id")):
        frame = tables[name].sort_values([key, "valid_from"])
        assert (frame["valid_from"] <= frame["valid_to"]).all()
        previous_end = frame.groupby(key)["valid_to"].shift()
        assert (previous_end.isna() | (previous_end < frame["valid_from"])).all(), (name, key)


def test_pos_keeps_ids_when_brand_moves(tables: dict[str, pd.DataFrame]) -> None:
    pos = tables["pos_terminals"]
    assert (pos.groupby("pos_id")["brand_id"].nunique() == 1).all()
    assert pos.groupby("brand_id")["pos_id"].nunique().between(1, 4).all()
    moved = pos.groupby("pos_id")["unit_code"].nunique()
    assert (moved > 1).any()


def test_every_transaction_maps_to_exactly_one_tenancy(tables: dict[str, pd.DataFrame]) -> None:
    tx = tables["transactions"][["txn_id", "ts", "pos_id"]].assign(date=lambda f: f["ts"].str[:10])
    pos = tx.merge(tables["pos_terminals"], on="pos_id")
    pos = pos[(pos["valid_from"] <= pos["date"]) & (pos["date"] <= pos["valid_to"])]
    assert pos["txn_id"].nunique() == len(tx) and pos["txn_id"].value_counts().max() == 1
    ten = pos.merge(tables["tenancies"], on=["brand_id", "unit_code"], suffixes=("", "_t"))
    ten = ten[(ten["valid_from_t"] <= ten["date"]) & (ten["date"] <= ten["valid_to_t"])]
    assert ten["txn_id"].nunique() == len(tx) and ten["txn_id"].value_counts().max() == 1


def test_returns_reference_original_receipts(tables: dict[str, pd.DataFrame]) -> None:
    tx = tables["transactions"]
    returns = tx[tx["is_return"] == "True"]
    assert len(returns) > 0 and (returns["amount_krw"].astype(int) < 0).all()
    receipts = tx[tx["is_return"] == "False"].set_index("txn_id")
    assert returns["ref_txn_id"].isin(receipts.index).all()
    original = receipts.loc[returns["ref_txn_id"]]
    assert (
        returns["amount_krw"].astype(int).to_numpy() == -original["amount_krw"].astype(int).to_numpy()
    ).all()
    assert (returns["ts"].to_numpy() > original["ts"].to_numpy()).all()
    brand_of = tables["pos_terminals"].drop_duplicates("pos_id").set_index("pos_id")["brand_id"]
    assert (returns["pos_id"].map(brand_of).to_numpy() == original["pos_id"].map(brand_of).to_numpy()).all()
    assert (receipts["ref_txn_id"] == "").all() and (receipts["amount_krw"].astype(int) > 0).all()


def test_totals_and_mix(generated: tuple[Path, Path, dict]) -> None:
    summary = generated[2]
    assert 1.8e12 <= summary["full_scale_annual_gross_sales_krw"] <= 2.2e12
    assert 120_000 <= summary["aov_krw"] <= 170_000
    assert 0.62 <= summary["identified_receipt_share"] <= 0.68
    mix = summary["member_sales_mix"]
    assert mix["gender"]["F"] > 0.5
    assert mix["age_band"]["30s"] + mix["age_band"]["40s"] + mix["age_band"]["50s"] > 0.6


def test_deterministic(inputs: tuple[list, dict]) -> None:
    brands, models = inputs
    cal = load_calibration()
    first = simulate_business(models, brands, cal, SEED, 0.001)
    second = simulate_business(models, brands, cal, SEED, 0.001)
    for name, frame in first.tables.items():
        pd.testing.assert_frame_equal(frame, second.tables[name])


def test_effect_a_segment_cross_shopping(generated: tuple[Path, Path, dict]) -> None:
    observed = _effect(generated, "a_segment_cross_shopping")
    assert observed["lift_focal"] >= 2.0
    assert 0.9 <= observed["lift_others"] <= 1.4
    assert set(observed["focal_brand_top_partners"]) == set(generated[2]["meta"]["partner_brands"])


def test_effect_d_fountain_and_shower(generated: tuple[Path, Path, dict]) -> None:
    observed = _effect(generated, "d_fountain_shower")
    assert observed["fountain_share_cosmetics"] >= 0.40
    assert observed["shower_share_after_8f"] >= 0.60


def test_effect_f_low_efficiency_zone(generated: tuple[Path, Path, dict]) -> None:
    observed = _effect(generated, "f_low_efficiency_zone")
    assert observed["units"] == [f"4F-E0{i}" for i in range(1, 7)]
    assert observed["share_in_bottom_decile"] >= 0.5
    assert observed["median_spm_band_vs_floor_p10"] <= 1.1


def test_effect_g_space_elasticity(generated: tuple[Path, Path, dict]) -> None:
    cosmetics = _effect(generated, "g_space_elasticity")["COSMETICS"]
    assert abs(cosmetics["beta_hat"] - 0.35) <= 3 * cosmetics["se"] + 0.05


def test_samples_are_small_and_consistent(generated: tuple[Path, Path, dict]) -> None:
    _, samples, _ = generated
    names = sorted(p.name for p in samples.iterdir())
    assert names == sorted(
        [
            "brands.csv", "categories.csv", "customers_sample.csv", "planted_effects.json", "pos_terminals.csv",
            "tenancies.csv", "transactions_sample.csv",
        ]
    )  # fmt: skip
    assert sum(p.stat().st_size for p in samples.iterdir()) < 1_000_000
    tx = pd.read_csv(samples / "transactions_sample.csv", dtype=str, keep_default_na=False)
    customers = pd.read_csv(samples / "customers_sample.csv", dtype=str)
    assert set(tx.loc[tx["ref_txn_id"] != "", "ref_txn_id"]) <= set(tx["txn_id"])
    assert set(tx.loc[tx["customer_id"] != "", "customer_id"]) <= set(customers["pseudo_id"])
