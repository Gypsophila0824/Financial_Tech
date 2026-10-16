"""抓取 30 檔主動型 ETF（玉山挑戰賽_主動型ETF列表_v1.pdf）的全部持股，來源 MoneyDJ 每日持股明細。

輸出（data/etf/）：
  active_etf_holdings_<資料日期>.csv  全部持股（含期貨/現金等非個股列，is_stock 欄標記）
  active_etf_top15_<資料日期>.csv     每檔 ETF 依權重排序的前 15 大「個股」

抓取失敗的 ETF：沿用前一次檔案中該檔的持股（資料日期照舊標示），印出警告；
  沒有前一次資料可沿用時以 exit code 1 結束（Active Share 無法對 30 檔全部檢查，不可產出 D-Plan）。
  不會寫出日期未知的檔案（否則檔名排序在日期之後，會被下游誤當成最新資料）。

執行 : python fetch_active_etf_holdings.py
"""
import csv
import re
import sys
import time
import urllib.request
from pathlib import Path

from .. import rules

ETFS = [
    ("00980A", "主動野村臺灣優選"), ("00982A", "主動群益台灣強棒"), ("00981A", "主動統一台股成長"),
    ("00983A", "主動中信ARK創新"), ("00984A", "主動安聯台灣高息"), ("00985A", "主動野村台灣50"),
    ("00986A", "主動台新龍頭成長"), ("00989A", "主動摩根美國科技"), ("00988A", "主動統一全球創新"),
    ("00991A", "主動復華未來50"), ("00990A", "主動元大AI新經濟"), ("00987A", "主動台新優勢成長"),
    ("00992A", "主動群益科技創新"), ("00994A", "主動第一金台股優"), ("00995A", "主動中信台灣卓越"),
    ("00993A", "主動安聯台灣"), ("00996A", "主動兆豐台灣豐收"), ("00400A", "主動國泰動能高息"),
    ("00401A", "主動摩根台灣鑫收"), ("00997A", "主動群益美國增長"), ("00999A", "主動野村臺灣高息"),
    ("00403A", "主動統一升級50"), ("00402A", "主動安聯美國科技"), ("00404A", "主動聯博動能50"),
    ("00405A", "主動富邦台灣龍耀"), ("00406A", "主動中信台灣收益"), ("00407A", "主動凱基台灣"),
    ("00408A", "主動第一金優股息"), ("00410A", "主動永豐科技趨勢"), ("00409A", "主動復華全球50"),
]
URL = "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={}.TW"
OUT_DIR = Path(__file__).resolve().parents[3] / "data" / "etf"  # 專案根目錄的 data/etf


def fetch(etf_id):
    req = urllib.request.Request(URL.format(etf_id), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse(html):
    """回傳 (資料日期, [(名稱, 代碼, 市場後綴, 權重%, 股數), ...])"""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", "|", text)
    text = re.sub(r"\s*\|[\s|]*", "|", text)
    m = re.search(r"全部持股\|資料日期：(\d{4}/\d{2}/\d{2})\|個股名稱\|投資比例\(%\)\|持有股數[^|]*\|(.*?)\|相關基金", text)
    if not m:
        return None, []
    # 每列為「名稱|權重|股數」三格；名稱可能帶 (代碼.市場)，部分海外持股無代碼
    cells = m.group(2).split("|")
    rows = []
    for i in range(0, len(cells) - 2, 3):
        label, w, sh = cells[i], cells[i + 1], cells[i + 2]
        if not re.fullmatch(r"-?[\d.]+", w):
            raise ValueError(f"無法解析持股列：{cells[i:i + 3]}")
        t = re.fullmatch(r"(.+?)\(([^()]+?)\.([A-Z]{2,3})\)", label)
        name, code, mkt = (t.group(1), t.group(2), t.group(3)) if t else (label, "", "")
        # MoneyDJ 名稱帶 *（如「國巨*」），主辦方 150 檔清單沒有，去掉以便對照
        rows.append((name.replace("*", "").strip(), code, mkt, float(w), sh.replace(",", "")))
    return m.group(1).replace("/", "-"), rows


def is_stock(name, code, mkt):
    # 台股個股：TW/TWO 且代碼為 4 碼數字；海外個股：非 TF(期貨) 的其他市場，含無代碼者
    if mkt == "TF" or "選擇權" in name or "期貨" in name:
        return False
    if re.search(r"\bETF\b", name, re.I):  # 用整字比對，避免誤判 Netflix
        return False
    if mkt in ("TW", "TWO"):
        return bool(re.fullmatch(r"\d{4}", code)) and not code.startswith("00")  # 00 開頭為台股 ETF
    return True


def previous_rows():
    """前一次檔案中各 ETF 的持股列：{etf_id: [(名稱, 代碼, 市場, 權重, 股數, 資料日期), ...]}"""
    files = sorted(OUT_DIR.glob("active_etf_holdings_????-??-??.csv"))
    if not files:
        return {}
    prev = {}
    with open(files[-1], encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            prev.setdefault(r["etf_id"], []).append(
                (r["name"], r["code"], r["market"], float(r["weight_pct"]), r["shares"], r["data_date"]))
    return prev


def main():
    all_rows, top_rows, dates, failed, reused = [], [], set(), [], []
    prev = previous_rows()
    for etf_id, etf_name in ETFS:
        try:
            date, rows = parse(fetch(etf_id))
        except Exception as e:  # 網路錯誤：記錄後繼續
            date, rows = None, []
            print(f"{etf_id} 抓取失敗：{e}", file=sys.stderr)
        if not rows and prev.get(etf_id):
            date = prev[etf_id][0][5]
            rows = [r[:5] for r in prev[etf_id]]
            reused.append(f"{etf_id}（{date}）")
        if not rows:
            failed.append(etf_id)
            print(f"{etf_id} {etf_name}: 無資料", file=sys.stderr)
            continue
        dates.add(date)
        for name, code, mkt, w, sh in rows:
            all_rows.append([etf_id, etf_name, date, code, mkt, name, w, sh, is_stock(name, code, mkt)])
        stocks = sorted((r for r in rows if is_stock(*r[:3])), key=lambda r: -r[3])[:rules.ACTIVE_SHARE_TOP_N]
        for rank, (name, code, mkt, w, sh) in enumerate(stocks, 1):
            top_rows.append([etf_id, etf_name, date, rank, code, mkt, name, w])
        print(f"{etf_id} {etf_name}: {date} 共 {len(rows)} 列，前{rules.ACTIVE_SHARE_TOP_N}大個股合計 {sum(r[3] for r in stocks):.2f}%")
        time.sleep(1)

    if failed:
        sys.exit(f"❌ {len(failed)} 檔 ETF 抓取失敗且無前次資料可沿用：{'、'.join(failed)}；"
                 "Active Share 無法完整檢查，已停止")
    tag = max(dates)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / f"active_etf_holdings_{tag}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["etf_id", "etf_name", "data_date", "code", "market", "name", "weight_pct", "shares", "is_stock"])
        w.writerows(all_rows)
    with open(OUT_DIR / f"active_etf_top{rules.ACTIVE_SHARE_TOP_N}_{tag}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["etf_id", "etf_name", "data_date", "rank", "code", "market", "name", "weight_pct"])
        w.writerows(top_rows)
    print(f"資料日期：{sorted(dates)}")
    if reused:
        print(f"⚠️  抓取失敗、沿用前次資料：{'、'.join(reused)}")


if __name__ == "__main__":
    main()
