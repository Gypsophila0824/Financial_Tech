import json

import pandas as pd
import pytest

from src.agent.backtest.compare_report import render, summarize_pair


def account(nav=(110.,121.,108.9)):
    return pd.DataFrame({'date':['2026-08-31','2026-09-01','2026-09-02'],
                         'prior_nav':[100.,nav[0],nav[1]],'nav':nav})


def test_months_use_actual_opening_equity_and_reconcile_profit():
    totals, months=summarize_pair(account(),account((105.,115.5,127.05)))
    assert totals['baseline']['profit']==pytest.approx(8.9)
    assert months.baseline_profit.sum()==pytest.approx(totals['baseline']['profit'])
    assert months.iloc[1].baseline_return==pytest.approx(-0.01)
    # September starts at August's ending equity, not a new 100.
    assert months.iloc[1].experiment_return==pytest.approx(0.21)


def test_misaligned_or_broken_accounts_are_not_compared():
    other=account().iloc[1:]
    with pytest.raises(ValueError,match='日期不一致'):
        summarize_pair(account(),other)
    broken=account();broken.loc[1,'prior_nav']=100
    with pytest.raises(ValueError,match='不連續'):
        summarize_pair(account(),broken)


def test_failed_backtest_never_invents_monthly_profits(tmp_path,monkeypatch):
    from src.agent.backtest import archive
    monkeypatch.setattr(archive,'OUTPUT_ROOT',tmp_path)
    path=tmp_path/'comparison.json'
    path.write_text(json.dumps({'spec':{'start':'2026-09-01','end':'2026-10-13'},
                                'fair_comparison_complete':False,
                                'results':{'baseline':{'status':'failed','error':'missing dividend'}}}),encoding='utf-8')
    out=tmp_path/'report'
    report=render(path,out)
    assert 'missing dividend' in report.read_text(encoding='utf-8')
    assert not (out/'monthly_comparison.csv').exists()
    assert json.loads((out/'summary.json').read_text(encoding='utf-8'))['status']=='incomplete'
    with pytest.raises(FileExistsError):
        render(path,out)
