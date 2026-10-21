"""Research-only adapter for the team-proposed strategy variant.

It reuses the production portfolio sizing, compliance checks, data timing and
execution ledger while allowing isolated scoring and turnover parameters.
It is not wired into the live trading path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math

import pandas as pd

from . import strategy
from .factor_engine import Targets, build_targets


@dataclass(frozen=True)
class TeamResearchStrategy:
    pv_weight: float = 0.45
    fund_weight: float = 0.45
    news_weight: float = 0.10
    revenue_growth_weight: float = 0.50
    revenue_acceleration_weight: float = 0.50
    buffer_rank: int = 50
    no_trade_band: float = 0.03
    enforce_active_share: bool = True
    version: str = "team-research-mix-v1-buffer50-band3"

    def __post_init__(self):
        weights = (self.pv_weight, self.fund_weight, self.news_weight)
        fund_weights = (self.revenue_growth_weight, self.revenue_acceleration_weight)
        if any(not math.isfinite(v) or v < 0 for v in weights) or abs(sum(weights) - 1) > 1e-12:
            raise ValueError("量價、基本面、新聞權重須非負且合計 100%")
        if any(not math.isfinite(v) or v < 0 for v in fund_weights) or abs(sum(fund_weights) - 1) > 1e-12:
            raise ValueError("營收成長與加速度權重須非負且合計 100%")
        if type(self.buffer_rank) is not int or self.buffer_rank < strategy.N_HOLDINGS:
            raise ValueError("續抱排名須不小於正式目標持股數")
        if not math.isfinite(self.no_trade_band) or not 0 <= self.no_trade_band <= 1:
            raise ValueError("無交易帶須介於 0–100%")

    def manifest(self):
        body = {
            **asdict(self),
            "total_score": "pv_score*pv_weight + fund_score*fund_weight + news_score*news_weight",
            "fund_score": "available percentile ranks of B1 revenue growth and B3 revenue acceleration; missing rows renormalize available factors; both missing use neutral 0.5",
            "selection": "retain eligible holdings within the top buffer_rank; fill vacancies by score",
            "allocation": ("shared production score-edge/volatility sizing, per-stock caps and Active Share constraints"
                           if self.enforce_active_share else
                           "shared score-edge/volatility sizing and per-stock caps; Active Share disabled for research-only comparison"),
            "active_share_enforced": self.enforce_active_share,
            "execution": "shared production trading, fees/tax, cash rules and settlement; custom no-trade band",
            "formal_strategy_modified": False,
            "promotion": "research-only; manual review required",
        }
        body["sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return body

    def prepare(self, inputs, day):
        return None

    def __call__(self, day, state) -> Targets:
        fund = day.fund.copy()
        factor_weights = {
            "B1_rank": self.revenue_growth_weight,
            "B3_rank": self.revenue_acceleration_weight,
        }
        score = pd.Series(0.0, index=fund.index)
        available = pd.Series(0.0, index=fund.index)
        for column, weight in factor_weights.items():
            values = pd.to_numeric(fund[column], errors="coerce") if column in fund else pd.Series(float("nan"), index=fund.index)
            valid = values.notna()
            score.loc[valid] += values.loc[valid] * weight
            available.loc[valid] += weight
        fund["fund_score"] = (score.div(available.where(available > 0))).fillna(0.5)
        return build_targets(
            day.master, day.pv, fund, day.news, state.held_tickers(), day.benches,
            score_weights={"pv_score": self.pv_weight, "fund_score": self.fund_weight,
                           "news_score": self.news_weight},
            buffer_rank=self.buffer_rank,
            enforce_active_share=self.enforce_active_share,
        )
