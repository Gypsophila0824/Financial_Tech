import pandas as pd

from decimal import Decimal

from src.agent.factor_engine import build_targets, plan_trades
from src.agent import strategy, research_config
from src.agent.strategy_interface import ResearchPortfolio
from src.agent.state import PortfolioState
from src.agent.datafeed.fund_factors import production_score


def test_production_retains_through_rank_50(mock_inputs):
    day = mock_inputs.day('2026-10-27')
    ranked = build_targets(day.master, day.pv, day.fund, None, [], day.benches)
    at50, at51 = ranked.scored.index[49:51]
    result = build_targets(day.master, day.pv, day.fund, None, [at50, at51], day.benches)
    assert strategy.BUFFER_RANK == 50
    assert at50 in result.picks.index
    assert at51 not in result.picks.index
    assert len(result.picks) == strategy.N_HOLDINGS == 25


def test_production_weights_match_requested_variant():
    assert strategy.SCORE_WEIGHTS == {'pv_score': 0.35, 'fund_score': 0.45, 'news_score': 0.10,
                                      'market_cap_score': 0.10}
    assert strategy.FUND_SCORE_WEIGHTS == {'B1_rank': 0.50, 'B3_rank': 0.50}
    assert strategy.NO_TRADE_BAND == 0.03
    factors = pd.DataFrame({'B1_rank': [1.0, 0.0], 'B3_rank': [0.0, 1.0],
                            'B2_rank': [0.0, 1.0], 'B4_rank': [0.0, 1.0]})
    assert production_score(factors).tolist() == [0.5, 0.5]


def test_production_no_trade_band_includes_exact_three_percent_boundary():
    picks = pd.DataFrame({'weight': [0.13]}, index=pd.Index(['2330'], name='ticker'))
    plan = plan_trades(picks, {'2330': 1000}, Decimal('50000'), Decimal('1000000'),
                       pd.Series({'2330': 100.0}), {'2330'}, {}, '2026-10-27',
                       enforce_active_share=False)
    assert not plan.decisions
    assert '符合 3% 以內無交易帶' in plan.no_trades[0]['why']


def research_day(mock_inputs):
    day = mock_inputs.day('2026-10-27')
    tickers = list(day.master.index)
    day.research_ranking = pd.DataFrame({'ticker':tickers, 'shadow_rank':range(1,len(tickers)+1),
                                        'selected':[i<25 for i in range(len(tickers))],
                                        'research_eligible':True, 'news_veto':False})
    return day, tickers


def test_only_research_retains_55_and_exits_56(mock_inputs):
    day, tickers = research_day(mock_inputs)
    state = PortfolioState.initial()
    state.holdings = {tickers[54]:1000,tickers[55]:1000}
    adapter = ResearchPortfolio(25)
    result = adapter(day,state)
    assert research_config.PORTFOLIO_BUFFER_RANK == 55
    assert tickers[54] in result.picks.index
    assert tickers[55] not in result.picks.index
    assert len(result.picks)==25
    assert adapter.manifest()['holding_buffer_rank']==55
    # Archived v1 semantics are explicitly available without retention.
    direct = ResearchPortfolio(25,version='research-portfolio-equal-v1',holding_buffer_rank=None)
    assert tickers[54] not in direct(day,state).picks.index
    assert 'holding_buffer_rank' not in direct.manifest()


def test_research_veto_and_quality_gate_override_rank_retention(mock_inputs):
    day,tickers = research_day(mock_inputs)
    state = PortfolioState.initial()
    state.holdings={tickers[53]:1000,tickers[54]:1000}
    day.research_ranking.loc[53,'news_veto']=True
    day.research_ranking.loc[54,'research_eligible']=False
    result=ResearchPortfolio(25)(day,state)
    assert not set(state.holdings) & set(result.picks.index)
    assert tickers[53] in result.vetoed.index
