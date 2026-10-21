"""Independent strategy adapters; only execution/accounting is shared.

ResearchPortfolio is an explicit experimental extension of the existing signal
study, NOT a claimed reconstruction of a previously unspecified portfolio policy.
It is unavailable from live.run and never changes strategy.py.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

import pandas as pd

from .factor_engine import Targets, cap_weights, check_active_share
from . import rules, research_config
from .shadow_quant import compute_shadow_ranking


@dataclass(frozen=True)
class ResearchPortfolio:
    selected_count: int
    target_cash: float = 0.05
    version: str = 'research-portfolio-equal-v2-buffer55'
    holding_buffer_rank: int | None = research_config.PORTFOLIO_BUFFER_RANK

    def manifest(self):
        body = {**asdict(self), 'technical_weights': dict(research_config.EXPERIMENTAL_TECHNICAL_WEIGHTS),
                'research_score_weights': dict(research_config.EXPERIMENTAL_RESEARCH_WEIGHTS),
                'selection': ('daily direct ranking; no holding buffer' if self.holding_buffer_rank is None
                              else 'retain eligible holdings within buffer rank; fill vacancies by ranking'),
                'allocation': 'equal capped weights',
                'execution': 'shared production plan_trades v1 (band/cash/AS rules)',
                'promotion': 'research-only; manual review required'}
        if self.version == 'research-portfolio-equal-v1' and self.holding_buffer_rank is None:
            body.pop('holding_buffer_rank')  # Original v1 manifest shape.
        body['sha256'] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return body

    def prepare(self, inputs, day):
        if day.research_ranking is None:
            if not hasattr(inputs, 'daily'):
                raise ValueError('研究投組缺少歷史行情或封存研究因子')
            day.research_ranking = compute_shadow_ranking(
                inputs.daily, day.data_date, selected_count=self.selected_count,
                fundamental=day.fund, news=day.news, formal_a3=day.pv['A3'])

    def __call__(self, day, state):
        if not rules.MIN_HOLDINGS <= self.selected_count <= rules.MAX_HOLDINGS:
            raise ValueError('競賽投組實驗須明確指定 20–30 檔；45 檔訊號研究仍保留')
        if not 0 <= self.target_cash <= rules.CASH_MAX:
            raise ValueError('研究目標現金超出限制')
        if day.research_ranking is None:
            raise ValueError('缺少研究排名')
        frame = day.research_ranking.set_index('ticker').copy()
        if not frame.index.is_unique:
            raise ValueError('研究排名有重複股票')
        frame = frame.loc[frame.index.intersection(day.master.index)]
        picks = frame.loc[frame['selected']].copy()
        if len(picks) != self.selected_count:
            raise ValueError('研究投組入選不足；不得補選或偷偷改變持股數')
        if self.holding_buffer_rank is not None:
            if type(self.holding_buffer_rank) is not int or self.holding_buffer_rank < self.selected_count:
                raise ValueError('研究續抱排名須為不小於持股數的整數')
            held = set(state.held_tickers())
            if held - set(frame.index):
                raise ValueError('研究持股缺少有效排名，須先核實資料，不得默默出清')
            # Keep the existing quality gate and news veto ahead of rank retention.
            kept = [t for t in frame.index if t in held
                    and frame.loc[t, 'shadow_rank'] <= self.holding_buffer_rank
                    and bool(frame.loc[t, 'research_eligible']) and not bool(frame.loc[t, 'news_veto'])]
            if len(kept) > self.selected_count:
                raise ValueError('可續抱檔數超過研究目標持股數，須核實帳本')
            added = [t for t in picks.index if t not in kept][:self.selected_count-len(kept)]
            picks = frame.loc[kept+added].copy()
            if len(picks) != self.selected_count:
                raise ValueError('研究續抱後入選不足，不得改變目標持股數')
        picks['cap'] = [0.24 if t == rules.TSMC else 0.09 for t in picks.index]
        picks['weight'] = cap_weights(pd.Series(1.0, index=picks.index), picks['cap'], 1-self.target_cash).round(4)
        return Targets(frame, picks, check_active_share(picks.weight, day.benches),
                       frame.loc[frame.news_veto].copy())
