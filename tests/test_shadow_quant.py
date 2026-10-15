from __future__ import annotations

import pandas as pd

from src.agent.shadow_quant import MODEL_VERSION, compute_shadow_ranking, read_news_snapshot


def _daily() -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2025-11-03", periods=61, freq="B")
    for n, ticker in enumerate(("1000", "1001", "1002")):
        for day, market_date in enumerate(dates):
            close = 100 + n * 5 + day * (1 + n * 0.5)
            rows.append({"date": market_date.date().isoformat(),
                         "ticker": ticker, "close": close, "volume": 1000 + n * 100 + day,
                         "value": close * (1000 + n * 100 + day)})
    return pd.DataFrame(rows)


def test_shadow_quant_is_ranked_and_never_exposes_trading_fields():
    result = compute_shadow_ranking(_daily(), "2026-01-26", selected_count=2)
    assert result["shadow_rank"].tolist() == [1, 2, 3]
    assert result["selected"].sum() == 2
    assert set(result["model_version"]) == {MODEL_VERSION}
    assert not {"target_weight", "orders", "decisions"} & set(result.columns)


def test_shadow_quant_uses_fundamental_as_quality_gate_and_keeps_news_fixed():
    fundamental = pd.DataFrame({"ticker": ["1000", "1001", "1002"], "B1": [-20, 5, 8],
                                "B2": [-1, 2, 3], "B4": [2, 4, 5]})
    news = pd.DataFrame({"ticker": ["1000", "1001", "1002"], "news_score": [0.5, 0.8, 0.2],
                         "worst_event": ["", "", ""]})
    result = compute_shadow_ranking(_daily(), "2026-01-26", selected_count=3,
                                    fundamental=fundamental, news=news,
                                    formal_a3=pd.Series({"1000": 0.1, "1001": 0.1, "1002": 0.1}))
    blocked = result.set_index("ticker").loc["1000"]
    assert blocked["fundamental_status"] == "blocked"
    assert not blocked["selected"]
    assert set(result["news_policy"]) == {"fixed-production-score"}
    assert "research_score" in result


def test_high_news_veto_uses_strict_formal_adjusted_a3_boundary():
    news = pd.DataFrame({"ticker": ["1000", "1001"], "news_score": [0.5, 0.5],
                         "worst_event": ["high:事件:負面", "high:事件:邊界"]})
    result = compute_shadow_ranking(_daily(), "2026-01-26", selected_count=3, news=news,
                                    formal_a3=pd.Series({"1000": -0.001, "1001": 0.0, "1002": 0.2}))
    result = result.set_index("ticker")
    assert bool(result.loc["1000", "news_veto"])
    assert not bool(result.loc["1001", "news_veto"])


def test_high_news_without_formal_a3_is_rejected_not_substituted():
    news = pd.DataFrame({"ticker": ["1000"], "news_score": [0.5], "worst_event": ["high:事件:負面"]})
    try:
        compute_shadow_ranking(_daily(), "2026-01-26", news=news)
    except ValueError as exc:
        assert "正式 A3" in str(exc)
    else:
        raise AssertionError("重大利空不得用研究均線代替正式 A3")


def test_news_snapshot_is_filtered_to_the_requested_date(tmp_path):
    path = tmp_path / "news.csv"
    pd.DataFrame({"as_of_date": ["2026-01-25", "2026-01-26"], "ticker": ["1000", "1000"],
                  "news_score": [0.1, 0.9]}).to_csv(path, index=False, encoding="utf-8-sig")
    loaded = read_news_snapshot(path, "2026-01-26", require_dated=True)
    assert loaded.iloc[0]["news_score"] == 0.9
