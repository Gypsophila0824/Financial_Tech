"""競賽回測專用股利權益帳；不修改正式 PortfolioState 或可交易資產。

明細須來自已核實公告，不可由除權息參考價差推算。未知公司事件採 fail-closed。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("股利／股數必須是有限非負數") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("股利／股數必須是有限非負數")
    return result


@dataclass(frozen=True)
class CorporateAction:
    event_id: str
    ticker: str
    ex_date: str
    published_at: str
    source: str
    kind: str = "dividend"
    cash_per_share: str = "0"
    stock_per_share: str = "0"  # 新配股數／原一股，並非面額元數
    currency: str = "TWD"

    def __post_init__(self):
        date.fromisoformat(self.ex_date)
        ts = datetime.fromisoformat(self.published_at)
        if ts.tzinfo is None or not self.event_id or not self.ticker or not self.source:
            raise ValueError("事件必須有唯一 ID、股票、來源及含時區的公告時間")
        if self.currency != "TWD":
            raise ValueError("僅支援已核實的 TWD 每股股利，不自動換匯")
        cash, stock = number(self.cash_per_share), number(self.stock_per_share)
        if self.kind == "dividend" and cash == stock == 0:
            raise ValueError("股利事件至少須有現金股利或配股比例")


def load_actions(path: Path) -> tuple[list[CorporateAction], dict]:
    """保留匯入原文與雜湊；完整性仍需公告資料核對，空清單不代表無事件。"""
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("公司事件檔案須為 JSON array")
    actions = [CorporateAction(**row) for row in payload]
    validate_actions(actions)
    return actions, {"source_path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                     "original_text": raw.decode("utf-8-sig")}


def validate_actions(actions: list[CorporateAction]) -> None:
    ids = [a.event_id for a in actions]
    keys = [(a.ticker, a.ex_date, a.kind) for a in actions]
    if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
        raise ValueError("重複事件 ID 或同股票同日同類事件；請合併核實後再匯入")


@dataclass
class DividendLedger:
    entitlements: list[dict] = field(default_factory=list)
    processed_dates: set[str] = field(default_factory=set)
    terminal: dict | None = None

    def accrue(self, day: str, prior_holdings: dict[str, int], actions: list[CorporateAction],
               known_exrights: set[tuple[str, str]] | None = None) -> None:
        """除權息日按前日實際持股記權益；除息當日新買進者沒有權益。"""
        if self.terminal is not None or day in self.processed_dates:
            raise ValueError("已結算期末或重複處理除權息日")
        validate_actions(actions)
        todays = [a for a in actions if a.ex_date == day]
        covered = {a.ticker for a in todays}
        exposed = {t for t, n in prior_holdings.items() if n > 0}
        # 配股權益仍存在時，後續分割／再配股等處理須官方確認，不自行複利。
        deferred = {r['ticker'] for r in self.entitlements if number(r['stock_shares']) > 0}
        missing = sorted(t for d, t in (known_exrights or set())
                         if d == day and t in exposed | deferred and t not in covered)
        if missing:
            raise ValueError(f"{day} 已知除權息缺少股利明細：{missing}；不可由參考價差推算")
        rows = []
        for action in todays:
            shares = prior_holdings.get(action.ticker, 0)
            if not shares and action.ticker not in deferred:
                continue
            if action.kind != "dividend":
                raise ValueError(f"{action.event_id}：{action.kind} 尚無核實的股數／下單調整規則，停止回測")
            if action.ticker in deferred:
                raise ValueError(f"{action.ticker} 已有待配股權益又遇公司事件，需確認權益基數")
            cutoff = datetime.fromisoformat(f"{day}T08:55:00+08:00")
            if datetime.fromisoformat(action.published_at) > cutoff:
                raise ValueError(f"{action.event_id} 公告晚於事件日盤前，不能回填當時已知資料")
            if not isinstance(shares, int) or shares < 0:
                raise ValueError("權益基數必須為非負整數股")
            stock = Decimal(shares) * number(action.stock_per_share)
            if stock != stock.to_integral_value():
                raise ValueError(f"{action.event_id} 配股不足一股的捨入方式尚待官方確認")
            rows.append({"event_id": action.event_id, "ticker": action.ticker, "ex_date": day,
                         "published_at": action.published_at, "source": action.source,
                         "eligible_shares": shares,
                         "cash_amount": str(Decimal(shares) * number(action.cash_per_share)),
                         "stock_shares": str(stock), "status": "deferred_to_terminal"})
        # 全日驗證成功才寫入，錯誤不留下部分已入帳事件。
        self.entitlements.extend(rows)
        self.processed_dates.add(day)

    @property
    def cash(self) -> Decimal:
        return sum((number(r['cash_amount']) for r in self.entitlements), Decimal(0))

    @property
    def stocks(self) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for row in self.entitlements:
            if number(row['stock_shares']):
                result[row['ticker']] = result.get(row['ticker'], Decimal(0)) + number(row['stock_shares'])
        return result

    def finalize(self, day: str, close_prices: dict[str, float]) -> dict:
        """只產生一次期末 NAV 調整；不將權益變成盤中可交易現金。"""
        if self.terminal is not None:
            raise ValueError("期末權益已結算，禁止重複入帳")
        if not self.processed_dates or day != max(self.processed_dates):
            raise ValueError("期末日必須是最後一個已處理交易日")
        stock_value = Decimal(0)
        prices = {}
        for ticker, shares in self.stocks.items():
            if ticker not in close_prices or number(close_prices[ticker]) <= 0:
                raise ValueError(f"期末配股估值缺少有效當日收盤價：{ticker}")
            prices[ticker] = str(close_prices[ticker])
            stock_value += shares * number(close_prices[ticker])
        self.terminal = {"date": day, "cash_dividends": str(self.cash),
                         "stock_dividend_value": str(stock_value), "close_prices": prices,
                         "nav_adjustment": str(self.cash + stock_value)}
        return dict(self.terminal)

    def snapshot(self) -> dict:
        rows = [dict(r, status='included_in_terminal_nav' if self.terminal else 'deferred_to_terminal')
                for r in self.entitlements]
        return {"policy": "contest_deferred_dividends_v1", "entitlements": rows,
                "pending_cash": str(self.cash) if self.terminal is None else "0",
                "pending_stock_shares": {t: str(n) for t, n in self.stocks.items()} if self.terminal is None else {},
                "terminal": self.terminal}
