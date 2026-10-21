"""抓取月營收、營益分析與財務報表，計算 150 檔的基本面因子與基本面分數（factor_spec.md 第 2 節 B、第 3 節）。

  B1 營收成長  最新一期月營收「去年同月增減(%)」
  B2 獲利能力  最新一季營業利益率(%)
  B3 營收加速  B1 −「累計營收前期比較增減(%)」（當月年增率 − 今年累計年增率）
               > 0 代表當月成長快於今年以來的平均，即成長正在加快（基本面版本的動能）。
               兩者都在同一份月營收資料內，不需另外抓取。1 月當月即累計，B3 全為 0（不含資訊）。
  B4 ROE       年化股東權益報酬率(%) ＝ 淨利歸屬於母公司業主（今年累計）× 4 ÷ 季別 ÷ 歸屬於母公司業主之權益
               財報數字為今年累計（第 2 季 ＝ 上半年），故乘 4 ÷ 季別年化；權益 ≤ 0 不計算。
               財報依產業分格式（一般業、銀行、證券期貨、金控、保險、異業），金融股也有，
               是金融股唯一有資料的基本面因子。全部 150 檔一起排名（金融業 ROE 天生較低，不另外分組）。
  各因子在 150 檔內做百分位排名（0 最差、1 最好，缺值給 0.5）。正式 fund_score 依 strategy.py
  僅以 B1 營收成長與 B3 營收加速各 50% 計算；B2、B4 保留供診斷及其他研究使用。
  金融保險業的 B1、B3 一律給中性分（營收含投資損益，與一般公司不可比）；
  B2 則因金融業、異業財報無營業利益科目而查無資料，同樣給中性分。

來源（證交所／櫃買 OpenAPI，只提供最新一期，每次一個請求拿到全部公司）：
  上市月營收 t187ap05_L、上櫃月營收 mopsfin_t187ap05_O
  上市營益   t187ap17_L、上櫃營益   mopsfin_187ap17_O
  上市財報   t187ap06_L_<產業>（綜合損益表）、t187ap07_L_<產業>（資產負債表）
  上櫃財報   mopsfin_t187ap06_O_<產業>、mopsfin_t187ap07_O_<產業>
注意：OpenAPI 的月營收在全體公司申報期限（每月 10 日）後才整批更新，會比個別公司公布晚數天。

抓取失敗時：沿用最近一次存下的同名原始資料（MAX_FALLBACK_DAYS 天內），複製進今天的資料夾並印出警告。
  月營收每月、財報每季才更新，用前幾天存下的版本結果相同；資料內的「出表日期」仍是原始公布日，
  D-Plan 引用的來源時間因此正確。2026/10/3 櫃買中心全站故障約 5 小時，即為此設計的情境。
  找不到存檔時：月營收、營益分析停止（B1–B3 無從計算）；財報只讓 B4 給中性分，不中斷流程。

輸出：
  data/fundamental/<執行日>/*.json        原始回應留存（D-Plan 引用與事後核對用）
  data/factors/fund_factors_<執行日>.csv

執行 : python compute_fund_factors.py
"""
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .. import strategy

HERE = Path(__file__).resolve().parents[3]  # 專案根目錄（原為程式所在資料夾）
SECURITY_MASTER = HERE / "data" / "security_master.csv"
RAW_DIR = HERE / "data" / "fundamental"
OUT_DIR = HERE / "data" / "factors"

SOURCES = {
    "twse_revenue": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "tpex_revenue": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
    "twse_margin": "https://openapi.twse.com.tw/v1/opendata/t187ap17_L",
    "tpex_margin": "https://www.tpex.org.tw/openapi/v1/mopsfin_187ap17_O",
}
FACTORS = ["B1", "B2", "B3", "B4"]


