"""Synthetic business data calibrated to the mockgen drawings (shopping-trip simulation).

Usage::

    uv run --no-sync python -m cad2anything.mockgen.business --out data/generated --seed 42 [--scale 0.02] \
        [--samples data/samples]

Pipeline: truth layouts R0/R1/R2 -> unit sales weights (area^beta x exposure x brand x planted effects) ->
tenancies and POS terminals -> calendar -> members -> shopping trips (date, arrival, entry floor, ordered
purchases) -> receipts and returns -> planted-effect measurements -> CSV/JSON files and a data card.
Every parameter lives in ``calibration.yaml`` with a source note; all data and brand names are synthetic.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from shapely.ops import unary_union

from cad2anything.catalog import AGE_BANDS, CATEGORIES, FLOOR_CODES, floor_program
from cad2anything.mockgen.brands import Brand, brands_to_rows
from cad2anything.mockgen.calibration import Calibration, load_calibration
from cad2anything.mockgen.layout import stable_rng
from cad2anything.model import EntityKind, SpatialModel

REVS = ("R0", "R1", "R2")
CATS = tuple(CATEGORIES)
N_CATS = len(CATS)
CAT_IX = {c: i for i, c in enumerate(CATS)}
FLOOR_IX = {f: i for i, f in enumerate(FLOOR_CODES)}
FLOOR_ORDINAL = np.array([floor_program(f).ordinal for f in FLOOR_CODES])
GENDER_LIST = ("F", "M")
AGE_LIST = tuple(AGE_BANDS)
TIER_LIST = ("MEMBER", "GOLD", "VIP", "VVIP", "ANON")
ANON = 4
N_SEG = len(GENDER_LIST) * len(AGE_LIST) * len(TIER_LIST)
FNB = np.array([CATEGORIES[c].is_food_beverage for c in CATS])
PAYMENTS = ("CARD", "STORE_CARD", "MOBILE_PAY", "CASH", "GIFT_VOUCHER")
# Context slots of a trip by arrival time: other, weekday lunch, weekend afternoon, evening.
SLOT_OTHER, SLOT_LUNCH, SLOT_AFTERNOON, SLOT_EVENING = 0, 1, 2, 3
TX_COLUMNS = [
    "txn_id", "ts", "pos_id", "customer_id", "amount_krw", "items", "payment", "is_return", "ref_txn_id", "weight",
]  # fmt: skip


def seg_id(gender: Any, age: Any, tier: Any) -> Any:
    return (gender * len(AGE_LIST) + age) * len(TIER_LIST) + tier


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _by_category(mapping: dict[str, Any], default: float | None = None) -> np.ndarray:
    base = mapping.get("default", default if default is not None else 1.0)
    return np.array([float(mapping.get(c, base)) for c in CATS])


def _sample_rows(weights: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Row-wise categorical sampling (inverse CDF) from a non-negative weight matrix."""
    cdf = np.cumsum(weights, axis=1)
    u = rng.random(len(weights)) * cdf[:, -1]
    return np.minimum((cdf <= u[:, None]).sum(axis=1), weights.shape[1] - 1)


# --- units, tenancies and POS terminals -----------------------------------------------------------


def _shared_length(a: Any, b: Any, tol: float) -> float:
    return float(a.boundary.intersection(b.buffer(tol)).length)


def unit_table(models: dict[str, SpatialModel], brands: list[Brand], cal: Calibration) -> pd.DataFrame:
    """One row per occupied unit and actual revision with planted sales weights.

    ``weight`` = area^beta x (1 + A*exp(-d/L)) x attractiveness x cafe-adjacency uplift x moved-brand factor,
    where ``d`` (``esc_dist_m``) is the Euclidean distance from the unit centroid to the nearest escalator
    footprint on the same floor (an exposure proxy for walking distance).
    """
    by_id = {b.brand_id: b for b in brands}
    amp, length = cal.value("space.exposure_amplitude"), cal.value("space.exposure_length_m")
    attr_exp = cal.value("space.attractiveness_exponent")
    adjacency = cal.tree["planted"]["b_cafe_adjacency"]
    fashion = set(adjacency["fashion_categories"]["value"])
    traffic_uplift, aov_uplift = adjacency["traffic_uplift"]["value"], adjacency["aov_uplift"]["value"]
    min_shared = adjacency["min_shared_m"]["value"]
    rows: list[dict[str, Any]] = []
    for rev in REVS:
        model = models[rev]
        for floor in FLOOR_CODES:
            escalators = unary_union(
                [
                    v.geometry
                    for v in model.by_kind(EntityKind.VERTICAL_LINK, floor)
                    if v.attributes.get("link_type") == "escalator"
                ]
            )
            units = [u for u in model.units(floor) if not u.attributes.get("vacant")]
            cafes = [u for u in units if u.attributes["category"] == "CAFE"]
            for unit in units:
                attrs = unit.attributes
                category = attrs["category"]
                dist = float(unit.geometry.centroid.distance(escalators)) if not escalators.is_empty else 60.0
                cafe_adjacent = category in fashion and any(
                    unit.geometry.distance(c.geometry) <= 0.05
                    and _shared_length(unit.geometry, c.geometry, 0.05) >= min_shared
                    for c in cafes
                )
                brand = by_id[attrs["brand_id"]]
                exposure = 1.0 + amp * math.exp(-dist / length)
                weight = (
                    unit.geometry.area ** CATEGORIES[category].space_beta
                    * exposure
                    * brand.attractiveness**attr_exp
                    * (1.0 + traffic_uplift if cafe_adjacent else 1.0)
                )
                rows.append(
                    {
                        "rev": rev,
                        "unit_code": attrs["unit_code"],
                        "floor": floor,
                        "band": attrs.get("band"),
                        "category": category,
                        "brand_id": brand.brand_id,
                        "area_m2": round(unit.geometry.area, 2),
                        "cx": round(unit.geometry.centroid.x, 3),
                        "cy": round(unit.geometry.centroid.y, 3),
                        "bounds": tuple(round(v, 3) for v in unit.geometry.bounds),
                        "esc_dist_m": round(dist, 2),
                        "exposure": exposure,
                        "cafe_adjacent": cafe_adjacent,
                        "weight": weight,
                        "aov_mult": 1.0 + aov_uplift if cafe_adjacent else 1.0,
                        "brand_factor": 1.0,
                    }
                )
    table = pd.DataFrame(rows)
    _apply_moved_brand_uplift(table, cal)
    return table


def moved_brands(table: pd.DataFrame, rev_from: str, rev_to: str) -> list[tuple[str, str, str]]:
    """``(brand_id, unit_from, unit_to)`` for brands relocated to a unit that does not cover their old space.

    Merges, splits and resizes keep the old unit centroid inside the new unit and are not relocations.
    """
    before = table[table.rev == rev_from].set_index("brand_id")
    after = table[table.rev == rev_to].set_index("brand_id")
    moves = []
    for brand_id, new in after.iterrows():
        if brand_id not in before.index:
            continue
        old = before.loc[brand_id]
        x0, y0, x1, y1 = new["bounds"]
        covered = x0 <= old["cx"] <= x1 and y0 <= old["cy"] <= y1 and new["floor"] == old["floor"]
        if old["unit_code"] != new["unit_code"] and not covered:
            moves.append((str(brand_id), str(old["unit_code"]), str(new["unit_code"])))
    return sorted(moves)


def _apply_moved_brand_uplift(table: pd.DataFrame, cal: Calibration) -> None:
    """Planted effect h: the R1 3F relocated brand gets ``uplift`` more share of its floor-category sales."""
    uplift = cal.value("planted.h_md_relayout.moved_brand_uplift")
    for brand_id, unit_from, _ in moved_brands(table, "R0", "R1"):
        if not unit_from.startswith("3F-"):
            continue
        r0 = table[(table.rev == "R0") & (table.floor == "3F")]
        category = r0.loc[r0.brand_id == brand_id, "category"].iloc[0]
        r0 = r0[r0.category == category]
        share0 = float(r0.loc[r0.brand_id == brand_id, "weight"].iloc[0] / r0["weight"].sum())
        target = min((1.0 + uplift) * share0, 0.95)
        factor = 1.0
        for rev in ("R1", "R2"):
            mask = (table.rev == rev) & (table.floor == "3F") & (table.category == category)
            own = mask & (table.brand_id == brand_id)
            if not own.any():
                continue
            others = float(table.loc[mask & ~own, "weight"].sum())
            if rev == "R1":
                factor = target * others / (1.0 - target) / float(table.loc[own, "weight"].iloc[0])
            table.loc[own, "weight"] *= factor
            table.loc[own, "brand_factor"] = factor


def revision_windows(cal: Calibration) -> dict[str, tuple[date, date]]:
    """Inclusive ``(valid_from, valid_to)`` of each actual revision inside the data period."""
    eff = {rev: date.fromisoformat(d) for rev, d in cal.value("period.revision_effective_dates").items()}
    end = date.fromisoformat(cal.value("period.end"))
    windows = {}
    for i, rev in enumerate(REVS):
        stop = eff[REVS[i + 1]] - timedelta(days=1) if i + 1 < len(REVS) else end
        windows[rev] = (eff[rev], stop)
    return windows


def commission_rate(brand: Brand, seed: int) -> float:
    lo, hi = CATEGORIES[brand.category].commission_range
    return round(float(stable_rng(seed, "commission", brand.brand_id).uniform(lo, hi)) / 0.005) * 0.005


def build_tenancies(units: pd.DataFrame, brands: list[Brand], cal: Calibration, seed: int) -> pd.DataFrame:
    by_id = {b.brand_id: b for b in brands}
    windows = revision_windows(cal)
    rows = [
        {
            "brand_id": r.brand_id,
            "unit_code": r.unit_code,
            "rev_id": r.rev,
            "valid_from": windows[r.rev][0].isoformat(),
            "valid_to": windows[r.rev][1].isoformat(),
            "commission_rate": round(commission_rate(by_id[r.brand_id], seed), 3),
        }
        for r in units.sort_values(["rev", "unit_code"]).itertuples()
    ]
    return pd.DataFrame(rows)


def build_pos_terminals(units: pd.DataFrame, cal: Calibration) -> pd.DataFrame:
    """1-4 terminals per brand by first unit area; terminals follow the brand when it moves."""
    windows = revision_windows(cal)
    thresholds = cal.value("pos.terminals_by_area")
    ordered = units.sort_values(["rev", "unit_code"])
    first = ordered.drop_duplicates("brand_id")
    rows: list[dict[str, Any]] = []
    seq = 0
    for brand in first.itertuples():
        n_pos = next(n for limit, n in thresholds if brand.area_m2 <= limit)
        pos_ids = [f"P{seq + i + 1:05d}" for i in range(n_pos)]
        seq += n_pos
        spans: list[list[Any]] = []
        for r in ordered[ordered.brand_id == brand.brand_id].itertuples():
            start, stop = windows[r.rev]
            if spans and spans[-1][0] == r.unit_code and spans[-1][2] + timedelta(days=1) == start:
                spans[-1][2] = stop
            else:
                spans.append([r.unit_code, start, stop])
        for pos_id in pos_ids:
            for unit_code, start, stop in spans:
                rows.append(
                    {
                        "pos_id": pos_id,
                        "brand_id": brand.brand_id,
                        "unit_code": unit_code,
                        "valid_from": start.isoformat(),
                        "valid_to": stop.isoformat(),
                    }
                )
    return pd.DataFrame(rows)


