from decimal import Decimal
import json

import pandas as pd
import pytest

from src.agent.backtest import archive
from src.agent.backtest.corporate_actions import CorporateAction, DividendLedger, load_actions
from src.agent.backtest.runner import run_formal_backtest, write_result, _summary
from src.agent.inputs import MockInputs
from src.agent import rules


def event(**kw):
    args = dict(event_id='2330-20261028', ticker='2330', ex_date='2026-10-28',
                published_at='2026-10-20T10:00:00+08:00', source='synthetic-test-only',
                cash_per_share='2.5', stock_per_share='0.025')
    return CorporateAction(**(args | kw))


def test_dividends_deferred_and_odd_lots_retained():
    book = DividendLedger()
    holdings = {'2330': 1000}
    book.accrue('2026-10-28', holdings, [event()])
    assert holdings == {'2330': 1000}
    assert book.cash == Decimal('2500')
    assert book.stocks == {'2330': Decimal('25')}
    # 當天／後來賣出不取消權益，期末只估值一次，不產生交易或費稅。
    book.accrue('2026-10-29', {}, [event()])
    terminal = book.finalize('2026-10-29', {'2330': 100})
    assert Decimal(terminal['nav_adjustment']) == 5000
    assert book.snapshot()['pending_cash'] == '0'
    with pytest.raises(ValueError, match='重複'):
        book.finalize('2026-10-29', {'2330': 100})


def test_ex_date_purchase_not_entitled():
    book = DividendLedger()
    book.accrue('2026-10-28', {}, [event()])
    assert book.entitlements == []


@pytest.mark.parametrize('price', [None, 0, -1, float('nan'), float('inf')])
def test_terminal_requires_current_valid_close(price):
    book = DividendLedger()
    book.accrue('2026-10-28', {'2330': 1000}, [event()])
    with pytest.raises(ValueError):
        book.finalize('2026-10-28', {} if price is None else {'2330': price})
    assert book.terminal is None


def test_duplicates_future_and_unknown_actions_fail_without_partial_accrual():
    for actions in [[event(), event()], [event(published_at='2026-10-28T09:00:00+08:00')],
                    [event(kind='split')], [event(stock_per_share='0.0001')]]:
        book = DividendLedger()
        with pytest.raises(ValueError):
            book.accrue('2026-10-28', {'2330': 1000}, actions)
        assert book.entitlements == [] and book.processed_dates == set()


def test_missing_known_event_and_ambiguous_repeated_entitlement_stop():
    book = DividendLedger()
    with pytest.raises(ValueError, match='缺少股利'):
        book.accrue('2026-10-28', {'2330': 1000}, [], {('2026-10-28', '2330')})
    book.accrue('2026-10-28', {'2330': 1000}, [event()])
    with pytest.raises(ValueError, match='權益基數'):
        book.accrue('2026-10-29', {}, [event(event_id='next', ex_date='2026-10-29')])


def test_event_loader_snapshot(tmp_path):
    path = tmp_path / 'events.json'
    from dataclasses import asdict
    path.write_text(json.dumps([asdict(event())]), encoding='utf-8')
    actions, source = load_actions(path)
    assert actions == [event()] and len(source['sha256']) == 64
    with pytest.raises(ValueError):
        event(published_at='2026-10-20T10:00:00')
    with pytest.raises(ValueError):
        event(cash_per_share='NaN')


@pytest.fixture(scope='module')
def data():
    return MockInputs(seed=42)


def test_runner_entitlements_final_nav_and_replay(data, tmp_path, monkeypatch):
    baseline = run_formal_backtest(data, start='2026-10-27', days=3)
    prior = baseline.holdings[baseline.holdings.date == '2026-10-27'].iloc[0]
    action = event(ticker=prior.ticker, stock_per_share='0', cash_per_share='1')
    result = run_formal_backtest(data, start='2026-10-27', days=3,
                                corporate_actions=[action], terminal_date='2026-10-29')
    assert result.daily_nav.nav.iloc[-1] == pytest.approx(baseline.daily_nav.nav.iloc[-1] + prior.shares)
    pd.testing.assert_frame_equal(result.trades, baseline.trades)
    assert result.daily_nav.cash.tolist() == baseline.daily_nav.cash.tolist()
    assert result.dividend_ledger['entitlements'][0]['eligible_shares'] == prior.shares
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', tmp_path)
    target = tmp_path / 'run'
    write_result(result, target, config={})
    frozen = archive.ArchivedInputs(target)
    replay = run_formal_backtest(frozen, corporate_actions=[action], terminal_date='2026-10-29')
    pd.testing.assert_frame_equal(result.daily_nav, replay.daily_nav, rtol=1e-10, atol=1e-6)
    pd.testing.assert_frame_equal(result.trades, replay.trades, rtol=1e-10, atol=1e-6)
    with pytest.raises(FileExistsError):
        write_result(result, target, config={})
    (target / 'daily_nav.csv').write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='完整性'):
        archive.verify_archive(target)


