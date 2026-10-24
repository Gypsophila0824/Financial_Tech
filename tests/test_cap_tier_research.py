from decimal import Decimal
import pandas as pd
import pytest
from src.agent.cap_tier_research import CapTierResearch, tier_score
from src.agent.cap_research_execution import plan_trades as research_plan
from src.agent.factor_engine import plan_trades as production_plan
from src.agent.state import PortfolioState


def research_case(mock_inputs,rank):
    day=mock_inputs.day('2026-10-27')
    day.benches={'test':pd.Series({'FOREIGN':1.0})}
    day.cap_research_ranks=pd.Series(21,index=day.master.index)
    state=PortfolioState.initial()
    adapter=CapTierResearch(pv_weight=.4,cap_weight=0)
    first=adapter(day,state)
    target=first.scored.index[rank-1]
    day.cap_research_ranks.loc[target]=1
    state.holdings={target:1000}
    return adapter,day,state,target


def test_market_cap_tier_boundaries():
    assert [tier_score(r) for r in [1,2,3,4,5,6,10,11,20,21]]==[2,1.5,1,.8,.8,.65,.65,.55,.55,.5]


def test_explicit_no_active_share_mode_uses_no_future_etf_snapshot(mock_inputs):
    a,d,s,t=research_case(mock_inputs,71)
    a.enforce_active_share=False
    d.benches={}
    result=a(d,s)
    assert len(result.picks)==25 and result.as_report.empty
    assert result.picks.weight.sum()==pytest.approx(.95,abs=.002)


def test_protect_through_70_and_trim_at_71(mock_inputs):
    a,d,s,t=research_case(mock_inputs,70)
    result=a(d,s)
    assert t in result.picks.index and not result.picks.loc[t,'force_rebalance']
    a,d,s,t=research_case(mock_inputs,71)
    result=a(d,s)
    audit=next(r for r in a.audit if r['ticker']==t and r['trim_triggered'])
    assert len(result.picks)==25 and result.picks.loc[t,'force_rebalance']
    assert audit['target_weight']==pytest.approx(audit['untrimmed_target']*.5)


def test_cap_top3_exit_restores_regular_buffer(mock_inputs):
    a,d,s,t=research_case(mock_inputs,71)
    d.cap_research_ranks.loc[t]=4
    assert t not in a(d,s).picks.index


def test_news_veto_overrides_protection(mock_inputs):
    a,d,s,t=research_case(mock_inputs,71)
    d.news=pd.DataFrame({'news_score':.5,'worst_event':''},index=d.master.index)
    d.news.loc[t,'worst_event']='high:bad'
    d.pv.loc[t,'A3']=-.1
    result=a(d,s)
    assert t not in result.picks.index and t in result.vetoed.index


def test_forced_trim_bypasses_band_without_changing_ordinary_trades():
    picks=pd.DataFrame({'weight':[.08],'force_rebalance':[False]},index=['2330'])
    args=({'2330':10000},Decimal('500000'),Decimal('10000000'),pd.Series({'2330':100.}),{'2330'},{},'2026-10-02')
    original=production_plan(picks,*args,enforce_active_share=False)
    ordinary=research_plan(picks,*args,enforce_active_share=False)
    assert original.decisions==ordinary.decisions==[]
    picks.loc['2330','force_rebalance']=True
    forced=research_plan(picks,*args,enforce_active_share=False)
    assert forced.decisions[0]['side']=='SELL'
    assert forced.decisions[0]['target_shares']==8000
