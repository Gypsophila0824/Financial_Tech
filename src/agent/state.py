"""持倉帳本（Portfolio State Tool）。

正式比賽時，持股與淨值以主辦方結算為準：每天繳交前要先核對後台「庫存明細」，
帳本不一致時以主辦方為準並找出原因（賣超會導致整份 D-Plan 被拒收，C13）。
settle() 依官方規則模擬結算，只用於模擬測試與自家帳本推估：
  六(四) 當日決策全數成交；六(五) 成交價＝當日成交均價；
  六(十一) 手續費 0.1425%、證交稅 0.3% 從現金扣除；
  五、評分方式 NAV = Σ(持股張數 × 1000 × 當日收盤價) + 現金。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import rules
from .models import OrderDraft


@dataclass
class PortfolioState:
    as_of: str | None                       # 最近一次結算的交易日；None 表示尚未開始交易
    nav: float                              # 該日結算 NAV（下單公式的「前日淨值」）
    cash: float
    holdings: dict[str, int] = field(default_factory=dict)          # 股數
    overweight_days: dict[str, int] = field(default_factory=dict)   # 因價格波動連續超限的天數
    last_close: dict[str, float] = field(default_factory=dict)      # 持股最後結算收盤價（缺當日價時估權重用，不用於下單）
    active_share_history: list[dict] = field(default_factory=list)  # 本地收盤觀測，非官方裁定

    @classmethod
    def initial(cls) -> PortfolioState:
        return cls(as_of=None, nav=float(rules.INITIAL_CAPITAL), cash=float(rules.INITIAL_CAPITAL))

    @classmethod
    def load(cls, path: Path) -> PortfolioState:
        if not path.exists():
            return cls.initial()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    def held_tickers(self) -> list[str]:
        return [t for t, s in self.holdings.items() if s > 0]


def trade_fee(amount: float) -> float:
    # 待確認：主辦方手續費是否無條件捨去至整數元、是否有最低手續費；先採捨去、無下限
    return math.floor(amount * rules.FEE_RATE)


def trade_tax(amount: float) -> float:
    return math.floor(amount * rules.TAX_RATE)


def settle(
    state: PortfolioState,
    trade_date: str,
    orders: list[OrderDraft],
    fill_prices: dict[str, float],
    close_prices: dict[str, float],
    *, benchmarks: dict | None = None, etf_dates: dict[str, str] | None = None,
) -> PortfolioState:
    invalid = sorted({o.ticker for o in orders if o.ticker not in fill_prices or
                      not math.isfinite(fill_prices[o.ticker]) or fill_prices[o.ticker] <= 0})
    if invalid:
        raise ValueError(f"缺少有效成交均價：{invalid}；禁止以收盤價替代")
    holdings = dict(state.holdings)
    cash = state.cash
    for o in orders:
        amount = o.shares * fill_prices[o.ticker]
        if o.side == "BUY":
            cash -= amount + trade_fee(amount)
            holdings[o.ticker] = holdings.get(o.ticker, 0) + o.shares
        else:
            if o.shares > holdings.get(o.ticker, 0):
                raise ValueError(f"{o.ticker} 賣出 {o.shares} 股超過持有 {holdings.get(o.ticker, 0)} 股")
            cash += amount - trade_fee(amount) - trade_tax(amount)
            holdings[o.ticker] -= o.shares
    holdings = {t: s for t, s in holdings.items() if s > 0}
    last_close = {t: close_prices.get(t, state.last_close.get(t)) for t in holdings}
    missing = [t for t, px in last_close.items() if px is None]
    if missing:
        raise ValueError(f"結算缺少收盤價：{missing}")
    nav = sum(s * last_close[t] for t, s in holdings.items()) + cash

    overweight_days = {}
    for t, s in holdings.items():
        if s * last_close[t] / nav > rules.max_weight(t):
            overweight_days[t] = state.overweight_days.get(t, 0) + 1
    history = list(state.active_share_history)
    if benchmarks is not None:
        from .active_share_risk import record_close
        history = record_close(history, trade_date, {t: s * last_close[t] / nav for t, s in holdings.items()},
                               benchmarks, etf_dates or {})
    return PortfolioState(as_of=trade_date, nav=nav, cash=cash, holdings=holdings,
                          overweight_days=overweight_days, last_close=last_close, active_share_history=history)
