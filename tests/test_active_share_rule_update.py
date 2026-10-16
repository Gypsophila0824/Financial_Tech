"""Regression tests for the organizer's top-fifteen / 15% rule update."""
import copy
import csv

import pandas as pd
import pytest

from src.agent import rules, strategy
from src.agent.active_share_risk import (EXPECTED_ETFS, matches_current_rule,
    record_close, rule_definition, validate_benchmarks)
from src.agent.backtest.risk import active_share_status
from src.agent.compliance import ComplianceContext, estimate_post_trade, run_checks
from src.agent.factor_engine import active_share, check_active_share, etf_top_holdings
from src.agent.governance import production_manifest
from src.agent.state import PortfolioState
from src.agent.tools.universe import load_universe


def full_snapshot(weights, day='2026-10-13'):
    return ({code: dict(weights) for code in EXPECTED_ETFS},
            {code: day for code in EXPECTED_ETFS})


def test_official_rule_and_internal_buffers_are_separate():
    assert rules.ACTIVE_SHARE_TOP_N == 15
    assert rules.ACTIVE_SHARE_MIN == .15
    assert strategy.AS_THRESHOLD == .30
    assert strategy.POST_AS_THRESHOLD == .25
    assert strategy.ACTIVE_SHARE_WARN == .25
    controls = production_manifest()['production_model']['risk_controls']
    assert controls['official_active_share_top_n'] == 15
    assert controls['official_active_share_minimum'] == .15


def test_etf_selection_includes_eleventh_to_fifteenth_and_excludes_sixteenth():
    rows = [dict(etf_id='TEST', code=str(2000+i), name=str(i), market='TW',
                 is_stock=True, weight_pct=(16-i)/2) for i in range(16)]
    rows.append(dict(etf_id='TEST', code='', name='期貨', market='TF',
                     is_stock=False, weight_pct=100))
    bench = etf_top_holdings(pd.DataFrame(rows).sample(frac=1, random_state=4))['TEST']
    assert len(bench) == 15
    assert set(bench.index) == {str(2000+i) for i in range(15)}
    # Preserve NAV weights: selected weights must not be normalized to sum to one.
    assert bench.sum() == pytest.approx(sum(range(2,17))/200)


def test_strategy_active_share_uses_ours_fifteenth_position():
    ours = pd.Series({str(2000+i): (20-i)/500 for i in range(16)})
    bench = ours.iloc[:15].copy()
    assert check_active_share(ours, {'TEST': bench}).loc['TEST','active_share'] == 0
    assert active_share(ours.iloc[:10], bench) > 0


def test_benchmark_validator_accepts_fifteen_and_rejects_sixteen():
    bench, dates = full_snapshot({str(2000+i): .01 for i in range(15)})
    assert validate_benchmarks(bench, dates, '2026-10-13') == []
    bench[next(iter(bench))]['2015'] = .01
    with pytest.raises(ValueError, match='15'):
        validate_benchmarks(bench, dates, '2026-10-13')


@pytest.mark.parametrize('score,below',[(.1499,True),(.15,False),(.1501,False),(.18,False)])
def test_threshold_is_strictly_below_fifteen_percent(score, below):
    bench, dates = full_snapshot({'OTHER': 2*score-.05})
    history = record_close([], '2026-10-13', {'5483': .05}, bench, dates)
    row = history[-1]['etfs'][next(iter(bench))]
    assert row['active_share'] == pytest.approx(score)
    assert row['below_minimum'] is below
    assert row['estimated_breach_streak'] == int(below)


def test_close_history_uses_the_fifteenth_position():
    ours = {str(2000+i): (20-i)/500 for i in range(16)}
    top = dict(sorted(ours.items(),key=lambda x:-x[1])[:15])
    bench, dates = full_snapshot(top)
    entry = record_close([], '2026-10-13', ours, bench, dates)[-1]
    assert entry['active_share_rule'] == rule_definition()
    assert all(row['active_share'] == 0 for row in entry['etfs'].values())


