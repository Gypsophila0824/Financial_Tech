"""正式模式入口。只產生 D-Plan 與合規報告，繳交由值班人手動完成（人只能決定過／退回，不得手改）。

  1. 產生當日 D-Plan（繳交時窗：前一日曆日 19:30 ～ 交易日 08:55；T-1 收盤與三大法人資料公布後執行）：
     .venv/Scripts/python.exe -X utf8 -m src.agent.live run --trade-date 2026-10-27 --team-id TEAM_11161
     依序：交易日與時窗檢查 → 抓資料（行情＋法人、主動型 ETF 持股、基本面、新聞）→ 確認 T-1 收盤資料已到 →
     帳本核對（ledger_check）→ 印出帳本請值班人與後台「庫存明細」核對 → 加 --reconciled 才會產生 D-Plan。
     --skip-fetch：資料已抓過，只重算與產出。
  2. 當日收盤後結算帳本（主辦方 19:00 更新淨值後再與後台核對）：
     .venv/Scripts/python.exe -X utf8 -m src.agent.live settle --trade-date 2026-10-27 --team-id TEAM_11161

每次執行封存於 runs/；提交後須 confirm-submission 留存平台回執。
結算採最後成功提交時間的封存版本，而非最後產生的本地檔案；不得人工挑選交易內容。
除息現金與配股不在比賽期間計入（簡章六(十二) 註1、Slack），帳本也不加。
執行紀錄寫入 logs/live_<時間>.log。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ledger_check
from . import submissions
from .compliance import submit_window, summarize
from .inputs import StoreInputs
from .llm import AnthropicLLM, LLMError, load_dotenv
from .llm_reasoner import LLMReasoner
from .models import OrderDraft
from .reasoner import TemplateReasoner
from .run_daily import run_day
from .state import PortfolioState, settle
from .tools import news_scoring
from .tools.trading_calendar import is_trading_day

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "data" / "state" / "ledger.json"
OUT = ROOT / "output" / "live"
LOG_DIR = ROOT / "logs"
TPE = timezone(timedelta(hours=8))

FETCH = [  # (說明, 模組, 失敗是否中止)
    ("抓取價量與三大法人", "src.agent.datafeed.market", True),
    ("抓取主動型 ETF 持股", "src.agent.datafeed.etf_holdings", True),
    ("抓取基本面（月營收、營益分析、財報）", "src.agent.datafeed.fund_factors", True),
    ("抓取新聞與重大訊息", "src.agent.datafeed.news", False),     # 抓不到只警告：新聞分數視為中性
]


class Log:
    def __init__(self, name: str):
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / f"{name}_{datetime.now(TPE):%Y%m%d_%H%M%S}.log"
        self.f = open(self.path, "w", encoding="utf-8")

    def __call__(self, msg: str = "") -> None:
        print(msg)
        self.f.write(msg + "\n")
        self.f.flush()

    def stop(self, msg: str, code: int = 1) -> int:
        self(f"\n❌ {msg}\n執行紀錄：{self.path.relative_to(ROOT)}")
        return code


def run_module(module: str, say) -> bool:
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-u", "-m", module], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    for line in proc.stdout:
        say("    " + line.rstrip())
    return proc.wait() == 0


def fetch(say) -> bool:
    for title, module, required in FETCH:
        say(f"\n[抓取] {title}：python -m {module}")
        if not run_module(module, say):
            if required:
                return False
            say(f"  ⚠️ {title}失敗，繼續執行（新聞分數以已累積資料計算，缺者視為中性）")
    return True


def print_ledger(state: PortfolioState, say) -> None:
    say(f"帳本（最近結算日 {state.as_of or '尚未交易'}）：NAV {state.nav:,.0f}，現金 {state.cash:,.0f}"
        f"（{state.cash / state.nav:.2%}），持股 {len(state.held_tickers())} 檔")
    for t, s in sorted(state.holdings.items()):
        say(f"  {t}  {s:>12,} 股")


def make_llms(say):
    """回傳 (推論改寫 reasoner, 新聞分類 LLM, 新聞摘要 LLM)。沒有金鑰時用範本與中性新聞（Slack：規則式亦合規）。"""
    load_dotenv()
    if os.environ.get("LLM_PROVIDER", "anthropic") == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        say("⚠️ 未設定 ANTHROPIC_API_KEY：推論使用程式範本、新聞不經 LLM 判讀（視為中性）")
        return TemplateReasoner(), None, None
    try:
        return (LLMReasoner(AnthropicLLM()),
                AnthropicLLM(model=news_scoring.CLASSIFY_MODEL, effort=news_scoring.CLASSIFY_EFFORT),
                AnthropicLLM(model=news_scoring.SUMMARY_MODEL, effort="low"))
    except LLMError as exc:
        say(f"⚠️ LLM 設定錯誤（{exc}）：推論使用程式範本、新聞視為中性")
        return TemplateReasoner(), None, None


def cmd_run(args) -> int:
    say = Log("live_run")
    from .official_policy import require_formal_release
    try:
        require_formal_release()
    except (OSError, ValueError) as exc:
        return say.stop(str(exc))
    T = args.trade_date
    say(f"===== 正式執行 {T}（{datetime.now(TPE):%Y-%m-%d %H:%M:%S}）=====")
    if not is_trading_day(T):
        return say.stop(f"{T} 證交所休市，沒有 D-Plan（Slack 答覆）。")
    now = datetime.now(TPE)
    open_, close_ = submit_window(datetime.fromisoformat(T).date(), TPE)
    if not open_ <= now <= close_:
        return say.stop(f"現在 {now:%m/%d %H:%M} 不在 {T} 的繳交時窗 {open_:%m/%d %H:%M}～{close_:%m/%d %H:%M}（簡章六(三)）。"
                        "時窗外產生的 D-Plan 會被合規檢查退回。")

    if not args.skip_fetch and not fetch(say):
        return say.stop("資料抓取失敗，已停止（不產出不完整的 D-Plan）。請查看上方錯誤訊息，稍後重跑。")

    inputs = StoreInputs(history=False)
    expected = max(d for d in inputs.dates() if d < T) if inputs.dates() else None
    prev = T
    while True:                                   # 日曆上的前一個交易日
        prev = (datetime.fromisoformat(prev) - timedelta(days=1)).date().isoformat()
        if is_trading_day(prev):
            break
    if expected != prev:
        return say.stop(f"行情資料最新為 {expected}，尚未包含前一交易日 {prev} 的收盤資料。"
                        "請於收盤資料與三大法人公布後（建議 19:30 以後）重新執行。")

    # ---- 帳本核對 ----
    state = PortfolioState.load(LEDGER)
    say("\n[帳本]")
    print_ledger(state, say)
    if LEDGER.exists():
        errors, warnings, notes = ledger_check.check(LEDGER, OUT, args.team_id, inputs.daily)
        ok_confirmed = ledger_check.confirmed(LEDGER, state.as_of, args.confirm_ledger, errors)
        for n in notes:
            say(f"  ✅ {n}")
        for w in warnings:
            say(f"  ⚠️  {w}")
        for e in errors:
            say(f"  {'☑️ 已確認' if ok_confirmed else '❌'} {e}")
        if errors and not ok_confirmed:
            return say.stop("帳本核對未通過：請對照競技場後台「庫存明細」修正 data/state/ledger.json 後重跑；"
                            "若確認後台數字無誤（例如委託未成交），加上 --confirm-ledger。")
    if state.held_tickers() and state.as_of != prev:
        return say.stop(f"帳本結算日 {state.as_of} ≠ 前一交易日 {prev}：請先執行 live settle 或依後台庫存明細更新帳本。")
    if not args.reconciled:
        return say.stop("請先與後台「庫存明細」核對上列帳本（持股、現金、前日淨值），一致後加上 --reconciled 重新執行。", 1)

    # ---- 產出 D-Plan ----
    reasoner, classify_llm, summary_llm = make_llms(say)
    if isinstance(reasoner, LLMReasoner):
        reasoner.trade_date = T
    news = news_scoring.NewsScorer(classify_llm, summary_llm, live=True)
    say("\n[產出 D-Plan]")
    run_dir = OUT / "runs" / uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    result = run_day(T, mode="live", team_id=args.team_id, inputs=inputs, state=state, reasoner=reasoner,
                     out_dir=run_dir, news=news)
    ok, text = summarize(result.checks)
    submissions.seal_run(run_dir, args.team_id, T, ok)
    say(text)
    say(f"\nD-Plan：{result.path.relative_to(ROOT)}")
    say(f"審核摘要：{(run_dir / f'review_{args.team_id}_{T}.md').relative_to(ROOT)}")
    say(f"執行紀錄：{say.path.relative_to(ROOT)}")
    if ok:
        say(f"可送審：於 {close_:%m/%d %H:%M} 前手動繳交；成功後執行 confirm-submission 登錄本次 run-dir 與平台回執。")
        return 0
    return say.stop("不得提交：請退回重跑程式（不得手改 D-Plan）。", 2)


def cmd_confirm_submission(args) -> int:
    say = Log("live_submission")
    if not args.confirmed:
        return say.stop("請核對平台已成功接受這份 D-Plan，再加 --confirmed；此操作不會提交訂單。")
    if LEDGER.exists() and (PortfolioState.load(LEDGER).as_of or "") >= args.trade_date:
        return say.stop("帳本已結算此日或更晚日期，不可事後替換提交依據。")
    try:
        path = submissions.confirm_submission(OUT, args.run_dir, args.team_id, args.trade_date,
                                               args.receipt_file, args.submitted_at)
    except (ValueError, OSError, KeyError) as exc:
        return say.stop(str(exc))
    say(f"已登錄人工核實的平台回執：{path}；尚未結算，也未自動驗證平台狀態。")
    return 0


def cmd_settle(args) -> int:
    say = Log("live_settle")
    T = args.trade_date
    invalidated = getattr(args, "invalidated", False)
    if invalidated and args.no_trades:
        return say.stop("--invalidated 與 --no-trades 不可同時使用。")
    if not is_trading_day(T):
        return say.stop(f"{T} 證交所休市，沒有交易可結算。")
    state = PortfolioState.load(LEDGER)
    if state.as_of and state.as_of >= T:
        return say.stop("此日已結算或早於目前帳本，禁止重複／倒序結算。")
    prev = datetime.fromisoformat(T).date() - timedelta(days=1)
    while not is_trading_day(prev.isoformat()):
        prev -= timedelta(days=1)
    if state.as_of and state.as_of != prev.isoformat():
        return say.stop("帳本不是前一交易日，請先依序補齊結算。")
    try:
        official_raw = args.official_state.read_bytes()
        official = json.loads(official_raw)
        receipt_raw = args.receipt_file.read_bytes()
        if not receipt_raw:
            raise ValueError("請提供非空的平台當日結算／無交易證據")
        if args.no_trades:
            if list(submissions.day_dir(OUT, args.team_id, T).glob("*/submission.json")):
                raise ValueError("已有成功提交紀錄，不能逕行指定無交易；請查明官方是否判定交易無效")
            if not args.reason:
                raise ValueError("無交易結算須以 --reason 記錄未提交／拒收原因")
            before, plan, submission_path = asdict(state), {"orders": []}, None
        else:
            submission_path, _ = submissions.latest_submission(OUT, args.team_id, T)
            before = json.loads((submission_path / f"inputs_{args.team_id}_{T}.json").read_text(encoding="utf-8"))["state"]
            plan = json.loads((submission_path / f"D-Plan_{args.team_id}_{T}.json").read_text(encoding="utf-8"))
            if asdict(state) != asdict(PortfolioState(**before)):
                raise ValueError("帳本與成功提交版本的執行前帳本不同（包含現金與 NAV），請先對帳")
        submitted_orders = list(plan["orders"])
        if invalidated:
            # trade_status 是本地證據轉錄欄位，不假定平台匯出格式。
            if official.get("trade_status") != "invalidated" or not (args.reason or "").strip():
                raise ValueError("交易無效結算須提供 trade_status=invalidated、官方證據及非空 --reason")
            plan = {**plan, "orders": []}
        elif official.get("trade_status") == "invalidated":
            raise ValueError("官方證據標示交易無效，須使用 --invalidated 並保留成功提交紀錄")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return say.stop(f"結算依據未確認：{exc}")
    if not args.skip_fetch:
        say("[抓取] 當日行情（成交均價、收盤價）")
        if not run_module(FETCH[0][1], say):
            return say.stop("行情抓取失敗，稍後重跑。")
    orders = [OrderDraft(o["ticker"], o["side"], o["shares"], o["decision_ref"]) for o in plan["orders"]]
    inputs = StoreInputs(history=False)
    try:
        fill, close = inputs.fill_prices(T), inputs.close_prices(T)
    except ValueError as exc:
        return say.stop(f"行情無法結算：{exc}")
    missing = sorted({o.ticker for o in orders} - set(fill))
    if not close or missing:
        return say.stop(f"行情尚未包含 {T} 的成交均價（缺 {missing or '全部'}）：請於收盤資料公布後重跑；"
                        "若個股當日無成交，依後台庫存明細更新帳本。")
    closing_holdings = dict(state.holdings)
    for order in orders:
        closing_holdings[order.ticker] = closing_holdings.get(order.ticker, 0) + (order.shares if order.side == "BUY" else -order.shares)
    missing_close = [t for t, n in closing_holdings.items() if n > 0 and
                     (t not in close or not math.isfinite(close[t]) or close[t] <= 0)]
    if missing_close:
        return say.stop(f"缺少有效當日收盤價：{missing_close}；不得沿用舊價冒充當日估值。")
    if not args.skip_fetch and not run_module("src.agent.datafeed.etf_holdings", say):
        return say.stop("ETF 收盤資料抓取失敗，尚未更新帳本；請補齊後重跑。")
    try:
        etf_notes = []
        benches, _ = inputs._benches(T, etf_notes)
        new_state = settle(state, T, orders, fill, close,
                           benchmarks={k: v.to_dict() for k, v in benches.items()}, etf_dates=inputs.etf_dates)
        submissions.reconcile(new_state, official, args.team_id, T)
    except ValueError as exc:
        return say.stop(f"收盤結算／官方對帳未通過，尚未更新帳本：{exc}")
    except FileNotFoundError as exc:
        return say.stop(f"缺少 ETF 資料，尚未更新帳本：{exc}")
    for note in etf_notes:
        say(f"⚠️ {note}")
    observed = new_state.active_share_history[-1]
    for code, row in observed['etfs'].items():
        if row['below_minimum']:
            say(f"❗ {code} 收盤本地估計 {row['active_share']:.2%}，"
                f"連續估計低於門檻 {row['estimated_breach_streak']} 日；非官方裁定，須核對最新公告並優先處理。")
    say("Active Share 已逐檔留存本地收盤觀測；舊資料估計與同日資料計數分開，不能代替主辦方判定。")
    history = LEDGER.parent / "history"
    history.mkdir(parents=True, exist_ok=True)
    audit_path = history / f"settlement_{T}.json"
    if audit_path.exists():
        return say.stop("此日已有結算稽核，請先確認先前是否已完成，禁止覆寫。")
    audit = {
        "team_id": args.team_id, "trade_date": T, "before": before, "orders": plan["orders"],
        "submission_dir": str(submission_path) if submission_path else None,
        "submitted_orders": submitted_orders,
        "settlement_status": "invalidated" if invalidated else ("no_submission" if args.no_trades else "executed"),
        "invalidation_reason": args.reason if invalidated else None,
        "no_trades_reason": args.reason if args.no_trades else None,
        "official_sha256": submissions.digest(official_raw), "receipt_sha256": submissions.digest(receipt_raw),
        "ledger_digest": submissions.state_digest(new_state),
    }
    from .settlement_transaction import commit, encoded
    try:
        commit(LEDGER, T, args.team_id, asdict(state), asdict(new_state), {
            f'official_{T}.json': official_raw, f'receipt_{T}.bin': receipt_raw,
            f'settlement_{T}.json': encoded(audit), f'ledger_before_{T}.json': encoded(asdict(state))})
    except (OSError, ValueError) as exc:
        return say.stop(f'結算提交中斷：{exc}；保留 journal，請使用 recover')
    print_ledger(new_state, say)
    say("\n請與後台當日結算（19:00 更新）核對；不一致時以主辦方為準修正 data/state/ledger.json，"
        "下次 live run 會自動核對股數。")
    return 0


def cmd_recover(args) -> int:
    from .settlement_transaction import recover
    say = Log('live_recover')
    try:
        recover(LEDGER, args.trade_date, args.team_id)
    except (OSError, ValueError, KeyError) as exc:
        return say.stop(f'恢復未完成：{exc}')
    say('已由封存 journal 恢復；未重新成交或扣費。')
    return 0


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")       # cp950 終端機無法顯示 ✅ 等符號
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "settle", "confirm-submission", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--trade-date", required=True)
        p.add_argument("--team-id", required=True)
        p.add_argument("--skip-fetch", action="store_true", help="資料已抓過，略過網路抓取")
        if name == "run":
            p.add_argument("--reconciled", action="store_true", help="已與後台庫存明細核對帳本")
            p.add_argument("--confirm-ledger", action="store_true", help="股數差異已對照後台確認無誤")
        elif name == "confirm-submission":
            p.add_argument("--run-dir", type=Path, required=True)
            p.add_argument("--receipt-file", type=Path, required=True)
            p.add_argument("--submitted-at", required=True, help="平台成功提交時間，含 +08:00")
            p.add_argument("--confirmed", action="store_true", help="已核對回執對應本次執行檔")
        elif name == 'settle':
            p.add_argument("--official-state", type=Path, required=True, help="官方 team_id/as_of/holdings/cash/nav JSON")
            p.add_argument("--receipt-file", type=Path, required=True, help="平台當日結算／無交易證據")
            status = p.add_mutually_exclusive_group()
            status.add_argument("--no-trades", action="store_true", help="已確認當日未提交／拒收，僅更新估值")
            status.add_argument("--invalidated", action="store_true", help="已核對官方判定當日交易無效；保留提交紀錄，僅更新估值")
            p.add_argument("--reason", help="無交易原因；不代表合規豁免")
    args = ap.parse_args()
    if args.team_id in ("MOCK", "REPLAY"):
        sys.exit("正式模式請使用主辦方配發的 team_id")
    sys.exit({"run": cmd_run, "settle": cmd_settle, "confirm-submission": cmd_confirm_submission,
              'recover': cmd_recover}[args.cmd](args))


if __name__ == "__main__":
    main()