def production_score(frame: pd.DataFrame) -> pd.Series:
    """依正式策略設定的基本面子因子權重計算分數；排名缺值已由 pct_rank 設為中性 0.5。"""
    weights = strategy.FUND_SCORE_WEIGHTS
    missing = set(weights) - set(frame.columns)
    if missing:
        raise ValueError(f"正式基本面分數缺少欄位：{sorted(missing)}")
    return sum(pd.to_numeric(frame[column], errors="coerce").fillna(0.5) * weight
               for column, weight in weights.items())
MAX_FALLBACK_DAYS = 40    # 抓取失敗時，最多沿用幾天前存下的原始資料（月營收每月更新一次）★
STATEMENT_KINDS = ["ci", "basi", "bd", "fh", "ins", "mim"]   # 一般業、銀行、證券期貨、金控、保險、異業
# 欄名在各產業、上市上櫃間略有差異：「淨利（淨損）／淨利（損）歸屬於母公司業主」、「歸屬於母公司業主（之）權益（合計）」。
# 沒有子公司的公司（如采鈺、聯亞、精材）歸屬欄位空白，只填本期淨利與權益總計；無非控制權益時兩者相同，依序改用。
NET_INCOME_FIELDS = [r"淨利（.+）歸屬於母公司業主", r"本期(稅後)?淨利（淨損）"]
EQUITY_FIELDS = [r"歸屬於母公司業主之?權益(合計)?", r"權益總(計|額)"]


def statement_url(market, table, kind):
    """market：twse／tpex；table：is（綜合損益表）／bs（資產負債表）；kind：STATEMENT_KINDS"""
    code = {"is": "06", "bs": "07"}[table]
    if market == "twse":
        return f"https://openapi.twse.com.tw/v1/opendata/t187ap{code}_L_{kind}"
    return f"https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap{code}_O_{kind}"


