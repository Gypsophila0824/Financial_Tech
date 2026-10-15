"""競賽回測的收盤風控狀態機。

此模組只描述本地重播的規則結果，主辦方仍是最終裁定者。Active Share
的連續低於門檻是取消資格風險，不屬於第六(十四)當日交易無效／警告機制。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import rules
from ..active_share_risk import previous_session
from ..state import PortfolioState


@dataclass
class RiskState:
    warning_count: int = 0
    warning_days: list[dict] = field(default_factory=list)
    disqualified: bool = False
    disqualification_reason: str | None = None


def weights(state: PortfolioState) -> dict[str, float]:
    if state.nav <= 0:
        return {}
    return {ticker: shares * state.last_close[ticker] / state.nav
            for ticker, shares in state.holdings.items()}


def invalid_reasons(state: PortfolioState, allowed: set[str]) -> list[str]:
    """檢查六(十四)可導致當日交易無效的收盤狀態。

    單檔被動超限不在這裡立即判警告，由 passive_grace_reason 依第六日處理。
    """
    reasons = []
    if state.cash < 0 or state.cash / state.nav > rules.CASH_MAX:
        reasons.append("cash_out_of_range")
    n = len(state.holdings)
    if not rules.MIN_HOLDINGS <= n <= rules.MAX_HOLDINGS:
        reasons.append("holdings_out_of_range")
    illegal = sorted(set(state.holdings) - allowed)
    if illegal:
        reasons.append("ineligible_holdings:" + ",".join(illegal))
    return reasons


def buy_overweight_reasons(pre_close_state: PortfolioState, bought: set[str]) -> list[str]:
    """委託前價格已能預見的加碼超限，沒有價格波動寬限。"""
    return [f"buy_overweight:{ticker}" for ticker, weight in weights(pre_close_state).items()
            if ticker in bought and weight > rules.max_weight(ticker) + 1e-12]


def passive_grace_reasons(state: PortfolioState) -> list[str]:
    """第 6 個連續交易日仍被動超限時，當天記一次警告。"""
    return [f"passive_overweight_day_{days}:{ticker}"
            for ticker, days in state.overweight_days.items()
            if days > rules.PASSIVE_OVERWEIGHT_GRACE_DAYS
            and weights(state).get(ticker, 0) > rules.max_weight(ticker) + 1e-12]


def active_share_status(state: PortfolioState, day: str) -> dict:
    """只將基準資料同日的兩次低於門檻列為可確認連續風險。

    ETF 持股披露較舊時仍保留 estimated streak，但不能冒充官方確認。
    """
    current = next((row for row in state.active_share_history if row["date"] == day), None)
    if not current:
        return {"available": False, "confirmed_two_day_etfs": [], "estimated_two_day_etfs": []}
    prior = next((row for row in state.active_share_history
                  if row["date"] == previous_session(day)), None)
    confirmed, estimated = [], []
    for code, row in current["etfs"].items():
        if row["estimated_breach_streak"] >= 2:
            estimated.append(code)
        if (prior and row["same_date_breach_streak"] >= 2
                and prior["etfs"].get(code, {}).get("below_minimum")):
            confirmed.append(code)
    return {"available": True, "confirmed_two_day_etfs": sorted(confirmed),
            "estimated_two_day_etfs": sorted(estimated), "basis": current["basis"],
            "notes": current["notes"]}


def add_warning(risk: RiskState, day: str, reasons: list[str]) -> RiskState:
    """同一日多項違規只累計一警告；第三次即終止後續回測。"""
    if not reasons:
        return risk
    if risk.warning_days and risk.warning_days[-1]["date"] == day:
        raise ValueError("同日警告不得重複累計")
    risk.warning_count += 1
    risk.warning_days.append({"date": day, "reasons": sorted(set(reasons)),
                              "warning_number": risk.warning_count})
    if risk.warning_count >= 3:
        risk.disqualified = True
        risk.disqualification_reason = "three_daily_warnings"
    return risk