def build_calendar(cal: Calibration) -> pd.DataFrame:
    """Daily calendar with holidays, opening hours, traffic index and the revision in effect."""
    start, end = date.fromisoformat(cal.value("period.start")), date.fromisoformat(cal.value("period.end"))
    holidays = {h["date"]: h for h in cal.value("calendar.holidays")}
    mains = [date.fromisoformat(h["date"]) for h in holidays.values() if h["kind"] == "lunar_main"]
    monthly, dow_index = cal.value("calendar.monthly_index"), cal.value("calendar.dow_index")
    hours = cal.value("calendar.opening_hours")
    closed_main = cal.value("calendar.closed_on_lunar_new_year_and_chuseok")
    gift_days, gift_index = cal.value("calendar.gift_season_days"), cal.value("calendar.gift_season_index")
    windows = revision_windows(cal)
    dow_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    rows = []
    day = start
    while day <= end:
        holiday = holidays.get(day.isoformat())
        kind = holiday["kind"] if holiday else ""
        dow = day.weekday()
        closed = kind == "lunar_main" and bool(closed_main)
        weekend_like = dow >= 5 or bool(holiday)
        traffic = monthly[day.month] * (
            cal.value("calendar.public_holiday_index") if holiday else dow_index[dow_names[dow]]
        )
        if kind == "lunar_window":
            traffic *= cal.value("calendar.lunar_holiday_window_index")
        gift = ""
        if not holiday:
            for main in mains:
                if 1 <= (main - day).days <= gift_days:
                    gift = "seollal" if main.month in (1, 2) else "chuseok"
                    traffic *= gift_index
        opening = hours["weekend" if weekend_like else "weekday"]
        rows.append(
            {
                "date": day.isoformat(),
                "year": day.year,
                "month": day.month,
                "day": day.day,
                "dow": dow,
                "dow_name": "월화수목금토일"[dow],
                "is_weekend": dow >= 5,
                "is_holiday": bool(holiday),
                "holiday_name": holiday["name"] if holiday else "",
                "day_type": "weekend_holiday" if weekend_like else "weekday",
                "is_closed": closed,
                "open_time": "" if closed else opening[0],
                "close_time": "" if closed else opening[1],
                "gift_season": gift,
                "month_index": monthly[day.month],
                "traffic_index": 0.0 if closed else round(traffic, 4),
                "rev_id": next(r for r, (a, b) in windows.items() if a <= day <= b),
            }
        )
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def _shares(mapping: dict[str, float]) -> tuple[list[str], np.ndarray]:
    keys = list(mapping)
    values = np.array([float(mapping[k]) for k in keys])
    return keys, values / values.sum()


def expected_trips_per_member(cal: Calibration) -> float:
    """Mean expected trips of a member over the period (tier mix x visit rate x active years)."""
    tiers, shares = _shares(cal.value("customers.tier_share"))
    rates = cal.value("customers.trips_per_year")
    start, end = date.fromisoformat(cal.value("period.start")), date.fromisoformat(cal.value("period.end"))
    days = (end - start).days + 1
    new = cal.value("customers.new_member_share")
    active_days = (1 - new) * days + new * (days + 90) / 2
    return float(sum(s * rates[t] for t, s in zip(tiers, shares, strict=True)) * active_days / 365.0)


def build_customers(n: int, cal: Calibration, seed: int) -> pd.DataFrame:
    """Members with demographics, tier, join date and home area (internal ``_`` columns drive the simulation)."""
    rng = stable_rng(seed, "customers")
    start, end = date.fromisoformat(cal.value("period.start")), date.fromisoformat(cal.value("period.end"))
    period_days = (end - start).days + 1
    genders, g_p = _shares(cal.value("customers.gender_share"))
    ages, a_p = _shares(cal.value("customers.age_band_share"))
    tiers, t_p = _shares(cal.value("customers.tier_share"))
    areas, h_p = _shares(cal.value("customers.home_area_share"))
    gender = rng.choice(len(genders), size=n, p=g_p)
    age = rng.choice(len(ages), size=n, p=a_p)
    tier_key = rng.choice(len(tiers), size=n, p=t_p)
    tier = np.array([TIER_LIST.index(tiers[i]) for i in tier_key])
    first = date.fromisoformat(cal.value("customers.join_date_start"))
    existing_span = (start - first).days
    is_new = rng.random(n) < cal.value("customers.new_member_share")
    offset = np.where(
        is_new,
        rng.integers(0, max(period_days - 90, 1), n),
        -rng.integers(1, existing_span + 1, n),
    )
    join = np.datetime64(start.isoformat()) + offset.astype("timedelta64[D]")
    shape = cal.value("customers.trip_rate_gamma_shape")
    rates = cal.value("customers.trips_per_year")
    rate = np.array([rates[TIER_LIST[t]] for t in tier]) * rng.gamma(shape, 1.0 / shape, n)
    join_day = np.maximum(offset, 0)
    expected = rate * (period_days - join_day) / 365.0
    return pd.DataFrame(
        {
            "pseudo_id": [f"C{i + 1:07d}" for i in range(n)],
            "age_band": [ages[i] for i in age],
            "gender": [genders[i] for i in gender],
            "tier": [TIER_LIST[t] for t in tier],
            "join_date": np.datetime_as_string(join, unit="D"),
            "home_area": [areas[i] for i in rng.choice(len(areas), size=n, p=h_p)],
            "_gender": [GENDER_LIST.index(genders[i]) for i in gender],
            "_age": [AGE_LIST.index(ages[i]) for i in age],
            "_tier": tier,
            "_join_day": join_day,
            "_expected_trips": expected,
        }
    )


@dataclass
class Purchases:
    """Simulated purchases (one row per receipt) in visiting order."""

    trip: np.ndarray
    day: np.ndarray
    minute: np.ndarray
    unit: np.ndarray
    category: np.ndarray
    floor: np.ndarray
    customer: np.ndarray
    tier: np.ndarray
    amount: np.ndarray
    items: np.ndarray
    payment: np.ndarray
    pos_id: np.ndarray
    n_trips: int

    def __len__(self) -> int:
        return len(self.trip)


