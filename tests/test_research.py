from __future__ import annotations

import pandas as pd

from src.agent.research import (compare_rankings, paired_evaluable_report, run_shadow_backtest,
                                selected_difference_report, summarize_backtest)


def _daily() -> pd.DataFrame:
    dates = pd.date_range("2025-09-01", periods=130, freq="B")
    rows = []
    for n, ticker in enumerate(("1000", "1001", "1002")):
        for i, market_date in enumerate(dates):
            close = 100 + n * 5 + i * (1 + n * 0.3)
            volume = 1000 + n * 100 + i
            rows.append({"date": market_date.date().isoformat(), "ticker": ticker, "close": close,
                         "volume": volume, "value": close * volume})
    return pd.DataFrame(rows)


def test_research_backtest_uses_future_returns_only_for_evaluation():
    def fundamentals(as_of_date: str) -> pd.DataFrame:
        return pd.DataFrame({"ticker": ["1000", "1001", "1002"], "B1": [3, 4, 5], "B2": [2, 2, 2], "B4": [5, 5, 5]})

    audit, backtest, skipped = run_shadow_backtest(
        _daily(), selected_count=2, holding_periods=(5, 20), fundamental_loader=fundamentals,
    )
    assert not audit.empty and not backtest.empty
    assert set(backtest["holding_period_days"]) == {5, 20}
    assert set(backtest["variant"]) == {"baseline", "experiment"}
    assert backtest["turnover_rate"].dropna().between(0, 1).all()
    mature = backtest["future_date"].notna()
    assert (backtest.loc[mature, "future_date"] > backtest.loc[mature, "as_of_date"]).all()
    summary = summarize_backtest(backtest, skipped)
    assert summary["records"] == len(backtest)
    assert {"baseline_5d", "experiment_20d"} <= set(summary["by_holding_period"])
    assert summary["coverage_disclosure"]["integrity_status_counts"]["not_mature"] > 0
    assert set(backtest["news_policy"].dropna()) == {"fixed-neutral"}


def test_model_comparison_reports_selected_overlap():
    audit, _, _ = run_shadow_backtest(_daily(), selected_count=2, holding_periods=(5,))
    first_available = audit.loc[audit["ranking_status"] == "available", "as_of_date"].iloc[0]
    one_day = audit[audit["as_of_date"] == first_available]
    production = one_day[["as_of_date", "ticker", "selected"]].copy()
    report = compare_rankings(one_day, production, selected_count=2)
    assert report.iloc[0]["overlap_count"] == 2
    assert report.iloc[0]["jaccard_overlap"] == 1.0


def test_backtest_retains_future_missing_and_does_not_reaverage_selected_portfolio():
    daily = _daily()
    # 將某檔在一個未來評估點移除，選股仍要保留、紀錄缺值，不能補另一檔。
    target = str(daily["date"].iloc[-6])
    daily = daily.loc[~((daily["date"] == target) & (daily["ticker"] == "1002"))]
    audit, report, _ = run_shadow_backtest(daily, selected_count=3, holding_periods=(5,))
    row = report.loc[(report["future_date"] == target) & (report["variant"] == "experiment")].iloc[0]
    assert row["integrity_status"] == "partial_coverage"
    assert row["actual_selected_count"] == 3 and row["evaluable_selected_count"] == 2
    assert pd.isna(row["selected_return"])
    assert "1002" in row["missing_tickers"]
    assert not selected_difference_report(audit).empty
    assert not paired_evaluable_report(report).empty


def test_news_requires_formal_a3_and_uses_no_future_information():
    news = lambda _: pd.DataFrame({"ticker": ["1000"], "news_score": [0.2], "worst_event": ["high:事件:測試"]})
    try:
        _, report, unavailable = run_shadow_backtest(_daily(), selected_count=2, holding_periods=(5,), news_loader=news)
        assert unavailable and (report["integrity_status"] == "ranking_unavailable").any()
    except Exception as exc:  # 不應由缺 A3 洩漏成其他例外
        raise AssertionError(str(exc)) from exc
    formal_a3 = lambda _: pd.Series({"1000": -0.01, "1001": 0.01, "1002": 0.01})
    audit, _, unavailable = run_shadow_backtest(_daily(), selected_count=2, holding_periods=(5,), news_loader=news,
                                                 formal_a3_loader=formal_a3)
    # 最初 60 日資料不足仍應保留為 unavailable；在可排名日，A3 < 0 的重大利空必須否決。
    assert unavailable
    available = audit.loc[audit["ranking_status"] == "available"]
    assert not available.loc[available["ticker"] == "1000", "selected"].any()
