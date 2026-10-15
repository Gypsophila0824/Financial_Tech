"""推論文字。

決策、權重、股數一律由程式（factor_engine）決定；推論文字先由程式依規則寫成範本（run_daily 產生），
TemplateReasoner 直接採用，LLMReasoner（llm_reasoner.py）改寫成分析說明、通過檢查才取代範本。

主辦方 Slack 答覆：程式自主取得資料、計算因子、決定權重與交易且無人工介入，即符合 Agent 自主決策，
不一定要呼叫 LLM。所以範本推論可用於正式模式（is_mock=False），也是 LLM 失敗時的備援。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import InferenceDraft


@dataclass
class StockItem:
    key: str                      # inference key，例如 inf:2330
    ticker: str
    name: str
    action: str                   # BUY / ADD / TRIM / SELL_ALL / NO_TRADE
    facts: dict[str, str]         # obs key → 事實原文（取自 evidence，與 D-Plan 內容一致）
    logic: str                    # 程式依規則寫的範本推論
    counter: str | None           # 程式依規則寫的反方證據


class TemplateReasoner:
    is_mock = False
    model_provider = "other"
    model_version = "rule-based-template-v1"
    input_tokens = output_tokens = calls = 0
    fallback: list[str] = []

    def stocks(self, items: list[StockItem]) -> dict[str, InferenceDraft]:
        return {it.key: InferenceDraft(key=it.key, premise_keys=list(it.facts), logic=it.logic[:1000],
                                       conclusion=None, counter_evidence=(it.counter or None) and it.counter[:500])
                for it in items}

    def market_view(self, facts: dict[str, str], logic: str, counter: str | None) -> tuple[str, str | None]:
        return logic[:1000], counter


# 舊名稱相容（模擬、彩排、測試沿用）
MockReasoner = TemplateReasoner