class TripSimulator:
    """Vectorized shopping-trip simulation over the unit sales weights of each revision."""

    def __init__(
        self,
        cal: Calibration,
        units: pd.DataFrame,
        calendar: pd.DataFrame,
        brands: list[Brand],
        pos: pd.DataFrame,
        seed: int,
    ) -> None:
        self.cal, self.seed = cal, seed
        self.units = units.reset_index(drop=True)
        self.n_days = len(calendar)
        self.day_type = (calendar["day_type"] == "weekend_holiday").to_numpy().astype(int)
        self.month = calendar["month"].to_numpy()
        self.gift = (calendar["gift_season"] != "").to_numpy().astype(int)
        self.rev = calendar["rev_id"].map({r: i for i, r in enumerate(REVS)}).to_numpy()
        self.open_min = np.array([_minutes(t) if t else 0 for t in calendar["open_time"]])
        self.close_min = np.array([_minutes(t) if t else 0 for t in calendar["close_time"]])
        traffic = calendar["traffic_index"].to_numpy(dtype=float)
        self.day_pmf = traffic / traffic.sum()
        self._init_preferences()
        self._init_units(brands, pos)
        self._init_planted(brands)

    # -- setup ---------------------------------------------------------------------------------
    def _init_preferences(self) -> None:
        cal = self.cal
        ticket = np.array([CATEGORIES[c].mean_ticket_krw for c in CATS], dtype=float)
        base = _by_category(cal.value("categories.sales_share"), 0.0) / ticket
        aff = cal.value("categories.segment_affinity")
        self.pi = np.zeros((N_SEG, N_CATS))
        for g, gender in enumerate(GENDER_LIST):
            for a, age in enumerate(AGE_LIST):
                for t, tier in enumerate(TIER_LIST):
                    v = base * _by_category(aff["gender"][gender]) * _by_category(aff["age_band"][age])
                    self.pi[seg_id(g, a, t)] = v * _by_category(aff["tier"][tier])
        planted_a = cal.tree["planted"]["a_segment_cross_shopping"]
        focal = planted_a["focal_segment"]["value"]
        self.focal_seg = np.zeros(N_SEG, dtype=bool)
        damp = planted_a["focal_base_damping"]["value"]
        for tier in focal["tiers"]:
            s = seg_id(
                GENDER_LIST.index(focal["gender"]), AGE_LIST.index(focal["age_band"]), TIER_LIST.index(tier)
            )
            self.focal_seg[s] = True
            for c in ("COSMETICS", "GLOBAL_FASHION", "LUXURY"):
                self.pi[s, CAT_IX[c]] *= damp
        self.pi /= self.pi.sum(axis=1, keepdims=True)
        time_cfg = cal.tree["planted"]["e_time_patterns"]
        lunch, kids = time_cfg["weekday_lunch"]["value"], time_cfg["weekend_kids_sports"]["value"]
        self.lunch_window = (_minutes(lunch["start"]), _minutes(lunch["end"]))
        self.kids_window = (_minutes(kids["start"]), _minutes(kids["end"]))
        evening = time_cfg["weekday_luxury_evening"]["value"]
        self.evening_window = (_minutes(evening["start"]), _minutes(evening["end"]))
        self.lux_evening_mult = evening["aov_multiplier"]
        self.ctx_mult = np.ones((8, N_CATS))
        self.ctx_mult[0 * 4 + SLOT_LUNCH, FNB] *= lunch["fnb_multiplier"]
        for c in kids["categories"]:
            self.ctx_mult[1 * 4 + SLOT_AFTERNOON, CAT_IX[c]] *= kids["multiplier"]
        dinner = time_cfg["dinner_restaurant_multiplier"]["value"]
        self.ctx_mult[[SLOT_EVENING, 4 + SLOT_EVENING], CAT_IX["RESTAURANT"]] *= dinner
        self.month_mult = np.ones((13, N_CATS))
        for c, factors in cal.value("categories.monthly_factor").items():
            for m, f in factors.items():
                self.month_mult[int(m), CAT_IX[c]] = f
        self.gift_mult = np.ones((2, N_CATS))
        for c, f in cal.value("calendar.gift_season_category_multiplier").items():
            self.gift_mult[1, CAT_IX[c]] = f
        self.ticket = ticket
        self.sigma = _by_category(cal.value("categories.ticket_sigma"))
        self.items_lambda = _by_category(cal.value("categories.items_lambda"))
        self.return_rate = _by_category(cal.value("categories.return_rate"))
        spend = cal.value("customers.spend_multiplier")
        self.spend = np.array([spend[t] for t in TIER_LIST])
        extra = cal.value("customers.extra_purchases_lambda")
        self.extra_lambda = np.array([extra[t] for t in TIER_LIST])
        self.max_k = int(cal.value("customers.max_purchases_per_trip"))
        names, self.pay_p = _shares(cal.value("payment.mix"))
        high_names, self.pay_high_p = _shares(cal.value("payment.high_tier_mix"))
        assert tuple(names) == PAYMENTS and tuple(high_names) == PAYMENTS
        self.arrival_bins = {dt: self._arrival_bins(dt) for dt in (0, 1)}

    def _arrival_bins(self, day_type: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        hours = self.cal.value("calendar.opening_hours")["weekend" if day_type else "weekday"]
        profile = self.cal.value("calendar.hourly_arrival_profile")["weekend" if day_type else "weekday"]
        open_m, close_m = _minutes(hours[0]), _minutes(hours[1]) - 20
        starts, ends, weights = [], [], []
        for hour, weight in profile.items():
            lo, hi = max(int(hour) * 60, open_m), min((int(hour) + 1) * 60, close_m)
            if hi > lo:
                starts.append(lo)
                ends.append(hi)
                weights.append(weight * (hi - lo) / 60)
        w = np.array(weights)
        return np.array(starts), np.array(ends), w / w.sum()

    def _init_units(self, brands: list[Brand], pos: pd.DataFrame) -> None:
        u = self.units
        self.u_rev = u["rev"].map({r: i for i, r in enumerate(REVS)}).to_numpy()
        self.u_floor = u["floor"].map(FLOOR_IX).to_numpy()
        self.u_cat = u["category"].map(CAT_IX).to_numpy()
        self.brand_ix = {b.brand_id: i for i, b in enumerate(brands)}
        self.u_brand = u["brand_id"].map(self.brand_ix).to_numpy()
        self.u_weight = u["weight"].to_numpy(dtype=float)
        self.u_aov = u["aov_mult"].to_numpy(dtype=float)
        self.W = np.zeros((len(REVS), len(FLOOR_CODES), N_CATS))
        np.add.at(self.W, (self.u_rev, self.u_floor, self.u_cat), self.u_weight)
        self.present = self.W > 0
        self.present_any = self.present.any(axis=1)
        self.groups: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        key = self._group_key(self.u_rev, self.u_floor, self.u_cat)
        for k in np.unique(key):
            rows = np.nonzero(key == k)[0]
            self.groups[int(k)] = (rows, self.u_weight[rows] / self.u_weight[rows].sum())
        tiers = self.cal.value("space.price_tier_multiplier")
        raw = np.array([tiers[b.price_tier] for b in brands], dtype=float)
        cats = np.array([b.category for b in brands])
        self.price_mult = raw.copy()
        for c in set(cats):
            mask = cats == c
            self.price_mult[mask] = raw[mask] / raw[mask].mean()
        self.brand_pos = np.full((len(brands), 4), "", dtype=object)
        self.n_pos = np.zeros(len(brands), dtype=int)
        for brand_id, group in pos.groupby("brand_id", sort=False):
            ids = list(dict.fromkeys(group["pos_id"]))
            b = self.brand_ix[brand_id]
            self.brand_pos[b, : len(ids)] = ids
            self.n_pos[b] = len(ids)
        last_rev = u.groupby("brand_id")["rev"].max()
        end_of = {r: i for i, r in enumerate(REVS)}
        self.brand_last_rev = np.full(len(brands), -1)
        for brand_id, rev in last_rev.items():
            self.brand_last_rev[self.brand_ix[brand_id]] = end_of[rev]

    @staticmethod
    def _group_key(rev: Any, floor: Any, cat: Any) -> Any:
        return (rev * len(FLOOR_CODES) + floor) * N_CATS + cat

    def _init_planted(self, brands: list[Brand]) -> None:
        """Focal cosmetics brand and partner brands present in every revision (planted effect a)."""
        cfg = self.cal.tree["planted"]["a_segment_cross_shopping"]
        u = self.units
        stable = u.groupby("brand_id")["rev"].nunique()
        stable = set(stable[stable == len(REVS)].index)
        attractiveness = {b.brand_id: b.attractiveness for b in brands}
        cos = sorted(
            (b for b in stable if u.loc[u.brand_id == b, "category"].iloc[0] == "COSMETICS"),
            key=lambda b: (-attractiveness[b], b),
        )
        partners = sorted(
            (b for b in stable if u.loc[u.brand_id == b, "category"].iloc[0] in ("GLOBAL_FASHION", "LUXURY")),
            key=lambda b: (-attractiveness[b], b),
        )
        self.focal_brand = cos[0]
        self.partner_brands = partners[: int(cfg["n_partners"]["value"])]
        self.focal_brand_share = cfg["focal_brand_share"]["value"]
        self.partner_share = cfg["partner_share"]["value"]
        self.combo_prob = cfg["combo_trip_prob"]["value"]
        self.brand_unit = {
            (b, r): int(np.nonzero((self.u_brand == self.brand_ix[b]) & (self.u_rev == r))[0][0])
            for b in [self.focal_brand, *self.partner_brands]
            for r in range(len(REVS))
        }
        d = self.cal.tree["planted"]["d_fountain_shower"]
        self.fountain_prob = d["fountain_link_prob"]["value"]
        self.fountain_cos = d["fountain_cosmetics_share"]["value"]
        self.shower_prob = d["shower_restaurant_first_prob"]["value"]
        cross = self.cal.value("planted.h_md_relayout.floor3_cross_category_link_prob")
        self.cross3 = np.array([cross[r] for r in REVS])
        self.same_floor_prob = self.cal.value("trips.same_floor_link_prob")
        self.beauty_fashion_prob = self.cal.value("trips.beauty_fashion_link_prob")

    # -- sampling helpers ----------------------------------------------------------------------
    def arrivals(self, day_type: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        minute = np.zeros(len(day_type))
        for dt, (starts, ends, p) in self.arrival_bins.items():
            rows = np.nonzero(day_type == dt)[0]
            b = rng.choice(len(p), size=len(rows), p=p)
            minute[rows] = starts[b] + rng.random(len(rows)) * (ends[b] - starts[b])
        return minute

    def slots(self, day_type: np.ndarray, minute: np.ndarray) -> np.ndarray:
        slot = np.full(len(minute), SLOT_OTHER)
        slot[(day_type == 0) & (minute >= self.lunch_window[0]) & (minute < self.lunch_window[1])] = (
            SLOT_LUNCH
        )
        slot[(day_type == 1) & (minute >= self.kids_window[0]) & (minute < self.kids_window[1])] = (
            SLOT_AFTERNOON
        )
        slot[minute >= self.evening_window[0]] = SLOT_EVENING
        return slot

    def _category_weights(self, t: np.ndarray, ctx: dict[str, np.ndarray]) -> np.ndarray:
        return (
            self.pi[ctx["seg"][t]]
            * self.ctx_mult[ctx["ctx"][t]]
            * self.month_mult[ctx["month"][t]]
            * self.gift_mult[ctx["gift"][t]]
        )

    def _independent(self, t: np.ndarray, ctx: dict[str, np.ndarray], rng: np.random.Generator) -> np.ndarray:
        out = np.empty(len(t), dtype=int)
        for lo in range(0, len(t), 100_000):
            tt = t[lo : lo + 100_000]
            w = self._category_weights(tt, ctx) * self.present_any[ctx["rev"][tt]]
            out[lo : lo + 100_000] = _sample_rows(w, rng)
        return out

    def _same_floor(
        self, t: np.ndarray, floor: np.ndarray, ctx: dict[str, np.ndarray], rng: np.random.Generator
    ) -> np.ndarray:
        w = self._category_weights(t, ctx) * self.present[ctx["rev"][t], floor]
        return _sample_rows(w, rng)

    def _floor_for(self, rev: np.ndarray, cat: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        return _sample_rows(self.W[rev, :, cat], rng)

    def _pick_units(
        self, rev: np.ndarray, floor: np.ndarray, cat: np.ndarray, rng: np.random.Generator
    ) -> np.ndarray:
        key = self._group_key(rev, floor, cat)
        unit = np.full(len(key), -1)
        order = np.argsort(key, kind="stable")
        bounds = np.flatnonzero(np.diff(key[order])) + 1
        for chunk in np.split(order, bounds):
            if not len(chunk):
                continue
            k = int(key[chunk[0]])
            if k not in self.groups:
                raise ValueError(f"no units for purchase group {k}")
            rows, p = self.groups[k]
            unit[chunk] = rows[rng.choice(len(rows), size=len(chunk), p=p)]
        return unit

    # -- simulation ----------------------------------------------------------------------------
    def simulate(
        self,
        day: np.ndarray,
        customer: np.ndarray,
        gender: np.ndarray,
        age: np.ndarray,
        tier: np.ndarray,
        label: str,
    ) -> Purchases:
        """Simulate trips (one per row) and return their purchases in visiting order."""
        rng = stable_rng(self.seed, "trips", label)
        n = len(day)
        day_type = self.day_type[day]
        arrival = self.arrivals(day_type, rng)
        ctx = {
            "seg": seg_id(gender, age, tier),
            "ctx": day_type * 4 + self.slots(day_type, arrival),
            "month": self.month[day],
            "gift": self.gift[day],
            "rev": self.rev[day],
        }
        entry_b1 = rng.random(n) < self.cal.value("trips.entry_floor_share")["B1"]
        k = 1 + np.minimum(rng.poisson(self.extra_lambda[tier]), self.max_k - 1)
        combo = self.focal_seg[ctx["seg"]] & (rng.random(n) < self.combo_prob)
        k = np.where(combo, np.maximum(k, 2), k)
        shower = rng.random(n) < self.shower_prob
        fashion_pick = np.where(rng.random(n) < 0.5, CAT_IX["GLOBAL_FASHION"], CAT_IX["LUXURY"])
        cosmetics_second = rng.random(n) < 0.5
        offsets = np.concatenate([[0], np.cumsum(k)[:-1]])
        trip = np.repeat(np.arange(n), k)
        slot = np.arange(len(trip)) - np.repeat(offsets, k)
        rev = ctx["rev"][trip]
        cat = np.full(len(trip), -1)
        floor = np.full(len(trip), -1)
        cos, wc = CAT_IX["COSMETICS"], CAT_IX["WOMEN_CONTEMPORARY"]
        for s in range(self.max_k):
            rows = np.nonzero(slot == s)[0]
            if not len(rows):
                break
            t = trip[rows]
            todo = np.ones(len(rows), dtype=bool)
            if s < 2:
                m = combo[t]
                is_cos = cosmetics_second[t] if s == 1 else ~cosmetics_second[t]
                cat[rows[m]] = np.where(is_cos[m], cos, fashion_pick[t[m]])
                todo &= ~m
            if s >= 1:
                pc, pf = cat[rows - 1], floor[rows - 1]
                u = rng.random((len(rows), 6))
                m = (
                    todo
                    & (pf == FLOOR_IX["B1"])
                    & FNB[pc]
                    & (u[:, 0] < self.fountain_prob)
                    & (u[:, 1] < self.fountain_cos)
                )
                cat[rows[m]], floor[rows[m]] = cos, FLOOR_IX["1F"]
                todo &= ~m
                m = todo & (pf == FLOOR_IX["3F"]) & (pc == wc) & (u[:, 2] < self.cross3[rev[rows]])
                cat[rows[m]] = np.where(u[m, 4] < 0.6, CAT_IX["ACCESSORIES"], CAT_IX["CAFE"])
                floor[rows[m]] = FLOOR_IX["3F"]
                todo &= ~m
                fashion = (pc == CAT_IX["GLOBAL_FASHION"]) | (pc == CAT_IX["LUXURY"])
                m = todo & ((pc == cos) | fashion) & (u[:, 5] < self.beauty_fashion_prob)
                cat[rows[m]] = np.where(pc[m] == cos, fashion_pick[t[m]], cos)
                todo &= ~m
                m = todo & (u[:, 3] < self.same_floor_prob)
                if m.any():
                    cat[rows[m]] = self._same_floor(t[m], pf[m], ctx, rng)
                    floor[rows[m]] = pf[m]
                todo &= ~m
            if todo.any():
                cat[rows[todo]] = self._independent(t[todo], ctx, rng)
            need = rows[floor[rows] < 0]
            floor[need] = self._floor_for(rev[need], cat[need], rng)
        unit = self._pick_units(rev, floor, cat, rng)
        self._plant_combo_brands(unit, cat, rev, combo[trip] & (slot < 2), rng)
        cat, floor = self.u_cat[unit], self.u_floor[unit]

        # Visiting order: shower trips eat at 8F first and shop downward; others go up from the entry floor.
        is_rest = (cat == CAT_IX["RESTAURANT"]) & (floor == FLOOR_IX["8F"])
        has_rest = np.zeros(n, dtype=bool)
        has_rest[trip[is_rest]] = True
        ordinal = FLOOR_ORDINAL[floor]
        key = np.where(entry_b1[trip], ordinal + 1, np.where(ordinal >= 0, ordinal, 50))
        key = np.where((has_rest & shower)[trip], np.where(is_rest, -1, 100 - ordinal), key)
        order = np.lexsort((slot, key, trip))
        trip, cat, floor, unit = trip[order], cat[order], floor[order], unit[order]
        first = np.r_[True, trip[1:] != trip[:-1]]
        lo, hi = self.cal.value("trips.first_purchase_gap_min")
        mean_gap = self.cal.value("trips.next_purchase_gap_mean_min")
        gap = np.where(
            first, rng.uniform(lo, hi, len(trip)), rng.lognormal(math.log(mean_gap) - 0.125, 0.5, len(trip))
        )
        cum = np.cumsum(gap)
        start = np.maximum.accumulate(np.where(first, cum - gap, -np.inf))
        minute = arrival[trip] + cum - start
        keep = minute <= self.close_min[day[trip]] - 3
        trip, cat, floor, unit, minute = trip[keep], cat[keep], floor[keep], unit[keep], minute[keep]

        m = len(trip)
        d, tr_tier = day[trip], tier[trip]
        brand = self.u_brand[unit]
        mean = self.ticket[cat] * self.price_mult[brand] * self.spend[tr_tier] * self.u_aov[unit]
        evening = (
            (self.day_type[d] == 0)
            & (cat == CAT_IX["LUXURY"])
            & (minute >= self.evening_window[0])
            & (minute < self.evening_window[1])
        )
        mean = np.where(evening, mean * self.lux_evening_mult, mean)
        sigma = self.sigma[cat]
        amount = mean * np.exp(sigma * rng.standard_normal(m) - sigma**2 / 2)
        amount = np.maximum(np.round(amount / 100) * 100, 1000).astype(np.int64)
        items = 1 + rng.poisson(self.items_lambda[cat])
        high = tr_tier >= TIER_LIST.index("VIP")
        high &= tr_tier != ANON
        payment = np.where(
            high, rng.choice(len(PAYMENTS), m, p=self.pay_high_p), rng.choice(len(PAYMENTS), m, p=self.pay_p)
        )
        pos_ix = (rng.random(m) * self.n_pos[brand]).astype(int)
        return Purchases(
            trip=trip,
            day=d,
            minute=minute,
            unit=unit,
            category=cat,
            floor=floor,
            customer=customer[trip],
            tier=tr_tier,
            amount=amount,
            items=items,
            payment=payment,
            pos_id=self.brand_pos[brand, pos_ix],
            n_trips=n,
        )

    def _plant_combo_brands(
        self,
        unit: np.ndarray,
        cat: np.ndarray,
        rev: np.ndarray,
        combo_rows: np.ndarray,
        rng: np.random.Generator,
    ) -> None:
        """Planted effect a: combo trips favor the focal cosmetics brand and its partner brands."""
        u = rng.random(len(unit))
        pick = rng.integers(0, len(self.partner_brands), len(unit))
        cos = cat == CAT_IX["COSMETICS"]
        for r in range(len(REVS)):
            mask = combo_rows & cos & (rev == r) & (u < self.focal_brand_share)
            unit[mask] = self.brand_unit[(self.focal_brand, r)]
            for j, brand_id in enumerate(self.partner_brands):
                mask = combo_rows & ~cos & (rev == r) & (u < self.partner_share) & (pick == j)
                unit[mask] = self.brand_unit[(brand_id, r)]

    def returns(self, p: Purchases, label: str) -> dict[str, np.ndarray]:
        """Full refunds at the same brand 1-30 days later, within the brand's tenancy and the period."""
        rng = stable_rng(self.seed, "returns", label)
        lo, hi = self.cal.value("categories.return_window_days")
        src = np.nonzero(rng.random(len(p)) < self.return_rate[p.category])[0]
        rday = p.day[src] + rng.integers(lo, hi + 1, len(src))
        for _ in range(3):
            closed = (rday < self.n_days) & (self.close_min[np.minimum(rday, self.n_days - 1)] == 0)
            rday[closed] += 1
        brand = self.u_brand[p.unit[src]]
        rev_end = np.array([np.nonzero(self.rev == r)[0].max() for r in range(len(REVS))])
        ok = (rday < self.n_days) & (rday <= rev_end[self.brand_last_rev[brand]])
        src, rday, brand = src[ok], rday[ok], brand[ok]
        open_m, close_m = self.open_min[rday], self.close_min[rday]
        minute = open_m + rng.random(len(src)) * (close_m - open_m - 10)
        pos_ix = (rng.random(len(src)) * self.n_pos[brand]).astype(int)
        return {"source": src, "day": rday, "minute": minute, "pos_id": self.brand_pos[brand, pos_ix]}


def _trip_tier_mix(cal: Calibration) -> tuple[np.ndarray, np.ndarray]:
    """Member trips by tier: tier share x visit rate."""
    tiers, shares = _shares(cal.value("customers.tier_share"))
    rates = cal.value("customers.trips_per_year")
    w = np.array([s * rates[t] for t, s in zip(tiers, shares, strict=True)])
    return np.array([TIER_LIST.index(t) for t in tiers]), w / w.sum()


def _demographics(n: int, cal: Calibration, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    genders, g_p = _shares(cal.value("customers.gender_share"))
    ages, a_p = _shares(cal.value("customers.age_band_share"))
    gender = np.array([GENDER_LIST.index(genders[i]) for i in rng.choice(len(genders), n, p=g_p)])
    age = np.array([AGE_LIST.index(ages[i]) for i in rng.choice(len(ages), n, p=a_p)])
    return gender, age


def pilot_rates(sim: TripSimulator, cal: Calibration, seed: int, n: int = 40_000) -> dict[str, float]:
    """Sales and purchases per trip for member and anonymous trips (sizes the main simulation)."""
    rng = stable_rng(seed, "pilot")
    day = rng.choice(sim.n_days, size=n, p=sim.day_pmf)
    member = np.arange(n) < n // 2
    tiers, p = _trip_tier_mix(cal)
    tier = np.where(member, tiers[rng.choice(len(tiers), n, p=p)], ANON)
    gender, age = _demographics(n, cal, rng)
    result = sim.simulate(day, np.full(n, -1), gender, age, tier, "pilot")
    by_member = member[result.trip]
    n_member, n_anon = member.sum(), n - member.sum()
    return {
        "sales_member": float(result.amount[by_member].sum() / n_member),
        "sales_anon": float(result.amount[~by_member].sum() / n_anon),
        "purchases_member": float(by_member.sum() / n_member),
        "purchases_anon": float((~by_member).sum() / n_anon),
    }


def _member_trip_days(
    customers: pd.DataFrame, n_trips: int, pmf: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Allocate member trips (Poisson around expected visits) to days after joining; one trip per day."""
    expected = customers["_expected_trips"].to_numpy()
    counts = rng.poisson(expected * n_trips / expected.sum())
    cust = np.repeat(np.arange(len(customers)), counts)
    cdf = np.cumsum(pmf)
    join = customers["_join_day"].to_numpy()[cust]
    lo = np.where(join > 0, cdf[np.maximum(join - 1, 0)], 0.0)

    def draw(rows: np.ndarray) -> np.ndarray:
        u = lo[rows] + rng.random(len(rows)) * (1.0 - lo[rows])
        return np.minimum(np.searchsorted(cdf, u, side="right"), len(pmf) - 1)

    day = draw(np.arange(len(cust)))
    for _ in range(6):
        key = cust.astype(np.int64) * len(pmf) + day
        _, first = np.unique(key, return_index=True)
        dup = np.setdiff1d(np.arange(len(key)), first)
        if not len(dup):
            break
        day[dup] = draw(dup)
    key = cust.astype(np.int64) * len(pmf) + day
    _, first = np.unique(key, return_index=True)
    keep = np.sort(first)
    return cust[keep], day[keep]


@dataclass
class BusinessData:
    tables: dict[str, pd.DataFrame]
    units: pd.DataFrame
    meta: dict[str, Any]


def categories_table(cal: Calibration) -> pd.DataFrame:
    share = cal.value("categories.sales_share")
    returns = _by_category(cal.value("categories.return_rate"))
    return pd.DataFrame(
        [
            {
                "category": c.code,
                "name_ko": c.name_ko,
                "mean_ticket_krw": c.mean_ticket_krw,
                "commission_min": c.commission_range[0],
                "commission_max": c.commission_range[1],
                "space_beta": c.space_beta,
                "unit_area_min_m2": c.unit_area_range_m2[0],
                "unit_area_max_m2": c.unit_area_range_m2[1],
                "fixture_types": ";".join(c.fixture_types),
                "is_food_beverage": c.is_food_beverage,
                "target_sales_share": share.get(c.code, 0.0),
                "return_rate": float(returns[CAT_IX[c.code]]),
            }
            for c in CATEGORIES.values()
        ]
    )


def assemble_transactions(
    p: Purchases, r: dict[str, np.ndarray], customers: pd.DataFrame, start: date, scale: float
) -> pd.DataFrame:
    """Receipts and returns sorted by time with ``T`` ids; returns carry negative amounts and ``ref_txn_id``."""
    n, src = len(p), r["source"]
    day = np.concatenate([p.day, r["day"]])
    seconds = np.floor(np.concatenate([p.minute, r["minute"]]) * 60).astype(np.int64)
    ts = (
        np.datetime64(start.isoformat(), "s")
        + day.astype("timedelta64[D]")
        + seconds.astype("timedelta64[s]")
    )
    is_return = np.r_[np.zeros(n, dtype=bool), np.ones(len(src), dtype=bool)]
    order = np.lexsort((np.arange(len(ts)), ts.astype(np.int64)))
    txn_ids = np.empty(len(ts), dtype=object)
    txn_ids[order] = np.char.add("T", np.char.zfill(np.arange(1, len(ts) + 1).astype(str), 8))
    ref = np.full(len(ts), None, dtype=object)
    ref[n:] = txn_ids[src]
    pseudo = customers["pseudo_id"].to_numpy(dtype=object)
    cust = np.concatenate([p.customer, p.customer[src]])
    frame = pd.DataFrame(
        {
            "txn_id": txn_ids,
            "ts": np.char.replace(np.datetime_as_string(ts, unit="s"), "T", " "),
            "pos_id": np.concatenate([p.pos_id, r["pos_id"]]),
            "customer_id": np.where(cust >= 0, pseudo[np.maximum(cust, 0)], None),
            "amount_krw": np.concatenate([p.amount, -p.amount[src]]),
            "items": np.concatenate([p.items, p.items[src]]),
            "payment": np.array(PAYMENTS, dtype=object)[np.concatenate([p.payment, p.payment[src]])],
            "is_return": is_return,
            "ref_txn_id": ref,
            "weight": round(1.0 / scale, 6),
        }
    )
    return frame.iloc[order].reset_index(drop=True)


def footfall_table(
    p: Purchases, units: pd.DataFrame, cal: Calibration, start: date, scale: float
) -> pd.DataFrame:
    """Estimated zone visits per hour: weighted purchases divided by an assumed conversion rate."""
    conversion = cal.value("footfall.conversion_rate")
    frame = pd.DataFrame(
        {
            "date": (np.datetime64(start.isoformat()) + p.day.astype("timedelta64[D]")).astype(str),
            "hour": (p.minute // 60).astype(int),
            "floor": units["floor"].to_numpy()[p.unit],
            "zone_code": units["zone_code"].to_numpy()[p.unit],
        }
    )
    grouped = (
        frame.groupby(["date", "hour", "floor", "zone_code"], sort=True)
        .size()
        .rename("purchases")
        .reset_index()
    )
    grouped["visits_est"] = np.round(grouped["purchases"] / scale / conversion).astype(int)
    return grouped


def simulate_business(
    models: dict[str, SpatialModel], brands: list[Brand], cal: Calibration, seed: int, scale: float
) -> BusinessData:
    """Build every business table in memory (no files written)."""
    started = time.perf_counter()
    units = unit_table(models, brands, cal)
    zone_of = {
        (rev, u.attributes["unit_code"]): u.attributes.get("zone_code")
        for rev in REVS
        for u in models[rev].units()
    }
    units["zone_code"] = [zone_of[(r.rev, r.unit_code)] for r in units.itertuples()]
    tenancies = build_tenancies(units, brands, cal, seed)
    pos = build_pos_terminals(units, cal)
    calendar = build_calendar(cal)
    sim = TripSimulator(cal, units, calendar, brands, pos, seed)
    rates = pilot_rates(sim, cal, seed)
    share = cal.value("targets.identified_txn_share")
    member_trip_share = (
        share
        * rates["purchases_anon"]
        / (share * rates["purchases_anon"] + (1 - share) * rates["purchases_member"])
    )
    sales_per_trip = member_trip_share * rates["sales_member"] + (1 - member_trip_share) * rates["sales_anon"]
    target_sales = cal.value("targets.annual_sales_krw") * scale * sim.n_days / 365.0
    n_trips = round(target_sales / sales_per_trip)
    member_trips = round(n_trips * member_trip_share)
    customers = build_customers(max(round(member_trips / expected_trips_per_member(cal)), 1), cal, seed)
    rng = stable_rng(seed, "allocation")
    cust, member_day = _member_trip_days(customers, member_trips, sim.day_pmf, rng)
    n_anon = n_trips - member_trips
    anon_day = rng.choice(sim.n_days, size=n_anon, p=sim.day_pmf)
    anon_gender, anon_age = _demographics(n_anon, cal, rng)
    day = np.concatenate([member_day, anon_day])
    order = np.argsort(day, kind="stable")
    customer = np.concatenate([cust, np.full(n_anon, -1)])[order]
    gender = np.concatenate([customers["_gender"].to_numpy()[cust], anon_gender])[order]
    age = np.concatenate([customers["_age"].to_numpy()[cust], anon_age])[order]
    tier = np.concatenate([customers["_tier"].to_numpy()[cust], np.full(n_anon, ANON)])[order]
    purchases = sim.simulate(day[order], customer, gender, age, tier, "main")
    refunds = sim.returns(purchases, "main")
    start = date.fromisoformat(cal.value("period.start"))
    transactions = assemble_transactions(purchases, refunds, customers, start, scale)
    tables = {
        "categories": categories_table(cal),
        "brands": pd.DataFrame(brands_to_rows(brands)),
        "tenancies": tenancies,
        "pos_terminals": pos,
        "customers": customers[[c for c in customers.columns if not c.startswith("_")]],
        "calendar": calendar,
        "transactions": transactions,
        "footfall_zone_hourly": footfall_table(purchases, units, cal, start, scale),
    }
    meta = {
        "seed": seed,
        "scale": scale,
        "weight": round(1.0 / scale, 6),
        "trips": int(len(cust) + n_anon),
        "planned_trips": int(n_trips),
        "member_trips": int(len(cust)),
        "anonymous_trips": int(n_anon),
        "member_trip_share": round(member_trip_share, 4),
        "pilot": {k: round(v, 2) for k, v in rates.items()},
        "focal_brand": sim.focal_brand,
        "partner_brands": list(sim.partner_brands),
        "moved_brands_r0_r1": [list(m) for m in moved_brands(units, "R0", "R1")],
        "simulation_s": round(time.perf_counter() - started, 2),
    }
    return BusinessData(tables, units, meta)


# --- planted-effect measurement (from the output tables, as an analyst would) --------------------


def enrich_transactions(data: BusinessData) -> pd.DataFrame:
    """Transactions joined to brand/unit through the POS validity window and to unit attributes."""
    tx = data.tables["transactions"].copy()
    tx["dt"] = pd.to_datetime(tx["ts"])
    tx["date"] = tx["ts"].str[:10]
    pos = data.tables["pos_terminals"].copy()
    pos["vf"] = pd.to_datetime(pos["valid_from"])
    pos["vt"] = pd.to_datetime(pos["valid_to"]) + pd.Timedelta(days=1)
    tx = tx.sort_values("dt", kind="stable")
    merged = pd.merge_asof(
        tx,
        pos[["pos_id", "vf", "vt", "brand_id", "unit_code"]].sort_values("vf"),
        left_on="dt",
        right_on="vf",
        by="pos_id",
    )
    merged = merged[merged["dt"] < merged["vt"]].drop(columns=["vf", "vt"])
    rev_of = data.tables["calendar"].set_index("date")["rev_id"]
    merged["rev"] = merged["date"].map(rev_of)
    cols = [
        "rev",
        "unit_code",
        "floor",
        "band",
        "category",
        "area_m2",
        "esc_dist_m",
        "exposure",
        "cafe_adjacent",
    ]
    merged = merged.merge(data.units[cols], on=["rev", "unit_code"], how="left")
    merged["minute"] = merged["dt"].dt.hour * 60 + merged["dt"].dt.minute
    day_type = data.tables["calendar"].set_index("date")["day_type"]
    merged["weekday"] = merged["date"].map(day_type) == "weekday"
    return merged


def _lift(frame: pd.DataFrame) -> float:
    a, b = frame["A"].mean(), frame["B"].mean()
    return float((frame["A"] & frame["B"]).mean() / (a * b)) if a > 0 and b > 0 else float("nan")


def customer_days(purchases: pd.DataFrame) -> pd.DataFrame:
    members = purchases[purchases["customer_id"].notna()]
    flags = members.assign(
        A=(members["category"] == "COSMETICS") & (members["floor"] == "1F"),
        B=members["category"].isin(["GLOBAL_FASHION", "LUXURY"]) & members["floor"].isin(["1F", "2F"]),
    )
    return flags.groupby(["customer_id", "date"])[["A", "B"]].max().reset_index()


def measure_cross_shopping(
    purchases: pd.DataFrame, customers: pd.DataFrame, cal: Calibration, meta: dict
) -> dict:
    focal = cal.value("planted.a_segment_cross_shopping.focal_segment")
    days = customer_days(purchases).merge(customers, left_on="customer_id", right_on="pseudo_id")
    is_focal = (
        (days["gender"] == focal["gender"])
        & (days["age_band"] == focal["age_band"])
        & days["tier"].isin(focal["tiers"])
    )
    by_segment = {}
    for (gender, age, high), group in days.assign(high=days["tier"].isin(["VIP", "VVIP"])).groupby(
        ["gender", "age_band", "high"]
    ):
        if len(group) >= 200:
            by_segment[f"{gender}|{age}|{'VIP+' if high else 'other tiers'}"] = round(_lift(group), 2)
    focal_days = purchases[purchases["customer_id"].isin(set(days.loc[is_focal, "customer_id"]))]
    with_focal = focal_days[focal_days["brand_id"] == meta["focal_brand"]][
        ["customer_id", "date"]
    ].drop_duplicates()
    partners = focal_days.merge(with_focal, on=["customer_id", "date"])
    partners = partners[partners["category"].isin(["GLOBAL_FASHION", "LUXURY"])]
    top = partners["brand_id"].value_counts().head(3)
    return {
        "unit": "member customer-day (customer_id + date)",
        "lift_focal": round(_lift(days[is_focal]), 2),
        "lift_others": round(_lift(days[~is_focal]), 2),
        "focal_customer_days": int(is_focal.sum()),
        "lift_by_segment": by_segment,
        "focal_brand_top_partners": {str(k): int(v) for k, v in top.items()},
    }


def measure_fountain_shower(purchases: pd.DataFrame) -> dict:
    members = purchases[purchases["customer_id"].notna()].copy()
    members["b1_food"] = (members["floor"] == "B1") & members["category"].map(
        lambda c: CATEGORIES[c].is_food_beverage
    )
    members["cos_1f"] = (members["category"] == "COSMETICS") & (members["floor"] == "1F")
    members["not_b1"] = members["floor"] != "B1"
    g = members.groupby(["customer_id", "date"])[["b1_food", "cos_1f", "not_b1"]].max()
    fountain = g[g["b1_food"] & g["not_b1"]]
    rest = (members["category"] == "RESTAURANT") & (members["floor"] == "8F")
    first_rest = members[rest].groupby(["customer_id", "date"])["dt"].min().rename("rest_ts")
    others = members[members["floor"] != "8F"].groupby(["customer_id", "date"])["dt"].min().rename("other_ts")
    both = pd.concat([first_rest, others], axis=1, join="inner")
    return {
        "fountain_share_cosmetics": round(float(fountain["cos_1f"].mean()), 3),
        "fountain_customer_days": len(fountain),
        "shower_share_after_8f": round(float((both["other_ts"] > both["rest_ts"]).mean()), 3),
        "shower_customer_days": len(both),
    }


def measure_time_patterns(purchases: pd.DataFrame, cal: Calibration) -> dict:
    cfg = cal.tree["planted"]["e_time_patterns"]
    lunch, kids, evening = (
        cfg[k]["value"] for k in ("weekday_lunch", "weekend_kids_sports", "weekday_luxury_evening")
    )
    fnb = purchases["category"].map(lambda c: CATEGORIES[c].is_food_beverage)
    weekday = purchases[purchases["weekday"]]
    in_lunch = weekday["minute"].between(_minutes(lunch["start"]), _minutes(lunch["end"]) - 1)
    weekend = purchases[~purchases["weekday"]]
    in_kids = weekend["minute"].between(_minutes(kids["start"]), _minutes(kids["end"]) - 1)
    kid = weekend["category"].isin(kids["categories"])
    lux = weekday[weekday["category"] == "LUXURY"]
    in_evening = lux["minute"].between(_minutes(evening["start"]), _minutes(evening["end"]) - 1)
    return {
        "weekday_lunch_share_fnb": round(float(in_lunch[fnb[weekday.index]].mean()), 3),
        "weekday_lunch_share_non_fnb": round(float(in_lunch[~fnb[weekday.index]].mean()), 3),
        "weekend_afternoon_share_kids_sports": round(float(in_kids[kid].mean()), 3),
        "weekend_afternoon_share_others": round(float(in_kids[~kid].mean()), 3),
        "weekday_luxury_aov_evening_ratio": round(
            float(lux.loc[in_evening, "amount_krw"].mean() / lux.loc[~in_evening, "amount_krw"].mean()), 3
        ),
    }


def unit_sales(purchases: pd.DataFrame, data: BusinessData, revs: tuple[str, ...]) -> pd.DataFrame:
    """Gross sales per unit-revision with sales per day and per m2 per day."""
    windows = {
        r: (a, b) for r, (a, b) in zip(REVS, _windows_from_calendar(data.tables["calendar"]), strict=True)
    }
    units = data.units[data.units["rev"].isin(revs)].copy()
    units["days"] = units["rev"].map(lambda r: (windows[r][1] - windows[r][0]).days + 1)
    sales = (
        purchases[purchases["rev"].isin(revs)]
        .groupby(["rev", "unit_code"])["amount_krw"]
        .agg(["sum", "count"])
    )
    units = units.merge(sales, left_on=["rev", "unit_code"], right_index=True, how="left").fillna(
        {"sum": 0, "count": 0}
    )
    units["sales_per_day"] = units["sum"] / units["days"]
    units["spm_per_day"] = units["sales_per_day"] / units["area_m2"]
    return units


def _windows_from_calendar(calendar: pd.DataFrame) -> list[tuple[date, date]]:
    out = []
    for rev in REVS:
        dates = calendar.loc[calendar["rev_id"] == rev, "date"]
        out.append((date.fromisoformat(dates.min()), date.fromisoformat(dates.max())))
    return out


def _ols(y: np.ndarray, columns: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    x = np.column_stack([np.ones(len(y)), *columns])
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    dof = max(len(y) - x.shape[1], 1)
    cov = (resid @ resid / dof) * np.linalg.pinv(x.T @ x)
    return beta, np.sqrt(np.diag(cov))


def measure_low_efficiency(purchases: pd.DataFrame, data: BusinessData, cal: Calibration) -> dict:
    zone = cal.value("planted.f_low_efficiency.zone")
    units = unit_sales(purchases, data, REVS)
    units = units[units["floor"] == zone["floor"]]
    per_unit = units.groupby("unit_code").agg(
        sales=("sum", "sum"), days=("days", "sum"), area=("area_m2", "last"), band=("band", "last"),
        category=("category", "last"), exposure=("exposure", "last"),
    )  # fmt: skip
    per_unit["spm"] = per_unit["sales"] / per_unit["days"] / per_unit["area"]
    per_unit["pct"] = per_unit["spm"].rank(pct=True)
    p10 = float(per_unit["spm"].quantile(0.10))
    target = per_unit[per_unit["band"] == zone["band"]]
    cats = sorted(per_unit["category"].unique())
    dummies = [(per_unit["category"] == c).to_numpy(float) for c in cats[1:]]
    y = np.log(per_unit["spm"].to_numpy())
    beta, _ = _ols(y, [np.log(per_unit["exposure"].to_numpy()), *dummies])
    x = np.column_stack([np.ones(len(y)), np.log(per_unit["exposure"].to_numpy()), *dummies])
    resid = pd.Series(y - x @ beta, index=per_unit.index)
    return {
        "units": sorted(target.index),
        "percentiles": {k: round(float(v), 3) for k, v in target["pct"].items()},
        "share_in_bottom_decile": round(float((target["spm"] <= p10 + 1e-9).mean()), 3),
        "median_spm_band_vs_floor_p10": round(float(target["spm"].median() / p10), 3),
        "floor_units": len(per_unit),
        "band_mean_residual_after_exposure_and_category": round(float(resid[target.index].mean()), 3),
    }


def measure_space_elasticity(
    purchases: pd.DataFrame, data: BusinessData, brands: pd.DataFrame, meta: dict
) -> dict:
    """OLS of log(sales/day) on log(area) with exposure, attractiveness, price-tier and floor x revision controls."""
    units = unit_sales(purchases, data, REVS)
    info = brands.set_index("brand_id")
    planted = {meta["focal_brand"], *meta["partner_brands"]}
    result = {}
    for category in ("COSMETICS", "ACCESSORIES"):
        rows = units[(units["category"] == category) & (units["sum"] > 0) & ~units["brand_id"].isin(planted)]
        groups = (rows["floor"] + "|" + rows["rev"]).to_numpy()
        tiers = rows["brand_id"].map(info["price_tier"]).to_numpy()
        controls = [(groups == g).astype(float) for g in sorted(set(groups))[1:]]
        controls += [(tiers == t).astype(float) for t in sorted(set(tiers))[1:]]
        beta, se = _ols(
            np.log(rows["sales_per_day"].to_numpy()),
            [
                np.log(rows["area_m2"].to_numpy()),
                np.log(rows["exposure"].to_numpy()),
                np.log(rows["brand_id"].map(info["attractiveness"]).to_numpy(float)),
                *controls,
            ],
        )
        result[category] = {
            "beta_hat": round(float(beta[1]), 3),
            "se": round(float(se[1]), 3),
            "beta_true": CATEGORIES[category].space_beta,
            "n_unit_revisions": len(rows),
        }
    return result


def measure_relayout(purchases: pd.DataFrame, data: BusinessData, meta: dict) -> dict:
    out: dict[str, Any] = {}
    moved = [m for m in meta["moved_brands_r0_r1"] if m[1].startswith("3F-")]
    if moved:
        brand_id, unit_from, unit_to = moved[0]
        category = data.units.loc[data.units["brand_id"] == brand_id, "category"].iloc[0]
        floor3 = purchases[(purchases["floor"] == "3F") & (purchases["category"] == category)]
        mine = floor3["brand_id"] == brand_id
        n = floor3.groupby("rev").size()
        k = floor3[mine].groupby("rev").size()
        sales = floor3[mine].groupby("rev")["amount_krw"].sum() / floor3.groupby("rev")["amount_krw"].sum()
        share = k / n
        ratio = float(share["R1"] / share["R0"])
        se = math.sqrt(sum((1 - share[r]) / (n[r] * share[r]) for r in ("R0", "R1")))
        units = data.units[(data.units["floor"] == "3F") & (data.units["category"] == category)]
        expected = {
            rev: round(float(g.loc[g["brand_id"] == brand_id, "weight"].sum() / g["weight"].sum()), 4)
            for rev, g in units.groupby("rev")
        }
        out["moved_brand"] = {
            "brand_id": brand_id,
            "unit_from": unit_from,
            "unit_to": unit_to,
            "expected_receipt_share_of_category": expected,
            "observed_receipt_share_of_category": {r: round(float(share[r]), 4) for r in share.index},
            "receipt_share_ratio_r1_vs_r0": round(ratio, 3),
            "sales_share_ratio_r1_vs_r0": round(float(sales["R1"] / sales["R0"]), 3),
            "receipts": {r: int(k[r]) for r in k.index},
            "approx_se_log": round(se, 3),
            "within_2se_of_planted": bool(abs(math.log(ratio) - math.log(1.30)) <= 2 * se),
        }
    members = purchases[(purchases["customer_id"].notna()) & (purchases["floor"] == "3F")]
    per_day = members.groupby(["rev", "customer_id", "date"])["category"].nunique()
    out["floor3_multi_category_share"] = {
        rev: round(float((g >= 2).mean()), 4) for rev, g in per_day.groupby(level=0)
    }
    return out


def measure_adjacency_and_traps(
    purchases: pd.DataFrame, data: BusinessData, models: dict[str, SpatialModel], cal: Calibration
) -> tuple[dict, dict]:
    units = unit_sales(purchases, data, REVS)
    treated = units[units["cafe_adjacent"]]
    did = []
    for code in sorted(treated["unit_code"].unique()):
        rows = units[units["unit_code"] == code]
        pre, post = rows[~rows["cafe_adjacent"]], rows[rows["cafe_adjacent"]]
        if pre.empty or post.empty or pre["brand_id"].iloc[0] != post["brand_id"].iloc[0]:
            continue
        floor, category = post["floor"].iloc[0], post["category"].iloc[0]
        controls = units[
            (units["floor"] == floor)
            & (units["category"] == category)
            & ~units["unit_code"].isin(treated["unit_code"])
        ]

        def ratio(frame: pd.DataFrame, pre_revs: set[str], post_revs: set[str]) -> float:
            a = frame[frame["rev"].isin(pre_revs)]
            b = frame[frame["rev"].isin(post_revs)]
            return float((b["sum"].sum() / b["days"].sum()) / (a["sum"].sum() / a["days"].sum()))

        pre_revs, post_revs = set(pre["rev"]), set(post["rev"])
        control_ratios = [
            ratio(g, pre_revs, post_revs)
            for _, g in controls.groupby("unit_code")
            if set(g["rev"]) >= pre_revs | post_revs and g["brand_id"].nunique() == 1
        ]
        own = purchases[purchases["unit_code"] == code]
        is_post = own["rev"].isin(post_revs)
        aov = own.groupby(is_post)["amount_krw"].mean()
        uplift = ratio(rows, pre_revs, post_revs) / float(np.median(control_ratios))
        se = math.sqrt((1 / max((~is_post).sum(), 1) + 1 / max(is_post.sum(), 1)) * 1.25)
        did.append(
            {
                "unit_code": code,
                "post_revisions": sorted(post_revs),
                "sales_ratio_treated": round(ratio(rows, pre_revs, post_revs), 3),
                "sales_ratio_controls_median": round(float(np.median(control_ratios)), 3),
                "did_uplift": round(uplift - 1, 3),
                "aov_ratio": round(float(aov.get(True, np.nan) / aov.get(False, np.nan)), 3),
                "receipts_pre_post": [int((~is_post).sum()), int(is_post.sum())],
                "approx_se_log": round(se, 3),
                "within_2se_of_planted": bool(abs(math.log(uplift) - math.log(1.15)) <= 2 * se),
                "n_controls": len(control_ratios),
            }
        )
    near = cal.value("space.near_escalator_m")
    r0 = units[units["rev"] == "R0"]
    geoms = {u.attributes["unit_code"]: u.geometry for u in models["R0"].units()}
    traps = []
    for floor, frame in r0.groupby("floor"):
        near_cats = sorted(frame.loc[frame["esc_dist_m"] <= near, "category"].unique())
        for cat_a in near_cats:
            for cat_b in near_cats:
                a_units = frame[frame["category"] == cat_a]
                b_codes = frame.loc[frame["category"] == cat_b, "unit_code"]
                if cat_a == cat_b or len(a_units) < 5:
                    continue
                b_geoms = [geoms[c] for c in b_codes]
                if any(geoms[a].distance(b) <= 0.05 for a in a_units["unit_code"] for b in b_geoms):
                    continue
                dist_b = np.array([min(geoms[a].distance(b) for b in b_geoms) for a in a_units["unit_code"]])
                y = np.log(a_units["spm_per_day"].clip(lower=1e-9).to_numpy())
                exposure = np.log(a_units["exposure"].to_numpy())
                naive = float(np.corrcoef(y, dist_b)[0, 1])
                ry = y - np.polyval(np.polyfit(exposure, y, 1), exposure)
                rd = dist_b - np.polyval(np.polyfit(exposure, dist_b, 1), exposure)
                traps.append(
                    {
                        "floor": floor,
                        "category_a": cat_a,
                        "category_b": cat_b,
                        "near_units_a": sorted(a_units.loc[a_units["esc_dist_m"] <= near, "unit_code"]),
                        "near_units_b": sorted(
                            frame.loc[
                                (frame["category"] == cat_b) & (frame["esc_dist_m"] <= near), "unit_code"
                            ]
                        ),
                        "adjacent": False,
                        "true_spillover": 0.0,
                        "naive_corr_sales_per_m2_vs_distance_to_b": round(naive, 3),
                        "partial_corr_given_exposure": round(float(np.corrcoef(ry, rd)[0, 1]), 3),
                        "is_trap": naive <= -0.2,
                        "n_units_a": len(a_units),
                    }
                )
    return {"did": did}, {"pairs": traps}


def _params(cal: Calibration, prefix: str) -> dict[str, Any]:
    return {
        k[len(prefix) + 1 :]: v["value"] for k, v in cal.parameters().items() if k.startswith(prefix + ".")
    }


def planted_effects(data: BusinessData, models: dict[str, SpatialModel], cal: Calibration) -> dict[str, Any]:
    """Planted effects with parameters, expected signals and the values observed in the generated data."""
    enriched = enrich_transactions(data)
    purchases = enriched[~enriched["is_return"]]
    meta = data.meta
    names = data.tables["brands"].set_index("brand_id")["brand_name"]
    a = measure_cross_shopping(purchases, data.tables["customers"], cal, meta)
    b, c = measure_adjacency_and_traps(purchases, data, models, cal)
    d = measure_fountain_shower(purchases)
    e = measure_time_patterns(purchases, cal)
    f = measure_low_efficiency(purchases, data, cal)
    g = measure_space_elasticity(purchases, data, data.tables["brands"], meta)
    h = measure_relayout(purchases, data, meta)
    zone = cal.value("planted.f_low_efficiency.zone")
    multi = h["floor3_multi_category_share"]
    effects = [
        {
            "effect_id": "a_segment_cross_shopping",
            "title": "세그먼트 교차구매(40대 VIP·VVIP 여성: 1F 화장품 ↔ 1F/2F 해외패션·명품)",
            "parameters": {
                **_params(cal, "planted.a_segment_cross_shopping"),
                "focal_brand": {"brand_id": meta["focal_brand"], "brand_name": names[meta["focal_brand"]]},
                "partner_brands": [{"brand_id": p, "brand_name": names[p]} for p in meta["partner_brands"]],
            },
            "expected_signal": "member customer-day lift(COSMETICS@1F, GLOBAL_FASHION|LUXURY@1F/2F) >= 2.5 in the focal segment, about 1.0-1.3 elsewhere; the focal brand's top co-purchased fashion/luxury brands are the partner brands",
            "observed": a,
            "recovered": bool(a["lift_focal"] >= 2.5 and 0.9 <= a["lift_others"] <= 1.35),
        },
        {
            "effect_id": "b_cafe_adjacency_spillover",
            "title": "카페 인접 패션 매장 매출·객단가 상승(진짜 인접 효과)",
            "parameters": _params(cal, "planted.b_cafe_adjacency"),
            "expected_signal": "fashion units sharing a wall with a CAFE unit: about +15% sales (traffic x1.095, AOV x1.05); identifiable as a difference-in-differences when the neighbor becomes a cafe",
            "observed": b,
            "recovered": bool(b["did"]) and all(x["within_2se_of_planted"] for x in b["did"]),
            "note": "현재 도면에서 카페와 벽을 공유하는 패션 매장은 R2의 3F-S02(이웃 3F-S01이 카페로 전환)뿐이다.",
        },
        {
            "effect_id": "c_confound_exposure_trap",
            "title": "함정: 에스컬레이터 노출이 만든 가짜 근접 효과",
            "parameters": {
                **_params(cal, "space"),
                "exposure_definition": "1 + A*exp(-d/L), d = Euclidean distance (m) from unit centroid to the nearest escalator footprint on the same floor",
            },
            "expected_signal": "categories whose units both sit near escalators (not adjacent) look associated (sales/m2 rises as distance to the other category falls) but the partial correlation given exposure is ~0; true spillover = 0",
            "observed": c,
            "recovered": any(
                p["is_trap"]
                and abs(p["partial_corr_given_exposure"])
                < 0.5 * abs(p["naive_corr_sales_per_m2_vs_distance_to_b"])
                for p in c["pairs"]
            ),
        },
        {
            "effect_id": "d_fountain_shower",
            "title": "분수·샤워 효과(B1 식품 → 1F 화장품, 8F 식당 → 하층 쇼핑)",
            "parameters": _params(cal, "planted.d_fountain_shower"),
            "expected_signal": ">= 40% of B1-food member customer-days with another-floor purchase include 1F COSMETICS; >= 60% of 8F-restaurant customer-days with other-floor purchases buy there after the 8F purchase",
            "observed": d,
            "recovered": bool(d["fountain_share_cosmetics"] >= 0.40 and d["shower_share_after_8f"] >= 0.60),
        },
        {
            "effect_id": "e_time_patterns",
            "title": "시간 패턴(평일 점심 F&B, 주말 오후 아동·스포츠, 평일 저녁 명품 객단가)",
            "parameters": _params(cal, "planted.e_time_patterns"),
            "expected_signal": "weekday 11:30-13:30 share of F&B receipts >> non-F&B; weekend 14:00-18:00 share of KIDS/SPORTS receipts > others; weekday 18:00-20:00 LUXURY AOV ~1.35x",
            "observed": e,
            "recovered": bool(
                e["weekday_lunch_share_fnb"] >= 1.5 * e["weekday_lunch_share_non_fnb"]
                and e["weekend_afternoon_share_kids_sports"] >= 1.15 * e["weekend_afternoon_share_others"]
                and e["weekday_luxury_aov_evening_ratio"] >= 1.2
            ),
        },
        {
            "effect_id": "f_low_efficiency_zone",
            "title": f"저효율 존: {zone['floor']} 동측 밴드({zone['floor']}-{zone['band']}01..)",
            "parameters": {
                "zone": zone,
                "cause": "low exposure (far from escalators) plus weak brands; no extra hidden penalty",
            },
            "expected_signal": "zone units sit in the bottom decile of floor sales per m2 (median <= floor P10); after controlling for exposure and category the mean residual is ~0",
            "observed": f,
            "recovered": bool(
                f["median_spm_band_vs_floor_p10"] <= 1.05 and f["share_in_bottom_decile"] >= 0.5
            ),
        },
        {
            "effect_id": "g_space_elasticity",
            "title": "면적 탄력성(매출 ∝ 면적^β, catalog space_beta)",
            "parameters": {c: CATEGORIES[c].space_beta for c in CATS},
            "expected_signal": "OLS log(sales/day) ~ log(area) + log(exposure) + log(attractiveness) + floor FE recovers beta: COSMETICS 0.35, ACCESSORIES 0.08 (planted focal/partner brands excluded)",
            "observed": g,
            "recovered": all(abs(v["beta_hat"] - v["beta_true"]) <= 2 * v["se"] + 0.05 for v in g.values()),
        },
        {
            "effect_id": "h_md_relayout",
            "title": "MD 개편 효과(R1 에스컬레이터 정면 이동, R2 3F 카테고리 믹스)",
            "parameters": _params(cal, "planted.h_md_relayout"),
            "expected_signal": "the R1 relocated 3F brand gains +30% (planted; +25-35% band) share of its floor-category receipts vs R0, seasonality-free (expected shares from the planted weights are listed); at scale 0.02 a single brand has ~+/-15% sampling error (approx_se_log) and sales-based ratios are noisier because receipt amounts are heavy-tailed. The share of 3F member customer-days with >= 2 categories on 3F rises in R2",
            "observed": h,
            "recovered": bool(
                "moved_brand" in h
                and h["moved_brand"]["within_2se_of_planted"]
                and multi.get("R2", 0) > multi.get("R1", 0)
            ),
        },
    ]
    return {
        "generator": "cad2anything.mockgen.business",
        "seed": meta["seed"],
        "scale": meta["scale"],
        "weight": meta["weight"],
        "period": {"start": cal.value("period.start"), "end": cal.value("period.end")},
        "definitions": {
            "customer_day": "member purchases grouped by customer_id and date (returns excluded)",
            "exposure_proxy": "Euclidean distance from unit centroid to the nearest escalator footprint on the same floor (m); not a walking distance",
            "adjacency": "unit boundaries within 0.05 m sharing >= 0.5 m (same as a shared wall)",
            "pos_mapping": "pos_terminals valid_from/valid_to are inclusive dates; a brand keeps its pos_ids when it moves",
        },
        "effects": effects,
    }


def _write_csv(frame: pd.DataFrame, path: Path) -> int:
    if path.suffix == ".gz":
        frame.to_csv(path, index=False, lineterminator="\n", compression={"method": "gzip", "mtime": 0})
    else:
        frame.to_csv(path, index=False, lineterminator="\n", encoding="utf-8")
    return path.stat().st_size


def _write_json(path: Path, payload: dict[str, Any]) -> int:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    return path.stat().st_size


def business_stats(data: BusinessData) -> dict[str, Any]:
    tx = data.tables["transactions"]
    purchases = tx[~tx["is_return"]]
    days = len(data.tables["calendar"])
    weight = data.meta["weight"]
    customers = data.tables["customers"].set_index("pseudo_id")
    member = purchases[purchases["customer_id"].notna()].join(customers, on="customer_id")
    gross = int(purchases["amount_krw"].sum())
    net = int(tx["amount_krw"].sum())

    def mix(column: str) -> dict[str, float]:
        shares = member.groupby(column)["amount_krw"].sum() / member["amount_krw"].sum()
        return {str(k): round(float(v), 3) for k, v in shares.items()}

    return {
        "rows": {name: len(frame) for name, frame in data.tables.items()},
        "receipts": len(purchases),
        "returns": int(tx["is_return"].sum()),
        "gross_sales_krw": gross,
        "net_sales_krw": net,
        "full_scale_gross_sales_krw": round(gross * weight),
        "full_scale_annual_gross_sales_krw": round(gross * weight * 365 / days),
        "aov_krw": round(float(purchases["amount_krw"].mean())),
        "identified_receipt_share": round(float(purchases["customer_id"].notna().mean()), 3),
        "member_sales_mix": {"gender": mix("gender"), "age_band": mix("age_band"), "tier": mix("tier")},
        "customers_by_tier": {
            str(k): int(v) for k, v in data.tables["customers"]["tier"].value_counts().items()
        },
    }


def data_card(data: BusinessData, effects: dict[str, Any], stats: dict[str, Any], cal: Calibration) -> str:
    rows = stats["rows"]
    lines = [
        "# 합성 업무 데이터 카드 (cad2anything.mockgen.business)",
        "",
        "> **면책:** 이 폴더의 모든 데이터는 합성 데이터다. 브랜드명은 모두 가상이며 실제 기업·고객·매출과 무관하다.",
        "> 공개 통계는 분포의 대략적 모양을 잡는 데에만 참고했고 실제 수치를 복제하지 않았다.",
        "",
        "## 개요",
        "",
        f"- 기간: {cal.value('period.start')} ~ {cal.value('period.end')} (개정 R0/R1/R2 적용일 기준 테넌시)",
        f"- seed {data.meta['seed']}, 축소 계수 scale {data.meta['scale']} → 모든 거래의 `weight` = {data.meta['weight']}",
        f"- 쇼핑 트립 {data.meta['trips']:,}건(회원 {data.meta['member_trips']:,}, 비회원 {data.meta['anonymous_trips']:,})",
        f"- 영수증 {stats['receipts']:,}건, 반품 {stats['returns']:,}건, 객단가 ₩{stats['aov_krw']:,}",
        f"- 총매출(표본) ₩{stats['gross_sales_krw']:,} → 전체 규모 환산 연 ₩{stats['full_scale_annual_gross_sales_krw']:,}",
        f"- 회원 식별 영수증 비중 {stats['identified_receipt_share']:.1%}",
        "",
        "## 파일",
        "",
        "| 파일 | 행 수 | 내용 |",
        "|---|---|---|",
        f"| categories.csv | {rows['categories']} | 카테고리 기준값(catalog)과 목표 매출 비중 |",
        f"| brands.csv | {rows['brands']} | 가상 브랜드(Brand 필드) |",
        f"| tenancies.csv | {rows['tenancies']} | brand_id, unit_code, rev_id, valid_from, valid_to(포함), commission_rate |",
        f"| pos_terminals.csv | {rows['pos_terminals']} | pos_id, brand_id, unit_code, valid_from, valid_to(포함). 브랜드가 이동해도 pos_id 유지 |",
        f"| customers.csv | {rows['customers']} | pseudo_id, age_band, gender, tier, join_date, home_area |",
        f"| calendar.csv | {rows['calendar']} | 공휴일·명절·영업시간·트래픽 지수·적용 개정 |",
        f"| transactions.csv.gz | {rows['transactions']} | txn_id, ts, pos_id, customer_id, amount_krw(반품 음수), items, payment, is_return, ref_txn_id, weight |",
        f"| footfall_zone_hourly.csv.gz | {rows['footfall_zone_hourly']} | 추정 방문수(구매 ÷ 가정 전환율, 관측값 아님) |",
        "| planted_effects.json | - | 심은 패턴, 파라미터, 기대 신호, 관측값 |",
        "",
        "## 규칙",
        "",
        "- 시각 `ts`는 Asia/Seoul 현지 시각(타임존 표기 없음)이다.",
        "- `valid_to`는 그날을 포함한다. 거래 시각의 POS 매핑은 `valid_from <= 날짜 <= valid_to`로 찾는다.",
        "- 반품은 원거래와 같은 브랜드에서 1~30일 뒤에 일어나며 `amount_krw`가 음수이고 `ref_txn_id`가 원거래를 가리킨다.",
        "- 매출·건수를 전체 규모로 환산할 때 `weight`를 곱한다. 고객 단위 비율(교차구매 등)은 가중치 없이 계산한다.",
        "",
        "## 심은 패턴(관측값)",
        "",
        "| ID | 내용 | 복원 |",
        "|---|---|---|",
    ]
    for effect in effects["effects"]:
        lines.append(
            f"| {effect['effect_id']} | {effect['title']} | {'예' if effect['recovered'] else '확인 필요'} |"
        )
    lines += [
        "",
        "자세한 파라미터와 관측값은 `planted_effects.json`, 생성기 설계와 보정 근거는 `docs/data/mock-business-data.md`를 본다.",
        "",
        "## 한계",
        "",
        "- 공개 통계의 대략적 모양만 반영한 가정치가 많다. 실제 점포·브랜드 성과를 대표하지 않는다.",
        "- 노출도는 에스컬레이터까지의 직선거리 대용치이며 실제 보행 동선을 모사하지 않는다.",
        "- 방문수(footfall)는 구매에서 역산한 추정치다.",
        "",
    ]
    return "\n".join(lines)


SAMPLE_DATES = ("2024-11-13", "2025-05-10", "2025-12-20", "2026-02-11")


def write_tables(data: BusinessData, folder: Path) -> dict[str, int]:
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, frame in data.tables.items():
        suffix = ".csv.gz" if name in ("transactions", "footfall_zone_hourly") else ".csv"
        columns = TX_COLUMNS if name == "transactions" else list(frame.columns)
        files[name + suffix] = _write_csv(frame[columns], folder / f"{name}{suffix}")
    return files


def write_business_samples(samples_dir: Path, data: BusinessData, effects: dict[str, Any]) -> dict[str, int]:
    """Small committed sample: reference tables, whole sample days of transactions and planted effects."""
    folder = Path(samples_dir) / "business"
    folder.mkdir(parents=True, exist_ok=True)
    tx = data.tables["transactions"]
    picked = tx[tx["ts"].str[:10].isin(SAMPLE_DATES)]
    originals = tx[tx["txn_id"].isin(set(picked["ref_txn_id"].dropna()))]
    sample = pd.concat([originals, picked]).drop_duplicates("txn_id").sort_values(["ts", "txn_id"])
    customers = data.tables["customers"]
    files = {}
    for name in ("categories", "brands", "tenancies", "pos_terminals"):
        files[f"{name}.csv"] = _write_csv(data.tables[name], folder / f"{name}.csv")
    files["customers_sample.csv"] = _write_csv(
        customers[customers["pseudo_id"].isin(set(sample["customer_id"].dropna()))],
        folder / "customers_sample.csv",
    )
    files["transactions_sample.csv"] = _write_csv(sample[TX_COLUMNS], folder / "transactions_sample.csv")
    files["planted_effects.json"] = _write_json(folder / "planted_effects.json", effects)
    return files


def generate_business(
    out_dir: Path,
    seed: int = 42,
    scale: float | None = None,
    models: dict[str, SpatialModel] | None = None,
    brands: list[Brand] | None = None,
    samples_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate business tables under ``out_dir/business`` (and optional samples); return a summary."""
    started = time.perf_counter()
    cal = load_calibration()
    scale = float(scale if scale is not None else cal.value("scale.default"))
    if models is None or brands is None:
        from cad2anything.mockgen.drawings import build_all

        built_brands, built_models, _ = build_all(seed)
        brands = brands if brands is not None else built_brands
        models = models if models is not None else built_models
    data = simulate_business(models, brands, cal, seed, scale)
    effects = planted_effects(data, models, cal)
    stats = business_stats(data)
    folder = Path(out_dir) / "business"
    files = write_tables(data, folder)
    files["planted_effects.json"] = _write_json(folder / "planted_effects.json", effects)
    card = folder / "data_card.md"
    card.write_text(data_card(data, effects, stats, cal), encoding="utf-8", newline="\n")
    files["data_card.md"] = card.stat().st_size
    summary: dict[str, Any] = {
        **stats,
        "meta": data.meta,
        "effects": {
            e["effect_id"]: {"recovered": e["recovered"], "observed": e["observed"]}
            for e in effects["effects"]
        },
        "files": files,
    }
    if samples_dir is not None:
        summary["samples"] = write_business_samples(samples_dir, data, effects)
    summary["elapsed_s"] = round(time.perf_counter() - started, 2)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate synthetic business data calibrated to the drawings."
    )
    parser.add_argument(
        "--out", type=Path, default=Path("data/generated"), help="output folder (business/ is created)"
    )
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument(
        "--scale", type=float, default=None, help="volume scale factor (default from calibration.yaml)"
    )
    parser.add_argument("--samples", type=Path, default=None, help="also write committed samples here")
    args = parser.parse_args(argv)
    summary = generate_business(args.out, args.seed, args.scale, samples_dir=args.samples)
    print(json.dumps(summary, ensure_ascii=True, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
