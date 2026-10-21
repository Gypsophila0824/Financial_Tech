"""Paired formal-line and research-only parameter study for the team proposal.

All candidates use the same strict point-in-time inputs, initial capital,
portfolio sizing, execution rules, fees/tax, and compliance checks. Missing
coverage or corporate-action records remain explicit failures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .. import strategy
from ..datafeed import etf_holdings
from ..datafeed.benchmark_prices import BENCHMARKS, _months, _parse, fetch_prices
from ..inputs import StoreInputs
from ..team_research_strategy import TeamResearchStrategy
from . import archive
from .corporate_actions import load_actions
from .runner import run_formal_backtest, write_result


CANDIDATES = {
    "team_45_45_10": (0.45, 0.45, 0.50),
    "pv_50_fund_40": (0.50, 0.40, 0.50),
    "revenue_growth_70": (0.45, 0.45, 0.70),
    "revenue_acceleration_70": (0.45, 0.45, 0.30),
}


def pct(value):
    return "—" if value is None or pd.isna(value) else f"{value:.2%}"


def money(value):
    return "—" if value is None or pd.isna(value) else f"{value/10000:+,.1f} 萬"


def dump(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def make_adapter(values, *, enforce_active_share=True):
    pv, fund, revenue = values
    return TeamResearchStrategy(pv_weight=pv, fund_weight=fund, news_weight=0.10,
                                revenue_growth_weight=revenue,
                                revenue_acceleration_weight=1-revenue,
                                buffer_rank=50, no_trade_band=0.03,
                                enforce_active_share=enforce_active_share)


def prepare_research_inputs() -> tuple[StoreInputs, dict]:
    """研究比較先更新 ETF 持股來源，再初始化嚴格歷史輸入。"""
    update = etf_holdings.refresh()
    inputs = StoreInputs(history=True, strict_history=True)
    return inputs, update


def return_for_dates(prices: pd.DataFrame, ticker: str, dates: list[str], all_dates: list[str]):
    if not dates:
        return None
    prior = [d for d in all_dates if d < dates[0]]
    if not prior or prices.empty:
        return None
    one = prices.loc[prices.ticker == ticker].set_index("date").close
    if not {prior[-1], dates[-1]} <= set(one.index):
        return None
    return float(one.loc[dates[-1]] / one.loc[prior[-1]] - 1)


def metrics(row):
    if not row or not row.get("summary"):
        return None
    summary = row["summary"]
    initial, final = float(summary["initial_nav"]), float(summary["final_nav"])
    return {"initial_nav": initial, "final_nav": final, "profit": final-initial,
            "return": final/initial-1, "total_cost": float(summary["total_cost"]),
            "turnover": float(summary["total_turnover"]),
            "max_drawdown": float(summary["max_drawdown"]), "days": int(summary["records"])}


def render_saved(out: Path):
    """Rebuild human-readable reports from saved runs and verified TWSE response cache."""
    runs = json.loads((out/"results.json").read_text(encoding="utf-8"))
    strategy_names = list(runs["recent"])
    spec = json.loads((out/"attempt.json").read_text(encoding="utf-8"))
    recent_start, recent_end = spec["periods"]["recent"]
    ytd_start, ytd_end = spec["periods"]["ytd"]
    active_share_enforced = spec.get("active_share_enforced", True)
    price_only = spec.get("price_only", False)
    inputs = StoreInputs(history=True, strict_history=True)
    all_dates = inputs.dates()
    annual_dates = [d for d in all_dates if ytd_start <= d <= ytd_end]
    first_period_start = min(recent_start, ytd_start)
    first_idx = all_dates.index(first_period_start) if first_period_start in all_dates else 0
    prior_idx = max(0, first_idx - 1)
    fetch_dates = [d for d in all_dates[prior_idx:] if d <= max(recent_end, ytd_end)]
    price_rows, sources = [], []
    for ticker in BENCHMARKS:
        for month in _months(fetch_dates[0], fetch_dates[-1]):
            cache = out/"benchmark_cache"/f"{ticker}_{month}.json"
            source = {"ticker": ticker, "month": month, "cache_file": cache.name,
                      "status": "failed", "error": "cache file missing"}
            if cache.exists():
                raw = cache.read_bytes()
                try:
                    prices = _parse(raw, ticker, month)
                    price_rows.append(prices)
                    source.update({"status": "ok", "error": None,
                                   "sha256": hashlib.sha256(raw).hexdigest()})
                except Exception as exc:
                    source["error"] = str(exc)
            sources.append(source)
    prices = pd.concat(price_rows, ignore_index=True) if price_rows else pd.DataFrame(columns=["date","ticker","close"])
    report_end = max(recent_end, ytd_end)
    prices = prices.loc[prices.date <= report_end].sort_values(["ticker","date"])
    prices.to_csv(out/"benchmark_prices.csv", index=False, encoding="utf-8-sig")
    dump(out/"benchmark_sources.json", sources)
    expected_recent = [d for d in all_dates if recent_start <= d <= recent_end]
    actual_sets = [set(runs["recent"][name].get("actual_dates", [])) for name in strategy_names]
    common_dates = sorted(set.intersection(*actual_sets)) if actual_sets else []
    period_complete = all(runs["recent"][name]["status"] == "complete" for name in strategy_names)
    scope_note = ("正式線及四組研究候選均完整覆蓋指定交易日，可依本模式比較整段結果。" if period_complete else
                  f"共同實測日期為 {common_dates[0]}～{common_dates[-1]} 共 {len(common_dates)} 日；未覆蓋的日期不推估，不能視為完整區間勝負。" if common_dates else
                  "沒有共同可比較日期。")
    if not active_share_enforced:
        scope_note += "本次研究比較略過 30 檔主動型 ETF 持股及 Active Share 約束，結果不代表符合競賽規範。"
    if price_only:
        scope_note += "採收盤價報酬口徑，不計現金股利、股票股利及其股數調整。"
    recent_md = [f"# {recent_start}–{recent_end} 正式線與研究候選比較", "", scope_note, "",
                 "| 策略 | 狀態／實際日期 | 損益 | 期末淨值 | 觀察報酬 | 換手 | 交易成本 | 最大回撤 | 失敗原因 |", "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for name in strategy_names:
        record, metric = runs["recent"][name], metrics(runs["recent"][name])
        actual = record.get("actual_dates", [])
        status = record["status"] + (f" ({actual[0]}～{actual[-1]}，{len(actual)}日)" if actual else "")
        error = (record.get("error") or "—").replace("|", "\\|")
        recent_md.append(f"| {name} | {status} | {money(metric['profit']) if metric else '—'} | {f'{metric['final_nav']/1e8:.4f} 億' if metric else '—'} | {pct(metric['return']) if metric else '—'} | {pct(metric['turnover']) if metric else '—'} | {money(metric['total_cost']) if metric else '—'} | {pct(metric['max_drawdown']) if metric else '—'} | {error} |")
    for ticker in BENCHMARKS:
        full_value = return_for_dates(prices, ticker, expected_recent, all_dates)
        recent_md.append(f"| {ticker} 全請求區間價格參考 | {'完整' if full_value is not None else '缺價'} | — | — | {pct(full_value)} | — | — | — |")
        observed = runs["recent"]["formal"].get("actual_dates", [])
        matched_value = return_for_dates(prices, ticker, observed, all_dates)
        recent_md.append(f"| {ticker} 同策略實際日期價格基準 | {'完整' if matched_value is not None else '缺價'} | — | — | {pct(matched_value)} | — | — | — |")
    recent_md += ["", "正式線採目前正式參數；研究組合為四組預先列明的敏感度候選。新聞資料採中性消融，未含歷史新聞判讀。NAV 未計入未提供的公司事件／股利，且未做完整 D-Plan 提交驗證。ETF 以 TWSE 官方收盤價計算，不含股利、費稅與整張成交。", ""]
    (out/"01_近期比較.md").write_text("\n".join(recent_md), encoding="utf-8")
    month_rows = []
    month_start = date.fromisoformat(ytd_start)
    month_end = date.fromisoformat(ytd_end)
    for ordinal in range(month_start.year * 12 + month_start.month - 1,
                          month_end.year * 12 + month_end.month):
        year, month0 = divmod(ordinal, 12)
        key = f"{year:04d}-{month0 + 1:02d}"
        md = [d for d in annual_dates if d.startswith(key)]
        row = {"month": key, "trading_days": len(md)}
        for name in strategy_names:
            row[name+"_monthly_return"] = row[name+"_ytd_return"] = None
        for ticker in BENCHMARKS:
            row[ticker+"_monthly_price_return"] = return_for_dates(prices, ticker, md, all_dates)
            ytd_dates = [d for d in annual_dates if md and d <= md[-1]]
            row[ticker+"_ytd_price_return"] = return_for_dates(prices, ticker, ytd_dates, all_dates)
        month_rows.append(row)
    pd.DataFrame(month_rows).to_csv(out/"02_各月及年初累積.csv", index=False, encoding="utf-8-sig")
    ytd_complete = all(runs["ytd"][name]["status"] == "complete" for name in runs["ytd"])
    ytd_sets = [set(runs["ytd"][name].get("actual_dates", [])) for name in strategy_names]
    common_ytd = sorted(set.intersection(*ytd_sets)) if ytd_sets else []
    ytd_note = ("策略年初連續帳戶完整。" if ytd_complete else
                f"策略線共同有效覆蓋 {common_ytd[0]}～{common_ytd[-1]} 共 {len(common_ytd)} 日；缺漏月份留空。" if common_ytd else
                "策略線沒有共同有效回測日，策略月報酬與年初累積留空。")
    ytd_errors = sorted({r.get("error") for r in runs["ytd"].values() if r.get("error")})
    if ytd_errors:
        ytd_note += "回測錯誤：" + "；".join(ytd_errors) + "。"
    monthly_md = [f"# 各月及累積報酬（{ytd_start} 至 {ytd_end}）", "",
                  ytd_note + f"ETF 行情採 TWSE 官方月資料，計算至 {ytd_end}。", "",
                  "| 月份 | 交易日 | 正式月／累積 | 組員方案月／累積 | 0050 月／累積 | 00981A 月／累積 | 00991A 月／累積 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
    for row in month_rows:
        def pair(t): return f"{pct(row[t+'_monthly_price_return'])} / {pct(row[t+'_ytd_price_return'])}"
        monthly_md.append(f"| {row['month']} | {row['trading_days']} | — / — | — / — | {pair('0050')} | {pair('00981A')} | {pair('00991A')} |")
    monthly_md += ["", "策略欄位空白表示完整策略帳本不足或回測失敗，不代表零報酬。ETF 僅為價格報酬，不含配息、費稅或整張成交。", ""]
    (out/"02_各月及年初累積.md").write_text("\n".join(monthly_md), encoding="utf-8")
    integrity = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in out.rglob("*") if p.is_file() and p.name != "delivery_integrity.json"}
    dump(out/"delivery_integrity.json", integrity)
    print("Reports refreshed from saved runs:", out, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--recent-start", default="2026-09-01", help="比較期間起日（YYYY-MM-DD）")
    parser.add_argument("--end", help="比較期間迄日（YYYY-MM-DD；預設資料庫最新日期）")
    parser.add_argument("--ytd-start", default="2026-01-01", help="逐月與累積報酬起日（YYYY-MM-DD）")
    parser.add_argument("--corporate-actions", type=Path)
    parser.add_argument("--skip-benchmark-fetch", action="store_true")
    parser.add_argument("--research-no-active-share", action="store_true",
                        help="研究比較專用：略過 ETF 持股與 Active Share 檢查；輸出不代表競賽合規績效")
    parser.add_argument("--research-price-only", action="store_true",
                        help="研究比較專用：只按原始收盤價估值，不計任何股利或配股調整")
    parser.add_argument("--render-existing", action="store_true", help="rebuild reports from a completed output folder")
    args = parser.parse_args()
    if args.research_price_only and args.corporate_actions:
        parser.error("--research-price-only 不可同時提供 --corporate-actions")
    if args.render_existing:
        render_saved(args.out_dir.resolve())
        return
    out = archive.safe_output(args.out_dir)
    out.mkdir(parents=True, exist_ok=False)
    # Refresh before input discovery/source archiving so this attempt captures the
    # newly published snapshot. Historical PIT checks still reject future data.
    inputs, etf_update = prepare_research_inputs()
    if etf_update.get("report_file"):
        update_report = Path(etf_update["report_file"])
        if update_report.is_file():
            (out/"etf_holdings_update.json").write_bytes(update_report.read_bytes())
    actions = load_actions(args.corporate_actions)[0] if args.corporate_actions else []
    all_dates = inputs.dates()
    try:
        recent_start = date.fromisoformat(args.recent_start).isoformat()
        end = date.fromisoformat(args.end).isoformat() if args.end else None
        ytd_start = date.fromisoformat(args.ytd_start).isoformat()
    except ValueError as exc:
        parser.error(f"日期須為 YYYY-MM-DD：{exc}")
    end = end or all_dates[-1]
    if recent_start > end or ytd_start > end:
        parser.error("起日不得晚於迄日")
    periods = {"recent": (recent_start, end), "ytd": (ytd_start, end)}
    snapshot, hashes = archive.capture_sources(True)
    for name, raw in snapshot.items():
        (out/name).write_bytes(raw)
    if args.corporate_actions:
        raw = args.corporate_actions.read_bytes()
        (out/"corporate_actions_source.json").write_bytes(raw)
        hashes[args.corporate_actions.resolve()] = hashlib.sha256(raw).hexdigest()
    spec = {"periods": periods, "formal_buffer_rank": strategy.BUFFER_RANK,
            "candidate_parameters": {name: make_adapter(values, enforce_active_share=not args.research_no_active_share).manifest() for name, values in CANDIDATES.items()},
            "news_policy": "neutral_ablation unless verified archived news snapshot is wired in",
            "benchmark": "official TWSE ETF closing-price returns; price-only, excludes dividends, fees and lot execution",
            "corporate_actions": str(args.corporate_actions) if args.corporate_actions else None,
            "data_mode": "strict historical price/fundamental inputs; Active Share disabled for research-only comparison" if args.research_no_active_share else "strict point-in-time historical ETF snapshots",
            "active_share_enforced": not args.research_no_active_share,
            "price_only": args.research_price_only,
            "etf_holdings_update": etf_update,
            "research_only": args.research_no_active_share,
            "formal_strategy_modified": False, "created_at": datetime.now(timezone.utc).isoformat()}
    dump(out/"attempt.json", spec)

    expected_dates = sorted(d for d in all_dates if periods["recent"][0] <= d <= end)
    first_period_start = min(recent_start, ytd_start)
    first_idx = all_dates.index(first_period_start) if first_period_start in all_dates else 0
    benchmark_dates = sorted(d for d in all_dates[max(0, first_idx - 1):] if d <= end)
    benchmarks, sources = (pd.DataFrame(columns=["date", "ticker", "close"]), [])
    if not args.skip_benchmark_fetch:
        benchmarks, sources = fetch_prices(benchmark_dates, out/"benchmark_cache")
        benchmarks = benchmarks.loc[benchmarks.date <= end]
    benchmarks.to_csv(out/"benchmark_prices.csv", index=False, encoding="utf-8-sig")
    dump(out/"benchmark_sources.json", sources)

    runs = {}
    arms = [("formal", None)] + [(name, make_adapter(values,
                   enforce_active_share=not args.research_no_active_share))
                                  for name, values in CANDIDATES.items()]
    for period, (start, end) in periods.items():
        folder = out/period
        folder.mkdir()
        dates = [d for d in all_dates if start <= d <= end]
        dump(folder/"coverage.json", {"requested_dates": dates,
              "active_share_enforced": not args.research_no_active_share,
              "dates_with_historical_etf_snapshot": [d for d in dates
                  if any(p.stem[-10:] <= inputs.prev_trading_day(d)
                         for p in (archive.ROOT/"data/etf").glob("active_etf_holdings_*.csv"))]})
        runs[period] = {}
        for name, adapter in arms:
            print(f"{period} {name}: {start}..{end}", flush=True)
            archive.check_unchanged(hashes)
            try:
                result = run_formal_backtest(StoreInputs(history=True, strict_history=True,
                                                         ignore_etf_holdings=args.research_no_active_share),
                                             start=start, end=end, strategy_adapter=adapter,
                                             corporate_actions=actions,
                                             enforce_active_share=not args.research_no_active_share,
                                             price_only=args.research_price_only)
                write_result(result, folder/name, config=spec)
                actual = result.daily_nav.date.tolist()
                complete = actual == dates
                runs[period][name] = {"status": "complete" if complete else "partial_coverage",
                                      "actual_dates": actual, "summary": result.summary,
                                      "error": None}
            except Exception as exc:
                runs[period][name] = {"status": "failed", "actual_dates": [],
                                      "error": str(exc), "traceback": traceback.format_exc()}
            dump(folder/(name+"_status.json"), runs[period][name])

    archive.check_unchanged(hashes)
    dump(out/"results.json", runs)
    recent_dates = runs["recent"]["formal"].get("actual_dates", [])
    recent_results = []
    for name in ["formal", *CANDIDATES]:
        record = runs["recent"][name]
        m = metrics(record)
        recent_results.append((name, record, m))
    common_dates = sorted(set.intersection(*(set(runs["recent"][name].get("actual_dates", []))
                                              for name in ["formal", *CANDIDATES])))
    period_complete = all(runs["recent"][name]["status"] == "complete" for name in ["formal", *CANDIDATES])
    scope_note = ("正式線及四組研究候選均完整覆蓋指定交易日，可依本模式比較整段結果。" if period_complete else
                  f"共同實測日期為 {common_dates[0]}～{common_dates[-1]} 共 {len(common_dates)} 日；缺漏日不推估。" if common_dates else
                  "沒有共同可比較日期。")
    if args.research_no_active_share:
        scope_note += "研究比較模式：略過 30 檔主動型 ETF 持股與 Active Share 約束；不代表符合競賽規範。"
    if args.research_price_only:
        scope_note += "價格報酬口徑：不計現金股利、股票股利或股數調整；非總報酬。"
    recent_md = [f"# {recent_start}–{end} 正式線與研究候選比較", "",
                 "正式線採本次執行時的正式參數；研究候選採獨立 adapter。" + scope_note, "",
                 "| 策略 | 狀態／實際日期 | 區間損益 | 期末淨值 | 報酬 | 換手 | 交易成本 | 最大回撤 | 失敗原因 |", "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for name, record, m in recent_results:
        dates = record.get("actual_dates", [])
        status = record["status"] + (f" ({dates[0]}～{dates[-1]}, {len(dates)}日)" if dates else "")
        error = (record.get("error") or "—").replace("|", "\\|")
        recent_md.append(f"| {name} | {status} | {money(m['profit']) if m else '—'} | {f'{m['final_nav']/1e8:.4f} 億' if m else '—'} | {pct(m['return']) if m else '—'} | {pct(m['turnover']) if m else '—'} | {money(m['total_cost']) if m else '—'} | {pct(m['max_drawdown']) if m else '—'} | {error} |")
    for ticker in BENCHMARKS:
        value = return_for_dates(benchmarks, ticker, expected_dates, all_dates)
        matched = return_for_dates(benchmarks, ticker, recent_dates, all_dates)
        recent_md.append(f"| {ticker} 全請求區間價格基準 | {'完整' if value is not None else '缺價'} | — | — | {pct(value)} | — | — | — |")
        recent_md.append(f"| {ticker} 同策略實際日期價格基準 | {'完整' if matched is not None else '缺價'} | — | — | {pct(matched)} | — | — | — |")
    recent_md += ["", "候選權重與門檻見 `attempt.json`；權重合計皆為 100%。回測採中性新聞消融，未含歷史新聞判讀；未提供的公司事件／股利不會估算。ETF 參考採 TWSE 收盤價，不含股利與策略交易成本。", ""]
    (out/"01_近期比較.md").write_text("\n".join(recent_md), encoding="utf-8")

    months = []
    ytd_complete = all(runs["ytd"][name]["status"] == "complete" for name in runs["ytd"])
    month_start = date.fromisoformat(ytd_start)
    month_end = date.fromisoformat(end)
    for ordinal in range(month_start.year * 12 + month_start.month - 1,
                          month_end.year * 12 + month_end.month):
        year, month0 = divmod(ordinal, 12)
        month_key = f"{year:04d}-{month0 + 1:02d}"
        md = [d for d in all_dates if ytd_start <= d <= end and d.startswith(month_key)]
        row = {"month": month_key, "trading_days": len(md)}
        for name in ["formal", *CANDIDATES]:
            row[name+"_monthly_return"] = None
            row[name+"_ytd_return"] = None
            rec = runs["ytd"][name]
            if rec["status"] == "complete" and rec.get("actual_dates"):
                navfile = out/"ytd"/name/"daily_nav.csv"
                frame = pd.read_csv(navfile)
                group = frame.loc[frame.date.str.startswith(month_key)]
                if not group.empty:
                    row[name+"_monthly_return"] = float(group.nav.iloc[-1]/group.prior_nav.iloc[0]-1)
                    row[name+"_ytd_return"] = float(group.nav.iloc[-1]/frame.prior_nav.iloc[0]-1)
        ytd_dates = [d for d in all_dates if ytd_start <= d <= (md[-1] if md else "0000")]
        for ticker in ("0050", "00981A", "00991A"):
            row[ticker+"_month_price_return"] = return_for_dates(benchmarks, ticker, md, all_dates)
            row[ticker+"_ytd_price_return"] = return_for_dates(benchmarks, ticker, ytd_dates, all_dates)
        months.append(row)
    month_df = pd.DataFrame(months)
    month_df.to_csv(out/"02_各月及年初累積.csv", index=False, encoding="utf-8-sig")
    monthly_md = [f"# 各月及累積報酬（{ytd_start} 至 {end}）", "",
                  f"完整年初累積策略曲線：{'是' if ytd_complete else '否'}。缺漏月份不以零報酬補值。", "",
                  "| 月份 | 交易日 | 正式月／累積 | 組員方案月／累積 | 0050 月／累積 | 00981A 月／累積 | 00991A 月／累積 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
    for row in months:
        def pair(t): return f"{pct(row[t+'_month_price_return'])} / {pct(row[t+'_ytd_price_return'])}"
        monthly_md.append(f"| {row['month']} | {row['trading_days']} | {pct(row['formal_monthly_return'])} / {pct(row['formal_ytd_return'])} | {pct(row['team_45_45_10_monthly_return'])} / {pct(row['team_45_45_10_ytd_return'])} | {pair('0050')} | {pair('00981A')} | {pair('00991A')} |")
    monthly_md += ["", "ETF 為價格報酬參考，不含配息、費稅或整張成交；策略月報酬需完整 YTD 帳本才能計算。", ""]
    (out/"02_各月及年初累積.md").write_text("\n".join(monthly_md), encoding="utf-8")
    integrity = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in out.rglob("*") if p.is_file()}
    dump(out/"delivery_integrity.json", integrity)
    print("Reports saved:", out, flush=True)


if __name__ == "__main__":
    main()
