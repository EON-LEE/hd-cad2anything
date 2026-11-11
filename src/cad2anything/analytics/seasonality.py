"""Empirical monthly seasonality of observed sales, used to annualize periods of different length.

Annualizing a period with ``365 / period days`` assumes every day of the year sells the same. Department
store sales are strongly seasonal, so a revision measured over autumn and winter and a revision measured
over spring and summer do not compare directly on that basis.

``monthly_sales_index`` estimates one index per calendar month from the transactions observed up to a
cut-off (never from the generator's calibration): the average daily sales of that month divided by the
average day of a full year. ``season_weighted_days`` then converts a period into "average days", and
``seasonal_factor`` is the annualization factor that corrects for the season the period covers. Months
without observed days keep the neutral index 1.0.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import numpy as np
import pandas as pd

MONTHS = tuple(range(1, 13))
YEAR_DAYS = tuple(calendar.monthrange(2001, month)[1] for month in MONTHS)  # a non-leap year
MIN_DAYS_PER_MONTH = 7  # fewer observed days than this is too thin to estimate a month
NEUTRAL = 1.0


def daily_sales(frame: pd.DataFrame, amount: str = "net_sales_krw", day: str = "date") -> pd.Series:
    """Total sales per calendar day from an enriched transaction frame (index: ``datetime64`` days)."""
    if frame.empty:
        return pd.Series(dtype=float)
    days = pd.to_datetime(frame[day]).dt.normalize()
    return frame[amount].astype(float).groupby(days).sum().sort_index()


def monthly_sales_index(daily: pd.Series, through: date | None = None) -> dict[int, float]:
    """Average daily sales of each calendar month against the average day (mean over a year = 1).

    ``daily`` is total sales per day; ``through`` drops later days so that a revision never uses sales
    recorded after its own data end. Months with fewer than ``MIN_DAYS_PER_MONTH`` observed days keep the
    neutral index 1.0, and the result is rescaled so that a full non-leap year averages exactly 1.
    """
    index = dict.fromkeys(MONTHS, NEUTRAL)
    series = _clip(daily, through)
    if series.empty:
        return index
    months = series.index.month
    means = series.groupby(months).mean()
    counts = series.groupby(months).size()
    overall = float(series.mean())
    if not np.isfinite(overall) or overall <= 0:
        return index
    for month in MONTHS:
        if counts.get(month, 0) >= MIN_DAYS_PER_MONTH and float(means.get(month, 0.0)) > 0:
            index[month] = float(means[month]) / overall
    weighted = sum(index[m] * d for m, d in zip(MONTHS, YEAR_DAYS, strict=True)) / sum(YEAR_DAYS)
    if weighted > 0:
        index = {m: value / weighted for m, value in index.items()}
    return index


def observed_months(daily: pd.Series, through: date | None = None) -> int:
    """Calendar months the index could be estimated from (at least ``MIN_DAYS_PER_MONTH`` observed days)."""
    series = _clip(daily, through)
    if series.empty:
        return 0
    counts = series.groupby(series.index.month).size()
    return int((counts >= MIN_DAYS_PER_MONTH).sum())


def _clip(daily: pd.Series, through: date | None) -> pd.Series:
    if daily is None or daily.empty:
        return pd.Series(dtype=float)
    series = daily.copy()
    series.index = pd.to_datetime(series.index)
    return series.loc[series.index <= pd.Timestamp(through)] if through is not None else series


def season_weighted_days(start: date, end: date, index: dict[int, float]) -> float:
    """Length of an inclusive period in "average days": every day counts with its month's index."""
    if end < start:
        return 0.0
    total, day = 0.0, start
    while day <= end:
        total += float(index.get(day.month, NEUTRAL))
        day += timedelta(days=1)
    return total


def seasonal_factor(start: date, end: date, index: dict[int, float]) -> float:
    """Annualization factor that corrects for the season the period covers (365 / weighted days)."""
    weighted = season_weighted_days(start, end, index)
    return 365.0 / weighted if weighted > 0 else 0.0
