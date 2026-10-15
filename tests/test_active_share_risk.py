import copy

import pandas as pd
import pytest

from src.agent.active_share_risk import EXPECTED_ETFS, record_close, validate_benchmarks
from src.agent.factor_engine import etf_top10, active_share
from src.agent.inputs import StoreInputs
from src.agent.state import PortfolioState, settle
from src.agent.compliance import ComplianceContext, run_checks
from src.agent.tools.universe import load_universe


def snapshot(day='2026-10-13'):
    return ({code: {'5483': .1} for code in EXPECTED_ETFS}, {code: day for code in EXPECTED_ETFS})


def test_taiwan_markets_identify_same_stock():
    for market in ('TW', 'TWO'):
        h = pd.DataFrame([dict(etf_id='TEST', code='5483', name='TEST', market=market,
                               is_stock=True, weight_pct=10)])
        b = etf_top10(h)['TEST']
        assert list(b.index) == ['5483']
        assert active_share(pd.Series({'5483': .1}), b) == 0
        with pytest.raises(ValueError, match='重複'):
            etf_top10(pd.concat([h, h]))


def test_parser_preserves_three_letter_market():
    from src.agent.datafeed.etf_holdings import parse
    html = '全部持股|資料日期：2026/10/13|個股名稱|投資比例(%)|持有股數|中美晶(5483.TWO)|10|1,000|相關基金'
    day, rows = parse(html)
    assert day == '2026-10-13'
    assert rows[0][1:3] == ('5483', 'TWO')


def test_recovery_exact_threshold_and_independent_etfs():
    b, dates = snapshot('2026-10-12')
    h = record_close([], '2026-10-12', {'5483': .1}, b, dates)
    code = next(iter(b))
    b[code] = {'OTHER': .3}  # .5 * (.1 + .3) == .2，等於門檻不算低於
    dates = {c: '2026-10-13' for c in b}
    h = record_close(h, '2026-10-13', {'5483': .1}, b, dates)
    assert h[-1]['etfs'][code]['estimated_breach_streak'] == 0
    assert all(row['estimated_breach_streak'] == 2 for c, row in h[-1]['etfs'].items() if c != code)


def test_bundle_preserves_per_etf_dates(mock_run, tmp_path):
    from src.agent.bundle import BundleInputs, load, save_bundle
    trade_date, state, result = mock_run.days[0]
    day = mock_run.inputs.day(trade_date)
    day.etf_dates = {'00980A': day.data_date}
    p = tmp_path / 'inputs.json'
    save_bundle(p, day=day, state=state, plan=result.plan)
    assert BundleInputs(load(p)).day(trade_date).etf_dates == day.etf_dates


def test_benchmark_coverage_and_dates():
    b, dates = snapshot()
    assert validate_benchmarks(b, dates, '2026-10-13') == []
    code = next(iter(b))
    dates[code] = '2026-10-08'
    assert code in validate_benchmarks(b, dates, '2026-10-13')[0]
    dates[code] = '2026-10-14'
    with pytest.raises(ValueError, match='晚於'):
        validate_benchmarks(b, dates, '2026-10-13')
    dates[code] = ''
    with pytest.raises(ValueError, match='有效資料日期'):
        validate_benchmarks(b, dates, '2026-10-13')
    del b[code]
    with pytest.raises(ValueError, match='清單不完整'):
        validate_benchmarks(b, dates, '2026-10-13')


@pytest.mark.parametrize('weight', [float('nan'), float('inf'), -0.1, 1.1])
def test_invalid_weight_rejected(weight):
    b, dates = snapshot()
    b[next(iter(b))]['5483'] = weight
    with pytest.raises(ValueError, match='權重無效'):
        validate_benchmarks(b, dates, '2026-10-13')


