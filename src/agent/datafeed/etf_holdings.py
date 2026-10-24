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
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
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
    # MoneyDJ 的台指選擇權也可能以英文合約名稱、無代碼／市場列出。
    if re.fullmatch(r"TAIWAN\s+TAIE\s+[CP]\s+\d{1,2}[A-Z]{3}\d{2}\s+\d+(?:\.\d+)?\s+TWSE", name, re.I):
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


def _save_snapshot(tag: str, all_rows: list[list], top_rows: list[list], *, complete: bool) -> list[str]:
    """完整快照使用既有可讀檔名；不完整抓取另存 .partial，避免被回測誤讀。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if complete else ".partial"
    holdings_path = OUT_DIR / f"active_etf_holdings_{tag}{suffix}.csv"
    top_path = OUT_DIR / f"active_etf_top{rules.ACTIVE_SHARE_TOP_N}_{tag}{suffix}.csv"
    outputs = [(holdings_path,
                ["etf_id", "etf_name", "data_date", "code", "market", "name", "weight_pct", "shares", "is_stock"], all_rows),
               (top_path,
                ["etf_id", "etf_name", "data_date", "rank", "code", "market", "name", "weight_pct"], top_rows)]
    saved = []
    for path, header, rows in outputs:
        if path.exists() and complete:
            archive_dir = OUT_DIR / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = archive_dir / f"{path.stem}_{stamp}{path.suffix}"
            shutil.copy2(path, backup)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=OUT_DIR)
        try:
            with open(fd, "w", newline="", encoding="utf-8-sig", closefd=True) as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            Path(temp_name).replace(path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        saved.append(str(path))
    return saved


def refresh() -> dict:
    """抓取最新披露；歷史日期保留來源日期，不將新持股回填到過去。"""
    all_rows, top_rows, dates, failed, reused, fetch_failures = [], [], set(), [], [], []
    errors = {}
    prev = previous_rows()
    for etf_id, etf_name in ETFS:
        try:
            date, rows = parse(fetch(etf_id))
            if rows and not date:
                raise ValueError("持股資料缺少有效資料日期")
        except Exception as e:  # 網路錯誤：記錄後繼續
            date, rows = None, []
            errors[etf_id] = str(e)
            print(f"{etf_id} 抓取失敗：{e}", file=sys.stderr)
        if not rows:
            fetch_failures.append({"etf_id": etf_id, "error": errors.get(etf_id, "來源未回傳持股資料"),
                                   "reused_data_date": prev[etf_id][0][5] if prev.get(etf_id) else None})
        if not rows and prev.get(etf_id):
            date = prev[etf_id][0][5]
            rows = [r[:5] for r in prev[etf_id]]
            reused.append(f"{etf_id}（{date}）")
        if not rows:
            failed.append({"etf_id": etf_id, "etf_name": etf_name,
                           "date": date, "error": errors.get(etf_id, "來源未回傳持股資料")})
            print(f"{etf_id} {etf_name}: 無資料（缺漏日期：未取得）", file=sys.stderr)
            continue
        dates.add(date)
        for name, code, mkt, w, sh in rows:
            all_rows.append([etf_id, etf_name, date, code, mkt, name, w, sh, is_stock(name, code, mkt)])
        stocks = sorted((r for r in rows if is_stock(*r[:3])), key=lambda r: -r[3])[:rules.ACTIVE_SHARE_TOP_N]
        for rank, (name, code, mkt, w, sh) in enumerate(stocks, 1):
            top_rows.append([etf_id, etf_name, date, rank, code, mkt, name, w])
        print(f"{etf_id} {etf_name}: {date} 共 {len(rows)} 列，前{rules.ACTIVE_SHARE_TOP_N}大個股合計 {sum(r[3] for r in stocks):.2f}%")
        time.sleep(1)

    complete = not failed
    saved = _save_snapshot(max(dates), all_rows, top_rows, complete=complete) if dates else []
    result = {"complete": complete, "expected_etfs": [code for code, _ in ETFS],
              "observed_dates": sorted(dates), "reused_previous": reused,
              "fetch_failures": fetch_failures, "missing": failed, "saved_files": saved,
              "active_snapshot_written": complete,
              "updated_at": datetime.now(timezone.utc).isoformat()}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / ("active_etf_update_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    result["report_file"] = str(report)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"資料日期：{sorted(dates)}")
    if reused:
        print(f"⚠️  抓取失敗、沿用前次資料：{'、'.join(reused)}")
    if failed:
        print("❌ ETF 持股快照不完整；成功資料已另存 .partial 診斷檔，不會作為回測快照。缺漏：" +
              "、".join(f"{item['etf_id']}（{item['date'] or '日期未知'}）" for item in failed), file=sys.stderr)
    return result


def main():
    result = refresh()
    if not result["complete"]:
        sys.exit(f"❌ ETF 快照不完整；詳見 {result['report_file']}。研究回測會依嚴格歷史輸入規則處理缺漏。")


if __name__ == "__main__":
    main()
