"""Order Generation Tool：由目標權重機械換算委託，純程式、不經 LLM。

官方公式（D-Plan 撰寫指南 ⑥、schema C2）：
  目標股數 = floor(target_weight × 前日淨值 ÷ 前日收盤價 ÷ 1000) × 1000
  委託股數 = 目標股數 − 目前持有股數   （正 = BUY，負 = SELL）
用 Decimal 計算，避免浮點誤差讓 108.0 變成 107.999… 而少算一張。
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from .. import rules
from ..models import DecisionDraft, OrderDraft, TargetPosition


def target_shares(target_weight: float, prev_nav: float, prev_close: float) -> int:
    lots = (Decimal(str(target_weight)) * Decimal(str(prev_nav))
            / Decimal(str(prev_close)) / Decimal(rules.LOT_SIZE))
    return int(lots.to_integral_value(rounding=ROUND_FLOOR)) * rules.LOT_SIZE


def derive_orders(
    decisions: list[DecisionDraft],
    holdings: dict[str, int],
    prev_close: dict[str, float],
    prev_nav: float,
) -> list[OrderDraft]:
    """C2 的參考實作：compliance 也用這支重算，確認委託逐筆相符。"""
    orders = []
    for d in decisions:
        current = holdings.get(d.ticker, 0)
        target = target_shares(d.target_weight, prev_nav, prev_close[d.ticker])
        delta = target - current
        if delta == 0:
            continue
        orders.append(OrderDraft(
            ticker=d.ticker,
            side="BUY" if delta > 0 else "SELL",
            shares=abs(delta),
            decision_key=d.key,
            derivation_detail=(
                f"target_shares = floor({d.target_weight}×{prev_nav:,.0f} / {prev_close[d.ticker]} / 1000)"
                f"×1000 = {target:,}；現持 {current:,} → {'BUY' if delta > 0 else 'SELL'} {abs(delta):,}"
            ),
        ))
    return orders


def split_decisions(
    targets: list[TargetPosition],
    holdings: dict[str, int],
    prev_close: dict[str, float],
    prev_nav: float,
) -> tuple[list[TargetPosition], list[TargetPosition]]:
    """依公式算出的股數差決定哪些要交易、哪些不動。

    動作由股數差反推（BUY/ADD/TRIM/SELL_ALL），保證決策方向與委託方向一致；
    股數差為 0 的部位歸入不動，未持有且算出 0 股的標的直接略過。
    """
    trades, holds = [], []
    for tp in targets:
        current = holdings.get(tp.ticker, 0)
        target = target_shares(tp.target_weight, prev_nav, prev_close[tp.ticker])
        if target == current:
            if current > 0:
                holds.append(tp)
            continue
        trades.append(tp)
    return trades, holds


def action_for(ticker: str, target_weight: float, holdings: dict[str, int],
               prev_close: dict[str, float], prev_nav: float) -> str:
    current = holdings.get(ticker, 0)
    target = target_shares(target_weight, prev_nav, prev_close[ticker])
    if current == 0:
        return "BUY"
    if target == 0:
        return "SELL_ALL"
    return "ADD" if target > current else "TRIM"
