"""抓取 150 檔股票的每日價量與三大法人買賣超，存成一份長表，供量價因子計算（factor_spec.md 第 2 節 A）。

來源（皆依日期查詢，可補歷史）：
  上市行情  證交所 MI_INDEX（每日收盤行情）
  上市法人  證交所 T86（三大法人買賣超日報）
  上櫃行情  櫃買中心 afterTrading/otc
  上櫃法人  櫃買中心 insti/dailyTrade

輸出：data/market/daily.csv
  date, ticker, market, open, high, low, close, volume, value, avg_price,
  insti_net_shares, foreign_net_shares, trust_net_shares, dealer_net_shares, ref_next
  avg_price ＝ 成交金額 ÷ 成交股數（即官方成交價定義）；當日無成交者價格欄位留空。
  insti_net_shares ＝ 三大法人買賣超股數合計 ＝ foreign ＋ trust ＋ dealer：
      foreign ＝ 外資及陸資（含外資自營商）、trust ＝ 投信、dealer ＝ 自營商（自行買賣＋避險）。
      分開存放供回測比較「合計」與「只看外資＋投信」（自營商多為短線、避險交易）。
      每列都檢查三者加總等於合計，不符即停止，避免欄位位置判讀錯誤時默默存入錯誤資料。
  ref_next ＝ 上櫃次日參考價（已反映除權息、分割、減資），由櫃買公布的次日漲停價與跌停價取中點還原；
      僅上櫃有，用於還原股價。
      data/market/twse_exright.csv
  date, ticker, pre_close, ref_price ← 證交所 TWT49U 除權除息計算結果表，上市股票還原股價用。
  每次執行重抓資料期間涵蓋的月份（每月一次請求）。

  src, insti_src ＝ 該列行情、法人資料的來源（twse／tpex，或下方備援來源名稱；法人全部失敗為 none）。

上櫃備援（只用於最近一個交易日，即當晚產出 D-Plan 所需的那一天；歷史資料一律只用櫃買官方）：
  2026/10/3 櫃買中心全站故障約 5 小時，若發生在晚上會產不出 D-Plan。故上櫃行情依序嘗試：
    櫃買資料 API → 櫃買 OpenAPI → 證交所 MIS（基本市況報導網站，主機獨立於櫃買）→ FinMind
  法人依序：櫃買資料 API → 櫃買 OpenAPI → FinMind；都失敗時法人留空、照常寫入（只影響 A4，不影響委託股數）。
  不用 Yahoo Finance：會回頭調整除權後的歷史股價，與交易所原始收盤價不符（實測富喬 9 月上旬差約 5%）。
  備援資料視為「暫用」，下次執行時若官方已恢復，會重抓並以官方資料取代。
  FinMind 未設定 token 時有請求次數限制；可設環境變數 FINMIND_TOKEN。
每日比對紀錄：最近一日取得官方上櫃資料時，同時抓各備援來源比對收盤價與法人買賣超，
  結果附加於 data/market/fallback_check.csv（同一日期、來源只記一次），累積驗證備援來源與官方是否一致。

增量更新：已存在且欄位完整的日期不重抓；舊格式（缺外資／投信／自營商欄位）的日期視為不完整，會重抓取代。
某日四份資料只要有一份尚未公布（例如法人資料較晚出），該日整天不寫入，下次執行再補，確保每一天的資料都是完整的。
每補完一天就整檔改寫一次（先寫暫存檔再取代），中途中斷也不會留下重複或寫到一半的資料。

執行 : python fetch_market_data.py            # 補齊最近 80 個交易日
       python fetch_market_data.py --days 250
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[3]  # 專案根目錄（原為程式所在資料夾）
SECURITY_MASTER = HERE / "data" / "security_master.csv"
OUT_PATH = HERE / "data" / "market" / "daily.csv"
EXRIGHT_PATH = HERE / "data" / "market" / "twse_exright.csv"
INSTI_FIELDS = ["insti_net_shares", "foreign_net_shares", "trust_net_shares", "dealer_net_shares"]
FIELDS = ["date", "ticker", "market", "open", "high", "low", "close", "volume", "value", "avg_price",
          *INSTI_FIELDS, "ref_next", "src", "insti_src"]
CHECK_LOG = HERE / "data" / "market" / "fallback_check.csv"
CHECK_FIELDS = ["checked_at", "date", "source", "kind", "compared", "matched", "missing", "mismatch_examples"]
FALLBACK_WINDOW_DAYS = 7   # 只有這幾天內的最近一個交易日可用備援來源
OFFICIAL_SRC = ("", "twse", "tpex")   # 空白 ＝ 加入來源欄位前的舊資料（皆為官方）

TWSE_QUOTES = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"
TWSE_INSTI = "https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALL&response=json"
TWSE_EXRIGHT = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={start}&endDate={end}&response=json"
TPEX_QUOTES = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash}&type=EW&response=json"
TPEX_INSTI = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={slash}&response=json"
TPEX_OPENAPI_QUOTES = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TPEX_OPENAPI_INSTI = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
MIS_QUOTES = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex}&json=1&delay=0"
FINMIND = "https://api.finmindtrade.com/api/v4/data?{query}"

# 備援來源在 D-Plan 的引用資訊（authority, url, 名稱），generate_dplan.py 依 daily.csv 的 src／insti_src 選用
FALLBACK_QUOTE_SOURCES = {
    "tpex_openapi": ("tpex", TPEX_OPENAPI_QUOTES, "櫃買中心 OpenAPI 上櫃股票行情（備援）"),
    "mis": ("twse", "https://mis.twse.com.tw/stock/api/getStockInfo.jsp", "證交所基本市況報導網站 上櫃行情（備援）"),
    "finmind": ("vendor", "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice",
                "FinMind 台股日成交資訊（備援）"),
}
FALLBACK_INSTI_SOURCES = {
    "tpex_openapi": ("tpex", TPEX_OPENAPI_INSTI, "櫃買中心 OpenAPI 上櫃三大法人買賣（備援）"),
    "finmind": ("vendor", "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell",
                "FinMind 三大法人買賣超（備援）"),
}

# 櫃買三大法人表的欄位位置（欄名重複，只能依位置）：代號、名稱，之後每組「買進／賣出／買賣超」三欄，依序為
# 外資不含外資自營商、外資自營商、外資合計、投信、自營商自行買賣、自營商避險、自營商合計，最後一欄為三大法人合計
TPEX_INSTI_COLUMNS = 24
TPEX_FOREIGN, TPEX_TRUST, TPEX_DEALER, TPEX_TOTAL = 10, 13, 22, 23

# 證交所請求太密集會暫時封鎖 IP，每次請求間隔數秒
TWSE_SLEEP = 3.0
TPEX_SLEEP = 1.0


def get_json(url, pause, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", errors="ignore"))
    time.sleep(pause)
    return data


def num(s):
    """'1,234.50' → 1234.5；'--'、'' → None；FinMind 等來源已是數字者直接轉換"""
    if isinstance(s, (int, float)):
        return float(s)
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def clean(fields):
    return [f.strip() for f in fields]


def table_rows(table):
    """把 {fields, data} 轉成 [dict]，欄名去除前後空白（櫃買欄名帶多餘空白）"""
    fields = clean(table["fields"])
    return [dict(zip(fields, row)) for row in table["data"]]


def check_insti(insti, label):
    """每列 外資＋投信＋自營商 須等於三大法人合計；不符代表欄位判讀錯誤，停止而不存入錯誤資料"""
    bad = [t for t, (total, f, tr, d) in insti.items() if abs(f + tr + d - total) > 0.5]
    if bad:
        raise ValueError(f"{label} 法人欄位加總與合計不符 {len(bad)} 檔（例：{bad[:5]}），請檢查欄位位置")
    return insti


def twse_quotes(ymd):
    j = get_json(TWSE_QUOTES.format(ymd=ymd), TWSE_SLEEP)
    if j.get("stat") != "OK":
        return None
    for t in j.get("tables", []):
        if "每日收盤行情" in (t.get("title") or "") and t.get("data"):
            return {r["證券代號"]: (num(r["開盤價"]), num(r["最高價"]), num(r["最低價"]), num(r["收盤價"]),
                                   num(r["成交股數"]), num(r["成交金額"]), None) for r in table_rows(t)}
    return None


def twse_insti(ymd):
    """{代號: (合計, 外資, 投信, 自營商)}；外資 ＝ 外陸資（不含外資自營商）＋ 外資自營商"""
    j = get_json(TWSE_INSTI.format(ymd=ymd), TWSE_SLEEP)
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    i = {f: k for k, f in enumerate(clean(j["fields"]))}
    out = {}
    for r in j["data"]:
        v = lambda name: num(r[i[name]]) or 0.0  # noqa: E731
        out[r[0].strip()] = (v("三大法人買賣超股數"),
                             v("外陸資買賣超股數(不含外資自營商)") + v("外資自營商買賣超股數"),
                             v("投信買賣超股數"), v("自營商買賣超股數"))
    return check_insti(out, "上市")


def tpex_quotes(slash):
    j = get_json(TPEX_QUOTES.format(slash=slash), TPEX_SLEEP)
    tables = [t for t in j.get("tables", []) if t.get("data")]
    if not tables:
        return None
    out = {}
    for r in table_rows(tables[0]):
        # 此端點沒有「次日參考價」欄位；漲跌停價為參考價 ±10%，取中點還原參考價（誤差在半個跳動單位內）
        up, down = num(r["次日漲停價"]), num(r["次日跌停價"])
        ref_next = round((up + down) / 2, 4) if up and down else None
        out[r["代號"]] = (num(r["開盤"]), num(r["最高"]), num(r["最低"]), num(r["收盤"]),
                         num(r["成交股數"]), num(r["成交金額(元)"]), ref_next)
    return out


def tpex_insti(slash):
    """{代號: (合計, 外資, 投信, 自營商)}，依 TPEX_* 欄位位置讀取"""
    j = get_json(TPEX_INSTI.format(slash=slash), TPEX_SLEEP)
    tables = [t for t in j.get("tables", []) if t.get("data")]
    if not tables:
        return None
    rows = tables[0]["data"]
    if any(len(row) != TPEX_INSTI_COLUMNS for row in rows):
        raise ValueError(f"上櫃法人表欄數不是預期的 {TPEX_INSTI_COLUMNS} 欄（實際 {len(rows[0])} 欄），格式可能已改版")
    out = {row[0].strip(): tuple(num(row[k]) or 0.0 for k in (TPEX_TOTAL, TPEX_FOREIGN, TPEX_TRUST, TPEX_DEALER))
           for row in rows}
    return check_insti(out, "上櫃")


# ---------- 上櫃備援來源：只提供最近一個交易日；回傳格式與官方相同，日期不符或無資料回傳 None ----------

def roc_iso(s):
    """'1151002' → '2026-10-02'"""
    s = str(s).strip()
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"


def tpex_openapi_quotes(d):
    rows = [r for r in get_json(TPEX_OPENAPI_QUOTES, TPEX_SLEEP) if roc_iso(r["Date"]) == d.isoformat()]
    out = {}
    for r in rows:
        up, down = num(r.get("NextLimitUp")), num(r.get("NextLimitDown"))
        out[r["SecuritiesCompanyCode"].strip()] = (
            num(r["Open"]), num(r["High"]), num(r["Low"]), num(r["Close"]), num(r["TradingShares"]),
            num(r["TransactionAmount"]), round((up + down) / 2, 4) if up and down else None)
    return out or None


def tpex_openapi_insti(d):
    out = {}
    for r in get_json(TPEX_OPENAPI_INSTI, TPEX_SLEEP):
        r = {k.replace(" ", "").lower(): v for k, v in r.items()}  # 欄名空白不一致
        if roc_iso(r["date"]) != d.isoformat():
            continue
        v = lambda k: num(r[k]) or 0.0  # noqa: E731
        out[r["securitiescompanycode"].strip()] = (
            v("totaldifference"), v("foreigninvestorsincludemainlandareainvestors-difference"),
            v("securitiesinvestmenttrustcompanies-difference"), v("dealers-difference"))
    return check_insti(out, "上櫃（OpenAPI）") if out else None


def mis_quotes(d, tickers):
    """證交所 MIS：z ＝ 最近成交價（收盤後即收盤價）、v ＝ 累計成交量（張）；無成交金額與次日參考價"""
    out = {}
    for i in range(0, len(tickers), 25):
        ex = urllib.parse.quote("|".join(f"otc_{t}.tw" for t in tickers[i:i + 25]))
        j = get_json(MIS_QUOTES.format(ex=ex), TPEX_SLEEP, {"Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        for r in j.get("msgArray", []):
            if r.get("d") == d.strftime("%Y%m%d") and num(r.get("z")) is not None:
                vol = num(r.get("v"))
                out[r["c"]] = (num(r.get("o")), num(r.get("h")), num(r.get("l")), num(r["z"]),
                               vol * 1000 if vol is not None else None, None, None)
    return out or None


def finmind(dataset, ticker, d):
    query = urllib.parse.urlencode({"dataset": dataset, "data_id": ticker, "start_date": d.isoformat(),
                                    "end_date": d.isoformat()})
    token = os.environ.get("FINMIND_TOKEN")
    return get_json(FINMIND.format(query=query), 0.3,
                    {"Authorization": f"Bearer {token}"} if token else None).get("data", [])


def finmind_quotes(d, tickers):
    out = {}
    for t in tickers:
        for r in finmind("TaiwanStockPrice", t, d):
            out[t] = (num(r["open"]), num(r["max"]), num(r["min"]), num(r["close"]), num(r["Trading_Volume"]),
                      num(r["Trading_money"]), None)
    return out or None


def finmind_insti(d, tickers):
    """外資 ＝ Foreign_Investor ＋ Foreign_Dealer_Self、投信 ＝ Investment_Trust、自營商 ＝ Dealer_self ＋ Dealer_Hedging"""
    out = {}
    for t in tickers:
        net = {r["name"]: r["buy"] - r["sell"] for r in finmind("TaiwanStockInstitutionalInvestorsBuySell", t, d)}
        if net:
            f = net.get("Foreign_Investor", 0) + net.get("Foreign_Dealer_Self", 0)
            tr = net.get("Investment_Trust", 0)
            dl = net.get("Dealer_self", 0) + net.get("Dealer_Hedging", 0)
            out[t] = (float(f + tr + dl), float(f), float(tr), float(dl))
    return out or None


def first_available(chain):
    """依序嘗試 [(來源名稱, 函式)]，回傳 (資料, 來源名稱, 錯誤清單)；只有一個來源時不攔截例外（歷史回補維持原行為）"""
    if len(chain) == 1:
        name, fn = chain[0]
        data = fn()
        return data, name if data else None, []
    errors = []
    for name, fn in chain:
        try:
            data = fn()
        except Exception as e:  # noqa: BLE001 備援：任何錯誤都改試下一個來源
            errors.append(f"{name}：{type(e).__name__}")
            continue
        if data:
            return data, name, errors
        errors.append(f"{name}：無此日資料")
    return None, None, errors


def fetch_day(d, universe, allow_fallback=False):
    """回傳該日 150 檔的列；非交易日或資料尚未齊全回傳 None。allow_fallback 時上櫃資料可改用備援來源"""
    ymd, slash = d.strftime("%Y%m%d"), d.strftime("%Y/%m/%d")
    tw_q = twse_quotes(ymd)
    if tw_q is None:
        return None  # 非交易日或尚未公布
    tw_i = twse_insti(ymd)
    otc = [t for t, m in universe if m == "TPEX"]
    q_chain = [("tpex", lambda: tpex_quotes(slash))]
    i_chain = [("tpex", lambda: tpex_insti(slash))]
    if allow_fallback:
        q_chain += [("tpex_openapi", lambda: tpex_openapi_quotes(d)), ("mis", lambda: mis_quotes(d, otc)),
                    ("finmind", lambda: finmind_quotes(d, otc))]
        i_chain += [("tpex_openapi", lambda: tpex_openapi_insti(d)), ("finmind", lambda: finmind_insti(d, otc))]
    tp_q, q_src, q_err = first_available(q_chain)
    tp_i, i_src, i_err = first_available(i_chain)
    if q_src not in (None, "tpex") or i_src != "tpex":
        print(f"  {d} 櫃買官方資料無法取得，上櫃行情改用 {q_src or '（全部失敗）'}、法人改用 {i_src or '（全部失敗，留空）'}"
              f"；嘗試紀錄：{'、'.join(q_err + i_err)}", file=sys.stderr)
    if tp_i is None and allow_fallback and tp_q is not None:
        tp_i, i_src = {}, "none"  # 法人只影響 A4：留空照常寫入，下次執行再以官方資料取代
    if tw_i is None or tp_q is None or tp_i is None:
        print(f"  {d} 部分資料尚未公布（上市法人={tw_i is not None} 上櫃行情={tp_q is not None} "
              f"上櫃法人={tp_i is not None}），本次略過", file=sys.stderr)
        return None

    rows, missing = [], []
    for ticker, market in universe:
        quotes, insti = (tw_q, tw_i) if market == "TWSE" else (tp_q, tp_i)
        q = quotes.get(ticker)
        if q is None:
            missing.append(ticker)
            q = (None,) * 7
        o, h, l, c, vol, val, ref_next = q
        avg = round(val / vol, 4) if vol and val else None
        no_insti = market == "TPEX" and i_src == "none"
        rows.append({"date": d.isoformat(), "ticker": ticker, "market": market,
                     "open": o, "high": h, "low": l, "close": c, "volume": vol, "value": val, "avg_price": avg,
                     **dict(zip(INSTI_FIELDS, (None,) * 4 if no_insti else insti.get(ticker, (0.0,) * 4))),
                     "ref_next": ref_next, "src": "twse" if market == "TWSE" else q_src,
                     "insti_src": "twse" if market == "TWSE" else i_src})
    if missing:
        print(f"  {d} 行情查無：{missing}（可能停牌，價格留空）", file=sys.stderr)
    return rows


def check_fallbacks(d, rows):
    """最近一日的官方上櫃資料 vs 各備援來源：比對收盤價與法人四欄，附加到 CHECK_LOG（同日期、來源、種類只記一次）"""
    done = set()
    if CHECK_LOG.exists():
        with open(CHECK_LOG, encoding="utf-8-sig") as f:
            done = {(r["date"], r["source"], r["kind"]) for r in csv.DictReader(f)}
    official = {r["ticker"]: r for r in rows.values() if r["date"] == d.isoformat() and r["market"] == "TPEX"}
    otc = sorted(official)
    tests = [("tpex_openapi", "quotes", lambda: tpex_openapi_quotes(d)), ("mis", "quotes", lambda: mis_quotes(d, otc)),
             ("finmind", "quotes", lambda: finmind_quotes(d, otc)), ("tpex_openapi", "insti", lambda: tpex_openapi_insti(d)),
             ("finmind", "insti", lambda: finmind_insti(d, otc))]
    out = []
    for source, kind, fn in tests:
        if (d.isoformat(), source, kind) in done:
            continue
        try:
            got = fn() or {}
        except Exception as e:  # noqa: BLE001 比對失敗只記錄
            out.append({"source": source, "kind": kind, "compared": 0, "matched": 0, "missing": len(otc),
                        "mismatch_examples": f"抓取失敗 {type(e).__name__}"})
            continue
        matched, missing, bad = 0, 0, []
        for t, r in official.items():
            if kind == "quotes":
                ref = num(str(r["close"]))
                if ref is None:
                    continue  # 官方無成交
                mine = got.get(t, (None,) * 7)[3]
                ok = mine is not None and abs(mine - ref) < 1e-6
            else:
                ref = tuple(num(str(r[k])) for k in INSTI_FIELDS)
                if None in ref:
                    continue
                mine = got.get(t)
                ok = mine is not None and all(abs(a - b) < 0.5 for a, b in zip(mine, ref))
            if mine is None:
                missing += 1
            elif ok:
                matched += 1
            else:
                bad.append(f"{t}:{mine}≠{ref}")
        out.append({"source": source, "kind": kind, "compared": matched + len(bad), "matched": matched,
                    "missing": missing, "mismatch_examples": "；".join(bad[:5])})
    if not out:
        return []
    new = not CHECK_LOG.exists()
    with open(CHECK_LOG, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CHECK_FIELDS)
        if new:
            w.writeheader()
        now = datetime.now().replace(microsecond=0).isoformat()
        w.writerows({"checked_at": now, "date": d.isoformat(), **r} for r in out)
    return out


def save(rows):
    """整檔改寫：先寫暫存檔再取代原檔（OneDrive 同步偶爾短暫鎖檔，取代失敗時稍候重試）"""
    tmp = OUT_PATH.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["date"], r["ticker"])))
    for attempt in range(10):
        try:
            os.replace(tmp, OUT_PATH)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(3)


def fetch_twse_exright(first, last, tickers):
    """抓 first~last 期間上市股票的除權除息事件（逐月查詢），只留 150 檔"""
    events, start = [], first.replace(day=1)
    while start <= last:
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = min(nxt - timedelta(days=1), last)
        j = get_json(TWSE_EXRIGHT.format(start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d")), TWSE_SLEEP)
        for r in j.get("data", []) if j.get("stat") == "OK" else []:
            ticker = r[1].strip()
            if ticker in tickers:
                y, m, dd = (int(x) for x in r[0].replace("年", "-").replace("月", "-").replace("日", "").split("-"))
                events.append({"date": date(y + 1911, m, dd).isoformat(), "ticker": ticker,
                               "pre_close": num(r[3]), "ref_price": num(r[4])})
        start = nxt
    with open(EXRIGHT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["date", "ticker", "pre_close", "ref_price"])
        w.writeheader()
        w.writerows(sorted(events, key=lambda e: (e["date"], e["ticker"])))
    return len(events)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=80, help="要補齊的最近交易日數（預設 80，60 日因子需要緩衝）")
    args = ap.parse_args()

    with open(SECURITY_MASTER, encoding="utf-8-sig") as f:
        universe = [(r["ticker"], r["market"]) for r in csv.DictReader(f)]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = {}
    if OUT_PATH.exists():
        with open(OUT_PATH, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                row = {k: r.get(k, "") for k in FIELDS}
                for k in ("src", "insti_src"):  # 加入來源欄位前的舊資料皆為官方，補上以免欄位型別混雜
                    row[k] = row[k] or ("twse" if row["market"] == "TWSE" else "tpex")
                rows[(r["date"], r["ticker"])] = row  # 同鍵重複時保留後者
    # 欄位完整且皆為官方資料的日期才算已完成：舊格式（缺外資／投信／自營商）、備援暫用的日期會重抓取代
    incomplete = {d for (d, _), r in rows.items() if any(r[k] in ("", None) for k in INSTI_FIELDS)}
    provisional = {d for (d, _), r in rows.items()
                   if r["src"] not in OFFICIAL_SRC or r["insti_src"] not in OFFICIAL_SRC}
    done = {d for d, _ in rows} - incomplete - provisional
    if incomplete - provisional:
        print(f"欄位不完整的舊資料 {len(incomplete - provisional)} 天，將重抓取代")
    if provisional:
        print(f"使用備援資料的日期 {sorted(provisional)}，將嘗試以櫃買官方資料取代")

    # 從今天往回走，跳過週末；有資料的日子算一個交易日，直到湊滿 --days 天
    # 只有遇到的第一個（最近的）交易日、且在 FALLBACK_WINDOW_DAYS 天內，才允許上櫃資料改用備援來源
    trading_days, d, scanned, fetched = 0, date.today(), 0, 0
    newest = None
    while trading_days < args.days and scanned < args.days * 3:
        scanned += 1
        if d.weekday() < 5:
            if d.isoformat() in done:
                trading_days += 1
                newest = newest or d
            else:
                allow = trading_days == 0 and d >= date.today() - timedelta(days=FALLBACK_WINDOW_DAYS)
                new = fetch_day(d, universe, allow_fallback=allow)
                if new:
                    newest = newest or d
                    rows = {k: v for k, v in rows.items() if k[0] != d.isoformat()}
                    rows.update({(r["date"], r["ticker"]): r for r in new})
                    save(rows)
                    done.add(d.isoformat())
                    incomplete.discard(d.isoformat())
                    trading_days += 1
                    fetched += 1
                    print(f"{d} 已寫入 {len(new)} 檔（{trading_days}/{args.days}）")
        d -= timedelta(days=1)
    if not fetched and rows:
        save(rows)  # 沒有新資料時也以新格式改寫一次（舊檔升級欄位）

    if done:
        twse_tickers = {t for t, m in universe if m == "TWSE"}
        n = fetch_twse_exright(date.fromisoformat(min(done)), date.fromisoformat(max(done)), twse_tickers)
        print(f"上市除權除息事件：{n} 筆 → {EXRIGHT_PATH.relative_to(HERE)}")
    print(f"完成：{OUT_PATH.relative_to(HERE)} 共 {len(done)} 個完整交易日，最新 {max(done) if done else '無'}"
          f"{f'；仍有 {len(incomplete)} 天欄位不完整' if incomplete else ''}")

    # 每日比對紀錄：最近一日為官方上櫃資料時，與各備援來源比對（失敗不影響本程式結果）
    if newest and newest >= date.today() - timedelta(days=FALLBACK_WINDOW_DAYS):
        otc_rows = [r for r in rows.values() if r["date"] == newest.isoformat() and r["market"] == "TPEX"]
        if otc_rows and all(r["src"] in OFFICIAL_SRC and r["insti_src"] in OFFICIAL_SRC for r in otc_rows):
            try:
                for r in check_fallbacks(newest, rows):
                    flag = "✅" if r["compared"] and r["matched"] == r["compared"] else "⚠️ "
                    print(f"  {flag} 備援比對 {newest} {r['source']}／{r['kind']}：一致 {r['matched']}／{r['compared']}、"
                          f"缺漏 {r['missing']}{('；' + r['mismatch_examples']) if r['mismatch_examples'] else ''}")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️  備援比對失敗：{type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
