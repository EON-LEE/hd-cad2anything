"""Space productivity analytics on enriched transactions (deterministic; estimates labeled 추정치).

- ``prep``: POS/tenancy validity mapping, returns netting, sampling weights
- ``kpi``: space productivity, driver decomposition (OLS), time patterns
- ``affinity``: customer cross-shopping (support, confidence, lift, jaccard)
- ``flows``: floor transitions, fountain/shower indicators
- ``effects``: adjacency effects with confounding checks, before/after comparisons
- ``seasonality``: empirical monthly sales index for season-adjusted annualization
"""

from cad2anything.analytics.affinity import cross_shopping, top_partners
from cad2anything.analytics.effects import AdjacencyEffect, BeforeAfter, adjacency_effect, before_after
from cad2anything.analytics.flows import FloorFlows, floor_transitions
from cad2anything.analytics.kpi import (
    DriverDecomposition,
    OLSResult,
    driver_decomposition,
    ols,
    space_productivity,
    time_patterns,
)
from cad2anything.analytics.prep import PYEONG_M2, enrich_transactions
from cad2anything.analytics.seasonality import (
    daily_sales,
    monthly_sales_index,
    observed_months,
    season_weighted_days,
    seasonal_factor,
)

__all__ = [
    "PYEONG_M2",
    "AdjacencyEffect",
    "BeforeAfter",
    "DriverDecomposition",
    "FloorFlows",
    "OLSResult",
    "adjacency_effect",
    "before_after",
    "cross_shopping",
    "daily_sales",
    "driver_decomposition",
    "enrich_transactions",
    "floor_transitions",
    "monthly_sales_index",
    "observed_months",
    "ols",
    "season_weighted_days",
    "seasonal_factor",
    "space_productivity",
    "time_patterns",
    "top_partners",
]
