"""Compliance Validation：提交前的本地驗證器。

等級：
  REJECT  違反即會被拒收、記警告、或取消資格 → 不得提交，退回重跑程式
  WARN    接近限制或待確認的口徑 → 人工審核時特別留意
  INFO    提示
人只能依結果決定「過／退回」，不得手改 D-Plan 內容（六(一)、六(十四)(6)）。

委託後部位一律以 T-1 收盤價估算（與 server C12 口徑相同）；實際成交為 T 日均價，
所以現金與權重都要對硬限留緩衝。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from . import rules, strategy
from .active_share_risk import matches_current_rule, previous_session, validate_benchmarks
from .models import DecisionDraft
from .state import PortfolioState, daily_trade_fee, trade_tax
from .tools.order_generation import derive_orders
from .tools.universe import Security

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / rules.SCHEMA_RELATIVE_PATH


@dataclass
class Check:
    level: str          # REJECT / WARN / INFO
    rule: str           # 依據條號
    passed: bool
    detail: str


@dataclass
class ComplianceContext:
    state: PortfolioState                       # 主辦方前日結算（正式版須先與庫存明細對帳）
    prev_close: dict[str, float]
    universe: dict[str, Security]
    benchmarks: dict[str, dict[str, float]]     # 主動型 ETF 前 15 大
    filename: str
    live: bool                                  # 正式模式才檢查繳交時窗
    data_notes: list[str] = None                # 資料來源備援等提醒
    etf_dates: dict[str, str] | None = None
    data_date: str | None = None


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def submit_window(trade_day: date, tz) -> tuple[datetime, datetime]:
    """六(三)：交易日前一「日曆日」19:30 ～ 交易日 08:55。"""
    open_ = datetime.combine(trade_day - timedelta(days=1), rules.SUBMIT_WINDOW_OPEN, tzinfo=tz)
    close_ = datetime.combine(trade_day, rules.SUBMIT_WINDOW_CLOSE, tzinfo=tz)
    return open_, close_


def active_share(ours: dict[str, float], bench: dict[str, float]) -> float:
    """六(十三)：Active Share = 1/2 × Σ|Wi(投資組合) − Wi(Benchmark)|，取雙方前 15 大的聯集。"""
    names = set(ours) | set(bench)
    return 0.5 * sum(abs(ours.get(t, 0.0) - bench.get(t, 0.0)) for t in names)


def estimate_post_trade(holdings: dict[str, int], cash: float, orders: list[dict],
                        prices: dict[str, float]) -> tuple[dict[str, int], float, float, float, float]:
    """以 T-1 收盤價估算委託後部位（與 server C12 口徑相同）。回傳 (持股, 現金, 淨值, 買進金額, 賣出金額)。"""
    post = dict(holdings)
    buy_amt = sell_amt = 0.0
    amounts = []
    for o in orders:
        amt = o["shares"] * prices[o["ticker"]]
        amounts.append(amt)
        if o["side"] == "BUY":
            cash -= amt
            buy_amt += amt
            post[o["ticker"]] = post.get(o["ticker"], 0) + o["shares"]
        else:
            cash += amt - trade_tax(amt)
            sell_amt += amt
            post[o["ticker"]] = post.get(o["ticker"], 0) - o["shares"]
    cash -= daily_trade_fee(amounts)
    post = {t: s for t, s in post.items() if s > 0}
    nav = sum(s * prices[t] for t, s in post.items()) + cash
    return post, cash, nav, buy_amt, sell_amt


def posture_ok(intent: str, cash_range: tuple[float, float], cash_pct: float, net: float, prev_nav: float) -> bool:
    """C12：淨曝險意圖與淨流向一致，且預估現金落在宣告區間。"""
    lo, hi = cash_range
    tol = rules.HOLD_NET_FLOW_TOLERANCE * prev_nav
    intent_ok = (intent == "increase" and net > 0) or (intent == "reduce" and net < 0) \
        or (intent == "hold" and abs(net) <= tol)
    return intent_ok and lo <= cash_pct <= hi


def run_checks(plan: dict, ctx: ComplianceContext) -> list[Check]:
    checks: list[Check] = []

    def add(level, rule, passed, detail):
        checks.append(Check(level, rule, bool(passed), detail))

    # ---------- 結構 ----------
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(plan), key=lambda e: list(e.path))
    add("REJECT", f"schema v{rules.SCHEMA_VERSION}", not errors,
        "通過" if not errors else "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5]))

    for layer, limit in rules.LAYER_LIMITS.items():
        n = len(plan.get(layer, []))
        add("REJECT", f"schema {layer} 上限", n <= limit, f"{n}/{limit}")

    # ---------- ID 規則 ----------
    for layer, field, prefix in [("sources", "source_id", "S"), ("observations", "obs_id", "O"),
                                 ("inferences", "inf_id", "I"), ("decisions", "decision_id", "D")]:
        got = [x[field] for x in plan[layer]]
        want = [f"{prefix}{i}" for i in range(1, len(got) + 1)]
        add("REJECT", f"ID 連續編號 {layer}", got == want, "通過" if got == want else f"實際 {got[:8]}…")

    # ---------- C1 引用完整 ----------
    S = {x["source_id"] for x in plan["sources"]}
    O = {x["obs_id"] for x in plan["observations"]}
    I = {x["inf_id"] for x in plan["inferences"]}
    D = {x["decision_id"] for x in plan["decisions"]}
    dangling = []
    dangling += [r for o in plan["observations"] for r in o["source_ref"] if r not in S]
    dangling += [r for r in plan["market_view"]["basis_refs"] if r not in O]
    dangling += [r for i in plan["inferences"] for r in i["premise_refs"] if r not in O]
    dangling += [r for d in plan["decisions"] for r in d["inference_refs"] if r not in I]
    dangling += [r for d in plan["decisions"] for r in d.get("funding_for", []) if r not in D]
    dangling += [r for n in plan["no_trade_decisions"] for r in n["reason_refs"] if r not in I]
    dangling += [o["decision_ref"] for o in plan["orders"] if o["decision_ref"] not in D]
    add("REJECT", "C1 引用完整（無孤兒）", not dangling, "通過" if not dangling else f"不存在的引用：{dangling}")

    text_refs = re.findall(r"\b([SOID])(\d{1,3})\b", json.dumps(
        [plan["market_view"]["logic"]] + [i["logic"] for i in plan["inferences"]], ensure_ascii=False))
    bad_text = [f"{p}{n}" for p, n in text_refs if f"{p}{n}" not in (S | O | I | D)]
    add("WARN", "推論文字中的編號", not bad_text, "通過" if not bad_text else f"文字引用不存在：{bad_text}")

    # ---------- 決策覆蓋與動作一致 ----------
    holdings = {t: s for t, s in ctx.state.holdings.items() if s > 0}
    dec_t = [d["ticker"] for d in plan["decisions"]]
    nt_t = [n["ticker"] for n in plan["no_trade_decisions"]]
    dup = sorted({t for t in dec_t if dec_t.count(t) > 1} | {t for t in nt_t if nt_t.count(t) > 1}
                 | (set(dec_t) & set(nt_t)))
    missing = sorted(set(holdings) - set(dec_t) - set(nt_t))
    nt_not_held = sorted(set(nt_t) - set(holdings))
    add("REJECT", "昨日持股全數交代（擇一不重複）", not dup and not missing and not nt_not_held,
        "通過" if not (dup or missing or nt_not_held)
        else f"重複 {dup}；漏列 {missing}；未持有卻列不動 {nt_not_held}")

    bad_action = []
    for d in plan["decisions"]:
        cur = holdings.get(d["ticker"], 0)
        a = d["action"]
        if (a == "BUY" and cur != 0) or (a in ("ADD", "TRIM", "SELL_ALL") and cur == 0) \
                or (a == "SELL_ALL" and d["target_weight"] != 0):
            bad_action.append(f"{d['ticker']}:{a}(持有{cur})")
    add("REJECT", "action 與持股一致", not bad_action, "通過" if not bad_action else str(bad_action))

    # ---------- 六(七)／六(十四)(5) 150 檔清單 ----------
    all_t = set(dec_t) | set(nt_t) | {o["ticker"] for o in plan["orders"]} | set(holdings)
    outside = sorted(all_t - set(ctx.universe))
    add("REJECT", "六(七) 僅限 150 檔", not outside, "通過" if not outside else f"清單外：{outside}")
    if outside:
        return checks

    # ---------- C2 委託＝官方公式 ----------
    drafts = [DecisionDraft(key=d["decision_id"], ticker=d["ticker"], action=d["action"],
                            target_weight=d["target_weight"], inference_keys=d["inference_refs"])
              for d in plan["decisions"]]
    expected = {(o.ticker, o.side, o.shares, o.decision_key)
                for o in derive_orders(drafts, holdings, ctx.prev_close, ctx.state.nav)}
    actual = {(o["ticker"], o["side"], o["shares"], o["decision_ref"]) for o in plan["orders"]}
    add("REJECT", "C2 委託逐筆等於官方公式", expected == actual,
        "通過" if expected == actual else f"多出 {sorted(actual - expected)}；缺少 {sorted(expected - actual)}")
    no_order = sorted(d["decision_id"] for d in plan["decisions"]
                      if d["decision_id"] not in {o["decision_ref"] for o in plan["orders"]})
    add("WARN", "決策皆有對應委託", not no_order, "通過" if not no_order else f"無委託的決策：{no_order}")

    # ---------- C13 防超賣 ----------
    oversell = [f"{o['ticker']} 賣 {o['shares']} > 持有 {holdings.get(o['ticker'], 0)}"
                for o in plan["orders"] if o["side"] == "SELL" and o["shares"] > holdings.get(o["ticker"], 0)]
    add("REJECT", "C13 賣出不超過持股", not oversell, "通過" if not oversell else str(oversell))
    add("REJECT", "六(十) 整股", all(o["shares"] % rules.LOT_SIZE == 0 for o in plan["orders"]),
        "千股整數倍")

    # ---------- 委託後估算（T-1 收盤價）----------
    def estimate(orders):
        return estimate_post_trade(holdings, ctx.state.cash, orders, ctx.prev_close)

    post, cash, nav, buy_amt, sell_amt = estimate(plan["orders"])
    weights = {t: s * ctx.prev_close[t] / nav for t, s in post.items()}
    cash_pct = cash / nav

    n_hold = len(post)
    add("REJECT", "六(十四)(3) 持股 20–30 檔", rules.MIN_HOLDINGS <= n_hold <= rules.MAX_HOLDINGS,
        f"委託後 {n_hold} 檔")

    add("REJECT", "六(十四)(2) 現金 0–25%", 0 <= cash_pct <= rules.CASH_MAX, f"預估現金 {cash_pct:.2%}")
    near = cash_pct < strategy.CASH_BUFFER_WARN or cash_pct > rules.CASH_MAX - strategy.CASH_BUFFER_WARN
    add("WARN", "現金緩衝", not near, f"預估現金 {cash_pct:.2%}（成交為當日均價，會偏移）")

    bought = {o["ticker"] for o in plan["orders"] if o["side"] == "BUY"}
    over_buy, over_passive = [], []
    for t, w in weights.items():
        if w > rules.max_weight(t):
            (over_buy if t in bought else over_passive).append(f"{t} {w:.2%}")
    add("REJECT", "六(十四)(4) 加碼不得超過權重上限", not over_buy, "通過" if not over_buy else str(over_buy))
    expired = [t for t, days in ctx.state.overweight_days.items()
               if days >= rules.PASSIVE_OVERWEIGHT_GRACE_DAYS and weights.get(t, 0) > rules.max_weight(t)]
    add("REJECT", "六(十四) 註3 被動超限 5 日內減碼", not expired, "通過" if not expired else f"逾期未減碼：{expired}")
    add("WARN", "價格波動造成的被動超限", not over_passive, "無" if not over_passive else str(over_passive))

    # ---------- C12 posture 一致 ----------
    mv = plan["market_view"]
    intent = mv["posture"]["net_exposure_intent"]
    lo, hi = mv["posture"]["target_cash_pct_range"]
    net = buy_amt - sell_amt
    tol = rules.HOLD_NET_FLOW_TOLERANCE * ctx.state.nav
    ok_intent = (intent == "increase" and net > 0) or (intent == "reduce" and net < 0) \
        or (intent == "hold" and abs(net) <= tol)
    add("REJECT", "C12 淨曝險意圖與委託一致", ok_intent,
        f"宣告 {intent}，淨流向 {net:+,.0f}（hold 容差 ±{tol:,.0f}）")
    add("REJECT", "C12 預估現金落在宣告區間", lo <= hi and lo <= cash_pct <= hi,
        f"宣告 [{lo:.2%}, {hi:.2%}]，預估 {cash_pct:.2%}")

    # ---------- C14 資金調度缺口真實性 ----------
    funding = [d for d in plan["decisions"] if d.get("funding_for")]
    if funding:
        dec_by_id = {d["decision_id"]: d for d in plan["decisions"]}
        bad_target = [d["decision_id"] for d in funding
                      if d["action"] not in ("TRIM", "SELL_ALL")
                      or any(dec_by_id.get(r, {}).get("action") not in ("BUY", "ADD") for r in d["funding_for"])]
        funding_ids = {d["decision_id"] for d in funding}
        _, c2, n2, _, _ = estimate([o for o in plan["orders"] if o["decision_ref"] not in funding_ids])
        gap = c2 / n2 < lo
        add("REJECT", "C14 funding_for 指向與缺口", not bad_target and gap,
            f"指向錯誤 {bad_target}；拿掉調度賣出後現金 {c2 / n2:.2%}（須 < 下限 {lo:.2%}）")

    # ---------- 六(十三) Active Share ----------
    if ctx.live:
        try:
            freshness = validate_benchmarks(ctx.benchmarks, ctx.etf_dates or {},
                                            ctx.data_date or previous_session(plan['trade_date']))
            add("REJECT", "Active Share 官方清單與資料完整性", True, "官方 30 檔皆齊全且日期有效")
            add("WARN", "Active Share 逐檔資料時點", not freshness,
                "逐檔資料日與檢查日一致" if not freshness else "；".join(freshness))
        except ValueError as exc:
            add("REJECT", "Active Share 官方清單與資料完整性", False, str(exc))
    top_holdings = dict(sorted(weights.items(), key=lambda kv: -kv[1])[:rules.ACTIVE_SHARE_TOP_N])
    scores = {code: active_share(top_holdings, b) for code, b in ctx.benchmarks.items()}
    if scores:
        worst_code = min(scores, key=scores.get)
        worst = scores[worst_code]
        # 預估值不是官方裁定；單日 WARN 並不表示官方豁免單日門檻。
        add("WARN", f"六(十三) Active Share ≥ {rules.ACTIVE_SHARE_MIN:.0%}", worst >= rules.ACTIVE_SHARE_MIN,
            f"最低 {worst:.2%}（vs {worst_code}）" + ("" if worst >= rules.ACTIVE_SHARE_MIN else
              "——❗ 預估低於官方門檻（非單日豁免）；連續兩日低於門檻，主辦方有權取消資格"))
        add("WARN", "Active Share 安全邊際", worst >= strategy.ACTIVE_SHARE_WARN,
            f"最低 {worst:.2%}，預警線 {strategy.ACTIVE_SHARE_WARN:.0%}")
    else:
        add("REJECT", f"六(十三) Active Share ≥ {rules.ACTIVE_SHARE_MIN:.0%}", False, "缺少主動型 ETF 前 15 大持股資料")

    if ctx.live:
        prior_day = previous_session(plan['trade_date'])
        prior = next((r for r in ctx.state.active_share_history
                      if r['date'] == prior_day and matches_current_rule(r)), None)
        add("WARN", "Active Share 前日收盤觀測", prior is not None or not holdings,
            f"本地觀測 {prior_day}（非官方裁定）" if prior else
            "尚無前一交易日的新制觀測（舊制紀錄不接續），不能判定連續低於門檻")
        risk = [code for code, score in scores.items() if score < rules.ACTIVE_SHARE_MIN
                and prior and prior['etfs'].get(code, {}).get('below_minimum')]
        add("REJECT", "Active Share 連續低於門檻風險（內部保護）", not risk,
            f"前日收盤估計及今日委託預估均低於門檻：{risk}；請自動重新規劃，不得手改交易；非官方違規裁定"
            if risk else "未發現前日觀測與今日預估連續低於門檻；非收盤合規保證")

    # ---------- C6 時間 ----------
    meta = plan["agent_metadata"]
    done = _ts(meta["run_completed_at"])
    stamps = [s[k] for s in plan["sources"] for k in ("content_as_of", "fetched_at", "published_at") if k in s]
    late = [s for s in stamps if _ts(s) > done]
    add("REJECT", "C6 資料時點 ≤ 執行完成", not late and _ts(meta["run_started_at"]) <= done,
        "通過" if not late else f"晚於完成時間：{late[:3]}")
    if ctx.live:
        trade_day = datetime.fromisoformat(plan["trade_date"]).date()
        open_, close_ = submit_window(trade_day, done.tzinfo)
        add("REJECT", "六(三) 繳交時窗", open_ <= done <= close_,
            f"完成 {done:%m/%d %H:%M}，時窗 {open_:%m/%d %H:%M}～{close_:%m/%d %H:%M}")

    # ---------- 檔名 ----------
    want = f"D-Plan_{plan['team_id']}_{plan['trade_date']}.json"
    add("REJECT", "檔名規約", ctx.filename == want and re.fullmatch(r"[A-Za-z0-9_\-.]+", want) is not None,
        ctx.filename)

    notes = ctx.data_notes or []
    add("WARN", "資料來源", not notes, "正常" if not notes else "；".join(notes))

    if "_NOTE" in plan and "MOCK" in plan["_NOTE"]:
        add("INFO", "模擬資料", True, "此檔使用模擬資料與模板推論，不可提交")
    return checks


def summarize(checks: list[Check]) -> tuple[bool, str]:
    ok = all(c.passed for c in checks if c.level == "REJECT")
    lines = []
    for c in checks:
        mark = "通過" if c.passed else {"REJECT": "不通過", "WARN": "注意", "INFO": "提示"}[c.level]
        lines.append(f"  [{mark:^4}] {c.level:<6} {c.rule}：{c.detail}")
    head = "結論：可送審（無 REJECT）" if ok else "結論：退回重跑（有 REJECT 項目，不得提交）"
    return ok, head + "\n" + "\n".join(lines)


def to_json(checks: list[Check]) -> list[dict]:
    return [c.__dict__ for c in checks]
