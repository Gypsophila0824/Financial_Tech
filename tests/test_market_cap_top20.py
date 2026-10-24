import pandas as pd
import pytest

from src.agent.datafeed import market_cap_top20
from src.agent.datafeed.market_cap_top20 import FIELDS, save_snapshot, score_for_date
from src.agent.factor_engine import build_targets
from src.agent import strategy


def test_top20_scores_only_competition_names_and_does_not_look_ahead(tmp_path):
    path = tmp_path / "market_cap.csv"
    rows = [{"date": "2026-10-22", "ticker": f"{i:04}", "name": f"n{i}", "rank": i,
             "market_cap_100m": 1000 - i, "yield_year": ""} for i in range(1, 21)]
    pd.DataFrame(rows, columns=FIELDS).to_csv(path, index=False)
    names = pd.Index(["0001", "0002", "0003", "0004", "0020", "9999"])

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(market_cap_top20, "fetch_snapshot", lambda: pytest.fail("同日快照存在時不應重抓 API"))
    current_live, used_live = score_for_date("2026-10-22", names, history=False, path=path)
    assert used_live == "2026-10-22"
    assert current_live.loc["0020"] == 0.55
    assert current_live.loc["9999"] == 0.5  # not in the overall top 20: neutral, not filtered out
    monkeypatch.undo()

    historical, used = score_for_date("2026-10-21", names, history=True, path=path)
    assert used is None
    assert historical.tolist() == [0.5] * 6

    current, used = score_for_date("2026-10-22", names, history=True, path=path)
    assert used == "2026-10-22"
    assert current.tolist() == [2.0, 1.5, 1.0, 0.80, 0.55, 0.5]
    assert [market_cap_top20.tier_score(rank) for rank in (1, 2, 3, 4, 5, 6, 10, 11, 20)] == [
        2.0, 1.5, 1.0, 0.80, 0.80, 0.65, 0.65, 0.55, 0.55,
    ]


def test_save_snapshot_records_source_and_row_count(tmp_path):
    path = tmp_path / "market_cap.csv"
    data = pd.DataFrame([{"date": "2026-10-22", "ticker": "2330", "name": "台積電", "rank": 1,
                          "market_cap_100m": 618487.02, "yield_year": "0.92 / 114"}], columns=FIELDS)
    save_snapshot(data, path)
    manifest = pd.read_json(path.with_name("market_cap_manifest.json"), typ="series")
    assert manifest["source_url"].endswith("/homeApi/ranking")
    assert manifest["snapshots"]["2026-10-22"]["row_count"] == 1
    assert market_cap_top20.snapshot_metadata_for_date("2026-10-22", path)["estimated"] is False


def test_estimated_snapshot_ranks_market_caps_and_keeps_top20_only():
    closes = {f"{i:04}": 100.0 for i in range(1, 22)}
    shares = {f"{i:04}": (22 - i) * 10_000_000 for i in range(1, 22)}
    estimated = market_cap_top20.estimate_snapshot("2026-10-21", closes, shares)
    assert len(estimated) == 20
    assert estimated.iloc[0]["ticker"] == "0001"
    assert estimated.iloc[-1]["ticker"] == "0020"
    assert estimated.iloc[0]["market_cap_100m"] == 210.0
    assert "0021" not in set(estimated["ticker"])


def test_load_issued_share_snapshot_subtracts_private_placements(tmp_path):
    path = tmp_path / "shares.json"
    path.write_text('[{"出表日期":"1151012","公司代號":"2408","公司簡稱":"南亞科",'
                    '"已發行普通股數或TDR原股發行股數":"3450205894","私募股數":"351578000"}]',
                    encoding="utf-8")
    shares, names, date = market_cap_top20.load_issued_share_snapshot(path)
    assert date == "2026-10-12"
    assert shares["2408"] == 3_098_627_894
    assert names["2408"] == "南亞科"


def test_estimated_snapshot_metadata_is_preserved(tmp_path):
    path = tmp_path / "market_cap.csv"
    data = pd.DataFrame([{"date": "2026-10-21", "ticker": "2330", "name": "台積電", "rank": 1,
                          "market_cap_100m": 600000, "yield_year": ""}], columns=FIELDS)
    save_snapshot(data, path, metadata={"estimated": True, "shares_source_date": "2026-10-12"})
    meta = market_cap_top20.snapshot_metadata_for_date("2026-10-21", path)
    assert meta["estimated"] is True
    assert meta["shares_source_date"] == "2026-10-12"


def test_market_cap_factor_changes_formal_ranking_without_changing_other_factors(mock_inputs):
    day = mock_inputs.day("2026-10-27")
    tickers = list(day.master.index)
    top = tickers[0]
    cap = pd.Series(0.5, index=day.master.index)
    cap.loc[top] = 1.0
    scored = build_targets(day.master, day.pv, day.fund, None, [], day.benches,
                           market_cap_score=cap, enforce_active_share=False).scored
    weights = strategy.SCORE_WEIGHTS
    base = sum(scored[column] * weight for column, weight in weights.items())
    assert scored["total_score"].equals(base)
    assert scored.loc[top, "market_cap_score"] == 1.0
    assert strategy.SCORE_WEIGHTS == {
        "pv_score": 0.50, "fund_score": 0.35, "news_score": 0.10,
        "market_cap_score": 0.05,
    }
