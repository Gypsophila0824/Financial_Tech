"""研究線的量價控制實驗與資料完整性稽核；絕不產生交易或 D-Plan。"""
from __future__ import annotations

import argparse
import json
import hashlib
from pathlib import Path
from typing import Callable

import pandas as pd

from .shadow_quant import MODEL_VERSION, compute_shadow_ranking, read_news_snapshot
from .metrics import selection_change
from . import research_config as R
from .datafeed.benchmark_prices import BENCHMARKS, fetch_prices

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDING_PERIODS = (5, 20, 60)
VARIANTS = {
    "baseline": ("technical_score", "baseline_research_score", "baseline_selected"),
    "experiment": ("technical_score", "research_score", "selected"),
}


def _market_dates(daily: pd.DataFrame) -> list[str]:
    return sorted(daily["date"].dropna().astype(str).unique())


def _formal_a3(daily: pd.DataFrame, as_of_date: str) -> pd.Series:
    """A3 僅能來自正式價格還原因子，不能用研究線均線替代。"""
    from .datafeed import pv_factors
    return pv_factors.compute(daily, as_of_date)[0]["A3"]


def _returns(daily: pd.DataFrame, as_of: str, future: str, tickers: set[str]) -> tuple[pd.Series, dict[str, str]]:
    """不補零、不補選；缺價保留原因，報酬只涵蓋兩端皆有效價格者。"""
    def closes(day: str) -> pd.Series:
        frame = daily.loc[daily["date"] == day, ["ticker", "close"]].drop_duplicates("ticker").set_index("ticker")["close"]
        frame.index = frame.index.astype(str)
        return pd.to_numeric(frame, errors="coerce")
    now, end = closes(as_of), closes(future)
    values, missing = {}, {}
    for ticker in sorted(tickers):
        start, stop = now.get(ticker), end.get(ticker)
        if pd.isna(start) or start <= 0:
            missing[ticker] = "ranking_price_missing"
        elif ticker not in end.index:
            missing[ticker] = "missing_quote_pending_review"
        elif pd.isna(stop) or stop <= 0:
            missing[ticker] = "future_price_missing"
        else:
            values[ticker] = float(stop / start - 1.0)
    return pd.Series(values, dtype=float), missing


def _rank_ic(scores: pd.Series, returns: pd.Series) -> float | None:
    common = scores.index.intersection(returns.index)
    if len(common) < 2:
        return None
    value = scores.loc[common].rank(method="average").corr(returns.loc[common].rank(method="average"), method="pearson")
    return None if pd.isna(value) else float(value)


def _record(*, as_of: str, future: str | None, period: int, variant: str, ranking: pd.DataFrame | None,
            selected: set[str], returns: pd.Series | None, missing: dict[str, str], intended: int,
            unavailable_reason: str = "") -> dict:
    """一個排名日／持有期／組別一列，未成熟與無法排名都不能消失。"""
    base = {"as_of_date": as_of, "future_date": future, "holding_period_days": period, "variant": variant,
            "model_version": MODEL_VERSION, "intended_selected_count": intended, "actual_selected_count": len(selected),
            "evaluable_selected_count": 0, "excluded_selected_count": len(selected), "coverage_rate": 0.0,
            "missing_tickers": json.dumps(sorted(missing), ensure_ascii=False),
            "missing_reasons": json.dumps(missing, ensure_ascii=False, sort_keys=True),
            "selected_return": None, "universe_return": None, "excess_return": None, "selected_win_rate": None,
            "technical_rank_ic": None, "combined_rank_ic": None, "rank_ic_evaluable_count": 0,
            "news_policy": None, "turnover_rate": None}
    if ranking is None:
        return base | {"ranking_status": "unavailable", "ranking_reason": unavailable_reason, "integrity_status": "ranking_unavailable"}
    base["news_policy"] = ranking["news_policy"].iloc[0]
    if not selected:
        return base | {'ranking_status': 'available', 'ranking_reason': '', 'integrity_status': 'no_selection'}
    if returns is None:
        return base | {"ranking_status": "available", "ranking_reason": "", "integrity_status": "not_mature"}
    score_tech, score_all, _ = VARIANTS[variant]
    indexed = ranking.set_index("ticker")
    evaluable = sorted(selected & set(returns.index))
    base.update({"ranking_status": "available", "ranking_reason": "", "evaluable_selected_count": len(evaluable),
                 "excluded_selected_count": len(selected) - len(evaluable),
                 "coverage_rate": len(evaluable) / len(selected) if selected else 0.0,
                 "rank_ic_evaluable_count": int(len(indexed.index.intersection(returns.index))),
                 "technical_rank_ic": _rank_ic(indexed[score_tech], returns),
                 "combined_rank_ic": _rank_ic(indexed[score_all], returns)})
    if len(evaluable) != len(selected):
        return base | {"integrity_status": "partial_coverage"}
    # 關鍵：選股少一檔都不計算投組平均，避免刪除缺值股票後重新平均。
    chosen = returns.reindex(evaluable)
    universe = returns.reindex(indexed.index.intersection(returns.index))
    return base | {"integrity_status": "complete", "selected_return": float(chosen.mean()),
                   "universe_return": float(universe.mean()), "excess_return": float(chosen.mean() - universe.mean()),
                   "selected_win_rate": float((chosen > 0).mean())}


