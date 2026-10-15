from src.agent import rules
from src.agent.backtest.risk import (RiskState, active_share_status, add_warning,
                                     buy_overweight_reasons, invalid_reasons,
                                     passive_grace_reasons, weights)
from src.agent.state import PortfolioState


def state(*, cash=100, holdings=None, nav=1_000, overweight_days=None):
    holdings = holdings or {'2330': 1}
    return PortfolioState(as_of='2026-10-27', nav=nav, cash=cash, holdings=holdings,
                          last_close={ticker: nav - cash for ticker in holdings},
                          overweight_days=overweight_days or {})


def test_daily_invalid_conditions_exclude_passive_weight():
    bad = state(cash=-1, holdings={'2330': 1, '9999': 1}, nav=100)
    reasons = invalid_reasons(bad, {'2330'})
    assert 'cash_out_of_range' in reasons
    assert 'holdings_out_of_range' in reasons
    assert 'ineligible_holdings:9999' in reasons
    assert not any('overweight' in r for r in reasons)


def test_buy_overweight_has_no_price_grace():
    projected = state(cash=1, holdings={'2330': 1}, nav=100)
    assert buy_overweight_reasons(projected, {'2330'}) == ['buy_overweight:2330']
    assert buy_overweight_reasons(projected, set()) == []


def test_passive_overweight_warns_on_sixth_session_only():
    fifth = state(cash=1, holdings={'2330': 1}, nav=100, overweight_days={'2330': 5})
    sixth = state(cash=1, holdings={'2330': 1}, nav=100, overweight_days={'2330': 6})
    assert passive_grace_reasons(fifth) == []
    assert passive_grace_reasons(sixth) == ['passive_overweight_day_6:2330']


def test_multiple_daily_reasons_make_only_one_warning_and_third_stops():
    risk = RiskState()
    add_warning(risk, '2026-10-27', ['cash_out_of_range', 'buy_overweight:2330'])
    assert risk.warning_count == 1 and len(risk.warning_days) == 1
    add_warning(risk, '2026-10-28', ['holdings_out_of_range'])
    add_warning(risk, '2026-10-29', ['passive_overweight_day_6:2330'])
    assert risk.disqualified and risk.disqualification_reason == 'three_daily_warnings'


def test_active_share_separates_estimate_from_confirmed():
    st = state()
    st.active_share_history = [
        {'date': '2026-10-27', 'basis': 'local_close_not_official', 'notes': [],
         'etfs': {'A': {'below_minimum': True, 'estimated_breach_streak': 1, 'same_date_breach_streak': 1},
                  'B': {'below_minimum': True, 'estimated_breach_streak': 1, 'same_date_breach_streak': 1}}},
        {'date': '2026-10-28', 'basis': 'local_close_not_official', 'notes': ['stale'],
         'etfs': {'A': {'below_minimum': True, 'estimated_breach_streak': 2, 'same_date_breach_streak': 0},
                  'B': {'below_minimum': True, 'estimated_breach_streak': 2, 'same_date_breach_streak': 2}}},
    ]
    report = active_share_status(st, '2026-10-28')
    assert report['estimated_two_day_etfs'] == ['A', 'B']
    assert report['confirmed_two_day_etfs'] == ['B']


def test_weight_helper_uses_close_and_nav():
    assert weights(state(cash=50, holdings={'2330': 1}, nav=100)) == {'2330': .5}
