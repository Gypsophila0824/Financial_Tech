"""Official TWSE monthly price history for ETF return benchmarks."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

BENCHMARKS = ("0050", "00981A", "00991A")
API = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={month}01&stockNo={ticker}&response=json"


def _months(start: str, end: str) -> list[str]:
    first, last = pd.Period(start, freq="M"), pd.Period(end, freq="M")
    return [str(p).replace("-", "") for p in pd.period_range(first, last, freq="M")]


def _parse(raw: bytes, ticker: str, month: str) -> pd.DataFrame:
    payload = json.loads(raw.decode("utf-8-sig"))
    if payload.get("stat") != "OK":
        raise ValueError(f"TWSE 回應狀態：{payload.get('stat')}")
    rows = []
    for values in payload.get("data", []):
        row = dict(zip(payload["fields"], values))
        year, mon, day = map(int, row["日期"].split("/"))
        date = f"{year + 1911:04d}-{mon:02d}-{day:02d}"
        try:
            close = float(row["收盤價"].replace(",", "").strip())
        except (AttributeError, ValueError):
            # Keep this date unavailable; never carry a prior close forward.
            continue
        if close <= 0:
            continue
        rows.append({"date": date, "ticker": ticker, "close": close})
    frame = pd.DataFrame(rows, columns=["date", "ticker", "close"])
    if frame.date.duplicated().any():
        raise ValueError(f"{ticker} {month} 日期重複")
    return frame


def fetch_prices(dates: list[str], cache_dir: Path, *, tickers: tuple[str, ...] = BENCHMARKS,
                 pause_seconds: float = 3.0):
    """Return price-only ETF history and a per-month source ledger.

    Older months are cached as raw official responses. The current month is
    refreshed on each run so subsequent reports can include the latest close.
    Individual source failures are recorded; they never become zero returns.
    """
    if not dates:
        raise ValueError("研究期間沒有行情日期")
    import requests
    import truststore
    truststore.inject_into_ssl()

    cache_dir.mkdir(parents=True, exist_ok=True)
    months = _months(min(dates), max(dates))
    current_month = datetime.now().strftime("%Y%m")
    all_prices, sources = [], []
    for ticker in tickers:
        for month in months:
            cache = cache_dir / f"{ticker}_{month}.json"
            url = API.format(month=month, ticker=ticker)
            status, error = "ok", None
            network_request = False
            try:
                if cache.exists() and month != current_month:
                    raw = cache.read_bytes()
                    origin = "cache"
                else:
                    network_request = True
                    response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                    response.raise_for_status()
                    raw = response.content
                    # Validate before replacing an existing monthly snapshot.
                    _parse(raw, ticker, month)
                    cache.write_bytes(raw)
                    origin = "twse"
                frame = _parse(raw, ticker, month)
                all_prices.append(frame)
            except Exception as exc:  # Preserve the gap in the report; do not synthesize a return.
                status, error, origin = "failed", str(exc), "unavailable"
                if cache.exists() and month == current_month:
                    try:
                        raw = cache.read_bytes()
                        frame = _parse(raw, ticker, month)
                        all_prices.append(frame)
                        status, error, origin = "stale_cache", str(exc), "cache"
                    except Exception:
                        pass
            finally:
                if network_request:
                    time.sleep(pause_seconds)
            sources.append({"ticker": ticker, "month": month, "url": url, "status": status,
                            "origin": origin, "error": error,
                            "sha256": hashlib.sha256(raw).hexdigest() if "raw" in locals() else None,
                            "cache_file": cache.name if cache.exists() else None})
            if "raw" in locals():
                del raw

    prices = (pd.concat(all_prices, ignore_index=True).drop_duplicates(["date", "ticker"])
              if all_prices else pd.DataFrame(columns=["date", "ticker", "close"]))
    if not prices.empty:
        prices = prices.sort_values(["ticker", "date"]).reset_index(drop=True)
    return prices, sources
