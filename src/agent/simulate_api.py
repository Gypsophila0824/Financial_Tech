"""多日模擬：連跑數個交易日，每天產出 D-Plan → 合規檢查 → 模擬結算（當日成交均價、手續費、證交稅）。

  模擬資料：.venv/Scripts/python.exe -X utf8 -m src.agent.simulate --days 5 --reset
  真實歷史資料彩排：.venv/Scripts/python.exe -X utf8 -m src.agent.simulate --data real --start 2026-09-15 --days 10 --reset
輸出：output/mock/ 或 output/replay/（D-Plan、合規報告、審核摘要、輸入包、帳本 state.json）。兩種都不可提交。
彩排的基本面依 fund_history.csv 還原當時已公布者；新聞無歷史資料，分數為中性。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .compliance import summarize
from .inputs import MockInputs, StoreInputs
from datetime import datetime
from .llm import AnthropicLLM
from .llm_reasoner import LLMReasoner
from .tools import news_scoring as NS
from .run_daily import run_day
from .state import PortfolioState, settle

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", choices=["mock", "real"], default="mock")
    ap.add_argument("--start", default="2026-10-27", help="初賽首個交易日為 10/27（10/26 休市、沒有 D-Plan）")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--reset", action="store_true", help="從 10 億現金重新開始")
    ap.add_argument("--verbose", action="store_true", help="列出每一項檢查")
    args = ap.parse_args()

    inputs, mode = (StoreInputs(history=True), "replay") if args.data == "real" else (MockInputs(), "mock")
    out = (
        ROOT / "output" / "replay_api"
        / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    )
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    if args.reset and state_path.exists():
        state_path.unlink()
    state = PortfolioState.load(state_path)
    reason_llm = AnthropicLLM()
    classify_llm = AnthropicLLM(
        model=NS.CLASSIFY_MODEL,
        effort=NS.CLASSIFY_EFFORT,
    )
    summary_llm = AnthropicLLM(
        model=NS.SUMMARY_MODEL,
        effort="low",
    )

    reasoner = LLMReasoner(
        reason_llm,
        cache_path=out / "narratives.jsonl",
    )

    # 使用本次模擬的快取，讓首次執行會重新呼叫 API。
    NS.LABELS = out / "news_labels.jsonl"
    NS.SUMMARIES = out / "news_summaries.jsonl"
    NS.ARCHIVE = out / "news_archive.jsonl"

    news = NS.NewsScorer(
        classify_llm=classify_llm,
        summary_llm=summary_llm,
        live=False,
    )

    day = args.start if state.as_of is None else inputs.next_trading_day(state.as_of)
    for i in range(args.days):
        reasoner.trade_date = day
        r = run_day(
            day,
            mode=mode,
            team_id=mode.upper(),
            inputs=inputs,
            state=state,
            reasoner=reasoner,
            out_dir=out,
            news=news,
        )

        print(
            "API 累計呼叫："
            f"推論 {reason_llm.calls} 次、"
            f"新聞分類 {classify_llm.calls} 次、"
            f"新聞摘要 {summary_llm.calls} 次"
        )

        # API 失敗時停止，避免誤把退回範本的結果當成 API 模擬。
        problems = reasoner.warnings + news.warnings
        if problems:
            raise RuntimeError("\n".join(problems))
        ok, text = summarize(r.checks)
        p, tp = r.plan, r.trade_plan
        print(f"\n===== {day}（資料日 {inputs.prev_trading_day(day)}）｜{p['market_view']['regime']} / "
              f"{tp.intent}｜決策 {len(p['decisions'])}、不動 {len(p['no_trade_decisions'])}、委託 {len(p['orders'])}"
              f"｜估算現金 {tp.cash_pct:.2%}｜Active Share 最低 {tp.as_report.iloc[0]['active_share']:.1%}")
        if args.verbose or not ok:
            print(text)
        else:
            warns = [c for c in r.checks if not c.passed and c.level == "WARN"]
            print(text.splitlines()[0] + (f"；注意 {len(warns)} 項：" + "、".join(c.rule for c in warns) if warns else ""))
        if not ok:
            print(f"\n{day} 未通過合規檢查，停止模擬（正式流程：退回重跑程式，不得手改）。")
            return
        state = settle(state, day, r.orders, inputs.fill_prices(day), inputs.close_prices(day))
        state.save(state_path)
        print(f"  模擬結算：NAV {state.nav:,.0f}，現金 {state.cash / state.nav:.2%}，持股 {len(state.held_tickers())} 檔")
        if i + 1 < args.days:
            try:
                day = inputs.next_trading_day(day)
            except (RuntimeError, IndexError) as exc:
                print(f"\n停止：{exc}")
                return


if __name__ == "__main__":
    main()
