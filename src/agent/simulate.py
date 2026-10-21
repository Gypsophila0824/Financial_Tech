"""多日模擬：連跑數個交易日，每天產出 D-Plan → 合規檢查 → 模擬結算（當日成交均價、手續費、證交稅）。

  模擬資料：.venv/Scripts/python.exe -X utf8 -m src.agent.simulate --days 5 --reset
  真實歷史資料彩排：.venv/Scripts/python.exe -X utf8 -m src.agent.simulate --data real --start 2026-09-15 --days 10 --reset
輸出：output/validation/ 下的獨立執行目錄（D-Plan、合規報告、審核摘要、輸入包、帳本 state.json）。兩種都不可提交。
彩排的基本面依 fund_history.csv 還原當時已公布者；新聞無歷史資料，分數為中性。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .compliance import summarize
from .inputs import MockInputs, StoreInputs
from .reasoner import TemplateReasoner
from .run_daily import run_day
from .state import PortfolioState, settle
from .validation_outputs import VALIDATION_ROOT, dated_run_name, print_output_paths, update_index

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", choices=["mock", "real"], default="mock")
    ap.add_argument("--start", default="2026-10-27", help="初賽首個交易日為 10/27（10/26 休市、沒有 D-Plan）")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--reset", action="store_true", help="從 10 億現金重新開始")
    ap.add_argument("--verbose", action="store_true", help="列出每一項檢查")
    ap.add_argument('--resume', type=Path, help='接續 output/validation 下既有彩排資料夾')
    args = ap.parse_args()

    inputs, mode = (StoreInputs(history=True, strict_history=True), "replay") if args.data == "real" else (MockInputs(), "mock")
    existing_state = PortfolioState.load(args.resume / 'state.json') if args.resume else PortfolioState.initial()
    if args.resume:
        out = args.resume.resolve()
    else:
        dates = inputs.dates()
        first = args.start if existing_state.as_of is None else inputs.next_trading_day(existing_state.as_of)
        selected = [d for d in dates if d >= first][:args.days]
        last = selected[-1] if selected else first
        out = VALIDATION_ROOT / mode / dated_run_name('period', first, last)
    if args.resume and (not out.is_relative_to((VALIDATION_ROOT / mode).resolve()) or not (out / 'state.json').is_file()):
        ap.error('--resume 必須指定同模式下含 state.json 的既有彩排資料夾')
    if args.resume and args.reset:
        ap.error('--resume 與 --reset 不可同時使用；新彩排預設從初始資金開始')
    out.mkdir(parents=True, exist_ok=True)
    update_index()
    state_path = out / "state.json"
    if args.reset and state_path.exists():
        state_path.unlink()
    state = PortfolioState.load(state_path)
    reasoner = TemplateReasoner()

    day = args.start if state.as_of is None else inputs.next_trading_day(state.as_of)
    for i in range(args.days):
        r = run_day(day, mode=mode, team_id=mode.upper(), inputs=inputs, state=state, reasoner=reasoner, out_dir=out)
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
            print_output_paths('彩排', out, files=(('彩排狀態', 'state.json'),))
            raise SystemExit(1)
        state = settle_rehearsal(state, day, r.orders, inputs)
        state.save(state_path)
        print(f"  模擬結算：NAV {state.nav:,.0f}，現金 {state.cash / state.nav:.2%}，持股 {len(state.held_tickers())} 檔")
        if i + 1 < args.days:
            try:
                day = inputs.next_trading_day(day)
            except (RuntimeError, IndexError) as exc:
                print(f"\n停止：{exc}")
                print_output_paths('彩排', out, files=(('彩排狀態', 'state.json'),))
                return
    files = [('彩排狀態', 'state.json')]
    plans = sorted(out.glob('D-Plan_*.json'))
    reviews = sorted(out.glob('review_*.md'))
    if plans:
        files.append(('最近一日 D-Plan', plans[-1].relative_to(out).as_posix()))
    if reviews:
        files.append(('最近一日審核摘要', reviews[-1].relative_to(out).as_posix()))
    print_output_paths('彩排', out, files=tuple(files))


def settle_rehearsal(state, day, orders, inputs):
    """歷史收盤觀測只使用該日可得 ETF，不以合成基準冒充正式觀測。"""
    benches, dates = (inputs.evaluation_benchmarks(day) if not inputs.is_mock else (None, None))
    return settle(state, day, orders, inputs.fill_prices(day), inputs.close_prices(day),
                  benchmarks=benches, etf_dates=dates)


if __name__ == "__main__":
    main()
