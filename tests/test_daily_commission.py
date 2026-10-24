from decimal import Decimal

import pytest

from src.agent.compliance import estimate_post_trade
from src.agent.factor_engine import estimate_cash
from src.agent.models import OrderDraft
from src.agent.state import (PortfolioState, allocate_daily_fees, daily_trade_fee,
                             settle)


def test_split_orders_do_not_change_daily_commission():
    assert daily_trade_fee([1001000, 1001000]) == 2852
    assert daily_trade_fee([1001000, 1001000, 1001000]) == 4279
    assert daily_trade_fee([3003000]) == 4279
    assert daily_trade_fee([]) == 0
    # Decimal summation must not lose a yuan at an exact fee boundary.
    assert daily_trade_fee([Decimal('400000'), Decimal('400000')]) == 1140
    assert sum(allocate_daily_fees([1001000] * 3)) == 4279


def test_mixed_buy_sell_aggregates_gross_not_net_and_matches_all_cash_paths():
    prices = {'2330': 1001, '2317': 1001, '2454': 1001}
    raw = [dict(ticker='2330', side='BUY', shares=1000),
           dict(ticker='2317', side='BUY', shares=1000),
           dict(ticker='2454', side='SELL', shares=1000)]
    orders = [OrderDraft(**o, decision_key='D1') for o in raw]
    state = PortfolioState('2026-10-21', 11001000, 10000000,
                           {'2454': 1000}, last_close={'2454': 1001})
    result = settle(state, '2026-10-22', orders, prices, prices)
    expected_cash = 10000000 - 1001000 - 4279 - 3003
    assert result.cash == expected_cash
    assert result.holdings == {'2330': 1000, '2317': 1000}
    assert estimate_post_trade(state.holdings, state.cash, raw, prices)[1] == expected_cash
    decisions = [{**o, 'price': prices[o['ticker']]} for o in raw]
    assert estimate_cash(Decimal(state.cash), decisions)[2] == expected_cash


def test_backtest_fee_columns_and_cash_reconcile_to_daily_aggregate():
    from src.agent.backtest.runner import run_formal_backtest
    from src.agent.inputs import MockInputs
    result = run_formal_backtest(MockInputs(seed=42), start='2026-10-27', days=3)
    cash = 1e9
    for day in result.daily_nav.itertuples():
        trades = result.trades[result.trades.date == day.date]
        assert trades.fee.sum() == daily_trade_fee(trades.amount)
        assert day.cost == trades.fee.sum() + trades.tax.sum()
        buy = trades.loc[trades.side == 'BUY', 'amount'].sum()
        sell = trades.loc[trades.side == 'SELL', 'amount'].sum()
        cash += sell - buy - day.cost
        assert day.cash == pytest.approx(cash, abs=1e-5)
    assert result.summary['fee_basis'] == 'daily_gross_buy_plus_sell_not_net'
