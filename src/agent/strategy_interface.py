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
    version: str = 'research-portfolio-equal-v1'

    def manifest(self):
        body = {**asdict(self), 'technical_weights': dict(research_config.EXPERIMENTAL_TECHNICAL_WEIGHTS),
                'selection': 'daily direct ranking; no holding buffer',
                'allocation': 'equal capped weights',
                'execution': 'shared production plan_trades v1 (band/cash/AS rules)',
                'promotion': 'research-only; manual review required'}
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
        frame = frame.loc[frame.index.intersection(day.master.index)]
        picks = frame.loc[frame['selected']].copy()
        if len(picks) != self.selected_count:
            raise ValueError('研究投組入選不足；不得補選或偷偷改變持股數')
        picks['cap'] = [0.24 if t == rules.TSMC else 0.09 for t in picks.index]
        picks['weight'] = cap_weights(pd.Series(1.0, index=picks.index), picks['cap'], 1-self.target_cash).round(4)
        return Targets(frame, picks, check_active_share(picks.weight, day.benches),
                       frame.loc[frame.news_veto].copy())