def test_consecutive_days_reset_gap_and_idempotency():
    b, dates = snapshot('2026-10-08')
    h = record_close([], '2026-10-08', {'5483': .1}, b, dates)
    b, dates = snapshot('2026-10-12')  # 10/9 假日與週末不打斷連續交易日
    h = record_close(h, '2026-10-12', {'5483': .1}, b, dates)
    code = next(iter(b))
    assert h[-1]['etfs'][code]['same_date_breach_streak'] == 2
    h = record_close(h, '2026-10-12', {'5483': .1}, b, dates)
    assert len(h) == 2 and h[-1]['etfs'][code]['same_date_breach_streak'] == 2
    b, dates = snapshot('2026-10-13')
    h = record_close(h, '2026-10-13', {'OTHER': .5}, b, dates)
    assert h[-1]['etfs'][code]['estimated_breach_streak'] == 0
    b, dates = snapshot('2026-10-15')
    h = record_close(h, '2026-10-15', {'5483': .1}, b, dates)
    assert h[-1]['etfs'][code]['estimated_breach_streak'] == 1
    assert not h[-1]['prior_session_observed']


def test_old_benchmark_not_confirmed_same_date():
    b, dates = snapshot('2026-10-12')
    h = record_close([], '2026-10-12', {'5483': .1}, b, dates)
    h = record_close(h, '2026-10-13', {'5483': .1}, b, dates)
    r = h[-1]['etfs'][next(iter(b))]
    assert r['estimated_breach_streak'] == 2
    assert r['same_date_breach_streak'] == 0
    assert h[-1]['notes']


def test_settlement_history_persists_and_old_ledgers_load(tmp_path):
    b, dates = snapshot()
    state = PortfolioState(as_of='2026-10-12', nav=1000, cash=900,
                           holdings={'5483': 1}, last_close={'5483': 100})
    result = settle(state, '2026-10-13', [], {}, {'5483': 100}, benchmarks=b, etf_dates=dates)
    assert not state.active_share_history
    p = tmp_path / 'ledger.json'
    result.save(p)
    assert PortfolioState.load(p).active_share_history == result.active_share_history
    assert settle(result, '2026-10-14', [], {}, {'5483': 100}).active_share_history == result.active_share_history
    p.write_text('{"as_of": null, "nav": 1000, "cash": 1000}', encoding='utf-8')
    assert PortfolioState.load(p).active_share_history == []


def test_store_rejects_partial_etf_file(tmp_path, monkeypatch):
    import src.agent.inputs as inputs_module
    monkeypatch.setattr(inputs_module, 'ETF_DIR', tmp_path)
    pd.DataFrame([dict(etf_id='00980A', code='5483', name='TEST', market='TW',
                       is_stock=True, weight_pct=10, data_date='2026-10-13')]).to_csv(
        tmp_path / 'active_etf_holdings_2026-10-13.csv', index=False)
    with pytest.raises(ValueError, match='清單不完整'):
        StoreInputs()._benches('2026-10-13', [])


def test_live_preflight_checks_full_list_and_prior_risk(mock_run):
    trade_date, state, result = mock_run.days[1]
    day = mock_run.inputs.day(trade_date)
    # 與當日委託預估相同的 top10，故 AS 為 0。
    from src.agent.compliance import estimate_post_trade
    post, _, nav, _, _ = estimate_post_trade(state.holdings, state.cash, result.plan['orders'], day.close.to_dict())
    weights = {t: s * day.close[t] / nav for t, s in post.items()}
    top = dict(sorted(weights.items(), key=lambda x: -x[1])[:10])
    b = {code: top for code in EXPECTED_ETFS}
    dates = {code: day.data_date for code in b}
    state = copy.deepcopy(state)
    state.active_share_history = record_close([], day.data_date, weights, b, dates)
    ctx = ComplianceContext(state, day.close.to_dict(), load_universe(), b, result.path.name, True,
                            etf_dates=dates, data_date=day.data_date)
    checks = run_checks(result.plan, ctx)
    assert any(c.rule == 'Active Share 連續低於門檻風險（內部保護）' and not c.passed for c in checks)
    del b[next(iter(b))]
    assert any(c.rule == 'Active Share 官方清單與資料完整性' and not c.passed for c in run_checks(result.plan, ctx))
