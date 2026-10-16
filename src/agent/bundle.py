"""輸入包（input bundle）：每次產生 D-Plan 時保存當天全部輸入，供 12/2 資格審查黑箱重現。

外部資料事後可能變動、LLM 文字每次不同，所以重現以「同一版本程式＋同一份輸入包 →
決策與委託逐筆相同」為驗證標準（推論文字不比對）。
主辦方 Slack 答覆：入圍檢核時，投資決策、D-Plan 與訂單產生邏輯須與競賽期間實際使用之版本一致。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .governance import production_manifest
from .active_share_risk import rule_definition
from .inputs import DayInputs, SourceSpec, load_master


def _frame(df: pd.DataFrame | None):
    return None if df is None else json.loads(df.to_json(orient="index", force_ascii=False))


def save_bundle(path: Path, *, day: DayInputs, state, plan: dict) -> None:
    data = {
        "trade_date": day.trade_date,
        "data_date": day.data_date,
        "code_version": plan["agent_metadata"]["code_version"],
        "active_share_rule": rule_definition(),
        "strategy_manifest": production_manifest(),
        "pv": _frame(day.pv), "fund": _frame(day.fund), "news": _frame(day.news),
        "news_items": day.news_items,
        "benches": {k: v.to_dict() for k, v in day.benches.items()},
        "etf_date": day.etf_date,
        "etf_dates": day.etf_dates,
        "close": {t: float(p) for t, p in day.close.items()},
        "tradable": sorted(day.tradable),
        "market_sources": {k: asdict(v) for k, v in day.market_sources.items()},
        "fund_sources": {k: asdict(v) for k, v in day.fund_sources.items()},
        "statement_sources": [[list(k), asdict(v)] for k, v in day.statement_sources.items()],
        "notes": day.notes,
        "state": asdict(state),
        "expected": {"decisions": [{k: d[k] for k in ("ticker", "action", "target_weight")} for d in plan["decisions"]],
                     "no_trade": sorted(n["ticker"] for n in plan["no_trade_decisions"]),
                     "orders": [{k: o[k] for k in ("ticker", "side", "shares")} for o in plan["orders"]]},
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class BundleInputs:
    """由輸入包還原當天輸入（重現用元件，不可用於正式繳交）。"""

    is_mock = True

    def __init__(self, data: dict):
        self.data = data

    def day(self, trade_date: str) -> DayInputs:
        d = self.data
        frame = lambda x: None if x is None else pd.DataFrame.from_dict(x, orient="index")  # noqa: E731
        pv, fund, news = frame(d["pv"]), frame(d["fund"]), frame(d["news"])
        for df in (pv, fund, news):
            if df is not None:
                df.index = df.index.astype(str)
        return DayInputs(
            trade_date=d["trade_date"], data_date=d["data_date"], master=load_master(), pv=pv, fund=fund, news=news,
            news_items=d["news_items"], benches={k: pd.Series(v) for k, v in d["benches"].items()},
            etf_date=d["etf_date"], close=pd.Series(d["close"]), tradable=set(d["tradable"]),
            market_sources={k: SourceSpec(**v) for k, v in d["market_sources"].items()},
            fund_sources={k: SourceSpec(**v) for k, v in d["fund_sources"].items()},
            statement_sources={tuple(k): SourceSpec(**v) for k, v in d["statement_sources"]},
            notes=d["notes"], etf_dates=d.get("etf_dates", {}))