def test_runner_does_not_pay_on_arbitrary_end(data):
    first = run_formal_backtest(data, start='2026-10-27', days=1)
    ticker = first.holdings.iloc[0].ticker
    result = run_formal_backtest(data, start='2026-10-27', days=2,
                                corporate_actions=[event(ticker=ticker, stock_per_share='0')])
    assert result.dividend_ledger['terminal'] is None
    assert Decimal(result.dividend_ledger['pending_cash']) > 0
    assert result.daily_nav.terminal_dividend_adjustment.sum() == 0
    with pytest.raises(ValueError, match='terminal_date'):
        run_formal_backtest(data, start='2026-10-27', days=2, terminal_date='2026-10-27')


def test_missing_midrun_and_missing_calendar_session_stop(data):
    class Missing:
        is_mock = True
        def __getattr__(self, key): return getattr(data, key)
        def day(self, d):
            if d == '2026-10-28': raise FileNotFoundError('test missing day')
            return data.day(d)
    with pytest.raises(ValueError, match='途中缺資料'):
        run_formal_backtest(Missing(), start='2026-10-27', days=3)
    class Gap(Missing):
        def dates(self): return [d for d in data.dates() if d != '2026-10-28']
    with pytest.raises(ValueError, match='不連續'):
        run_formal_backtest(Gap(), start='2026-10-27', days=3)


def test_initial_bad_data_is_not_silently_skipped(data):
    class Bad:
        is_mock = True
        def __getattr__(self, key): return getattr(data, key)
        def day(self, d): raise ValueError('malformed input')
    with pytest.raises(ValueError, match='malformed'):
        run_formal_backtest(Bad(), start='2026-10-27', days=2)


def test_summary_includes_first_day_loss():
    daily = pd.DataFrame(dict(date=['2026-10-27','2026-10-28'],
        nav=[rules.INITIAL_CAPITAL * .9] * 2, daily_return=[-.1, 0], benchmark_nav=[1,1],
        drawdown=[-.1,-.1], turnover=[0,0], cost=[0,0], min_active_share=[.3,.3]))
    result = _summary(daily, [])
    assert result['annualized_return'] == pytest.approx(.9 ** 126 - 1)
    assert result['annualization_periods'] == 2
    assert result['short_sample_warning']


def test_protected_output_and_source_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', tmp_path / 'allowed')
    with pytest.raises(ValueError): archive.safe_output(tmp_path / 'live')
    with pytest.raises(ValueError): archive.safe_output(tmp_path / 'allowed')
    with pytest.raises(ValueError): archive.safe_output(tmp_path / 'allowed' / '..' / 'state')
    import hashlib
    p = tmp_path / 'source'
    p.write_bytes(b'original')
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest()}
    p.write_bytes(b'updated')
    with pytest.raises(ValueError, match='來源已改變'): archive.check_unchanged(hashes)


def test_no_valid_days_and_missing_close(data):
    class Empty:
        is_mock = True
        def __getattr__(self, key): return getattr(data, key)
        def day(self, d): raise FileNotFoundError('no inputs')
    with pytest.raises(ValueError, match='沒有有效'):
        run_formal_backtest(Empty(), start='2026-10-27', days=2)
    class NoClose(Empty):
        def day(self, d): return data.day(d)
        def close_prices(self, d): return {}
    with pytest.raises(ValueError, match='有效收盤價'):
        run_formal_backtest(NoClose(), start='2026-10-27', days=2)


def test_replay_cli_and_external_master_not_used(data, tmp_path, monkeypatch):
    import sys
    from src.agent.backtest import runner
    from src.agent import inputs
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', tmp_path)
    result = run_formal_backtest(data, start='2026-10-27', days=2)
    original, replay_path = tmp_path / 'original', tmp_path / 'replay'
    write_result(result, original, config={})
    def forbidden(): raise AssertionError('must not read external master')
    monkeypatch.setattr(inputs, 'load_master', forbidden)
    monkeypatch.setattr(sys, 'argv', ['runner', '--replay', str(original), '--out-dir', str(replay_path)])
    runner.main()
    assert json.loads((replay_path / 'summary.json').read_text(encoding='utf-8'))['replay_verified']
    archive.verify_archive(replay_path)


def test_report_exports_daily_risk_and_per_etf_active_share(data, tmp_path, monkeypatch):
    result = run_formal_backtest(data, start='2026-10-27', days=1)
    out = tmp_path / 'allowed' / 'risk-report'
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', tmp_path / 'allowed')
    write_result(result, out, config={})
    details = pd.read_csv(out / 'active_share_detail.csv', dtype={'etf_id': str})
    assert {'date', 'etf_id', 'active_share', 'etf_snapshot_date', 'snapshot_age_trading_days'} <= set(details.columns)
    assert len(details) == len(data.benches)
    assert set(details['snapshot_age_trading_days']) == {1}
    report = (out / 'report.md').read_text(encoding='utf-8')
    assert '## 每日持倉與風險' in report
    assert '現金比' in report and '最低 Active Share' in report
