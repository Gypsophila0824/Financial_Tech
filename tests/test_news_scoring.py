"""新聞分數：只用截止時間前發布的新聞（防偷看未來）、只計主角新聞、判讀結果快取、LLM 無法使用時中性。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from src.agent.llm import FakeLLM, LLMUnavailable
from src.agent.tools import news_scoring as ns


def item(i, ticker, published, source="cnyes"):
    return {"id": i, "source": source, "authority": "mops" if source == "mops" else "media",
            "published_at": published, "title": f"新聞{i}", "body": "內容", "url": f"https://news.example/{i}",
            "tickers": [ticker]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name in ("ITEMS", "LABELS", "SUMMARIES", "ARCHIVE"):
        monkeypatch.setattr(ns, name, tmp_path / f"{name.lower()}.jsonl")
    rows = [item("a", "2330", "2026-10-27T05:00:00+08:00"),       # 截止前
            item("b", "2317", "2026-10-27T06:00:00+08:00"),       # 截止（05:30）後：不得使用
            item("c", "2454", "2026-10-26T12:00:00+08:00")]       # 只是順帶提及
    ns.append_jsonl(ns.ITEMS, rows)
    return tmp_path


def day():
    master = pd.DataFrame({"name": ["台積電", "鴻海", "聯發科"]}, index=["2330", "2317", "2454"])
    return SimpleNamespace(master=master, trade_date="2026-10-27", notes=[])


def classify(system, user, schema):
    t = "2330" if "2330" in user else "2317" if "2317" in user else "2454"
    rel = "mention" if t == "2454" else "primary"
    return {"labels": [{"ticker": t, "relevance": rel, "sentiment": "negative", "materiality": "high",
                        "event_type": "法律監管", "reason": "測試理由"}]}


def test_cutoff_relevance_and_cache(store):
    llm = FakeLLM(classify)
    df, items = ns.NewsScorer(llm, None, live=False).signals(day())
    assert set(items) == {"a", "c"}                         # b 在截止時間之後
    assert df.loc["2330", "news_score"] == 0.0 and df.loc["2330", "worst_event"].startswith("high:")
    assert df.loc["2454", "news_score"] == 0.5               # 順帶提及不計分
    assert df.loc["2317", "news_items"] == 0
    assert llm.calls == 2
    again = FakeLLM(lambda *a: pytest.fail("已判讀的新聞不應重新呼叫 LLM"))
    scorer2 = ns.NewsScorer(again, None, live=False)
    df2, _ = scorer2.signals(day())
    assert df2.loc["2330", "news_score"] == 0.0
    assert scorer2.models_used == {ns.CLASSIFY_MODEL}        # 沿用快取也如實記錄判讀模型


def test_ticker_with_name_still_counted(store):
    """模型把代號寫成「2330 台積電」時仍要採用；結構化輸出也限定只能填指定代號"""
    def named(system, user, schema):
        out = classify(system, user, schema)
        t = out["labels"][0]["ticker"]
        assert schema["properties"]["labels"]["items"]["properties"]["ticker"]["enum"] == [t]
        out["labels"][0]["ticker"] = f"{t} 名稱"
        return out
    df, _ = ns.NewsScorer(FakeLLM(named), None, live=False).signals(day())
    assert df.loc["2330", "news_score"] == 0.0


def test_mops_counts_even_if_mention(store):
    ns.append_jsonl(ns.ITEMS, [item("m", "2454", "2026-10-27T01:00:00+08:00", source="mops")])
    df, _ = ns.NewsScorer(FakeLLM(classify), None, live=False).signals(day())
    assert df.loc["2454", "news_score"] == 0.0


def test_llm_unavailable_neutral(store):
    def down(*a):
        raise LLMUnavailable("找不到 API 金鑰")
    d = day()
    df, _ = ns.NewsScorer(FakeLLM(down), None, live=False).signals(d)
    assert (df["news_score"] == 0.5).all()
    assert any("視為中性" in n for n in d.notes)
    assert not ns.LABELS.exists()                           # 未判讀者不寫入快取，下次有金鑰時再判讀
