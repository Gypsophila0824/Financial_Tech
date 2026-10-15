import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.agent.metrics import gross_turnover, selection_change
from src.agent.news_contract import validate_news
from src.agent.inputs import MockInputs
from src.agent.backtest.runner import run_formal_backtest
from src.agent.strategy_interface import ResearchPortfolio
from src.agent.state import PortfolioState, settle
from src.agent.models import OrderDraft
from src.agent import settlement_transaction as tx
from src.agent import research, validation


def news():
    return pd.DataFrame([dict(trade_date='2026-10-27', ticker='2330', news_score=.5, worst_event='',
        published_at='2026-10-26T13:00:00+08:00', available_at='2026-10-26T13:01:00+08:00',
        analyzed_at='2026-10-27T08:00:00+08:00', snapshot_cutoff='2026-10-27T08:55:00+08:00',
        source='test', snapshot_version='v1', model_version='test', prompt_version='v1', content_sha256='a'*64)])


def test_news_time_boundary():
    assert len(validate_news(news(), '2026-10-27', '2026-10-27T08:55:00+08:00')) == 1
    for field in ('available_at', 'analyzed_at', 'snapshot_cutoff'):
        frame = news(); frame[field] = '2026-10-27T08:55:01+08:00'
        with pytest.raises(ValueError):
            validate_news(frame, '2026-10-27', '2026-10-27T08:55:00+08:00')
    frame = news(); frame['published_at'] = '2026-10-26T13:00:00'
    with pytest.raises(ValueError):
        validate_news(frame, '2026-10-27', '2026-10-27T08:55:00+08:00')


def test_metrics_do_not_confuse_selection_with_trading():
    assert selection_change({'a','b'}, {'a','b'}) == 0
    assert selection_change(set(), set()) == 0
    assert gross_turnover(100, 50, 1000) == .15
    with pytest.raises(ValueError):
        gross_turnover(0, 0, 0)


def test_accounting_conservation():
    initial = PortfolioState.initial()
    orders = [OrderDraft('2330','BUY',1000,'D1')]
    final = settle(initial,'2026-10-27',orders,{'2330':100},{'2330':100})
    assert final.cash + 1000*100 == final.nav == initial.nav-142
    sold = settle(final,'2026-10-28',[OrderDraft('2330','SELL',1000,'D1')],{'2330':100},{'2330':100})
    assert sold.cash == sold.nav == initial.nav-142-142-300


def test_interrupted_settlement_recovers_without_double_charge(tmp_path, monkeypatch):
    ledger = tmp_path/'ledger.json'
    before = PortfolioState.initial(); before.save(ledger)
    after = settle(before,'2026-10-27',[OrderDraft('2330','BUY',1000,'D1')],{'2330':100},{'2330':100})
    real = tx.atomic_write
    def interrupted(path, data):
        if path == ledger:
            raise OSError('synthetic crash')
        real(path,data)
    monkeypatch.setattr(tx,'atomic_write',interrupted)
    with pytest.raises(OSError):
        tx.commit(ledger,'2026-10-27','TEST',asdict(before),asdict(after),{'official_2026-10-27.json':b'evidence'})
    monkeypatch.setattr(tx,'atomic_write',real)
    tx.recover(ledger,'2026-10-27','TEST')
    sealed = ledger.read_bytes()
    tx.recover(ledger,'2026-10-27','TEST')
    assert ledger.read_bytes() == sealed
    assert asdict(PortfolioState.load(ledger)) == asdict(after)


def test_research_adapter_is_explicit_and_separate():
    inputs = MockInputs()
    day = inputs.day('2026-10-27')
    adapter = ResearchPortfolio(25)
    adapter.prepare(inputs, day)
    one = adapter(day, PortfolioState.initial())
    two = adapter(day, PortfolioState.initial())
    pd.testing.assert_frame_equal(one.picks, two.picks)
    assert len(one.picks) == 25 and abs(one.picks.weight.sum()-.95)<1e-6
    with pytest.raises(ValueError):
        ResearchPortfolio(45)(day, PortfolioState.initial())
    result = run_formal_backtest(inputs,start='2026-10-27',days=2,strategy_adapter=adapter)
    assert result.summary['strategy_manifest']['version'] == adapter.version
    assert len(result.daily_nav) == 2


def test_post_close_etf_does_not_change_decisions():
    class Inputs(MockInputs):
        def evaluation_benchmarks(self, day):
            return {k: pd.Series({'UNRELATED':.6}) for k in self.benches}, {k:day for k in self.benches}
    a = run_formal_backtest(MockInputs(),start='2026-10-27',days=1)
    b = run_formal_backtest(Inputs(),start='2026-10-27',days=1)
    pd.testing.assert_frame_equal(a.trades,b.trades)
    assert b.daily_nav.active_share_same_day_data.all()


def test_lock_validates_temporal_split_and_mutation(tmp_path, monkeypatch):
    spec = {'development':['2025-01-01','2025-02-01'], 'validation':['2025-03-01','2025-04-01'],
            'test':['2025-05-01','2025-06-01'], 'research_selected_count':25,
            'news_policy':'neutral_ablation','prior_exposure_disclosure':'already seen','claims_unseen_oos':False}
    source = tmp_path/'source.py'; source.write_text('a')
    monkeypatch.setattr(validation.codeversion,'_files',lambda:[source])
    monkeypatch.setattr(validation,'ROOT',tmp_path)
    lock = tmp_path/'lock.json'; validation.seal(spec,lock)
    validation.verify(lock)
    source.write_text('b')
    with pytest.raises(ValueError): validation.verify(lock)
    with pytest.raises(FileExistsError): validation.seal(spec,lock)
    spec['test'][0]='2025-03-01'
    with pytest.raises(ValueError): validation.validate_spec(spec)