def run_shadow_backtest(
    daily: pd.DataFrame, *, selected_count: int = 45, holding_periods: tuple[int, ...] = DEFAULT_HOLDING_PERIODS,
    fundamental_loader: Callable[[str], pd.DataFrame | None] | None = None,
    news_loader: Callable[[str], pd.DataFrame | None] | None = None,
    formal_a3_loader: Callable[[str], pd.Series] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """只替換量價加權的 paired control；所有其他輸入由同一次排名共用。"""
    dates = _market_dates(daily)
    if not holding_periods or any(x <= 0 for x in holding_periods) or len(set(holding_periods)) != len(holding_periods):
        raise ValueError("holding_periods 必須為不重複的正整數")
    audits, rows, unavailable = [], [], []
    previous: dict[str, set[str] | None] = {variant: None for variant in VARIANTS}
    tickers = set(daily["ticker"].astype(str))
    for day_index, as_of in enumerate(dates):
        ranking, reason = None, ""
        try:
            fundamental = fundamental_loader(as_of) if fundamental_loader else None
            news = news_loader(as_of) if news_loader else None
            if news is not None and formal_a3_loader is None:
                raise ValueError("研究回測使用新聞快照時必須傳入正式還原價格 A3")
            formal_a3 = formal_a3_loader(as_of) if news is not None else None
            ranking = compute_shadow_ranking(daily, as_of, selected_count=selected_count, fundamental=fundamental,
                                             news=news, formal_a3=formal_a3)
            ranking["ranking_status"], ranking["ranking_reason"] = "available", ""
            audits.append(ranking)
        except ValueError as exc:
            reason = str(exc); unavailable.append(as_of)
            audits.append(pd.DataFrame([{"as_of_date": as_of, "ranking_status": "unavailable", "ranking_reason": reason}]))
        for variant, (_, _, selected_col) in VARIANTS.items():
            selected = set() if ranking is None else set(ranking.loc[ranking[selected_col], "ticker"].astype(str))
            change = None if previous[variant] is None or ranking is None else selection_change(previous[variant], selected)
            if ranking is not None:
                previous[variant] = selected
            for period in holding_periods:
                target_index = day_index + period
                if ranking is None:
                    row = _record(as_of=as_of, future=None, period=period, variant=variant, ranking=None, selected=selected,
                                  returns=None, missing={}, intended=selected_count, unavailable_reason=reason)
                elif target_index >= len(dates):
                    row = _record(as_of=as_of, future=None, period=period, variant=variant, ranking=ranking, selected=selected,
                                  returns=None, missing={}, intended=selected_count)
                else:
                    future = dates[target_index]
                    returns, all_missing = _returns(daily, as_of, future, tickers)
                    selected_missing = {ticker: all_missing[ticker] for ticker in selected if ticker in all_missing}
                    row = _record(as_of=as_of, future=future, period=period, variant=variant, ranking=ranking, selected=selected,
                                  returns=returns, missing=selected_missing, intended=selected_count)
                # No positions or fills in this signal study: never invent executed turnover.
                row["turnover_rate"] = None
                row["selection_change_rate"] = change
                row["turnover_definition"] = "not_available_signal_only"
                row["selection_shortfall"] = max(0, selected_count - len(selected))
                rows.append(row)
    return pd.concat(audits, ignore_index=True, sort=False), pd.DataFrame(rows), unavailable


def selected_difference_report(audit: pd.DataFrame) -> pd.DataFrame:
    records = []
    for as_of, group in audit.groupby("as_of_date"):
        if not {"ticker", "baseline_selected", "selected"} <= set(group.columns):
            continue
        base = set(group.loc[group["baseline_selected"].fillna(False), "ticker"].astype(str))
        experiment = set(group.loc[group["selected"].fillna(False), "ticker"].astype(str))
        records.append({"as_of_date": as_of, "baseline_selected_count": len(base), "experiment_selected_count": len(experiment),
                        "overlap_count": len(base & experiment), "baseline_only": " ".join(sorted(base - experiment)),
                        "experiment_only": " ".join(sorted(experiment - base))})
    return pd.DataFrame(records)


def paired_evaluable_report(backtest: pd.DataFrame) -> pd.DataFrame:
    key = ["as_of_date", "future_date", "holding_period_days"]
    base = backtest.loc[backtest["variant"] == "baseline"].set_index(key)
    experiment = backtest.loc[backtest["variant"] == "experiment"].set_index(key)
    rows = []
    for item in base.index.intersection(experiment.index):
        left, right = base.loc[item], experiment.loc[item]
        complete = left["integrity_status"] == right["integrity_status"] == "complete"
        rows.append(dict(zip(key, item)) | {"both_complete": complete,
            "baseline_selected_return": left["selected_return"], "experiment_selected_return": right["selected_return"],
            "selected_return_difference": None if not complete else float(right["selected_return"] - left["selected_return"]),
            "baseline_technical_rank_ic": left["technical_rank_ic"], "experiment_technical_rank_ic": right["technical_rank_ic"],
            "baseline_combined_rank_ic": left["combined_rank_ic"], "experiment_combined_rank_ic": right["combined_rank_ic"]})
    return pd.DataFrame(rows)


def benchmark_comparison_report(paired: pd.DataFrame, prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compare both strategies with price-only ETF returns on identical dates."""
    usable = paired.loc[paired["both_complete"]].copy()
    indexed = (prices.set_index(["ticker", "date"])["close"] if not prices.empty
               else pd.Series(dtype=float))
    rows = []
    for item in usable.itertuples(index=False):
        for ticker in BENCHMARKS:
            start = indexed.get((ticker, item.as_of_date))
            end = indexed.get((ticker, item.future_date))
            value = float(end / start - 1) if pd.notna(start) and pd.notna(end) and start > 0 else None
            reason = "" if value is not None else (
                "missing_start_close" if pd.isna(start) else "missing_future_close")
            rows.append({"as_of_date": item.as_of_date, "future_date": item.future_date,
                         "holding_period_days": int(item.holding_period_days), "benchmark": ticker,
                         "baseline_selected_return": item.baseline_selected_return,
                         "experiment_selected_return": item.experiment_selected_return,
                         "benchmark_price_return": value,
                         "baseline_excess_vs_benchmark": (None if value is None else item.baseline_selected_return-value),
                         "experiment_excess_vs_benchmark": (None if value is None else item.experiment_selected_return-value),
                         "coverage_status": "complete" if value is not None else reason})
    detail = pd.DataFrame(rows, columns=["as_of_date", "future_date", "holding_period_days", "benchmark",
        "baseline_selected_return", "experiment_selected_return", "benchmark_price_return",
        "baseline_excess_vs_benchmark", "experiment_excess_vs_benchmark", "coverage_status"])
    summaries = []
    periods = sorted(paired["holding_period_days"].dropna().astype(int).unique()) if not paired.empty else []
    for period in periods:
        for ticker in BENCHMARKS:
            group = detail.loc[(detail["holding_period_days"] == period) & (detail["benchmark"] == ticker)]
            valid = group.loc[group["benchmark_price_return"].notna()]
            summaries.append({"holding_period_days": int(period), "benchmark": ticker,
                "paired_dates": int(len(group)), "benchmark_covered_dates": int(len(valid)),
                "baseline_mean_return": None if valid.empty else float(valid["baseline_selected_return"].mean()),
                "experiment_mean_return": None if valid.empty else float(valid["experiment_selected_return"].mean()),
                "benchmark_mean_price_return": None if valid.empty else float(valid["benchmark_price_return"].mean()),
                "baseline_mean_excess": None if valid.empty else float(valid["baseline_excess_vs_benchmark"].mean()),
                "experiment_mean_excess": None if valid.empty else float(valid["experiment_excess_vs_benchmark"].mean())})
    return detail, pd.DataFrame(summaries)


def research_report_markdown(summary: dict, benchmark_summary: pd.DataFrame) -> str:
    baseline_weights = "/".join(f"{100*R.BASELINE_RESEARCH_WEIGHTS[k]:g}" for k in ("technical", "fundamental", "news"))
    experiment_weights = "/".join(f"{100*R.EXPERIMENTAL_RESEARCH_WEIGHTS[k]:g}" for k in ("technical", "fundamental", "news"))
    lines = ["# 研究線回測報告", "", f"- 模型版本：`{summary['model_version']}`",
             f"- 範圍：全歷史行情；前瞻期間 {', '.join(str(x) for x in sorted(summary.get('periods', [])))} 日",
             f"- 總分權重（量價／基本面／新聞）：對照 {baseline_weights}%；研究候選 {experiment_weights}%",
             "- 本報告為訊號研究，不含成交、費稅或正式投組 NAV。", "",
             "## 與 ETF 價格報酬比較", "",
             "以下以相同排名日與前瞻終點計算策略入選股等權報酬及 ETF 收盤價報酬；區間重疊，樣本不是獨立觀測。ETF 僅計價格變動，不含配息與交易成本。", "",
             "| 前瞻日 | 基準 | 有效配對／完整策略配對 | 對照策略平均 | 研究策略平均 | ETF 平均價格報酬 | 研究策略超額 |",
             "|---:|---|---:|---:|---:|---:|---:|"]
    if benchmark_summary.empty:
        lines.append("| — | — | 0／— | — | — | — | — |")
        lines += ["", "ETF 行情尚未取得；請檢查 benchmark_sources.json。缺少價格時不填零報酬。"]
    else:
        for row in benchmark_summary.itertuples(index=False):
            fmt = lambda value: "—" if pd.isna(value) else f"{value:.2%}"
            lines.append(f"| {row.holding_period_days} | {row.benchmark} | {row.benchmark_covered_dates}/{row.paired_dates} | "
                         f"{fmt(row.baseline_mean_return)} | {fmt(row.experiment_mean_return)} | "
                         f"{fmt(row.benchmark_mean_price_return)} | {fmt(row.experiment_mean_excess)} |")
    counts = summary.get("coverage_disclosure", {}).get("integrity_status_counts", {})
    lines += ["", "## 覆蓋與稽核", "",
              f"研究紀錄完整 {counts.get('complete', 0)} 筆、排名不可用 {counts.get('ranking_unavailable', 0)} 筆、尚未成熟 {counts.get('not_mature', 0)} 筆、覆蓋不完整 {counts.get('partial_coverage', 0)} 筆。未成熟或缺值不轉成零報酬。",
              "", "輸出包含 `benchmark_prices.csv`、`benchmark_sources.json`、`benchmark_comparison.csv` 與 `benchmark_summary.csv`，可追溯比較價格、來源網址及回應雜湊。", ""]
    return "\n".join(lines)


def summarize_backtest(backtest: pd.DataFrame, unavailable: list[str]) -> dict:
    summary = {"model_version": MODEL_VERSION, "ranking_unavailable_dates": unavailable, "records": int(len(backtest)),
               "strategy_scope": "訊號層控制實驗與完整性稽核，不是正式投組績效，不得用於下單或 D-Plan。",
            "control_invariants": ["同一候選池與排名日", "同一基本面資料、品質閘門與基本面分數算法", "同一正式新聞快照、截點、分數與 A3 否決", "同一入選數與直接排名規則；研究線沒有正式續抱／配權／成本邏輯", "兩組唯一差異為研究總分中量價／基本面配重"],
               "by_holding_period": {}, "coverage_disclosure": {}}
    for (variant, period), group in backtest.groupby(["variant", "holding_period_days"]):
        complete = group.loc[group["integrity_status"] == "complete"]
        summary["by_holding_period"][f"{variant}_{period}d"] = {"records": int(len(group)), "complete_records": int(len(complete)),
            "mean_technical_rank_ic": None if group["technical_rank_ic"].dropna().empty else float(group["technical_rank_ic"].mean()),
            "mean_combined_rank_ic": None if group["combined_rank_ic"].dropna().empty else float(group["combined_rank_ic"].mean()),
            "mean_selected_return_complete_only": None if complete.empty else float(complete["selected_return"].mean())}
    summary["coverage_disclosure"] = {"integrity_status_counts": {str(k): int(v) for k, v in backtest["integrity_status"].value_counts().items()},
        "excluded_selected_total": int(backtest["excluded_selected_count"].sum()),
        "rule": "未成熟、缺價及排名無法產生均保留；不補選、不當零報酬、不刪缺值後重新平均。"}
    return summary


def compare_rankings(shadow: pd.DataFrame, production: pd.DataFrame, *, selected_count: int = 45) -> pd.DataFrame:
    required = {"as_of_date", "ticker"}
    if required - set(production.columns): raise ValueError("正式排名需包含 as_of_date 與 ticker")
    prod = production.copy(); prod["as_of_date"] = prod["as_of_date"].astype(str); prod["ticker"] = prod["ticker"].astype(str)
    if "selected" not in prod:
        rank = next((x for x in ("rank", "quant_rank", "production_rank") if x in prod), None)
        if rank is None: raise ValueError("正式排名需包含 selected 或排名欄位")
        prod["selected"] = prod[rank].astype(float) <= selected_count
    prod["selected"] = prod["selected"].astype(str).str.lower().isin(("true", "1", "yes"))
    out = []
    for day, group in shadow.groupby("as_of_date"):
        other = prod.loc[prod["as_of_date"] == str(day)]
        if other.empty or "selected" not in group: continue
        left, right = set(group.loc[group["selected"].fillna(False), "ticker"].astype(str)), set(other.loc[other["selected"], "ticker"])
        union = left | right
        out.append({"as_of_date": day, "shadow_selected_count": len(left), "production_selected_count": len(right), "overlap_count": len(left & right),
                    "jaccard_overlap": len(left & right) / len(union) if union else None, "shadow_only": " ".join(sorted(left - right)), "production_only": " ".join(sorted(right - left))})
    return pd.DataFrame(out)


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); frame.to_csv(path, encoding="utf-8-sig", index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="研究線量價控制實驗；不得正式下單")
    parser.add_argument("--selected-count", type=int, default=45); parser.add_argument("--periods", default="5,20,60")
    parser.add_argument("--production-ranking", type=Path); parser.add_argument("--news-csv", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True, help="獨立新目錄；禁止覆寫")
    args = parser.parse_args(); periods = tuple(int(x) for x in args.periods.split(","))
    root = (ROOT / 'output' / 'research').resolve()
    target = args.out_dir.resolve()
    if not target.is_relative_to(root) or target == root or target.exists():
        parser.error('輸出必須是 output/research 下尚不存在的子目錄')
    daily = pd.read_csv(ROOT / "data" / "market" / "daily.csv", dtype={"ticker": str}, encoding="utf-8-sig")
    from . import codeversion
    source_paths = codeversion._files() + [ROOT / 'data/market/daily.csv', ROOT / 'data/history/fundamental/fund_history.csv']
    if args.news_csv:
        source_paths.append(args.news_csv)
    source_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths if p.is_file()}
    from .inputs import StoreInputs
    history = StoreInputs(history=True)
    from .news_contract import NewsSnapshots
    loader = NewsSnapshots(args.news_csv) if args.news_csv else None
    audit, report, unavailable = run_shadow_backtest(daily, selected_count=args.selected_count, holding_periods=periods,
        fundamental_loader=history.fundamental_for_date, news_loader=loader,
        formal_a3_loader=(lambda day: _formal_a3(daily, day)) if loader else None)
    market_dates = _market_dates(daily)
    benchmark_prices, benchmark_sources = fetch_prices(market_dates, ROOT / "output" / "benchmark_cache")
    if not benchmark_prices.empty:
        benchmark_prices = benchmark_prices.loc[
            benchmark_prices["date"].between(market_dates[0], market_dates[-1])].copy()
    paired = paired_evaluable_report(report)
    benchmark_detail, benchmark_summary = benchmark_comparison_report(paired, benchmark_prices)
    changed = [str(p) for p, h in source_hashes.items()
               if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != h]
    if changed:
        raise ValueError('研究執行中程式或資料已變更；不產出混合版本結果：' + ', '.join(changed))
    for source in benchmark_sources:
        cache_name = source.get("cache_file")
        if cache_name:
            raw = (ROOT / "output" / "benchmark_cache" / cache_name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != source.get("sha256"):
                raise ValueError(f"ETF 基準原始回應雜湊不符：{cache_name}")
            source["archived_file"] = f"benchmark_raw/{cache_name}"
    args.out_dir.mkdir(parents=True, exist_ok=False)
    benchmark_raw_dir = args.out_dir / "benchmark_raw"
    for source in benchmark_sources:
        cache_name = source.get("cache_file")
        if cache_name:
            benchmark_raw_dir.mkdir(exist_ok=True)
            raw = (ROOT / "output" / "benchmark_cache" / cache_name).read_bytes()
            (benchmark_raw_dir / cache_name).write_bytes(raw)
    manifest = {'model_version': MODEL_VERSION, 'selected_count': args.selected_count,
                'holding_periods': periods, 'strategy_scope': 'signal_only_no_formal_promotion',
                'benchmarks': list(BENCHMARKS), 'benchmark_basis': 'TWSE daily close price return; excludes distributions and costs',
                'hashes': {str(p): h for p, h in source_hashes.items()}}
    (args.out_dir / 'experiment_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    _write_csv(audit, args.out_dir / "research_ranking_audit.csv"); _write_csv(report, args.out_dir / "control_backtest_audit.csv")
    _write_csv(selected_difference_report(audit), args.out_dir / "selected_differences.csv")
    _write_csv(paired, args.out_dir / "paired_evaluable_comparison.csv")
    _write_csv(benchmark_prices, args.out_dir / "benchmark_prices.csv")
    _write_csv(benchmark_detail, args.out_dir / "benchmark_comparison.csv")
    _write_csv(benchmark_summary, args.out_dir / "benchmark_summary.csv")
    (args.out_dir / "benchmark_sources.json").write_text(json.dumps(benchmark_sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.production_ranking:
        production = pd.read_csv(args.production_ranking, dtype={'ticker': str})
        _write_csv(compare_rankings(audit, production, selected_count=args.selected_count),
                   args.out_dir / 'production_overlap.csv')
    summary = summarize_backtest(report, unavailable) | {"periods": periods, "benchmarks": list(BENCHMARKS)}
    (args.out_dir / "research_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "report.md").write_text(research_report_markdown(summary, benchmark_summary), encoding="utf-8")
    print(f"研究控制實驗已寫出至 {args.out_dir}；含三檔 ETF 價格基準比較，僅訊號研究，非正式投組績效")


if __name__ == "__main__": main()
