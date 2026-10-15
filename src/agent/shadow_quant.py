"""平行研究模型：量價趨勢主導、基本面品質篩選、新聞固定。

本模組僅產生研究用排名，不能產生 target weights、交易決策、訂單或 D-Plan。
量價分數決定主排名；基本面只作為品質閘門；新聞分數沿用正式策略既有定義與
權重，不是研究調參對象。正式交易一律由 factor_engine.py 決定。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from . import research_config as R
from . import strategy as S
from .news_policy import news_veto


ROOT = Path(__file__).resolve().parents[2]
MODEL_VERSION = R.MODEL_VERSION
REQUIRED_COLUMNS = {"date", "ticker", "close", "volume", "value"}
NEWS_WEIGHT = S.SCORE_WEIGHTS["news_score"]
TECHNICAL_WEIGHT = 1.0 - NEWS_WEIGHT


def _percentile(series: pd.Series) -> pd.Series:
    """最低為 0、最高為 1；同值取平均名次，全部相同時取中性 0.5。"""
    valid = series.dropna()
    if len(valid) <= 1 or valid.nunique() <= 1:
        return pd.Series(0.5, index=series.index)
    return (series.rank(method="average") - 1) / (len(valid) - 1)


def _fundamental_quality(out: pd.DataFrame, fundamental: pd.DataFrame | None) -> pd.DataFrame:
    """加入基本面品質閘門，但不把基本面納入研究主排名。

    ``blocked`` 需要營收明顯衰退，且至少一項可得獲利品質指標為非正值，避免
    因單一缺值或金融業的中性欄位錯誤排除股票。``watch`` 不阻擋續抱／研究排名，
    但會在稽核表中標示供團隊檢視。
    """
    out["fundamental_available"] = False
    out["fundamental_quality_score"] = 0.5
    out["fundamental_status"] = "unavailable"
    out["fundamental_blocked"] = False
    if fundamental is None:
        return out

    f = fundamental.copy()
    if "ticker" in f.columns:
        f = f.set_index("ticker")
    f.index = f.index.astype(str)
    f = f.reindex(out.index)
    metrics = pd.DataFrame(index=out.index)
    for column in ("B1", "B2", "B4"):
        metrics[column] = pd.to_numeric(f[column], errors="coerce") if column in f else float("nan")
    available = metrics.notna().any(axis=1)
    quality = pd.concat([_percentile(metrics[column]) for column in metrics], axis=1).mean(axis=1).fillna(0.5)
    revenue_bad = metrics["B1"] <= R.REVENUE_BLOCK_THRESHOLD
    profitability_bad = ((metrics["B2"] <= R.PROFITABILITY_BLOCK_THRESHOLD)
                         | (metrics["B4"] <= R.PROFITABILITY_BLOCK_THRESHOLD))
    blocked = available & revenue_bad & profitability_bad
    watch = available & ~blocked & (
        (metrics["B1"] < 0) | (metrics["B2"] <= 0) | (metrics["B4"] <= 0)
    )
    out["fundamental_available"] = available
    out["fundamental_quality_score"] = quality
    out["fundamental_status"] = "eligible"
    out.loc[~available, "fundamental_status"] = "unavailable"
    out.loc[watch, "fundamental_status"] = "watch"
    out.loc[blocked, "fundamental_status"] = "blocked"
    out["fundamental_blocked"] = blocked
    return out


def _fixed_news(out: pd.DataFrame, news: pd.DataFrame | None,
                formal_a3: pd.Series | None = None) -> pd.DataFrame:
    """沿用正式新聞分數；歷史快照不存在時以正式回測相同的中性政策處理。"""
    out["news_score"] = 0.5
    out["news_policy"] = "fixed-neutral"
    out["news_veto"] = False
    if news is None:
        return out

    n = news.copy()
    if "ticker" in n.columns:
        n = n.set_index("ticker")
    n.index = n.index.astype(str)
    if not n.index.is_unique:
        raise ValueError("新聞快照同日股票重複")
    n = n.reindex(out.index)
    if "news_score" in n:
        score = pd.to_numeric(n["news_score"], errors="raise")
        if not score.dropna().between(0, 1).all():
            raise ValueError("新聞分數超出 0–1，不可自行裁切改變正式快照")
        out["news_score"] = score.fillna(S.NEUTRAL_SCORE)
    # 正式線的重大利空否決條件：high 利空且跌破 60 日均線。
    worst = n["worst_event"].fillna("").astype(str) if "worst_event" in n else pd.Series("", index=out.index)
    high = worst.str.startswith(f"{S.VETO_MATERIALITY}:")
    a3 = formal_a3.reindex(out.index) if formal_a3 is not None else pd.Series(float('nan'), index=out.index)
    if (high & a3.isna()).any():
        raise ValueError("重大利空否決缺少正式 A3；禁止用研究線未還原均線替代")
    out["news_veto"] = news_veto(worst, a3)
    out["news_policy"] = "fixed-production-score"
    return out


def read_news_snapshot(path: Path, as_of_date: str, *, require_dated: bool = False,
                       require_cutoff: bool = False) -> pd.DataFrame:
    """讀取正式線已封存的新聞分數快照，不自行重算或調整新聞參數。"""
    news = pd.read_csv(path, dtype={"ticker": str}, encoding="utf-8-sig")
    if "ticker" not in news or "news_score" not in news:
        raise ValueError("新聞快照需包含 ticker 與 news_score 欄位")
    date_column = next((column for column in ("as_of_date", "date", "data_date") if column in news), None)
    if require_dated and date_column is None:
        raise ValueError("多日研究回測的新聞快照需包含 as_of_date、date 或 data_date 欄位")
    cutoff_column = next((column for column in ("snapshot_cutoff", "cutoff_at", "news_cutoff") if column in news), None)
    if require_cutoff and cutoff_column is None:
        raise ValueError("多日新聞快照需包含正式截點 snapshot_cutoff／cutoff_at，禁止以事後新聞回填")
    if date_column is not None:
        news = news.loc[news[date_column].astype(str) == as_of_date]
    if cutoff_column is not None and not news.empty:
        cutoff = pd.to_datetime(news[cutoff_column], errors="coerce")
        if cutoff.isna().any() or (cutoff.dt.date.astype(str) != as_of_date).any():
            raise ValueError("新聞快照截點無效或與排名日不一致")
    return news


def compute_shadow_ranking(
    daily: pd.DataFrame,
    as_of_date: str,
    *,
    selected_count: int = 45,
    fundamental: pd.DataFrame | None = None,
    news: pd.DataFrame | None = None,
    formal_a3: pd.Series | None = None,
) -> pd.DataFrame:
    """從截至 ``as_of_date`` 的行情計算研究排名，不讀寫檔案。

    使用未調整價格是刻意的：此模型保留原量價研究專案的平行基準，並非可直接
    提交的正式因子。``fundamental`` 僅影響品質閘門；``news`` 的分數與重大
    利空否決規則固定沿用正式線。
    """
    missing = REQUIRED_COLUMNS - set(daily.columns)
    if missing:
        raise ValueError(f"shadow quant 缺少欄位：{sorted(missing)}")
    if selected_count <= 0:
        raise ValueError("selected_count 必須為正整數")

    frame = daily.loc[daily["date"] <= as_of_date, list(REQUIRED_COLUMNS)].copy()
    frame["ticker"] = frame["ticker"].astype(str)
    frame = frame.sort_values(["ticker", "date"])
    rows = []
    for ticker, group in frame.groupby("ticker", sort=True):
        group = group.dropna(subset=["close", "volume", "value"]).tail(61)
        if len(group) < 61 or group["date"].iloc[-1] != as_of_date:
            continue
        close = group["close"].astype(float).reset_index(drop=True)
        volume = group["volume"].astype(float).reset_index(drop=True)
        value = group["value"].astype(float).reset_index(drop=True)
        prior_volume = volume.iloc[-21:-1].mean()
        if prior_volume <= 0 or close.iloc[-61] <= 0:
            continue
        returns = close.pct_change().iloc[-20:]
        rows.append({
            "ticker": ticker,
            "as_of_date": as_of_date,
            "close": close.iloc[-1],
            "momentum_5d": close.iloc[-1] / close.iloc[-6] - 1,
            "momentum_20d": close.iloc[-1] / close.iloc[-21] - 1,
            "momentum_60d": close.iloc[-1] / close.iloc[-61] - 1,
            "price_to_sma_20d": close.iloc[-1] / close.iloc[-20:].mean() - 1,
            "price_to_sma_60d": close.iloc[-1] / close.iloc[-60:].mean() - 1,
            "volume_ratio_20d": volume.iloc[-1] / prior_volume,
            "average_turnover_20d": value.iloc[-20:].mean(),
            "volatility_20d": returns.std(ddof=0),
        })
    if not rows:
        raise ValueError(f"{as_of_date} 沒有任何具備 61 日完整資料的標的")

    out = pd.DataFrame(rows).set_index("ticker")
    out["relative_strength_20d"] = out["momentum_20d"] - out["momentum_20d"].median()
    p = {column: _percentile(out[column]) for column in (
        "momentum_5d", "momentum_20d", "momentum_60d", "price_to_sma_20d", "price_to_sma_60d",
        "volume_ratio_20d", "average_turnover_20d", "relative_strength_20d", "volatility_20d",
    )}
    out["momentum_score"] = (R.MOMENTUM_WEIGHTS["5d"] * p["momentum_5d"]
                              + R.MOMENTUM_WEIGHTS["20d"] * p["momentum_20d"]
                              + R.MOMENTUM_WEIGHTS["60d"] * p["momentum_60d"])
    out["trend_score"] = 0.50 * p["price_to_sma_20d"] + 0.50 * p["price_to_sma_60d"]
    out["volume_score"] = (R.VOLUME_WEIGHTS["ratio_20d"] * p["volume_ratio_20d"]
                           + R.VOLUME_WEIGHTS["turnover_20d"] * p["average_turnover_20d"])
    out["strength_score"] = p["relative_strength_20d"]
    out["risk_score"] = 1.0 - p["volatility_20d"]
    def weighted_score(weights: dict[str, float]) -> pd.Series:
        return sum(weights[name] * out[f"{name}_score"] for name in weights)

    # 兩組共用同一批原始訊號與後續篩選；控制實驗唯一可替換輸入是這個量價加權。
    out["baseline_technical_score"] = weighted_score(R.BASELINE_TECHNICAL_WEIGHTS)
    out["technical_score"] = weighted_score(R.EXPERIMENTAL_TECHNICAL_WEIGHTS)
    out["shadow_score"] = out["technical_score"]  # 相容既有研究輸出欄位。
    out = _fundamental_quality(out, fundamental)
    out = _fixed_news(out, news, formal_a3)
    out["baseline_research_score"] = (TECHNICAL_WEIGHT * out["baseline_technical_score"]
                                      + NEWS_WEIGHT * out["news_score"])
    out["research_score"] = TECHNICAL_WEIGHT * out["technical_score"] + NEWS_WEIGHT * out["news_score"]
    out["research_eligible"] = ~out["fundamental_blocked"] & ~out["news_veto"]
    out = out.sort_values(["research_score", "technical_score", "strength_score"], ascending=False, kind="stable")
    out["shadow_rank"] = range(1, len(out) + 1)
    out["selected"] = False
    eligible = out.index[out["research_eligible"]][:selected_count]
    out.loc[eligible, "selected"] = True
    out["baseline_selected"] = False
    baseline_order = out.sort_values(
        ["baseline_research_score", "baseline_technical_score", "strength_score"],
        ascending=False, kind="stable",
    )
    baseline_eligible = baseline_order.index[baseline_order["research_eligible"]][:selected_count]
    out.loc[baseline_eligible, "baseline_selected"] = True
    out["model_version"] = MODEL_VERSION
    return out.reset_index()


def main() -> None:
    parser = argparse.ArgumentParser(description="輸出研究用量價＋基本面品質＋固定新聞排名；不得用於正式下單")
    parser.add_argument("--date", required=True, help="資料基準日 YYYY-MM-DD")
    parser.add_argument("--selected-count", type=int, default=45)
    parser.add_argument("--news-csv", type=Path, help="選填：正式線封存的新聞分數快照 CSV（固定使用，不重算）")
    parser.add_argument("--out", type=Path, help="輸出 CSV；預設 output/research/")
    args = parser.parse_args()
    market = ROOT / "data" / "market" / "daily.csv"
    if not market.exists():
        raise SystemExit("找不到 data/market/daily.csv，請先完成行情更新")
    daily = pd.read_csv(market, dtype={"ticker": str}, encoding="utf-8-sig")
    # 研究線的基本面使用當日可得的歷史快照；單次排名沒有可用新聞快照時，
    # 依正式歷史回測政策固定為中性新聞分數。
    from .inputs import StoreInputs
    fundamental = StoreInputs(history=True).fundamental_for_date(args.date)
    news = read_news_snapshot(args.news_csv, args.date, require_cutoff=True) if args.news_csv else None
    from .datafeed import pv_factors
    formal_a3 = pv_factors.compute(daily, args.date)[0]["A3"] if news is not None else None
    result = compute_shadow_ranking(
        daily, args.date, selected_count=args.selected_count, fundamental=fundamental, news=news, formal_a3=formal_a3,
    )
    out = args.out or ROOT / "output" / "research" / f"shadow_quant_{args.date}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, encoding="utf-8-sig", index=False)
    print(f"已寫出研究用排名 {out}（{len(result)} 檔；不得用於正式下單）")


if __name__ == "__main__":
    main()
