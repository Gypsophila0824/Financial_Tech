"""Fetch and score the TWSE's published listed-market-cap top 20 snapshot.

The TWSE page exposes its current leaderboard through a JSON endpoint, without
a historical-date parameter. Snapshots are therefore archived when fetched;
historical runs may only use a snapshot dated on or before their data date.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "market" / "twse_market_cap_top20.csv"
SHARES_FILE = ROOT / "data" / "universe_raw" / "twse_t187ap03_L.json"
URL = "https://www.twse.com.tw/rwd/zh/homeApi/ranking"
PAGE_URL = "https://accessibility.twse.com.tw/zh/listed/ranking.html"
SHARES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
FIELDS = ["date", "ticker", "name", "rank", "market_cap_100m", "yield_year"]
TOP_N = 20
NEUTRAL_SCORE = 0.5


def tier_score(rank: int) -> float:
    """User-selected cap tiers: 1, 1.5, 1, .8, .65, .55, then .5."""
    if rank == 1:
        return 2.0
    if rank == 2:
        return 1.5
    if rank == 3:
        return 1.0
    if rank <= 5:
        return 0.80
    if rank <= 10:
        return 0.65
    if rank <= TOP_N:
        return 0.55
    return NEUTRAL_SCORE


def fetch_snapshot() -> pd.DataFrame:
    request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list) or len(payload) != TOP_N:
        raise ValueError(f"TWSE 市值排行應有 {TOP_N} 筆，實際收到 {len(payload) if isinstance(payload, list) else '非列表'}")
    rows = []
    dates = set()
    for pos, item in enumerate(payload, 1):
        raw_date = str(item.get("DATE", ""))
        ticker = str(item.get("NO", "")).strip()
        try:
            cap = float(str(item.get("AMT", "")).replace(",", ""))
        except ValueError as exc:
            raise ValueError(f"TWSE 市值資料格式錯誤：{ticker}") from exc
        if len(raw_date) != 8 or not raw_date.isdigit() or not ticker or not math.isfinite(cap) or cap <= 0:
            raise ValueError(f"TWSE 市值排行第 {pos} 筆欄位無效")
        dates.add(datetime.strptime(raw_date, "%Y%m%d").date().isoformat())
        rows.append({"date": datetime.strptime(raw_date, "%Y%m%d").date().isoformat(), "ticker": ticker,
                     "name": str(item.get("NAME", "")), "rank": pos, "market_cap_100m": cap,
                     "yield_year": str(item.get("YIELD-YEAR", ""))})
    if len(dates) != 1 or len({r["ticker"] for r in rows}) != 20:
        raise ValueError("TWSE 市值排行日期不一致或股票代號重複")
    return pd.DataFrame(rows, columns=FIELDS)


def load_issued_share_snapshot(path: Path = SHARES_FILE) -> tuple[dict[str, float], dict[str, str], str]:
    """Read the latest archived TWSE share data; remove private-placement shares.

    Returns ticker -> public float approximation, ticker -> name, and the source
    statement date. TWSE's official top-20 ranking can contain ETFs; those units
    must be supplied separately when estimating historical snapshots.
    """
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("證交所上市公司股數檔為空或格式錯誤")
    raw_dates = {str(row.get("出表日期", "")).strip() for row in rows}
    if len(raw_dates) != 1:
        raise ValueError("證交所上市公司股數檔的出表日期不一致")
    raw_date = next(iter(raw_dates))
    if len(raw_date) != 7 or not raw_date.isdigit():
        raise ValueError(f"證交所上市公司股數檔日期無效：{raw_date}")
    share_date = f"{int(raw_date[:3]) + 1911:04d}-{raw_date[3:5]}-{raw_date[5:7]}"
    shares, names = {}, {}
    for row in rows:
        ticker = str(row.get("公司代號", "")).strip()
        try:
            issued = float(str(row.get("已發行普通股數或TDR原股發行股數", "")).replace(",", ""))
            private = float(str(row.get("私募股數", "0") or "0").replace(",", ""))
        except ValueError:
            continue
        public_float = issued - private
        if ticker and math.isfinite(public_float) and public_float > 0:
            shares[ticker] = public_float
            names[ticker] = str(row.get("公司簡稱") or row.get("公司名稱") or "").strip()
    if not shares:
        raise ValueError("證交所上市公司股數檔沒有可用股數")
    return shares, names, share_date


def estimate_snapshot(trading_date: str, closes: dict[str, float], shares: dict[str, float],
                      names: dict[str, str] | None = None) -> pd.DataFrame:
    """Estimate the listed-market top 20 from close × issued shares.

    The caller supplies point-in-time daily close prices and the best available
    share-count snapshot. Private-placement shares should already be excluded.
    The caller must add ETF units separately when the TWSE reference ranking
    includes ETFs such as 0050.
    """
    names = names or {}
    caps = []
    for ticker, close in closes.items():
        count = shares.get(str(ticker).strip())
        if count is None or close is None or not math.isfinite(float(close)) or float(close) <= 0:
            continue
        market_cap = float(close) * float(count) / 100_000_000
        if math.isfinite(market_cap) and market_cap > 0:
            caps.append((str(ticker).strip(), market_cap))
    if len(caps) < TOP_N:
        raise ValueError(f"{trading_date} 可估算市值的上市證券不足 {TOP_N} 檔")
    caps.sort(key=lambda item: (-item[1], item[0]))
    rows = [{"date": trading_date, "ticker": ticker, "name": names.get(ticker, ""), "rank": rank,
             "market_cap_100m": round(cap, 2), "yield_year": ""}
            for rank, (ticker, cap) in enumerate(caps[:TOP_N], 1)]
    return pd.DataFrame(rows, columns=FIELDS)


def save_snapshot(snapshot: pd.DataFrame, path: Path = OUT,
                  metadata: dict | None = None) -> None:
    """Replace the same-date snapshot atomically and retain earlier snapshots."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(path, dtype={"ticker": str}, encoding="utf-8-sig") if path.exists() else pd.DataFrame(columns=FIELDS)
    combined = pd.concat([old[old["date"] != snapshot.iloc[0]["date"]], snapshot], ignore_index=True)
    combined = combined[FIELDS].sort_values(["date", "rank"])
    temp = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(path)
    manifest_path = path.with_name(path.stem + "_manifest.json")
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
        "source_url": URL,
        "source_page": PAGE_URL,
        "market_cap_unit": "億新臺幣",
        "ranked_rows_per_snapshot": 20,
        "snapshots": {},
    }
    snapshot_date = str(snapshot.iloc[0]["date"])
    entry = {"row_count": int(len(snapshot)),
             "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "estimated": bool((metadata or {}).get("estimated", False))}
    entry.update(metadata or {})
    old_manifest["snapshots"][snapshot_date] = entry
    manifest_temp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    manifest_temp.write_text(json.dumps(old_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_temp.replace(manifest_path)


def snapshot_metadata_for_date(data_date: str, path: Path = OUT) -> dict:
    manifest_path = path.with_name(path.stem + "_manifest.json")
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return dict(manifest.get("snapshots", {}).get(data_date, {}))


def score_for_date(data_date: str, universe: pd.Index, *, history: bool,
                   path: Path = OUT) -> tuple[pd.Series, str | None]:
    """Return neutral=0.5 for all eligible names, tilting only top-20 names upward.

    User-selected market-cap tiers apply only within the overall TWSE top 20;
    other eligible names stay neutral at 0.5. The series only indexes the
    competition universe.
    """
    current = pd.DataFrame(columns=FIELDS)
    if path.exists():
        archived = pd.read_csv(path, dtype={"ticker": str}, encoding="utf-8-sig")
        current = archived[archived["date"] == data_date]
    if not history and current.empty:
        try:
            fetched = fetch_snapshot()
            fetched_date = str(fetched.iloc[0]["date"])
            if fetched_date == data_date:
                save_snapshot(fetched, path)
                current = fetched
            else:
                current = pd.DataFrame(columns=FIELDS)
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
            current = pd.DataFrame(columns=FIELDS)
    if current.empty and not history:
        raise ValueError(f"缺少 {data_date} 證交所市值前20快照，且 API 無法提供同日資料")
    scores = pd.Series(NEUTRAL_SCORE, index=universe, dtype=float, name="market_cap_score")
    if current.empty:
        return scores, None
    current = current.copy()
    current["ticker"] = current["ticker"].astype(str).str.strip()
    current["rank"] = pd.to_numeric(current["rank"], errors="raise").astype(int)
    for row in current.itertuples(index=False):
        if row.ticker in scores.index and 1 <= row.rank <= TOP_N:
            scores.loc[row.ticker] = tier_score(row.rank)
    return scores, str(current.iloc[0]["date"])
