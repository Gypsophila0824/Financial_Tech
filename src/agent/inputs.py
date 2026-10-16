"""每日輸入：行情、量價因子、基本面因子、主動型 ETF 前十五大、新聞分數，交給 factor_engine。

StoreInputs（真實，彩排與正式共用）
  行情      data/market/daily.csv（datafeed.market 累積；證交所／櫃買官方，上櫃備援依序 OpenAPI→MIS→FinMind）
  基本面    正式：data/fundamental/<日期>/ 最新一份原始資料（datafeed.fund_factors 抓取）
            彩排：data/history/fundamental/fund_history.csv 依「當時可用日」還原，不偷看未來
  ETF 持股  data/etf/active_etf_holdings_<日期>.csv（datafeed.etf_holdings，MoneyDJ），取資料日（含）以前最新一份
  因子表四捨五入到 4 位小數：與組員管線（經 CSV 存檔）一致，10/02 驗收時目標組合與委託逐筆相同。
MockInputs（模擬，測試用）：合成行情（含法人、參考價）、基本面與 ETF 持股，只存在記憶體。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from . import rules
from .datafeed import fund_factors, pv_factors
from .datafeed.etf_holdings import ETFS
from .datafeed.market import FALLBACK_INSTI_SOURCES, FALLBACK_QUOTE_SOURCES
from .factor_engine import etf_top_holdings
from .active_share_risk import validate_benchmarks
from .tools.trading_calendar import trading_days

ROOT = Path(__file__).resolve().parents[2]
DAILY = ROOT / "data" / "market" / "daily.csv"
FUND_DIR = ROOT / "data" / "fundamental"
FUND_HISTORY = ROOT / "data" / "history" / "fundamental" / "fund_history.csv"
ETF_DIR = ROOT / "data" / "etf"
SECURITY_MASTER = ROOT / "data" / "security_master.csv"

TWSE_QUOTES = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"
TWSE_INSTI = "https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALL&response=json"
TPEX_QUOTES = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash}&type=EW&response=json"
TPEX_INSTI = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={slash}&response=json"
FUND_SOURCES = {
    "twse_revenue": ("mops", "https://openapi.twse.com.tw/v1/opendata/t187ap05_L", "上市公司每月營業收入彙總表"),
    "tpex_revenue": ("mops", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O", "上櫃公司每月營業收入彙總表"),
    "twse_margin": ("mops", "https://openapi.twse.com.tw/v1/opendata/t187ap17_L", "上市公司營益分析彙總表"),
    "tpex_margin": ("mops", "https://www.tpex.org.tw/openapi/v1/mopsfin_187ap17_O", "上櫃公司營益分析彙總表"),
}
STATEMENT_KIND_NAME = {"ci": "一般業", "basi": "金融業", "bd": "證券期貨業", "fh": "金控業", "ins": "保險業",
                       "mim": "異業"}
HISTORY_FUND_URL = "https://mopsov.twse.com.tw/mops/web/index"


def read_csv(p) -> pd.DataFrame:
    return pd.read_csv(p, dtype={"ticker": str, "code": str}, encoding="utf-8-sig")


def load_master() -> pd.DataFrame:
    return read_csv(SECURITY_MASTER).set_index("ticker")


@dataclass
class SourceSpec:
    key: str
    authority: str
    url: str
    name: str
    content_as_of: str


@dataclass
class DayInputs:
    trade_date: str
    data_date: str
    master: pd.DataFrame
    pv: pd.DataFrame
    fund: pd.DataFrame
    news: pd.DataFrame | None
    news_items: dict[str, dict]                 # 新聞 id → 標題、網址、發布時間、authority、archive_url
    benches: dict[str, pd.Series]
    etf_date: str
    close: pd.Series                            # 資料日（含）以前最近一次有成交的收盤價
    tradable: set[str]                          # 資料日有成交者
    market_sources: dict[str, SourceSpec]       # quotes_twse / insti_twse / quotes_tpex / insti_tpex
    fund_sources: dict[str, SourceSpec]         # 月營收、營益分析（及財報）
    statement_sources: dict[tuple, SourceSpec] = field(default_factory=dict)   # (market, is/bs, kind)
    notes: list[str] = field(default_factory=list)
    etf_dates: dict[str, str] = field(default_factory=dict)
    research_ranking: pd.DataFrame | None = None  # optional strategy feature snapshot, never a live override


def _market_sources(daily_day: pd.DataFrame, data_date: str) -> dict[str, SourceSpec]:
    d = date.fromisoformat(data_date)
    ts = f"{data_date}T13:30:00+08:00"
    otc = daily_day[daily_day["market"] == "TPEX"]
    used = lambda col, known: next((s for s in otc[col].fillna("") if s in known), None) if col in otc else None  # noqa: E731
    q, i = used("src", FALLBACK_QUOTE_SOURCES), used("insti_src", FALLBACK_INSTI_SOURCES)
    spec = {
        "quotes_twse": SourceSpec("src:quotes_twse", "twse", TWSE_QUOTES.format(ymd=d.strftime("%Y%m%d")),
                                  "證交所每日收盤行情（上市）", ts),
        "insti_twse": SourceSpec("src:insti_twse", "twse", TWSE_INSTI.format(ymd=d.strftime("%Y%m%d")),
                                 "證交所三大法人買賣超日報（上市）", ts),
        "quotes_tpex": SourceSpec("src:quotes_tpex", *(FALLBACK_QUOTE_SOURCES[q] if q else
                                  ("tpex", TPEX_QUOTES.format(slash=d.strftime("%Y/%m/%d")), "櫃買中心上櫃股票每日收盤行情")), ts),
        "insti_tpex": SourceSpec("src:insti_tpex", *(FALLBACK_INSTI_SOURCES[i] if i else
                                 ("tpex", TPEX_INSTI.format(slash=d.strftime("%Y/%m/%d")), "櫃買中心三大法人買賣明細（上櫃）")), ts),
    }
    return spec


class StoreInputs:
    """真實資料來源（彩排、正式）。history=True 時基本面依歷史可用日還原（彩排）。"""

    is_mock = False

    def __init__(self, history: bool = False, strict_history: bool = False):
        self.history = history
        # 正式回測必須避免以較晚日期的 ETF 持股回填早期日期。既有 replay
        # 保持原行為（可使用最早一份資料彩排），但 strict_history=True 時寧可
        # 略過資料不足的日期，也不可引入未來資訊。
        self.strict_history = strict_history
        self.master = load_master()
        self._daily = None

    @property
    def daily(self) -> pd.DataFrame:
        if self._daily is None:
            if not DAILY.exists():
                raise FileNotFoundError("找不到 data/market/daily.csv，請先執行 python -m src.agent.datafeed.market")
            self._daily = read_csv(DAILY)
        return self._daily

    def reload(self) -> None:
        self._daily = None

    def dates(self) -> list[str]:
        return sorted(self.daily["date"].unique())

    def prev_trading_day(self, trade_date: str) -> str:
        prior = [d for d in self.dates() if d < trade_date]
        if not prior:
            raise RuntimeError(f"daily.csv 沒有 {trade_date} 之前的行情")
        return prior[-1]

    def next_trading_day(self, d: str) -> str:
        later = [x for x in self.dates() if x > d]
        if not later:
            raise RuntimeError(f"daily.csv 沒有 {d} 之後的行情")
        return later[0]

    def fill_prices(self, d: str) -> dict[str, float]:
        """只回傳有效成交均價；缺值不以收盤價冒充。呼叫端須檢查委託涵蓋率。"""
        day = self.daily[self.daily["date"] == d]
        if day["ticker"].duplicated().any():
            raise ValueError(f"{d} 行情有重複股票，無法決定成交均價")
        prices = pd.to_numeric(day["avg_price"], errors="coerce")
        return {t: float(a) for t, a in zip(day["ticker"], prices) if pd.notna(a) and math.isfinite(a) and a > 0}

    def close_prices(self, d: str) -> dict[str, float]:
        day = self.daily[(self.daily["date"] == d) & self.daily["close"].notna()]
        return dict(zip(day["ticker"], day["close"]))

    # ---- 基本面 ----
    def _fund_live(self, data_date: str):
        dirs = sorted(p for p in FUND_DIR.glob("????-??-??") if p.is_dir())
        if not dirs:
            raise FileNotFoundError("data/fundamental/ 沒有基本面原始資料，請先執行 python -m src.agent.datafeed.fund_factors")
        raw_dir = dirs[-1]
        fund = fund_factors.compute(raw_dir)
        fallback = fund.attrs.get("revenue_fallback", [])
        fund = fund.round(4)
        self._fund_notes = [f"最新月營收表未列入、沿用前期已公布月營收 {len(fallback)} 檔：{'、'.join(fallback[:5])}"
                            + ("…" if len(fallback) > 5 else "")] if fallback else []
        sources = {}
        for key, (auth, url, name) in FUND_SOURCES.items():
            raw = json.loads((raw_dir / f"{key}.json").read_text(encoding="utf-8"))
            roc = raw[0].get("出表日期") or raw[0].get("Date")
            published = date(int(roc[:-4]) + 1911, int(roc[-4:-2]), int(roc[-2:])).isoformat()
            sources[key] = SourceSpec(f"src:{key}", auth, url, f"{name}（出表日 {published}）",
                                      f"{published}T00:00:00+08:00")
        statements = {}
        for market in ("twse", "tpex"):
            for t, label in (("is", "綜合損益表"), ("bs", "資產負債表")):
                for kind in fund_factors.STATEMENT_KINDS:
                    p = raw_dir / f"{market}_{t}_{kind}.json"
                    if not p.exists():
                        continue
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    roc = (raw[0].get("出表日期") or raw[0].get("Date")) if raw else None
                    published = (date(int(roc[:-4]) + 1911, int(roc[-4:-2]), int(roc[-2:])).isoformat() if roc
                                 else raw_dir.name)
                    statements[(market, t, kind)] = SourceSpec(
                        f"src:stmt_{market}_{t}_{kind}", "mops", fund_factors.statement_url(market, t, kind),
                        f"{'上市' if market == 'twse' else '上櫃'}公司{label}（{STATEMENT_KIND_NAME[kind]}，出表日 {published}）",
                        f"{published}T00:00:00+08:00")
        return fund, sources, statements

    def _fund_history(self, data_date: str):
        """依 fund_history.csv 還原資料日當時已公布的最新一期（與組員 backtest.py 相同口徑）"""
        h = read_csv(FUND_HISTORY)
        h = h[h["available_from"] <= data_date]
        f = self.master[["name", "market", "industry"]].copy()
        last = lambda kind: h[h["kind"] == kind].sort_values("period").groupby("ticker").last()  # noqa: E731
        rev, mar, roe = last("revenue"), last("margin"), last("roe")
        f["rev_month"] = rev["period"]
        f["B1"] = rev["B1"]
        f["rev_ytd_yoy"] = rev["rev_ytd_yoy"]
        f["B3"] = f["B1"] - f["rev_ytd_yoy"]
        f["op_quarter"], f["B2"] = mar["period"], mar["B2"]
        f["roe_quarter"], f["B4"] = roe["period"], roe["B4"]
        f["roe_src"] = None
        f.loc[f["industry"] == "金融保險業", ["B1", "B3"]] = None
        for c in fund_factors.FACTORS:
            f[c] = pd.to_numeric(f[c])
            f[f"{c}_rank"] = fund_factors.pct_rank(f[c])
        f["fund_score"] = f[[f"{c}_rank" for c in fund_factors.FACTORS]].mean(axis=1)
        avail = h["available_from"].max()
        sources = {"history": SourceSpec("src:fund_history", "mops", HISTORY_FUND_URL,
                                         f"公開資訊觀測站月營收、營益分析、財報（彩排：{data_date} 當時已公布者）",
                                         f"{avail}T00:00:00+08:00")}
        return f.round(4), sources, {}

    def evaluation_benchmarks(self, trade_date: str):
        """Post-close only; never pass this result to the decision engine."""
        notes = []
        benches, _ = self._benches(trade_date, notes)
        return benches, dict(self.etf_dates)

    def fundamental_for_date(self, data_date: str) -> pd.DataFrame:
        """回傳指定資料日當時可得的基本面快照，供研究線與回測共用。"""
        if self.history:
            return self._fund_history(data_date)[0]
        return self._fund_live(data_date)[0]

    # ---- 主動型 ETF 持股 ----
    def _benches(self, data_date: str, notes: list[str]):
        files = sorted(ETF_DIR.glob("active_etf_holdings_????-??-??.csv"))
        usable = [p for p in files if p.stem[-10:] <= data_date]
        if usable:
            path = usable[-1]
        elif files and self.history and not self.strict_history:
            path = files[0]
            notes.append(f"彩排：沒有 {data_date} 以前的主動型 ETF 持股，改用最早一份 {path.stem[-10:]}")
        else:
            raise FileNotFoundError("data/etf/ 沒有主動型 ETF 持股，請先執行 python -m src.agent.datafeed.etf_holdings")
        etf_date = path.stem[-10:]
        if not self.history and etf_date < data_date:
            notes.append(f"主動型 ETF 持股資料日 {etf_date} 早於行情資料日 {data_date}")
        holdings = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig", keep_default_na=False)
        if "data_date" not in holdings or holdings.groupby("etf_id")["data_date"].nunique().ne(1).any():
            raise ValueError("ETF 快照缺少逐檔日期，或同一 ETF 混有不同日期")
        self.etf_dates = holdings.groupby("etf_id")["data_date"].first().to_dict()
        benches = etf_top_holdings(holdings)
        # 舊 replay 明確允許最早快照彩排；正式與 strict_history 不得使用未來日期。
        cutoff = max(data_date, etf_date) if self.history and not self.strict_history else data_date
        notes.extend(validate_benchmarks(benches, self.etf_dates, cutoff))
        return benches, etf_date

    def day(self, trade_date: str) -> DayInputs:
        data_date = self.prev_trading_day(trade_date)
        daily = self.daily[self.daily["date"] <= data_date]
        pv, warnings, _ = pv_factors.compute(daily, data_date)
        notes = [f"價格跳動未涵蓋（已視為 0）：{w}" for w in warnings]
        fund, fsrc, stmt = self._fund_history(data_date) if self.history else self._fund_live(data_date)
        if not self.history:
            notes += self._fund_notes
        benches, etf_date = self._benches(data_date, notes)
        known = daily[daily["close"].notna()].sort_values("date")
        close = known.groupby("ticker")["close"].last()
        tradable = set(known.loc[known["date"] == data_date, "ticker"])
        return DayInputs(trade_date, data_date, self.master, pv.round(4), fund, None, {}, benches, etf_date, close,
                         tradable, _market_sources(daily[daily["date"] == data_date], data_date), fsrc, stmt, notes,
                         self.etf_dates)


class MockInputs:
    """模擬資料：合成行情（含三大法人、參考價）、基本面與 ETF 持股（只存在記憶體，測試用）。"""

    is_mock = True

    def __init__(self, seed: int = 20261027):
        self.master = load_master()
        rng = random.Random(seed)
        self.days = trading_days("2026-05-01", "2026-12-31")
        tickers = list(self.master.index)
        mkt = [rng.gauss(0.0004, 0.01) for _ in self.days]
        rows = []
        for t in tickers:
            px, beta = math.exp(rng.gauss(math.log(150), 0.9)), rng.gauss(1.0, 0.3)
            vbase = math.exp(rng.gauss(math.log(5e6), 1.0))
            for i, d in enumerate(self.days):
                px *= math.exp(beta * mkt[i] + rng.gauss(0, 0.018))
                close = round(px, 2)
                vol = vbase * math.exp(rng.gauss(0, 0.4))
                avg = round(close * (1 + rng.gauss(0, 0.003)), 4)
                net = vol * rng.gauss(0, 0.05)
                rows.append({"date": d, "ticker": t, "market": self.master.loc[t, "market"], "close": close,
                             "volume": vol, "value": vol * avg, "avg_price": avg, "insti_net_shares": net,
                             "ref_next": None, "src": "", "insti_src": ""})
        self.daily = pd.DataFrame(rows)
        fund = self.master[["name", "market", "industry"]].copy()
        for c in fund_factors.FACTORS:
            fund[c] = [rng.gauss(10, 20) for _ in tickers]
            fund[f"{c}_rank"] = fund_factors.pct_rank(fund[c])
        fund["fund_score"] = fund[[f"{c}_rank" for c in fund_factors.FACTORS]].mean(axis=1)
        fund["rev_month"], fund["op_quarter"], fund["roe_quarter"], fund["rev_ytd_yoy"] = "2026-08", "2026Q2", "2026Q2", 5.0
        fund["roe_src"] = None
        self.fund = fund.round(4)
        self.benches = {}
        for code, _ in ETFS:
            names = rng.sample([t for t in tickers if t != "2330"], rules.ACTIVE_SHARE_TOP_N - 1)
            self.benches[code] = pd.Series({"2330": rng.uniform(0.08, 0.2),
                                            **{t: rng.uniform(0.02, 0.05) for t in names}})

    def prev_trading_day(self, trade_date: str) -> str:
        return self.days[self.days.index(trade_date) - 1]

    def dates(self) -> list[str]:
        return list(self.days)

    def next_trading_day(self, d: str) -> str:
        return self.days[self.days.index(d) + 1]

    def fill_prices(self, d: str) -> dict[str, float]:
        day = self.daily[self.daily["date"] == d]
        return dict(zip(day["ticker"], day["avg_price"]))

    def close_prices(self, d: str) -> dict[str, float]:
        day = self.daily[self.daily["date"] == d]
        return dict(zip(day["ticker"], day["close"]))

    def day(self, trade_date: str) -> DayInputs:
        data_date = self.prev_trading_day(trade_date)
        daily = self.daily[self.daily["date"] <= data_date]
        pv, _, _ = pv_factors.compute(daily, data_date)
        close = daily[daily["date"] == data_date].set_index("ticker")["close"]
        ts = f"{data_date}T13:30:00+08:00"
        msrc = {k: SourceSpec(f"src:{k}", auth, f"https://mock.invalid/{k}", f"【模擬】{k}", ts)
                for k, auth in (("quotes_twse", "twse"), ("insti_twse", "twse"), ("quotes_tpex", "tpex"),
                                ("insti_tpex", "tpex"))}
        fsrc = {"history": SourceSpec("src:fund_mock", "mops", "https://mock.invalid/mops", "【模擬】基本面", ts)}
        return DayInputs(trade_date, data_date, self.master, pv.round(4), self.fund, None, {}, self.benches,
                         data_date, close, set(close.index), msrc, fsrc)
