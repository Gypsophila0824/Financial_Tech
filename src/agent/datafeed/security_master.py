"""建立股票主檔 data/security_master.csv。

來源：
  1. 官方 150 檔清單 PDF（交易標的範圍的唯一依據，不自行重算市值）
  2. 證交所 OpenAPI 上市公司基本資料（補產業別）
  3. 櫃買中心 OpenAPI 上櫃股票基本資料（補產業別）

API 原始回應另存於 data/universe_raw/，供查核資料時點。

執行：.venv/Scripts/python.exe -X utf8 -m src.agent.datafeed.security_master
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from pathlib import Path

import pdfplumber
import requests

ROOT = Path(__file__).resolve().parents[3]
UNIVERSE_PDF = ROOT / "比賽文件" / "玉山挑戰賽_投資組合及交易標的_150檔清單_v1.pdf"
RAW_DIR = ROOT / "data" / "universe_raw"
OUTPUT_CSV = ROOT / "data" / "security_master.csv"

TWSE_BASIC_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_BASIC_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

EXPECTED_COUNT = {"TWSE": 100, "TPEX": 50}
TSMC = "2330"
MAX_WEIGHT_TSMC = 0.25
MAX_WEIGHT_OTHER = 0.10

# 證交所／櫃買中心共用的產業別代碼
INDUSTRY_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險業", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療業",
    "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意業", "33": "農業科技業", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票", "91": "存託憑證",
}

ROW_PATTERN = re.compile(r"(\d{4})\s+(\S+)")


def parse_universe_pdf(path: Path) -> list[dict]:
    """讀取官方清單，依【上市】/【上櫃】段落標記市場別。"""
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    stocks, market = [], None
    for line in text.splitlines():
        if "【上市】" in line:
            market = "TWSE"
            continue
        if "【上櫃】" in line:
            market = "TPEX"
            continue
        if market is None:
            continue
        for ticker, name in ROW_PATTERN.findall(line):
            stocks.append({"ticker": ticker, "name": name, "market": market})
    return stocks


def fetch_json(url: str, retries: int = 3) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries:
                raise RuntimeError(f"無法取得 {url}：{exc}") from exc
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def save_raw(name: str, data: list[dict]) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_industry_maps() -> dict[str, dict[str, dict]]:
    twse = fetch_json(TWSE_BASIC_URL)
    tpex = fetch_json(TPEX_BASIC_URL)
    save_raw("twse_t187ap03_L.json", twse)
    save_raw("tpex_mopsfin_t187ap03_O.json", tpex)
    return {
        "TWSE": {
            r["公司代號"]: {"abbr": r["公司簡稱"], "industry_code": r["產業別"]} for r in twse
        },
        "TPEX": {
            r["SecuritiesCompanyCode"]: {
                "abbr": r["CompanyAbbreviation"],
                "industry_code": r["SecuritiesIndustryCode"],
            }
            for r in tpex
        },
    }


def build() -> list[dict]:
    stocks = parse_universe_pdf(UNIVERSE_PDF)
    errors: list[str] = []

    tickers = [s["ticker"] for s in stocks]
    dupes = sorted({t for t in tickers if tickers.count(t) > 1})
    if dupes:
        errors.append(f"清單中有重複代號：{dupes}")
    for market, expected in EXPECTED_COUNT.items():
        actual = sum(s["market"] == market for s in stocks)
        if actual != expected:
            errors.append(f"{market} 應有 {expected} 檔，實際解析出 {actual} 檔")

    industry_maps = load_industry_maps()
    rows, name_diffs = [], []
    for s in stocks:
        info = industry_maps[s["market"]].get(s["ticker"])
        if info is None:
            errors.append(f"{s['ticker']} {s['name']} 不在 {s['market']} 公司基本資料中（市場別錯誤或已下市？）")
            continue
        code = info["industry_code"]
        if code not in INDUSTRY_NAMES:
            errors.append(f"{s['ticker']} 產業代碼 {code} 不在對照表中")
        if info["abbr"] != s["name"]:
            name_diffs.append(f"{s['ticker']}: 清單「{s['name']}」/ API「{info['abbr']}」")
        rows.append({
            "ticker": s["ticker"],
            "name": s["name"],
            "market": s["market"],
            "industry_code": code,
            "industry": INDUSTRY_NAMES.get(code, ""),
            "is_eligible": "true",
            "max_weight": MAX_WEIGHT_TSMC if s["ticker"] == TSMC else MAX_WEIGHT_OTHER,
        })

    if errors:
        print("建檔失敗：", *errors, sep="\n  - ", file=sys.stderr)
        sys.exit(1)
    if name_diffs:
        # 名稱以官方清單為準，只提示差異
        print("提示：清單名稱與 API 簡稱不同（以官方清單為準）：", *name_diffs, sep="\n  - ")
    return rows


def main() -> None:
    rows = build()
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    by_industry: dict[str, int] = {}
    for r in rows:
        by_industry[r["industry"]] = by_industry.get(r["industry"], 0) + 1
    print(f"已輸出 {OUTPUT_CSV.relative_to(ROOT)}：共 {len(rows)} 檔")
    for industry, n in sorted(by_industry.items(), key=lambda kv: -kv[1]):
        print(f"  {industry}: {n}")


if __name__ == "__main__":
    main()
