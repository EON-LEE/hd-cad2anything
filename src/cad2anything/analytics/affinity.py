"""Customer cross-shopping between brands or categories.

Ratios are unweighted (customers are sampled at the same rate as receipts) and computed over baskets of
identified customers (returns excluded):

- ``basket="customer"`` (default): one basket per customer over the period.
- ``basket="customer_day"``: one basket per customer and day (same-visit co-purchase); preferable over
  long periods, where period-level baskets saturate and lift drifts toward 1.

With n_ab baskets containing both items and N baskets in total:
support = n_ab / N, confidence_a_b = n_ab / n_a, confidence_b_a = n_ab / n_b,
lift = n_ab * N / (n_a * n_b), jaccard = n_ab / (n_a + n_b - n_ab). ``n_customers`` is always the number
of distinct customers behind the co-purchases and drives the ``min_customers`` filter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from cad2anything.analytics.prep import category_name, purchases

BASKETS = ("customer", "customer_day")
LEVEL_COLUMNS = {
    "brand": "brand_id",
    "category": "category",
    "floor": "floor",
    "zone": "zone_code",
    "unit": "unit_code",
}
PAIR_COLUMNS = [
    "level",
    "item_a",
    "item_b",
    "name_a",
    "name_b",
    "n_a",
    "n_b",
    "n_customers",
    "n_baskets",
    "n_total",
    "support",
    "confidence_a_b",
    "confidence_b_a",
    "lift",
    "jaccard",
]
PARTNER_COLUMNS = [
    "focal",
    "partner",
    "partner_name",
    "n_customers",
    "n_focal",
    "n_partner",
    "support",
    "confidence",
    "reverse_confidence",
    "lift",
    "jaccard",
]


def _item_names(rows: pd.DataFrame, level: str, items: pd.Index) -> dict[str, str]:
    if level == "category":
        return {item: category_name(item) for item in items}
    if level == "brand" and "brand_name" in rows:
        names = rows.dropna(subset=["brand_name"]).drop_duplicates("brand_id")
        lookup = dict(zip(names["brand_id"].astype(str), names["brand_name"].astype(str), strict=True))
        return {item: lookup.get(item, item) for item in items}
    return {item: item for item in items}


def _cooccurrence(
    basket_codes: np.ndarray, item_codes: np.ndarray, n_baskets: int, n_items: int, chunk_size: int
) -> np.ndarray:
    """Item x item basket co-occurrence counts (diagonal = baskets per item), in basket chunks."""
    order = np.argsort(basket_codes, kind="stable")
    bc, ic = basket_codes[order], item_codes[order]
    co = np.zeros((n_items, n_items), dtype=np.int64)
    for first in range(0, n_baskets, chunk_size):
        lo, hi = np.searchsorted(bc, first), np.searchsorted(bc, first + chunk_size)
        if lo == hi:
            continue
        block = np.zeros((min(chunk_size, n_baskets - first), n_items), dtype=np.float32)
        block[bc[lo:hi] - first, ic[lo:hi]] = 1.0
        co += np.rint(block.T @ block).astype(np.int64)
    return co


def cross_shopping(
    enriched: pd.DataFrame,
    customers: pd.DataFrame | None = None,
    level: str = "brand",
    segment: Mapping[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
    min_customers: int = 20,
    basket: str = "customer",
    chunk_size: int = 20_000,
) -> pd.DataFrame:
    """Pairwise cross-shopping table for identified customers (optionally a customer segment).

    ``segment`` filters the customers table, e.g. ``{"age_band": "40s", "tier": ["VIP", "VVIP"]}``.
    Only pairs co-purchased by at least ``min_customers`` distinct customers are returned, sorted by lift
    (desc). ``attrs`` carries ``n_total`` (baskets), ``n_customers`` (customers with a purchase),
    ``basket``, ``level`` and ``segment``.
    """
    if level not in LEVEL_COLUMNS:
        raise ValueError(f"level must be one of {sorted(LEVEL_COLUMNS)}")
    if basket not in BASKETS:
        raise ValueError(f"basket must be one of {BASKETS}")
    column = LEVEL_COLUMNS[level]
    rows = purchases(enriched, customers, segment, start, end)
    rows = rows.loc[rows[column].notna()].copy()
    rows["_item"] = rows[column].astype(str)
    rows["_day"] = rows["ts"].dt.normalize() if basket == "customer_day" else pd.NaT
    baskets = rows[["customer_key", "_day", "_item"]].drop_duplicates().reset_index(drop=True)
    keys = ["customer_key", "_day"] if basket == "customer_day" else ["customer_key"]
    basket_codes = baskets.groupby(keys, sort=True).ngroup().to_numpy() if len(baskets) else np.empty(0, int)
    n_total = int(basket_codes.max()) + 1 if len(basket_codes) else 0
    attrs = {
        "level": level,
        "basket": basket,
        "segment": dict(segment or {}),
        "n_total": n_total,
        "n_customers": int(baskets["customer_key"].nunique()),
    }
    if n_total == 0:
        result = pd.DataFrame(columns=PAIR_COLUMNS)
        result.attrs.update(attrs)
        return result
    item_codes, items = pd.factorize(baskets["_item"], sort=True)
    co = _cooccurrence(basket_codes, item_codes, n_total, len(items), chunk_size)
    n_item = np.diag(co).astype(float)
    ia, ib = np.triu_indices(len(items), k=1)
    n_ab = co[ia, ib].astype(float)
    if basket == "customer":
        n_cust = n_ab.copy()
    else:
        coded = baskets.assign(_basket=basket_codes, _code=item_codes)[["_basket", "customer_key", "_code"]]
        joined = coded.merge(coded, on=["_basket", "customer_key"], suffixes=("_a", "_b"))
        joined = joined.loc[joined["_code_a"] < joined["_code_b"]]
        distinct = joined.drop_duplicates(["customer_key", "_code_a", "_code_b"])
        counts = distinct.groupby(["_code_a", "_code_b"]).size()
        n_cust = counts.reindex(pd.MultiIndex.from_arrays([ia, ib]), fill_value=0).to_numpy(dtype=float)
    keep = (n_ab > 0) & (n_cust >= max(int(min_customers), 1))
    ia, ib, n_ab, n_cust = ia[keep], ib[keep], n_ab[keep], n_cust[keep]
    names = _item_names(rows, level, items)
    n_a, n_b = n_item[ia], n_item[ib]
    result = pd.DataFrame(
        {
            "level": level,
            "item_a": items[ia].astype(str),
            "item_b": items[ib].astype(str),
            "name_a": [names[items[i]] for i in ia],
            "name_b": [names[items[i]] for i in ib],
            "n_a": n_a.astype(int),
            "n_b": n_b.astype(int),
            "n_customers": n_cust.astype(int),
            "n_baskets": n_ab.astype(int),
            "n_total": n_total,
            "support": n_ab / n_total,
            "confidence_a_b": n_ab / n_a,
            "confidence_b_a": n_ab / n_b,
            "lift": n_ab * n_total / (n_a * n_b),
            "jaccard": n_ab / (n_a + n_b - n_ab),
        },
        columns=PAIR_COLUMNS,
    )
    result = result.sort_values(
        ["lift", "n_customers", "item_a", "item_b"], ascending=[False, False, True, True], kind="mergesort"
    ).reset_index(drop=True)
    result.attrs.update(attrs)
    return result


def top_partners(pairs: pd.DataFrame, focal: str, k: int = 10, sort_by: str = "lift") -> pd.DataFrame:
    """Top ``k`` cross-shopping partners of ``focal``; ``confidence`` is P(partner | focal)."""
    left = pairs.loc[pairs["item_a"] == focal]
    right = pairs.loc[pairs["item_b"] == focal]
    frames = [
        pd.DataFrame(
            {
                "focal": focal,
                "partner": part[partner],
                "partner_name": part[partner_name],
                "n_customers": part["n_customers"],
                "n_focal": part[n_focal],
                "n_partner": part[n_partner],
                "support": part["support"],
                "confidence": part[conf],
                "reverse_confidence": part[reverse],
                "lift": part["lift"],
                "jaccard": part["jaccard"],
            },
            columns=PARTNER_COLUMNS,
        )
        for part, partner, partner_name, n_focal, n_partner, conf, reverse in (
            (left, "item_b", "name_b", "n_a", "n_b", "confidence_a_b", "confidence_b_a"),
            (right, "item_a", "name_a", "n_b", "n_a", "confidence_b_a", "confidence_a_b"),
        )
        if len(part)
    ]
    if not frames:
        return pd.DataFrame(columns=PARTNER_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(
        [sort_by, "n_customers", "partner"], ascending=[False, False, True], kind="mergesort"
    )
    return out.head(k).reset_index(drop=True)
