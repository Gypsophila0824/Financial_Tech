"""值班審核摘要：每次產生 D-Plan 時一併輸出 review_<team_id>_<日期>.md。

值班人只能依此決定「過／退回重跑」，不得修改 D-Plan（六(一)）。
"""

from __future__ import annotations

from pathlib import Path

from . import rules
from .compliance import Check, active_share, estimate_post_trade
from .state import PortfolioState
from .tools.universe import Security

ACTION_ZH = {"BUY": "新建倉", "ADD": "加碼", "TRIM": "減碼", "SELL_ALL": "出清"}
REGIME_ZH = {"risk_on": "風險偏好", "neutral": "中性", "risk_off": "風險趨避"}
INTENT_ZH = {"increase": "淨買超", "hold": "大致持平", "reduce": "淨賣超"}


def write_review(plan: dict, checks: list[Check], state: PortfolioState, prices: dict[str, float],
                 universe: dict[str, Security], benchmarks: dict[str, dict[str, float]], out_dir: Path,
                 extra: list[str] | None = None) -> Path:
    return _write_review(plan, checks, state, prices, universe, benchmarks, out_dir, extra or [])


def _write_review(plan: dict, checks: list[Check], state: PortfolioState, prices: dict[str, float],
                 universe: dict[str, Security], benchmarks: dict[str, dict[str, float]], out_dir: Path,
                 extra: list[str]) -> Path:
    holdings = {t: s for t, s in state.holdings.items() if s > 0}
    post, cash, nav, buy_amt, sell_amt = estimate_post_trade(holdings, state.cash, plan["orders"], prices)
    w_before = {t: s * prices[t] / state.nav for t, s in holdings.items()}
    w_after = {t: s * prices[t] / nav for t, s in post.items()}
    rejects = [c for c in checks if c.level == "REJECT" and not c.passed]
    warns = [c for c in checks if c.level == "WARN" and not c.passed]
    mv = plan["market_view"]
    lo, hi = mv["posture"]["target_cash_pct_range"]

    lines = [f"# 值班審核｜{plan['trade_date']}｜{plan['team_id']}", ""]
    if rejects:
        lines += ["## ❌ 不得提交：請退回重跑程式（不得手改 D-Plan）", ""]
        lines += [f"- **{c.rule}**：{c.detail}" for c in rejects]
    else:
        lines += ["## ✅ 合規檢查通過，可送審", ""]
    if warns:
        lines += ["", "**注意項目**："] + [f"- {c.rule}：{c.detail}" for c in warns]
    if "_NOTE" in plan:
        lines += ["", f"> ⚠️ {plan['_NOTE']}"]
    if extra:
        lines += ["", "**程式自動調整與提醒**："] + [f"- {e}" for e in extra]

    lines += ["", "## 市場觀點", "",
              f"- 研判：{REGIME_ZH[mv['regime']]}；姿態：{INTENT_ZH[mv['posture']['net_exposure_intent']]}；"
              f"宣告現金區間 {lo:.1%}～{hi:.1%}",
              f"- 邏輯：{mv['logic']}",
              f"- 反方訊號：{mv['counter_evidence'] or '無'}",
              "", "## 部位變化（以前日收盤價估算）", "",
              f"| 項目 | 交易前 | 交易後（預估） |", "|---|---|---|",
              f"| 持股檔數 | {len(holdings)} | {len(post)}（規則 {rules.MIN_HOLDINGS}～{rules.MAX_HOLDINGS}） |",
              f"| 現金比例 | {state.cash / state.nav:.2%} | {cash / nav:.2%}（規則 < {rules.CASH_MAX:.0%}） |",
              f"| 買進金額 | | {buy_amt:,.0f} |", f"| 賣出金額 | | {sell_amt:,.0f} |"]

    dec = {d["decision_id"]: d for d in plan["decisions"]}
    lines += ["", f"## 今日委託（{len(plan['orders'])} 筆）", ""]
    if plan["orders"]:
        lines += ["| 代號 | 名稱 | 動作 | 方向 | 股數 | 估計金額 | 權重 前→後 |", "|---|---|---|---|---|---|---|"]
        for o in plan["orders"]:
            t = o["ticker"]
            lines.append(f"| {t} | {universe[t].name} | {ACTION_ZH[dec[o['decision_ref']]['action']]} | {o['side']} | "
                         f"{o['shares']:,} | {o['shares'] * prices[t]:,.0f} | "
                         f"{w_before.get(t, 0):.2%} → {w_after.get(t, 0):.2%} |")
    else:
        lines.append("無委託。")

    top_holdings = dict(sorted(w_after.items(), key=lambda kv: -kv[1])[:rules.ACTIVE_SHARE_TOP_N])
    scores = sorted((active_share(top_holdings, b), code) for code, b in benchmarks.items())
    lines += ["", f"## 前 {rules.ACTIVE_SHARE_TOP_N} 大持股（交易後預估）", "",
              "、".join(f"{t} {universe[t].name} {w:.1%}" for t, w in top_holdings.items()),
              "", f"## Active Share 最低 3 檔（規則下限 {rules.ACTIVE_SHARE_MIN:.0%}）", ""]
    lines += [f"- {code}：{s:.1%}" for s, code in scores[:3]] or ["- 無基準資料"]
    lines += ["", "---", "審核原則：只能決定「繳交」或「退回重跑」，不得修改 D-Plan 任何內容。"]

    path = out_dir / f"review_{plan['team_id']}_{plan['trade_date']}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
