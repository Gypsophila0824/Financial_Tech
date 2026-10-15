"""台股交易日曆（證交所 OpenAPI holidaySchedule）。

CLOSED_2026 為 2026-10-03 自證交所取得的休市日（不含週末）。清單中「開始交易」「最後交易」
是照常交易的日子，其餘項目皆為休市。verify_online() 可重新抓取並比對，正式執行前會檢查。

注意：2026-10-26（星期一）為臺灣光復節補假，證交所休市；初賽 24 個交易日為 10/27～11/27。
主辦方 Slack 答覆：**沒有 10/26 的 D-Plan**；首次繳交時窗為 10/26 19:30～10/27 08:55，繳交的是 10/27 交易日的報告。
"""

from __future__ import annotations

from datetime import date, timedelta

from .http import fetch_json

HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"

CLOSED_2026 = {
    "2026-01-01", "2026-02-12", "2026-02-13", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-27", "2026-04-03", "2026-04-06", "2026-05-01", "2026-06-19", "2026-09-25",
    "2026-09-28", "2026-10-09", "2026-10-26", "2026-12-25",
}


def is_trading_day(d: str, closed: set[str] = CLOSED_2026) -> bool:
    return date.fromisoformat(d).weekday() < 5 and d not in closed


def is_submit_day(d: str) -> bool:
    """需要繳交 D-Plan 的日子＝交易日（Slack 答覆：休市日沒有 D-Plan）。"""
    return is_trading_day(d)


def trading_days(start: str, end: str, closed: set[str] = CLOSED_2026) -> list[str]:
    days, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        if is_trading_day(d.isoformat(), closed):
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def fetch_closed() -> set[str]:
    """抓證交所當年度休市日（平日）。"""
    closed = set()
    for row in fetch_json(HOLIDAY_URL):
        if "開始交易" in row["Name"] or "最後交易" in row["Name"]:
            continue
        roc = row["Date"]
        d = date(int(roc[:-4]) + 1911, int(roc[-4:-2]), int(roc[-2:]))
        if d.weekday() < 5:
            closed.add(d.isoformat())
    return closed


def verify_online() -> tuple[set[str], set[str]]:
    """回傳 (證交所有但本地沒有, 本地有但證交所沒有)；兩者皆空表示一致。"""
    online = {d for d in fetch_closed() if d.startswith("2026")}
    return online - CLOSED_2026, CLOSED_2026 - online
