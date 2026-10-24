from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.agent import daily_workflow as workflow
from src.agent.simulate import settle_rehearsal
from src.agent.state import PortfolioState


def test_date_selection_uses_calendar_holidays():
    assert workflow.target_date(datetime(2026, 10, 23, 20)) == '2026-10-27'
    assert workflow.target_date(datetime(2026, 10, 27, 8, 30)) == '2026-10-27'
    assert workflow.previous_trade_date('2026-10-27') == '2026-10-23'


class ETFInputs:
    def __init__(self, day):
        self.etf_dates = {'ETF': day}

    def _benches(self, day, notes):
        return {'ETF': pd.Series({'2330': .2})}, self.etf_dates['ETF']


def test_same_day_snapshot_skips_network():
    def forbidden():
        pytest.fail('不應重抓')
    report = workflow.ensure_etfs(ETFInputs('2026-10-20'), '2026-10-20', forbidden)
    assert not report['fetched']


def test_old_snapshot_refresh_does_not_relabel_source_date():
    calls = []
    report = workflow.ensure_etfs(ETFInputs('2026-10-19'), '2026-10-20',
                                  lambda: calls.append(1) or {'complete': True})
    assert calls == [1]
    assert report['source_pending_update']
    assert report['etf_dates']['ETF'] == '2026-10-19'


def test_partial_fetch_stops():
    with pytest.raises(ValueError, match='不完整'):
        workflow.ensure_etfs(ETFInputs('2026-10-19'), '2026-10-20', lambda: {'complete': False})


def test_changed_frozen_inputs_block(monkeypatch, tmp_path):
    source = tmp_path / 'data.csv'
    source.write_text('original', encoding='utf-8')
    monkeypatch.setattr(workflow, 'ROOT', tmp_path)
    monkeypatch.setattr(workflow, 'source_files', lambda: [source])
    out = tmp_path / 'run'
    out.mkdir()
    hashes = workflow.freeze_sources(out)
    source.write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='變動'):
        workflow.verify_sources(hashes)
    assert (out / 'sources/data.csv').read_text(encoding='utf-8') == 'original'


def test_history_shortage_is_not_strategy_failure():
    result = SimpleNamespace(daily_nav=pd.DataFrame({'date': ['2026-10-20']}),
                             summary={'end_date': '2026-10-20'}, compliance=pd.DataFrame())
    assert workflow.assess_backtest(result, '2026-10-20', 5) == 'insufficient_history'


def test_backtest_risk_blocks_even_with_enough_days():
    result = SimpleNamespace(daily_nav=pd.DataFrame({'date': ['2026-10-20']}),
                             summary={'end_date': '2026-10-20', 'daily_warning_count': 1}, compliance=pd.DataFrame())
    with pytest.raises(ValueError, match='違規'):
        workflow.assess_backtest(result, '2026-10-20', 1)


def test_rehearsal_passes_close_benchmarks(monkeypatch):
    received = {}
    inputs = SimpleNamespace(is_mock=False, evaluation_benchmarks=lambda d: ({'ETF': 'weights'}, {'ETF': d}),
                             fill_prices=lambda d: {}, close_prices=lambda d: {})
    def fake_settle(*args, **kwargs):
        received.update(kwargs)
        return args[0]
    monkeypatch.setattr('src.agent.simulate.settle', fake_settle)
    settle_rehearsal(PortfolioState.initial(), '2026-10-20', [], inputs)
    assert received == {'benchmarks': {'ETF': 'weights'}, 'etf_dates': {'ETF': '2026-10-20'}}


def test_formal_failure_never_marked_ready(monkeypatch, tmp_path):
    # The official policy gate remains owned by live.cmd_run, never silently approved.
    from src.agent.official_policy import require_formal_release
    policy = tmp_path / 'policy.json'
    policy.write_text('{"schema":"official-policy-v1","formal_release_approved":false}', encoding='utf-8')
    with pytest.raises(ValueError, match='尚未核准'):
        require_formal_release(policy)


@pytest.mark.parametrize('formal_code,expected', [(0, 'ready_for_review'), (1, 'formal_blocked')])
def test_workflow_end_to_end_gate(monkeypatch, tmp_path, formal_code, expected):
    from src.agent.backtest import runner
    calls = []
    class Inputs:
        def __init__(self, **kwargs):
            self.master = SimpleNamespace(index=pd.Index(['2330']))
        def prev_trading_day(self, day):
            return '2026-10-20'
        def dates(self):
            return ['2026-10-19', '2026-10-20']
        def day(self, day):
            return object()
    monkeypatch.setattr(workflow, 'StoreInputs', Inputs)
    monkeypatch.setattr(workflow, 'ensure_etfs', lambda *a: {'same_day': True})
    monkeypatch.setattr(workflow.market_cap_top20, 'score_for_date',
                        lambda *a, **k: calls.append('market_cap') or (pd.Series(dtype=float), '2026-10-20'))
    monkeypatch.setattr(workflow, 'freeze_sources', lambda *a: calls.append('freeze') or {})
    monkeypatch.setattr(workflow, 'verify_sources', lambda *a: calls.append('verify'))
    monkeypatch.setattr(workflow, 'run_day', lambda *a, **k: SimpleNamespace(checks=[]))
    monkeypatch.setattr(workflow, 'summarize', lambda *a: (True, 'passed'))
    monkeypatch.setattr(workflow.live, 'FETCH', [])
    monkeypatch.setattr(workflow.live, 'LEDGER', tmp_path / 'absent.json')
    monkeypatch.setattr(workflow.live, 'OUT', tmp_path / 'live')
    monkeypatch.setattr(workflow, 'submit_window', lambda *a: (datetime(2020, 1, 1, tzinfo=workflow.live.TPE),
                                                             datetime(2030, 1, 1, tzinfo=workflow.live.TPE)))
    result = SimpleNamespace(daily_nav=pd.DataFrame({'date': ['2026-10-20']}),
                             summary={'end_date': '2026-10-20'}, compliance=pd.DataFrame())
    monkeypatch.setattr(runner, 'run_formal_backtest', lambda *a, **k: result)
    monkeypatch.setattr(runner, 'write_result', lambda *a, **k: calls.append('backtest'))
    def formal(args):
        assert args.skip_fetch and args.reconciled
        calls.append('formal')
        return formal_code
    monkeypatch.setattr(workflow.live, 'cmd_run', formal)
    args = SimpleNamespace(trade_date='2026-10-21', team_id='TEAM_TEST', checks_only=False,
                           min_backtest_days=1, lookback_days=5, corporate_actions=None,
                           allow_insufficient_history=False, reconciled=True)
    report = {}
    assert workflow.execute(args, tmp_path, report) == formal_code
    assert report['status'] == expected
    assert calls == ['market_cap', 'freeze', 'backtest', 'verify', 'formal']
