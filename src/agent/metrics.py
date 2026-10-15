"""Shared metric definitions; signal overlap is NOT executed turnover."""
import math


def gross_turnover(buy: float, sell: float, prior_nav: float) -> float:
    """Two-sided executed notional / pre-trade NAV (including cash); fees excluded."""
    if not all(math.isfinite(x) for x in (buy, sell, prior_nav)) or min(buy, sell) < 0 or prior_nav <= 0:
        raise ValueError('Invalid turnover inputs')
    return (buy + sell) / prior_nav


def selection_change(previous: set, current: set) -> float:
    """Jaccard distance of actual selections; empty -> empty is zero."""
    union = previous | current
    return 1 - len(previous & current) / len(union) if union else 0.0
