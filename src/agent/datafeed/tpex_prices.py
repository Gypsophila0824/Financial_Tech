"""TPEx daily totals less single-security block trades, with an audit trail.

This scope reproduces the six competition prices checked on 2026-10-22.
Keep the scope version explicit: an old or fallback quote is not interchangeable.
"""

import math
from datetime import date
from functools import lru_cache


PRICE_SCOPE = "tpex_daily_ex_single_block_v1"
DAILY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={slash}&id=&response=json"
BLOCK_URL = "https://www.tpex.org.tw/www/zh-tw/blockTrade/tradeInfo?date={year}&code=&id=&response=json"
AUDIT_FIELDS = ["price_scope", "total_volume", "total_value", "block_volume", "block_value", "block_source"]


class QuoteBatch(dict):
    """Retain the existing seven-value quote contract plus per-ticker evidence."""

    def __init__(self):
        super().__init__()
        self.audit = {}


def parse_date(value):
    text = str(value).strip().replace("-", "/")
    if "/" in text:
        year, month, day = map(int, text.split("/"))
    elif text.isdigit() and len(text) in (7, 8):
        year, month, day = int(text[:-4]), int(text[-4:-2]), int(text[-2:])
    else:
        raise ValueError(f"櫃買日期格式無效：{value!r}")
    return date(year + 1911 if year < 1911 else year, month, day)


def number(value, *, required=False):
    try:
        result = float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        if required:
            raise ValueError(f"櫃買成交數值缺漏：{value!r}") from None
        return None
    if not math.isfinite(result) or (required and result < 0):
        raise ValueError(f"櫃買成交數值無效：{value!r}")
    return result


def records(payload, required_fields):
    if str(payload.get("stat", "")).lower() != "ok":
        raise ValueError("櫃買資料尚未公布或查詢失敗；不能把失敗當成沒有鉅額交易")
    for table in payload.get("tables", []):
        fields = ["".join(str(field).split()) for field in table.get("fields", [])]
        if not set(required_fields) <= set(fields):
            continue
        data = table.get("data")
        if not isinstance(data, list):
            raise ValueError("櫃買表格缺少資料陣列")
        if "totalCount" in table and int(table["totalCount"]) != len(data):
            raise ValueError("櫃買回應不完整，資料筆數與 totalCount 不符")
        if any(len(row) != len(fields) for row in data):
            raise ValueError("櫃買表格欄數不符")
        return [dict(zip(fields, row)) for row in data]
    raise ValueError(f"櫃買表格缺少必要欄位：{', '.join(required_fields)}")


@lru_cache(maxsize=8)
def block_trades(year, get_json, pause):
    """Fetch once per year per process; row dates, not response.date, select a day."""
    payload = get_json(BLOCK_URL.format(year=year), pause)
    if parse_date(payload.get("date")).year != year:
        raise ValueError("櫃買鉅額交易回應年份與查詢不符")
    result = {}
    for row in records(payload, ["證券代號", "成交日期", "成交股數", "成交金額"]):
        day = parse_date(row["成交日期"])
        if day.year != year:
            raise ValueError("櫃買鉅額交易明細混入其他年份")
        key = (day.isoformat(), str(row["證券代號"]).strip())
        shares = number(row["成交股數"], required=True)
        value = number(row["成交金額"], required=True)
        if shares != int(shares) or (shares == 0) != (value == 0):
            raise ValueError("櫃買鉅額交易股數／金額無效")
        old_shares, old_value = result.get(key, (0.0, 0.0))
        result[key] = (old_shares + shares, old_value + value)
    return result


def fetch_quotes(slash, get_json, pause):
    requested = parse_date(slash)
    payload = get_json(DAILY_URL.format(slash=requested.strftime("%Y/%m/%d")), pause)
    if str(payload.get("stat", "")).lower() != "ok":
        return None
    if parse_date(payload.get("date")) != requested:
        raise ValueError("櫃買完整行情日期與查詢不符")
    rows = records(payload, ["代號", "收盤", "開盤", "最高", "最低", "成交股數", "成交金額(元)", "次日參考價"])
    if not rows:
        return None
    blocks = block_trades(requested.year, get_json, pause)
    out = QuoteBatch()
    for row in rows:
        ticker = str(row["代號"]).strip()
        if ticker in out:
            raise ValueError(f"櫃買完整行情代號重複：{ticker}")
        total_volume = number(row["成交股數"], required=True)
        total_value = number(row["成交金額(元)"], required=True)
        block_volume, block_value = blocks.get((requested.isoformat(), ticker), (0.0, 0.0))
        volume, value = total_volume - block_volume, total_value - block_value
        if volume != int(volume) or volume < 0 or value < 0 or (volume == 0) != (value == 0):
            raise ValueError(f"{ticker} 扣除鉅額後股數／金額不合理，停止使用均價")
        out[ticker] = (number(row["開盤"]), number(row["最高"]), number(row["最低"]),
                       number(row["收盤"]), volume, value, number(row["次日參考價"]))
        out.audit[ticker] = dict(price_scope=PRICE_SCOPE, total_volume=total_volume, total_value=total_value,
                                 block_volume=block_volume, block_value=block_value,
                                 block_source=BLOCK_URL.format(year=requested.year))
    return out


def needs_refresh(row):
    return row.get("market") == "TPEX" and row.get("price_scope") != PRICE_SCOPE
