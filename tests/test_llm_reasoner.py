"""LLM 改寫推論：通過檢查才採用，否則逐項改用範本；LLM 無法使用時全用範本、不中斷；結果快取可重現。"""

from __future__ import annotations

import json

import pytest

from src.agent.llm import FakeLLM, LLMUnavailable
from src.agent.llm_reasoner import ITEMS_SCHEMA, LLMReasoner, numbers_ok
from src.agent.reasoner import StockItem
from src.agent.run_daily import run_day

ITEM = StockItem(key="inf:2330", ticker="2330", name="台積電", action="ADD",
                 facts={"obs:stk:2330": "2330 台積電：收盤 1,250.00；近 20 日還原漲跌 +8.4%；2026-08 營收年增 +33.5%"},
                 logic="2330 台積電 量價分數 0.81、基本面分數 0.77，總分排名第 3，目標權重 18.00%。",
                 counter="趨勢因子排名落在後 30%")


def echo(system, user, schema):
    """把程式範本加上動作關鍵詞原樣交回（合格的改寫）"""
    if schema is ITEMS_SCHEMA:
        items = json.loads(user.split("\n", 1)[1])
        return {"items": [{"ticker": x["ticker"], "logic": f"決定{x['action']}。{x['program_logic']}",
                           "counter_evidence": x["program_counter_evidence"]} for x in items]}
    return {"logic": "市場說明：" + json.loads(user.split("\n", 1)[1])["program_logic"]}


def make(handler, tmp_path):
    return LLMReasoner(FakeLLM(handler), trade_date="2026-10-27", cache_path=tmp_path / "cache.jsonl")


def test_good_rewrite_used(tmp_path):
    r = make(echo, tmp_path)
    inf = r.stocks([ITEM])["inf:2330"]
    assert inf.logic.startswith("決定加碼") and not r.fallback and r.model_provider == "anthropic"
    assert inf.premise_keys == ["obs:stk:2330"]


@pytest.mark.parametrize("logic,why", [
    ("加碼台積電，因為本益比只有 15 倍、近 20 日上漲 8.4%。", "未使用的因子"),
    ("加碼台積電，近 20 日上漲 9.9%，營收年增 33.5%。", "沒有的數字"),
    ("台積電近 20 日上漲 8.4%，營收年增 33.5%，量價與基本面皆佳。", "動作"),
])
def test_bad_rewrite_falls_back(tmp_path, logic, why):
    r = make(lambda s, u, sch: {"items": [{"ticker": "2330", "logic": logic, "counter_evidence": "趨勢較弱"}]},
             tmp_path)
    inf = r.stocks([ITEM])["inf:2330"]
    assert inf.logic == ITEM.logic and any(why in f for f in r.fallback)
    assert r.model_provider == "other"           # 一段都沒採用 → 如實標示為規則式


def test_forbidden_word_allowed_only_when_in_facts(tmp_path):
    """事實（新聞）提到 EPS 時可以轉述；事實沒有的本益比仍不可自行引入"""
    item = StockItem(key="inf:1303", ticker="1303", name="南亞", action="NO_TRADE",
                     facts={"obs:stk:1303": "1303 南亞：收盤 80.00；近 20 日還原漲跌 +5.0%",
                            "obs:news:1303": "新聞：南亞第 3 季 EPS 4.9 元，創同期新高"},
                     logic="1303 南亞 總分排名第 10，今日不交易：權重偏離小於 2%。", counter=None)
    ok = make(lambda s, u, sch: {"items": [{"ticker": "1303", "logic": "維持南亞持股，第 3 季 EPS 4.9 元創同期新高。",
                                            "counter_evidence": ""}]}, tmp_path)
    ok.stocks([item])
    assert not ok.fallback
    bad = make(lambda s, u, sch: {"items": [{"ticker": "1303", "logic": "維持南亞持股，本益比偏低、EPS 4.9 元。",
                                             "counter_evidence": ""}]}, tmp_path / "b")
    bad.stocks([item])
    assert any("本益比" in f for f in bad.fallback)


def test_counter_evidence_required(tmp_path):
    r = make(lambda s, u, sch: {"items": [{"ticker": "2330", "logic": "加碼台積電，近 20 日上漲 8.4%。",
                                           "counter_evidence": ""}]}, tmp_path)
    r.stocks([ITEM])
    assert any("counter_evidence 不可為空" in f for f in r.fallback)


def test_unavailable_uses_template_without_retry(tmp_path):
    def down(s, u, sch):
        raise LLMUnavailable("找不到 API 金鑰")
    r = make(down, tmp_path)
    items = [ITEM] + [StockItem(f"inf:{t}", t, "x", "BUY", {}, "買進理由說明文字足夠長度。", None) for t in "123456789"]
    out = r.stocks(items)
    assert all(out[i.key].logic == i.logic for i in items) and r.llm.calls == 1
    assert r.market_view({}, "市場說明", None) == ("市場說明", None) and r.llm.calls == 1


def test_cache_reused(tmp_path):
    make(echo, tmp_path).stocks([ITEM])
    again = make(lambda *a: pytest.fail("不應再呼叫 LLM"), tmp_path)
    assert again.stocks([ITEM])["inf:2330"].logic.startswith("決定加碼")


def test_numbers_rounding_allowed():
    assert numbers_ok("約 8%、33.5%", "+8.43% 33.52%") == []
    assert numbers_ok("約 9%", "+8.43%") == ["9"]


def test_pipeline_with_llm_passes(mock_inputs, tmp_path):
    from src.agent.state import PortfolioState
    r = run_day("2026-10-27", mode="mock", team_id="MOCK", inputs=mock_inputs, state=PortfolioState.initial(),
                reasoner=make(echo, tmp_path), out_dir=tmp_path)
    assert not [c for c in r.checks if c.level == "REJECT" and not c.passed]
    assert r.plan["agent_metadata"]["model_version"] == "rule-based+fake-llm"
    assert all(i["logic"].startswith("決定") for i in r.plan["inferences"])
