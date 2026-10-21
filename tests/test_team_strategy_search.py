from src.agent.backtest import team_strategy_search as search
from src.agent import strategy


def test_research_inputs_refresh_holdings_before_loading(monkeypatch):
    events = []
    result = {"complete": True, "observed_dates": ["2026-10-16"]}

    def refresh():
        events.append("refresh")
        return result

    def inputs(**kwargs):
        events.append(("inputs", kwargs))
        return object()

    monkeypatch.setattr(search.etf_holdings, "refresh", refresh)
    monkeypatch.setattr(search, "StoreInputs", inputs)

    loaded, update = search.prepare_research_inputs()

    assert events == ["refresh", ("inputs", {"history": True, "strict_history": True})]
    assert update is result
    assert loaded is not None


def test_comparison_candidates_do_not_duplicate_formal_strategy():
    assert 'pv_40_fund_50' not in search.CANDIDATES
    formal = (strategy.SCORE_WEIGHTS['pv_score'], strategy.SCORE_WEIGHTS['fund_score'],
              strategy.FUND_SCORE_WEIGHTS['B1_rank'])
    assert formal not in search.CANDIDATES.values()
