"""計算 150 檔的量價因子與量價分數（factor_spec.md 第 2 節 A、第 3 節），以及配權重用的 60 日波動率。

輸入：data/market/daily.csv（fetch_market_data.py 產出）、universe/security_master.csv
輸出：data/factors/pv_factors_<日期>.csv

  A1 短期動能  close(t) / close(t-20) - 1
  A2 中期動能  close(t-5) / close(t-60) - 1
  A3 趨勢      close(t) / 60 日均價 - 1
  A4 法人買超  近 10 日法人買賣超金額 / 近 20 日平均成交金額
  vol60        近 60 日日報酬標準差（不進打分，供第 6 節配權重）
  各因子在 150 檔內做百分位排名（0 最差、1 最好，缺值給 0.5），pv_score ＝ 四者平均。

t 為計算基準日（預設資料中最新的交易日），「t-20」指往前第 20 個交易日。

A1–A3、vol60 使用「還原股價」：除權息、分割、減資當天的價格跳動不算漲跌。
  日報酬 ＝ 收盤價 ÷ 基準價 − 1
  基準價：上櫃 ＝ 前一交易日公布的次日參考價（daily.csv 的 ref_next）
          上市 ＝ 前一交易日收盤價；遇除權除息日（twse_exright.csv）則乘上 參考價 ÷ 除權息前收盤價
  事件日若休市（例如颱風假），於其後第一個交易日生效。
  仍超過 ±10.5% 的單日報酬（台股漲跌幅上限 10%，代表有未涵蓋的公司行動）視為 0 並列出警告。

執行 : python compute_pv_factors.py
       python compute_pv_factors.py --date 2026-10-01
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parents[3]  # 專案根目錄（原為程式所在資料夾）
DAILY = HERE / "data" / "market" / "daily.csv"
EXRIGHT = HERE / "data" / "market" / "twse_exright.csv"
SECURITY_MASTER = HERE / "data" / "security_master.csv"
OUT_DIR = HERE / "data" / "factors"

# factor_spec.md 標 ★ 的期間參數
MOM_SHORT = 20
MOM_LONG = 60
MOM_SKIP = 5
TREND_MA = 60
INSTI_DAYS = 10
VALUE_DAYS = 20
VOL_DAYS = 60

FACTORS = ["A1", "A2", "A3", "A4"]
MAX_DAILY_MOVE = 0.105


def adjusted_returns(df):
    """回傳還原後的日報酬表（index=日期、columns=代號）與未涵蓋事件的警告清單"""
    close = df.pivot(index="date", columns="ticker", values="close").sort_index()
    ref_next = df.pivot(index="date", columns="ticker", values="ref_next").sort_index()
    market = df.drop_duplicates("ticker").set_index("ticker")["market"]

    prev = close.ffill().shift(1)  # 前一個「有成交」的收盤價，停牌期間的漲跌併入復牌日
    base = prev.copy()
    tpex = market[market == "TPEX"].index
    tpex_ref = ref_next[tpex].ffill().shift(1)
    base[tpex] = tpex_ref.where(tpex_ref.notna(), prev[tpex])

    if EXRIGHT.exists():
        dates = list(close.index)
        for e in pd.read_csv(EXRIGHT, dtype={"ticker": str}, encoding="utf-8-sig").itertuples():
            eff = next((d for d in dates if d >= e.date), None)
            if eff is None or e.ticker not in base.columns or not e.pre_close:
                continue
            base.loc[eff, e.ticker] = prev.loc[eff, e.ticker] * e.ref_price / e.pre_close

    ret = close / base - 1
    jumps = ret.stack()  # pandas 3 的 stack 不會丟掉空值，需自行篩選
    jumps = jumps[jumps.abs() > MAX_DAILY_MOVE]
    warnings = [f"{d} {t} {r:+.1%}" for (d, t), r in jumps.items()]
    ret = ret.mask(ret.abs() > MAX_DAILY_MOVE, 0.0)
    return ret, warnings


def pct_rank(s):
    """150 檔內百分位排名：最小 0、最大 1、同值取平均；缺值給中性 0.5"""
    r = (s.rank(method="average") - 1) / (s.notna().sum() - 1)
    return r.fillna(0.5)


def compute(df, t):
    """以 daily.csv 的資料表 df 計算基準日 t 的量價因子；回傳 (因子表, 未涵蓋價格跳動警告)。不連網。"""
    dates = sorted(df["date"].unique())
    if t not in dates:
        raise ValueError(f"{t} 不是資料中的交易日")
    hist = dates[: dates.index(t) + 1]
    need = max(MOM_LONG, TREND_MA, VOL_DAYS) + 1
    if len(hist) < need:
        raise ValueError(f"歷史資料只有 {len(hist)} 個交易日，至少需要 {need} 天")

    df = df[df["date"].isin(hist)]
    close = df.pivot(index="date", columns="ticker", values="close").sort_index()
    value = df.pivot(index="date", columns="ticker", values="value").sort_index()
    insti = df.pivot(index="date", columns="ticker", values="insti_net_shares").sort_index()

    ret, warnings = adjusted_returns(df)
    # 只回報會影響因子的期間（最近 need 個交易日）；更早的事件不影響今天的因子，天天出現只會讓值班人忽略警告
    warnings = [w for w in warnings if w[:10] >= hist[-need]]
    # 還原股價指數：由還原日報酬連乘而得；上市前（從未成交）維持空值
    adj = (1 + ret.fillna(0)).cumprod().where(close.ffill().notna())

    def at(lag):
        """往前第 lag 個交易日的還原股價（lag=0 為 t）"""
        return adj.iloc[-1 - lag]

    f = pd.DataFrame(index=close.columns)
    f["A1"] = at(0) / at(MOM_SHORT) - 1
    f["A2"] = at(MOM_SKIP) / at(MOM_LONG) - 1
    f["A3"] = at(0) / adj.iloc[-TREND_MA:].mean() - 1
    insti_value = (insti * close).iloc[-INSTI_DAYS:].sum(min_count=1)
    f["A4"] = insti_value / value.iloc[-VALUE_DAYS:].mean()
    f["vol60"] = ret.iloc[-VOL_DAYS:].std()

    for c in FACTORS:
        f[f"{c}_rank"] = pct_rank(f[c])
    f["pv_score"] = f[[f"{c}_rank" for c in FACTORS]].mean(axis=1)

    master = pd.read_csv(SECURITY_MASTER, dtype={"ticker": str}, encoding="utf-8-sig").set_index("ticker")
    out = master[["name", "market", "industry"]].join(f).sort_values("pv_score", ascending=False)
    return out, warnings, len(hist)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="計算基準日 YYYY-MM-DD（預設：資料中最新交易日）")
    args = ap.parse_args()
    if not DAILY.exists():
        sys.exit("找不到 data/market/daily.csv，請先執行 python -m src.agent.datafeed.market")
    df = pd.read_csv(DAILY, dtype={"ticker": str}, encoding="utf-8-sig")
    t = args.date or sorted(df["date"].unique())[-1]
    try:
        out, warnings, n_hist = compute(df, t)
    except ValueError as e:
        sys.exit(str(e))
    hist = range(n_hist)
    f = out
    out.insert(0, "date", t)
    out = out.round({c: 4 for c in FACTORS + ["vol60", "pv_score"] + [f"{c}_rank" for c in FACTORS]})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"pv_factors_{t}.csv"
    out.to_csv(out_path, encoding="utf-8-sig", index_label="ticker")

    missing = {c: int(f[c].isna().sum()) for c in FACTORS + ["vol60"]}
    print(f"已寫出 {out_path.relative_to(HERE)}（基準日 {t}，使用 {len(hist)} 個交易日歷史）")
    print(f"缺值檔數：{missing}")
    if warnings:
        print(f"未涵蓋的價格跳動（已視為 0，請確認是否為減資、新上市等）：{warnings}")


if __name__ == "__main__":
    main()