@pytest.mark.parametrize('old_rule',[None,{'top_n':10,'minimum':.20}])
def test_legacy_history_is_preserved_but_not_joined_to_new_streak(old_rule):
    bench, dates = full_snapshot({'5483': .1},'2026-10-12')
    old = record_close([], '2026-10-12', {'5483': .1}, bench, dates)[-1]
    if old_rule is None:
        del old['active_share_rule']
    else:
        old['active_share_rule'] = old_rule
    before = copy.deepcopy(old)
    bench, dates = full_snapshot({'5483': .1})
    history = record_close([old], '2026-10-13', {'5483': .1}, bench, dates)
    assert history[0] == before
    assert not history[-1]['prior_session_observed']
    assert all(r['estimated_breach_streak'] == 1 for r in history[-1]['etfs'].values())
    assert history[-1]['notes']
    state = PortfolioState.initial()
    state.active_share_history = history
    status = active_share_status(state,'2026-10-13')
    assert status['estimated_two_day_etfs'] == status['confirmed_two_day_etfs'] == []


def test_two_new_rule_days_continue_the_streak():
    bench, dates = full_snapshot({'5483': .1},'2026-10-12')
    history = record_close([], '2026-10-12', {'5483': .1}, bench, dates)
    bench, dates = full_snapshot({'5483': .1})
    history = record_close(history, '2026-10-13', {'5483': .1}, bench, dates)
    state = PortfolioState.initial()
    state.active_share_history = history
    status = active_share_status(state,'2026-10-13')
    assert set(status['confirmed_two_day_etfs']) == EXPECTED_ETFS


def test_compliance_and_review_both_use_top_fifteen(mock_run):
    trade_date, state, result = mock_run.days[0]
    day = mock_run.inputs.day(trade_date)
    prices = day.close.to_dict()
    post, _, nav, _, _ = estimate_post_trade(state.holdings,state.cash,result.plan['orders'],prices)
    ours = {t:s*prices[t]/nav for t,s in post.items()}
    top = dict(sorted(ours.items(),key=lambda x:-x[1])[:15])
    ctx = ComplianceContext(state,prices,load_universe(),{'CLONE':top},result.path.name,False)
    checks = run_checks(result.plan,ctx)
    check = next(c for c in checks if c.rule == '六(十三) Active Share ≥ 15%')
    assert not check.passed and '0.00%' in check.detail
    report = (result.path.parent/f'review_MOCK_{trade_date}.md').read_text(encoding='utf-8')
    assert '前 15 大持股' in report and '規則下限 15%' in report


def test_preflight_does_not_use_legacy_breach_flags(mock_run):
    trade_date, state, result = mock_run.days[1]
    state = copy.deepcopy(state)
    day = mock_run.inputs.day(trade_date)
    prices=day.close.to_dict()
    post,_,nav,_,_=estimate_post_trade(state.holdings,state.cash,result.plan['orders'],prices)
    top=dict(sorted(((t,s*prices[t]/nav) for t,s in post.items()),key=lambda x:-x[1])[:15])
    bench, dates = full_snapshot(top,day.data_date)
    old = record_close([],day.data_date,top,bench,dates)[-1]
    del old['active_share_rule']
    state.active_share_history=[old]
    ctx=ComplianceContext(state,prices,load_universe(),bench,result.path.name,True,
        etf_dates=dates,data_date=day.data_date)
    checks=run_checks(result.plan,ctx)
    assert next(c for c in checks if c.rule=='Active Share 連續低於門檻風險（內部保護）').passed
    assert not next(c for c in checks if c.rule=='Active Share 前日收盤觀測').passed


def test_mock_snapshots_have_fifteen_stocks_per_official_etf(mock_inputs):
    assert set(mock_inputs.benches) == EXPECTED_ETFS
    assert all(len(b)==15 for b in mock_inputs.benches.values())


def test_fetcher_exports_fifteen_without_network(tmp_path,monkeypatch):
    from src.agent.datafeed import etf_holdings as feed
    rows=[(str(i),str(2000+i),'TW',16-i,'1000') for i in range(16)]
    monkeypatch.setattr(feed,'OUT_DIR',tmp_path)
    monkeypatch.setattr(feed,'ETFS',[('TEST','TEST')])
    monkeypatch.setattr(feed,'fetch',lambda _: 'fixture')
    monkeypatch.setattr(feed,'parse',lambda _: ('2026-10-13',rows))
    monkeypatch.setattr(feed.time,'sleep',lambda _: None)
    feed.main()
    exported=list(csv.DictReader((tmp_path/'active_etf_top15_2026-10-13.csv').open(encoding='utf-8-sig')))
    assert len(exported)==15
    assert exported[-1]['code']=='2014'
    assert not (tmp_path/'active_etf_top10_2026-10-13.csv').exists()
