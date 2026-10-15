"""抓取 150 檔相關的新聞與重大訊息，累積存檔，供新聞分數計算（factor_spec.md 第 2 節 C）。

來源：
  公開資訊觀測站重大訊息  上市 OpenAPI t187ap04_L、上櫃 mopsfin_t187ap04_O      authority = mops
      只提供最近一個發言日、查不到歷史，須每天執行累積；漏跑的那天無法回補。
  鉅亨網台股新聞          api.cnyes.com newslist/category/tw_stock             authority = media
      依發布時間查詢近 --days 天（預設 3 天，對應新聞因子的計算期間）。
      對應股票 ＝ 新聞的股票標籤 ∪ 標題內文中的「(代號-TW)」「(代號-TWO)」，只保留 150 檔。
      標籤只涵蓋約 35% 的新聞，加上內文代號約 66%（2026-10-03 實測）。
      不以公司名稱比對：簡稱常是一般詞彙（如「大量」「精確」），容易誤判。

輸出：
  data/news/raw/<執行日>/*.json   原始回應留存（事後核對、資格審查重現用）
  data/news/items.jsonl           累積新聞庫，一行一則，以 id 去重，只新增不修改
      id, source, authority, published_at, tickers, title, body, url, fetched_at

任一來源失敗只印出警告、不中斷：新聞是加分項，不能因為新聞抓不到就產不出 D-Plan
（沒有新聞的股票新聞分數為中性）。全部來源都失敗時仍以 exit code 0 結束，但會明確標示。

執行 : python fetch_news.py
       python fetch_news.py --days 7
"""
import argparse
import csv
import hashlib
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[3]  # 專案根目錄（原為程式所在資料夾）
SECURITY_MASTER = HERE / "data" / "security_master.csv"
NEWS_DIR = HERE / "data" / "news"
STORE = NEWS_DIR / "items.jsonl"
TPE = timezone(timedelta(hours=8))

MOPS_SOURCES = {
    "twse_material": "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
    "tpex_material": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O",
}
CNYES_LIST = ("https://api.cnyes.com/media/api/v1/newslist/category/tw_stock"
              "?startAt={start}&endAt={end}&limit=30&page={page}")
CNYES_ARTICLE = "https://news.cnyes.com/news/id/{id}"
CNYES_SLEEP = 1.0
CNYES_MAX_PAGES = 50      # 防呆：近 3 天約 10 頁
CODE_IN_TEXT = re.compile(r"[(（](\d{4})-TWO?[)）]")


