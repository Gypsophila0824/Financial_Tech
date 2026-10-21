import pandas as pd
import pytest

from src.agent.backtest.retest_reports import close_returns, portfolio_months


def test_ytd_and_monthly_returns_use_different_denominators():
    frame=pd.DataFrame({'date':['2026-01-30','2026-02-02','2026-02-26'],
                        'prior_nav':[100.,110.,121.], 'nav':[110.,121.,133.1]})
    months=portfolio_months(frame)
    assert months['2026-02']['monthly_return']==pytest.approx(.21)
    assert months['2026-02']['ytd_return']==pytest.approx(.331)
    assert months['2026-01']['monthly_return']==pytest.approx(.10)


def test_close_returns_calculates_daily_and_monthly_from_closes():
    prices=pd.DataFrame({
        'date':['2025-12-31','2026-01-02','2026-01-30','2026-02-02','2026-02-26'],
        'ticker':['0050']*5,
        'close':[100.,110.,121.,110.,133.1],
    })
    daily,monthly=close_returns(prices,['2026-01-02','2026-01-30','2026-02-02','2026-02-26'],['0050'])
    assert daily.set_index('date').loc['2026-01-02','daily_return']==pytest.approx(.10)
    assert daily.set_index('date').loc['2026-02-02','daily_return']==pytest.approx(110/121-1)
    month_returns=monthly.set_index('month').monthly_return
    assert month_returns['2026-01']==pytest.approx(.21)
    assert month_returns['2026-02']==pytest.approx(.10)


def test_close_returns_does_not_forward_fill_missing_dates():
    prices=pd.DataFrame({'date':['2025-12-31','2026-01-02','2026-01-05'],
                         'ticker':['0050']*3,'close':[100.,110.,121.]})
    daily,_=close_returns(prices,['2026-01-05','2026-01-06'],['0050'])
    assert daily.set_index('date').loc['2026-01-05','daily_return']==pytest.approx(.10)
    assert daily.set_index('date').loc['2026-01-06','daily_return'] is None or pd.isna(
        daily.set_index('date').loc['2026-01-06','daily_return'])