def test_formal_release_is_blocked():
    from src.agent.official_policy import require_formal_release
    with pytest.raises(ValueError): require_formal_release()


def test_research_cli_binds_a3_daily_argument(tmp_path, monkeypatch):
    daily = MockInputs().daily
    monkeypatch.setattr(research,'ROOT',tmp_path)
    monkeypatch.setattr(pd,'read_csv',lambda *a,**k:daily)
    from src.agent import news_contract
    monkeypatch.setattr(news_contract,'NewsSnapshots',lambda p:lambda day:news())
    monkeypatch.setattr(research,'_formal_a3',lambda data,day:pd.Series({'2330':len(data)}))
    seen=[]
    def invoke(data,**kw):
        seen.append(kw['formal_a3_loader']('2026-10-27')['2330'])
        raise RuntimeError('callback checked')
    monkeypatch.setattr(research,'run_shadow_backtest',invoke)
    import sys
    monkeypatch.setattr(sys,'argv',['research','--news-csv','synthetic.csv','--out-dir',str(tmp_path/'output/research/test')])
    with pytest.raises(RuntimeError,match='callback checked'): research.main()
    assert seen == [len(daily)]


def test_production_entry_and_backtest_share_decisions(tmp_path):
    from src.agent.run_daily import run_day
    from src.agent.reasoner import TemplateReasoner
    inputs = MockInputs()
    result = run_day('2026-10-27', mode='mock', team_id='MOCK', inputs=inputs,
                     state=PortfolioState.initial(), reasoner=TemplateReasoner(), out_dir=tmp_path)
    replay = run_formal_backtest(inputs,start='2026-10-27',days=1)
    expected = {(o.ticker,o.side,o.shares) for o in result.orders}
    got = {(r.ticker,r.side,r.shares) for r in replay.trades.itertuples()}
    assert expected == got


def test_research_archive_replays_same_strategy(tmp_path, monkeypatch):
    from src.agent.backtest import archive
    from src.agent.backtest.runner import write_result
    monkeypatch.setattr(archive,'OUTPUT_ROOT',tmp_path)
    adapter=ResearchPortfolio(25)
    first=run_formal_backtest(MockInputs(),start='2026-10-27',days=2,strategy_adapter=adapter)
    write_result(first,tmp_path/'archive',config={'test':True})
    second=run_formal_backtest(archive.ArchivedInputs(tmp_path/'archive'),strategy_adapter=adapter)
    pd.testing.assert_frame_equal(first.daily_nav,second.daily_nav)
    pd.testing.assert_frame_equal(first.trades,second.trades)


def test_research_shortfall_never_invents_turnover():
    daily = MockInputs().daily
    daily = daily.loc[daily.ticker.isin(sorted(daily.ticker.unique())[:3])]
    _, report, _ = research.run_shadow_backtest(daily,selected_count=45,holding_periods=(5,))
    rows = report.loc[report.actual_selected_count==3]
    assert rows.turnover_rate.isna().all()
    assert (rows.selection_change_rate.dropna()==0).all()
    assert (rows.selection_shortfall==42).all()


def test_lock_blocks_second_evaluation(tmp_path, monkeypatch):
    from src.agent.backtest import archive
    spec={'development':['2025-01-01','2025-02-01'],'validation':['2025-03-01','2025-04-01'],
          'test':['2026-10-27','2026-10-28'],'research_selected_count':25,
          'news_policy':'neutral_ablation','prior_exposure_disclosure':'synthetic','claims_unseen_oos':False}
    monkeypatch.setattr(validation,'StoreInputs',lambda **kwargs:MockInputs())
    monkeypatch.setattr(archive,'OUTPUT_ROOT',tmp_path)
    lock=tmp_path/'study.json'; validation.seal(spec,lock)
    result=validation.run_locked(lock,tmp_path/'first')
    assert set(result['results'])=={'production','research_equal'}
    assert not result['formal_promotion']
    with pytest.raises(FileExistsError): validation.run_locked(lock,tmp_path/'second')


def test_dividend_retry_is_atomic_and_cannot_double_accrue():
    from src.agent.backtest.corporate_actions import CorporateAction,DividendLedger
    ledger=DividendLedger()
    valid=CorporateAction('a','2330','2026-10-27','2026-10-26T12:00:00+08:00','synthetic',cash_per_share='2')
    unsupported=CorporateAction('b','2317','2026-10-27','2026-10-26T12:00:00+08:00','synthetic',kind='split')
    with pytest.raises(ValueError): ledger.accrue('2026-10-27',{'2330':1000,'2317':1000},[valid,unsupported])
    assert not ledger.entitlements
    ledger.accrue('2026-10-27',{'2330':1000},[valid])
    before=ledger.snapshot()
    with pytest.raises(ValueError): ledger.accrue('2026-10-27',{'2330':1000},[valid])
    assert ledger.snapshot()==before


def test_writer_lock_released_after_process_death(tmp_path):
    import subprocess, sys
    lock=tmp_path/'settlement.lock'
    script="from src.agent.settlement_transaction import writer_lock; from pathlib import Path; import os;\nwith writer_lock(Path("+repr(str(lock))+")): os._exit(0)"
    subprocess.run([sys.executable,'-B','-c',script],check=True)
    with tx.writer_lock(lock):
        with pytest.raises(OSError):
            with tx.writer_lock(lock):
                pass