def get_json(url, attempts=3):
    """回應偶爾會傳到一半被切斷，失敗時隔幾秒重試"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (http.client.IncompleteRead, urllib.error.URLError, json.JSONDecodeError):
            if i == attempts - 1:
                raise
            time.sleep(5)


def roc_datetime(d, t):
    """('1151002', '70003') → '2026-10-02T07:00:03+08:00'；發言時間省略開頭的 0"""
    t = str(t).zfill(6)
    dt = datetime(int(d[:-4]) + 1911, int(d[-4:-2]), int(d[-2:]), int(t[:2]), int(t[2:4]), int(t[4:]), tzinfo=TPE)
    return dt.isoformat()


def plain_text(s):
    """鉅亨網內文為轉義過的 HTML：還原後去標籤、壓縮空行"""
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<br\s*/?>|</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def fetch_mops(universe, raw_dir, fetched_at):
    items, stats = [], {}
    for name, url in MOPS_SOURCES.items():
        rows = get_json(url)
        (raw_dir / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        kept = 0
        for r in rows:
            r = {k.strip(): v for k, v in r.items()}  # 上市「主旨 」欄名帶空白
            code = (r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
            if code not in universe:
                continue
            subject = re.sub(r"\s*\r?\n\s*", "", r.get("主旨", "")).strip()
            digest = hashlib.sha1(subject.encode("utf-8")).hexdigest()[:8]
            items.append({
                "id": f"mops:{code}:{r['發言日期']}:{r['發言時間']}:{digest}",
                "source": "mops", "authority": "mops",
                "published_at": roc_datetime(r["發言日期"], r["發言時間"]),
                "tickers": [code], "title": subject,
                "body": (r.get("說明") or "").replace("\r\n", "\n").strip(),
                "url": url, "fetched_at": fetched_at,
            })
            kept += 1
        stats[name] = f"{len(rows)} 則，150 檔相關 {kept} 則"
    return items, stats


def fetch_cnyes(universe, raw_dir, fetched_at, days):
    end = int(datetime.now(TPE).timestamp())
    start = end - days * 86400
    news, page, last = [], 1, 1
    while page <= min(last, CNYES_MAX_PAGES):
        j = get_json(CNYES_LIST.format(start=start, end=end, page=page))
        last = j["items"]["last_page"]
        news += j["items"]["data"]
        page += 1
        time.sleep(CNYES_SLEEP)
    (raw_dir / "cnyes_tw_stock.json").write_text(json.dumps(news, ensure_ascii=False), encoding="utf-8")

    items = []
    for n in news:
        title = html.unescape(n.get("title") or "").strip()
        body = plain_text(n.get("content"))
        tags = {s for s in n.get("stock") or [] if s in universe}
        in_text = {c for c in CODE_IN_TEXT.findall(title + "\n" + body) if c in universe}
        tickers = sorted(tags | in_text)
        if not tickers:
            continue
        items.append({
            "id": f"cnyes:{n['newsId']}", "source": "cnyes", "authority": "media",
            "published_at": datetime.fromtimestamp(n["publishAt"], TPE).isoformat(),
            "tickers": tickers, "title": title, "body": body,
            "url": CNYES_ARTICLE.format(id=n["newsId"]), "fetched_at": fetched_at,
        })
    return items, {"cnyes": f"近 {days} 天 {len(news)} 則，150 檔相關 {len(items)} 則"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="鉅亨網新聞回溯天數（預設 3）")
    args = ap.parse_args()

    with open(SECURITY_MASTER, encoding="utf-8-sig") as f:
        universe = {r["ticker"] for r in csv.DictReader(f)}
    fetched_at = datetime.now(TPE).replace(microsecond=0).isoformat()
    raw_dir = NEWS_DIR / "raw" / date.today().isoformat()
    raw_dir.mkdir(parents=True, exist_ok=True)

    items, stats, failed = [], {}, []
    for label, fetch in [("重大訊息", lambda: fetch_mops(universe, raw_dir, fetched_at)),
                         ("鉅亨網", lambda: fetch_cnyes(universe, raw_dir, fetched_at, args.days))]:
        try:
            got, st = fetch()
            items += got
            stats.update(st)
        except Exception as e:  # 新聞是加分項：記錄後繼續，不中斷每日流程
            failed.append(label)
            print(f"⚠️  {label} 抓取失敗：{type(e).__name__}: {e}")

    seen = set()
    if STORE.exists():
        with open(STORE, encoding="utf-8") as f:
            seen = {json.loads(line)["id"] for line in f if line.strip()}
    new = sorted((x for x in items if x["id"] not in seen), key=lambda x: x["published_at"])
    new = list({x["id"]: x for x in new}.values())  # 同一次抓取內的重複
    with open(STORE, "a", encoding="utf-8") as f:
        for x in new:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    for k, v in stats.items():
        print(f"{k}：{v}")
    print(f"新增 {len(new)} 則（已存在 {len(items) - len(new)} 則）→ {STORE.relative_to(HERE)}，"
          f"累積共 {len(seen) + len(new)} 則；原始回應存於 {raw_dir.relative_to(HERE)}")
    top = Counter(t for x in items for t in x["tickers"]).most_common(10)
    if top:
        print("本次新聞最多的股票：" + "、".join(f"{t}({n})" for t, n in top))
    if failed:
        print(f"⚠️  失敗來源：{'、'.join(failed)}"
              f"{'（全部失敗，今日新聞分數將全部為中性）' if len(failed) == 2 else ''}")


if __name__ == "__main__":
    main()
