"""Offline cap-tier parameter study; no live entry-point integration."""
from dataclasses import dataclass, asdict
import hashlib
import json
import pandas as pd
from . import strategy as S, rules
from .factor_engine import Targets, build_targets, cap_weights, check_active_share


def tier_score(rank):
    if rank == 1: return 2.0
    if rank == 2: return 1.5
    if rank == 3: return 1.0
    if rank <= 5: return .80
    if rank <= 10: return .65
    if rank <= 20: return .55
    return .50


@dataclass
class CapTierResearch:
    pv_weight: float = .30
    cap_weight: float = .10
    retain_fraction: float | None = .50
    protect_rank: int = 70
    no_trade_band: float = .03
    normal_buffer: int = 50
    news_weight: float = .10
    enforce_active_share: bool = True

    def __post_init__(self):
        if min(self.pv_weight, self.cap_weight, self.fund_weight) < 0:
            raise ValueError('Invalid scoring weights')
        if self.retain_fraction is not None and not 0 < self.retain_fraction <= 1:
            raise ValueError('Invalid retain fraction')
        self.audit = []

    @property
    def fund_weight(self):
        return round(1-self.news_weight-self.pv_weight-self.cap_weight, 10)

    def manifest(self):
        body = {**asdict(self), 'fund_weight': self.fund_weight,
                'version': 'cap-tier-research-v1', 'cap_scores': [2,1.5,1,.8,.65,.55,.5],
                'allocation': 'production score-edge/vol60 sizing; cap-top3 held names protected; trim beyond protect_rank caps weight at retain_fraction times same-day untrimmed target; redistribute released allocation',
                'protection_exit': 'current market-cap rank outside top3 restores ordinary buffer50; news veto and mandatory risk controls override protection',
                'execution': 'shared engine research copy; force trimmed names beyond rank threshold through no-trade band; minimum one existing lot retained',
                'promotion': 'offline research only; no production settings changed'}
        body['sha256'] = hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        return body

    def prepare(self, inputs, day):
        return None

    def __call__(self, day, state):
        ranks = day.cap_research_ranks
        capscores = ranks.map(tier_score)
        base = build_targets(day.master,day.pv,day.fund,day.news,[],day.benches,
            score_weights={'pv_score':self.pv_weight,'fund_score':self.fund_weight,
                           'news_score':self.news_weight,'market_cap_score':self.cap_weight},
            market_cap_score=capscores,buffer_rank=self.normal_buffer,enforce_active_share=False)
        df=base.scored
        held=set(state.held_tickers())
        top3=set(ranks[ranks<=3].index)
        protected=held & top3 if self.retain_fraction is not None else set()
        kept=[t for t in df.index if t in held and not df.loc[t,'veto'] and
              (df.loc[t,'rank']<=self.normal_buffer or t in protected)]
        added=[t for t in df.index if t not in kept and not df.loc[t,'veto']][:S.N_HOLDINGS-len(kept)]
        picks=df.loc[kept+added].copy()
        raw=(picks.total_score-S.NEUTRAL_SCORE).clip(lower=.01)/picks.vol60.fillna(picks.vol60.median())
        caps=pd.Series(S.CAP_OTHER,index=picks.index)
        if rules.TSMC in caps.index: caps.loc[rules.TSMC]=S.CAP_2330
        untrimmed=cap_weights(raw,caps,1-S.TARGET_CASH)
        trimmed=[t for t in picks.index if t in protected and df.loc[t,'rank']>self.protect_rank]
        picks['force_rebalance']=False
        for t in trimmed:
            one_lot=1000*float(day.close[t])/state.nav
            caps.loc[t]=min(caps.loc[t],max(one_lot,untrimmed[t]*self.retain_fraction))
            picks.loc[t,'force_rebalance']=True
        adjustments=[]
        for _ in range(S.MAX_AS_ITERATIONS if self.enforce_active_share else 0):
            weights=cap_weights(raw,caps,1-S.TARGET_CASH)
            report=check_active_share(weights,day.benches)
            worst=report.iloc[0]
            if worst.active_share>=S.AS_THRESHOLD: break
            top=weights.nlargest(rules.ACTIVE_SHARE_TOP_N)
            shared=top.index.intersection(day.benches[worst.name].index)
            if shared.empty: raise ValueError('Unable to enforce Active Share')
            target=max(shared,key=lambda t:min(top[t],day.benches[worst.name][t]))
            caps[target]=max(min(caps[target],weights[target])-.01,.005)
            adjustments.append(f'{target}: Active Share cap adjustment')
        else:
            if self.enforce_active_share:
                raise ValueError('Active Share iterations exceeded')
            weights=cap_weights(raw,caps,1-S.TARGET_CASH)
            report=check_active_share(weights,{})
        picks['weight']=weights.round(4)
        picks['cap']=caps
        for t in sorted(top3):
            self.audit.append({'date':day.trade_date,'ticker':t,'market_cap_rank':int(ranks[t]),
                'score_rank':int(df.loc[t,'rank']),'held':t in held,'protected':t in protected,
                'trim_triggered':t in trimmed,'target_weight':float(weights.get(t,0)),
                'untrimmed_target':float(untrimmed.get(t,0)), 'veto':bool(df.loc[t,'veto'])})
        vetoed=df[df.veto].copy()
        vetoed['held']=[t in held for t in vetoed.index]
        return Targets(df,picks.sort_values('weight',ascending=False),report,vetoed,adjustments)