def get_json(url, attempts=3):
    """回應偶爾會傳到一半被切斷，失敗時隔幾秒重試"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except (http.client.IncompleteRead, urllib.error.URLError, json.JSONDecodeError):
            if i == attempts - 1:
                raise
            time.sleep(5)


def fetch_or_fallback(name, url, raw_dir, fallbacks):
    """抓取並存成 raw_dir/<name>.json；失敗時改用最近一次存下的同名檔（MAX_FALLBACK_DAYS 天內），
    同樣寫進 raw_dir，下游（generate_dplan.py 讀出表日期）不受影響。找不到存檔則拋出原本的例外。"""
    try:
        rows = get_json(url)
    except Exception as e:
        oldest = (date.today() - timedelta(days=MAX_FALLBACK_DAYS)).isoformat()
        saved = sorted((p for p in RAW_DIR.glob(f"*/{name}.json")
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.parent.name) and p.parent.name >= oldest),
                       key=lambda p: p.parent.name)
        if not saved:
            raise
        rows = json.loads(saved[-1].read_text(encoding="utf-8"))
        fallbacks.append(f"{name}（{type(e).__name__}，改用 {saved[-1].parent.name} 的存檔）")
    with open(raw_dir / f"{name}.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return rows


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def roc_month(s):
    """'11508' → '2026-08'"""
    return f"{int(s[:-2]) + 1911}-{s[-2:]}"


def parse_statement(rows, fields):
    """{代號: (數值, '2026Q2')}；fields 依序比對，取第一個有值的欄位。代號、年度、季別欄名上市為中文、上櫃部分為英文"""
    out = {}
    for r in rows:
        r = {k.strip(): v for k, v in r.items()}
        code = (r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
        year, season = r.get("年度") or r.get("Year"), r.get("季別") or r.get("Season")
        value = next((num(r[k]) for field in fields for k in r
                      if re.fullmatch(field, k) and num(r[k]) is not None), None)
        if code and year and season:
            out[code] = (value, f"{int(year) + 1911}Q{season}")
    return out


def annualized_roe(income, equity):
    """(淨利, 季別) 與 (權益, 季別) → (ROE %, 季別)；期別不同或權益 ≤ 0 時 ROE 為 None"""
    (ni, q), (eq, q_eq) = income, equity
    if ni is None or eq is None or eq <= 0 or q != q_eq:
        return None, q
    return ni * 4 / int(q[-1]) / eq * 100, q


def pct_rank(s):
    """150 檔內百分位排名：最小 0、最大 1、同值取平均；缺值給中性 0.5"""
    r = (s.rank(method="average") - 1) / (s.notna().sum() - 1)
    return r.fillna(0.5)


def fetch_all(raw_dir):
    """抓取月營收、營益分析與各產業財報存到 raw_dir；回傳 (fallbacks, failed)。財報失敗只影響 B4。"""
    fallbacks, failed = [], []
    for name, url in SOURCES.items():
        fetch_or_fallback(name, url, raw_dir, fallbacks)
    for market in ("twse", "tpex"):
        for kind in STATEMENT_KINDS:
            try:
                for t in ("is", "bs"):
                    fetch_or_fallback(f"{market}_{t}_{kind}", statement_url(market, t, kind), raw_dir, fallbacks)
            except Exception as e:  # noqa: BLE001 財報是加分項，抓不到也沒有存檔時不中斷
                failed.append(f"{market}_{kind}（{type(e).__name__}）")
    return fallbacks, failed


def compute(raw_dir):
    """由 raw_dir 內已存的原始資料計算 B1–B4 與 fund_score（不連網，供每日管線、彩排與重現共用）。"""
    def load(name):
        p = raw_dir / f"{name}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    raw = {name: load(name) for name in SOURCES}
    missing = [n for n, v in raw.items() if v is None]
    if missing:
        raise FileNotFoundError(f"{raw_dir} 缺少 {missing}，月營收／營益分析無法計算")

    # 上市、上櫃月營收欄位相同
    def parse_revenue(rows):
        return {r["公司代號"].strip(): (num(r["營業收入-去年同月增減(%)"]), roc_month(r["資料年月"]),
                                     num(r["累計營業收入-前期比較增減(%)"])) for r in rows}

    revenue = parse_revenue(raw["twse_revenue"] + raw["tpex_revenue"])
    # OpenAPI 換月時整批更新、但新月份表可能還不完整（2026-10-12 的 9 月表上市只有 869 家，台達電等 34 檔未列入）。
    # 不在最新表的股票沿用較早存檔中最近一期已公布的月營收，與回測 fund_history（每檔取最新已公布一期）口徑一致。
    oldest = (date.fromisoformat(raw_dir.name) - timedelta(days=MAX_FALLBACK_DAYS)).isoformat() \
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_dir.name) else None
    revenue_fallback = []
    if oldest:
        master_codes = set(pd.read_csv(SECURITY_MASTER, dtype={"ticker": str}, encoding="utf-8-sig")["ticker"])
        for d in sorted((p for p in RAW_DIR.glob("????-??-??") if oldest <= p.name < raw_dir.name), reverse=True):
            todo = master_codes - revenue.keys()
            if not todo:
                break
            rows = []
            for name in ("twse_revenue", "tpex_revenue"):
                p = d / f"{name}.json"
                if p.exists():
                    rows += json.loads(p.read_text(encoding="utf-8"))
            older = {c: v for c, v in parse_revenue(rows).items() if c in todo}
            revenue.update(older)
            revenue_fallback += [f"{c}（{v[1]}，{d.name} 存檔）" for c, v in sorted(older.items())]

    # 營益分析：上市為中文欄名，上櫃為英文欄名
    margin = {}
    for r in raw["twse_margin"]:
        margin[r["公司代號"].strip()] = (num(r["營業利益率(%)(營業利益)/(營業收入)"]), f"{int(r['年度']) + 1911}Q{r['季別']}")
    for r in raw["tpex_margin"]:
        margin[r["SecuritiesCompanyCode"].strip()] = (num(r["營業利益率"]), f"{int(r['Year']) + 1911}Q{r['季別']}")

    # 財報：逐產業格式讀綜合損益表與資產負債表；缺檔只影響 B4
    roe, roe_src = {}, {}
    for market in ("twse", "tpex"):
        for kind in STATEMENT_KINDS:
            tables = {t: load(f"{market}_{t}_{kind}") for t in ("is", "bs")}
            if None in tables.values():
                continue
            income = parse_statement(tables["is"], NET_INCOME_FIELDS)
            equity = parse_statement(tables["bs"], EQUITY_FIELDS)
            for code in income.keys() & equity.keys():
                roe[code] = annualized_roe(income[code], equity[code])
                roe_src[code] = f"{market}:{kind}"

    master = pd.read_csv(SECURITY_MASTER, dtype={"ticker": str}, encoding="utf-8-sig").set_index("ticker")
    f = master[["name", "market", "industry"]].copy()
    f["rev_month"] = [revenue.get(t, (None, None, None))[1] for t in f.index]
    f["B1"] = [revenue.get(t, (None, None, None))[0] for t in f.index]
    f["rev_ytd_yoy"] = pd.to_numeric([revenue.get(t, (None, None, None))[2] for t in f.index])
    f["op_quarter"] = [margin.get(t, (None, None))[1] for t in f.index]
    f["B2"] = [margin.get(t, (None, None))[0] for t in f.index]
    f["B1"] = pd.to_numeric(f["B1"])
    f["B3"] = f["B1"] - f["rev_ytd_yoy"]
    f["roe_quarter"] = [roe.get(t, (None, None))[1] for t in f.index]
    f["B4"] = pd.to_numeric([roe.get(t, (None, None))[0] for t in f.index])
    f["roe_src"] = [roe_src.get(t) for t in f.index]  # 供 D-Plan 引用對應的財報來源
    for c in FACTORS:
        f[c] = pd.to_numeric(f[c])
    # 金融股營收含投資損益、隨股市大幅波動，與一般公司營收成長不可比，B1、B3 給中性分
    f.loc[f["industry"] == "金融保險業", ["B1", "B3"]] = None
    for c in FACTORS:
        f[f"{c}_rank"] = pct_rank(f[c])
    f["fund_score"] = production_score(f)
    f.attrs["revenue_fallback"] = revenue_fallback
    return f


def main():
    run_date = date.today().isoformat()
    raw_dir = RAW_DIR / run_date
    raw_dir.mkdir(parents=True, exist_ok=True)
    fallbacks, failed = fetch_all(raw_dir)
    f = compute(raw_dir)

    out = f.sort_values("fund_score", ascending=False).round(4)
    out.insert(0, "date", run_date)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"fund_factors_{run_date}.csv"
    out.to_csv(out_path, encoding="utf-8-sig", index_label="ticker")

    print(f"已寫出 {out_path.relative_to(HERE)}；原始資料存於 {raw_dir.relative_to(HERE)}")
    print(f"月營收資料月份：{f['rev_month'].value_counts(dropna=False).to_dict()}")
    if f.attrs["revenue_fallback"]:
        print(f"最新月營收表未列入、沿用前期已公布者 {len(f.attrs['revenue_fallback'])} 檔")
    print(f"營益分析季別：{f['op_quarter'].value_counts(dropna=False).to_dict()}")
    print(f"財報（ROE）季別：{f['roe_quarter'].value_counts(dropna=False).to_dict()}")
    for msg in fallbacks:
        print(f"⚠️  抓取失敗，沿用存檔：{msg}")
    if failed:
        print(f"⚠️  財報抓取失敗且無存檔（B4 給中性分）：{'、'.join(failed)}")
    for c in FACTORS:
        miss = f[f[c].isna()]
        if len(miss):
            print(f"{c} 缺值 {len(miss)} 檔（給中性分 0.5）：{', '.join(miss.index + miss['name'])}")


if __name__ == "__main__":
    main()
