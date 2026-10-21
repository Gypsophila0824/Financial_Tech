"""正式策略的 point-in-time 歷史回測入口。

重用 factor_engine 的正式評分、選股、配權與下單規則；本模組只模擬歷史
成交與帳本，絕不產生 D-Plan、訂單提交檔或修改正式 ledger.json。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from io import StringIO
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from .. import rules
from ..factor_engine import build_targets, check_active_share, plan_trades
from ..governance import production_manifest
from ..inputs import StoreInputs
from ..state import PortfolioState, settle, trade_fee, trade_tax
from ..tools.trading_calendar import trading_days
from ..metrics import gross_turnover
from ..validation_outputs import VALIDATION_ROOT, dated_run_name, update_index
from . import archive
from .corporate_actions import CorporateAction, DividendLedger, load_actions, validate_actions
from .risk import (RiskState, active_share_status, add_warning, buy_overweight_reasons,
                   invalid_reasons, passive_grace_reasons)


ROOT = Path(__file__).resolve().parents[3]


@dataclass
class BacktestResult:
    """一次正式回測的記錄；各表可直接寫為稽核輸出。"""

    daily_nav: pd.DataFrame
    holdings: pd.DataFrame
    trades: pd.DataFrame
    compliance: pd.DataFrame
    factor_diagnostics: pd.DataFrame
    summary: dict
    skipped_dates: list[dict] = field(default_factory=list)
    dividend_ledger: dict = field(default_factory=dict)
    artifacts: dict[str, bytes] = field(default_factory=dict, repr=False)
    active_share_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    active_share_detail: pd.DataFrame = field(default_factory=pd.DataFrame)


def _dates(inputs, start: str | None, end: str | None, days: int | None) -> list[str]:
    dates = list(inputs.dates() if hasattr(inputs, "dates") else inputs.days)
    # trade_date 需要前一個交易日作為資料日，因此第一日不可能執行。
    dates = dates[1:]
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    if days is not None:
        dates = dates[:days]
    if not dates:
        raise ValueError("指定區間沒有可回測的交易日")
    return dates


def _weights(state: PortfolioState) -> pd.Series:
    if not state.holdings or not state.nav:
        return pd.Series(dtype=float)
    return pd.Series({t: shares * state.last_close[t] / state.nav for t, shares in state.holdings.items()})


def _summary(daily: pd.DataFrame, skipped_dates: list[dict]) -> dict:
    if daily.empty:
        return {"records": 0, "skipped_dates": skipped_dates}
    returns = daily["daily_return"].dropna()
    nav = daily["nav"]
    benchmark = daily["benchmark_nav"]
    drawdown = daily["drawdown"]
    vol = returns.std(ddof=0) * (252 ** 0.5) if len(returns) > 1 else None
    initial_nav = float(daily['prior_nav'].iloc[0]) if 'prior_nav' in daily else float(rules.INITIAL_CAPITAL)
    annualized = (float(nav.iloc[-1]) / initial_nav) ** (252 / len(daily)) - 1 if nav.iloc[-1] > 0 else None
    sharpe = (returns.mean() / returns.std(ddof=0) * (252 ** 0.5)
              if len(returns) > 1 and returns.std(ddof=0) > 0 else None)
    return {
        "records": int(len(daily)),
        "start_date": str(daily["date"].iloc[0]),
        "end_date": str(daily["date"].iloc[-1]),
        "initial_nav": initial_nav,
        "final_nav": float(nav.iloc[-1]),
        "cumulative_return": float(nav.iloc[-1] / initial_nav - 1),
        "annualized_return": None if annualized is None else float(annualized),
        "annualization_periods": int(len(daily)),
        "short_sample_warning": len(daily) < 252,
        "annualized_volatility": None if vol is None else float(vol),
        "sharpe_zero_risk_free": None if sharpe is None else float(sharpe),
        "max_drawdown": float(drawdown.min()),
        "total_turnover": float(daily["turnover"].sum()),
        "total_cost": float(daily["cost"].sum()),
        "benchmark_cumulative_return": float(benchmark.iloc[-1] - 1),
        "excess_return": float(nav.iloc[-1] / rules.INITIAL_CAPITAL - benchmark.iloc[-1]),
        "min_active_share": None if daily["min_active_share"].dropna().empty else float(daily["min_active_share"].min()),
        "active_share_breach_days": int((daily["min_active_share"] < rules.ACTIVE_SHARE_MIN).sum()),
        "skipped_dates": skipped_dates,
        "benchmark_definition": "150 檔可得收盤價之等權、每日再平衡價格報酬；未含股利，非總報酬基準。",
    }


def run_formal_backtest(
    inputs,
    *,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
    corporate_actions: list[CorporateAction] | None = None,
    terminal_date: str | None = None,
    known_exrights: set[tuple[str, str]] | None = None,
    news_loader=None,
    strategy_adapter=None,
    enforce_active_share: bool = True,
    price_only: bool = False,
) -> BacktestResult:
    """以當時可用資料重播正式策略，回傳完整稽核資料但不寫檔。

    ``inputs`` 通常是 ``StoreInputs(history=True, strict_history=True)``。strict
    模式在 ETF 持股快照尚未存在時會略過該日，避免把未來 ETF 資料帶回歷史。
    """
    if isinstance(inputs, StoreInputs) and not (inputs.history and inputs.strict_history):
        raise ValueError('正式回測必須使用 history=True, strict_history=True')
    actions = list(corporate_actions or [])
    validate_actions(actions)
    artifacts, source_hashes = archive.capture_sources(isinstance(inputs, StoreInputs))
    planned_dates = _dates(inputs, start, end, days)
    for action in actions:
        if planned_dates[0] <= action.ex_date <= planned_dates[-1] and action.ex_date not in planned_dates:
            raise ValueError(f'{action.event_id} 生效日沒有對應交易資料，請核實實際除權息日')
    if terminal_date is not None and terminal_date != planned_dates[-1]:
        raise ValueError('terminal_date 必須等於本次回測最後交易日；不得提前釋放權益')
    known_exrights = set(known_exrights or set())
    if isinstance(inputs, StoreInputs):
        ex_path = ROOT / 'data/market/twse_exright.csv'
        if ex_path.exists() and not price_only:
            all_dates = inputs.dates()
            for r in pd.read_csv(ex_path, dtype={'ticker': str}).itertuples():
                effective = next((d for d in all_dates if d >= r.date), None)
                if effective:
                    known_exrights.add((effective, r.ticker))
        # 上櫃次日參考價若與收盤不同，必須有核實事件，不能當成一般報酬。
        daily = inputs.daily
        if 'ref_next' in daily:
            for r in daily[daily['market'] == 'TPEX'].itertuples():
                ref = pd.to_numeric(r.ref_next, errors='coerce')
                if pd.notna(ref) and pd.notna(r.close) and abs(ref - r.close) > 1e-6:
                    later = next((d for d in inputs.dates() if d > r.date), None)
                    if later:
                        known_exrights.add((later, r.ticker))
    dividend_book = DividendLedger()
    archived_days = []
    state = PortfolioState.initial()
    daily_rows, holding_rows, trade_rows, compliance_rows, factor_rows, active_share_rows = [], [], [], [], [], []
    skipped_dates: list[dict] = []
    benchmark_nav = 1.0
    peak_nav = state.nav
    risk = RiskState()

    for trade_date in planned_dates:
        if risk.disqualified:
            break
        try:
            day = inputs.day(trade_date)
        except FileNotFoundError as exc:
            if daily_rows:
                raise ValueError(f'{trade_date} 回測途中缺資料，停止以避免跳日與漏記股利') from exc
            skipped_dates.append({"trade_date": trade_date, "reason": str(exc)})
            continue
        if daily_rows and (day.data_date != state.as_of or len(trading_days(state.as_of, trade_date)) != 2):
            raise ValueError(f'{trade_date} 交易日不連續；帳本 {state.as_of}、資料日 {day.data_date}')

        dividend_book.accrue(trade_date, state.holdings,
                             [] if price_only else actions,
                             set() if price_only else known_exrights)

        if news_loader is not None:
            day.news = news_loader(trade_date)
            if day.news is None or set(day.master.index) - set(day.news.index):
                raise ValueError('新聞快照未涵蓋全部候選；無新聞亦須明確記錄中性，不能默默補值')
        if strategy_adapter is not None:
            strategy_adapter.prepare(inputs, day)
            targets = strategy_adapter(day, state)
        else:
            targets = build_targets(day.master, day.pv, day.fund, day.news, state.held_tickers(), day.benches,
                                    enforce_active_share=enforce_active_share)
        trade_plan = plan_trades(
            targets.picks, state.holdings, pd_to_decimal(state.cash), pd_to_decimal(state.nav),
            day.close, day.tradable, day.benches, day.data_date,
            no_trade_band=getattr(strategy_adapter, 'no_trade_band', None),
            enforce_active_share=enforce_active_share,
        )
        fill_prices = inputs.fill_prices(trade_date)
        missing_fill = sorted({d["ticker"] for d in trade_plan.decisions} - set(fill_prices))
        if missing_fill:
            raise ValueError(f"{trade_date} 缺少成交均價：{missing_fill}；回測停止，不使用收盤價替代")
        close_prices = inputs.close_prices(trade_date)
        prev_nav = state.nav
        prev_close = inputs.close_prices(day.data_date)
        exposed = set(state.holdings) | {d['ticker'] for d in trade_plan.decisions}
        invalid_close = [t for t in exposed if t not in close_prices or
                         not math.isfinite(close_prices[t]) or close_prices[t] <= 0]
        if invalid_close:
            raise ValueError(f'{trade_date} 缺少當日有效收盤價：{sorted(invalid_close)}；不得沿用舊價')
        archived_days.append(archive.capture_day(day, fill_prices, close_prices, prev_close))

        buy = sell = cost = 0.0
        pending_trade_rows = []
        for decision in trade_plan.decisions:
            ticker, shares, side = decision["ticker"], decision["shares"], decision["side"]
            price = float(fill_prices[ticker])
            amount = shares * price
            fee = trade_fee(amount)
            tax = trade_tax(amount) if side == "SELL" else 0.0
            buy += amount if side == "BUY" else 0.0
            sell += amount if side == "SELL" else 0.0
            cost += fee + tax
            pending_trade_rows.append({
                "date": trade_date, "ticker": ticker, "action": decision["action"], "side": side,
                "shares": shares, "fill_price": price, "amount": amount, "fee": fee, "tax": tax,
                "target_weight": decision["target_weight"], "data_date": day.data_date,
            })

        orders = [
            type("Order", (), {"ticker": d["ticker"], "side": d["side"], "shares": d["shares"]})()
            for d in trade_plan.decisions
        ]
        # 合成測試資料不冒充官方 30 檔 ETF；封存重播仍須完整追蹤。
        track_active_share = (enforce_active_share and not getattr(inputs, "is_mock", False)
                              or (isinstance(inputs, archive.ArchivedInputs)
                                  and inputs.payload.get("active_share_trackable", True)))
        etf_dates = day.etf_dates or {code: day.etf_date for code in day.benches}
        evaluation_benches, evaluation_dates = (inputs.evaluation_benchmarks(trade_date)
            if hasattr(inputs, 'evaluation_benchmarks') else (day.benches, etf_dates))
        archived_days[-1]['evaluation_benches'] = {k: v.to_dict() for k, v in evaluation_benches.items()}
        archived_days[-1]['evaluation_etf_dates'] = evaluation_dates
        risk_benches = evaluation_benches if track_active_share and enforce_active_share else None
        risk_etf_dates = evaluation_dates if track_active_share else None
        # 委託當下以前日收盤價可預見的加碼超限，不適用價格波動寬限。
        projected = settle(state, trade_date, orders, prev_close, prev_close)
        bought = {order.ticker for order in orders if order.side == "BUY"}
        candidate = settle(state, trade_date, orders, fill_prices, close_prices,
                            benchmarks=risk_benches, etf_dates=risk_etf_dates)
        submission_reasons = (['submission_order_limit'] if len(orders) > rules.LAYER_LIMITS['orders'] else [])
        reasons = (submission_reasons + invalid_reasons(candidate, set(day.master.index))
                   + buy_overweight_reasons(projected, bought)
                   + passive_grace_reasons(candidate))
        invalidated = bool(reasons)
        if invalidated:
            # 六(十四)：交易無效，使用前一日持倉，僅按本日收盤重新估值。
            state = settle(state, trade_date, [], {}, close_prices,
                           benchmarks=risk_benches, etf_dates=risk_etf_dates)
            # 無效後的舊組合仍可能在當日第六日被動超限，合併為同一警告。
            reasons += invalid_reasons(state, set(day.master.index)) + passive_grace_reasons(state)
            buy = sell = cost = 0.0
        else:
            state = candidate
            trade_rows.extend(pending_trade_rows)
        add_warning(risk, trade_date, reasons)
        weights = _weights(state)
        as_report = check_active_share(weights, evaluation_benches)
        min_as = float(as_report["active_share"].min()) if not as_report.empty else None
        for etf_id, row in as_report.iterrows():
            snapshot_date = evaluation_dates.get(etf_id)
            age = (len(trading_days(snapshot_date, trade_date)) - 1
                   if snapshot_date and snapshot_date <= trade_date else None)
            active_share_rows.append({
                "date": trade_date, "etf_id": etf_id,
                "active_share": float(row["active_share"]),
                "overlap_tickers": row.get("overlap", ""),
                "etf_snapshot_date": snapshot_date,
                "snapshot_age_trading_days": age,
                "same_day_snapshot": snapshot_date == trade_date,
            })
        max_weight = float(weights.max()) if not weights.empty else 0.0
        max_other = float(weights.drop(index=rules.TSMC, errors="ignore").max()) if len(weights) else 0.0
        benchmark_returns = pd.Series(close_prices, dtype=float).div(pd.Series(prev_close, dtype=float)).sub(1).dropna()
        benchmark_return = float(benchmark_returns.mean()) if not benchmark_returns.empty else 0.0
        benchmark_nav *= 1 + benchmark_return
        daily_return = state.nav / prev_nav - 1
        peak_nav = max(peak_nav, state.nav)
        drawdown = state.nav / peak_nav - 1
        hard_ok = (not invalidated and rules.MIN_HOLDINGS <= len(state.holdings) <= rules.MAX_HOLDINGS
                   and 0 <= state.cash / state.nav <= rules.CASH_MAX
                   and max_weight <= rules.MAX_WEIGHT_TSMC + 1e-12
                   and max_other <= rules.MAX_WEIGHT_OTHER + 1e-12)
        as_status = active_share_status(state, trade_date)
        daily_rows.append({
            "date": trade_date, "data_date": day.data_date, "nav": state.nav, "cash": state.cash,
            "prior_nav": prev_nav, "trading_nav": state.nav, "terminal_dividend_adjustment": 0.0,
            "deferred_cash_dividends": float(dividend_book.cash),
            "deferred_stock_shares": float(sum(dividend_book.stocks.values())),
            "cash_pct": state.cash / state.nav, "n_holdings": len(state.holdings), "daily_return": daily_return,
            "drawdown": drawdown, "turnover": gross_turnover(buy, sell, prev_nav), "cost": cost,
            "active_share_same_day_data": bool(evaluation_dates) and all(d == trade_date for d in evaluation_dates.values()),
            "benchmark_return": benchmark_return, "benchmark_nav": benchmark_nav,
            "min_active_share": min_as, "max_weight": max_weight, "max_other_weight": max_other,
            "settlement_status": "invalidated" if invalidated else "executed",
            "warning_number": risk.warning_count if reasons else 0,
            "warning_reasons": "|".join(sorted(set(reasons))),
            "active_share_confirmed_two_day_risk": bool(as_status["confirmed_two_day_etfs"]),
            "active_share_estimated_two_day_risk": bool(as_status["estimated_two_day_etfs"]),
        })
        compliance_rows.append({
            "date": trade_date, "holdings_ok": rules.MIN_HOLDINGS <= len(state.holdings) <= rules.MAX_HOLDINGS,
            "cash_ok": 0 <= state.cash / state.nav <= rules.CASH_MAX,
            "weight_ok": max_weight <= rules.MAX_WEIGHT_TSMC + 1e-12 and max_other <= rules.MAX_WEIGHT_OTHER + 1e-12,
            "active_share_ok": (None if not enforce_active_share else
                                 min_as is None or min_as >= rules.ACTIVE_SHARE_MIN),
            "overall_hard_limits_ok": hard_ok, "min_active_share": min_as,
            "active_share_warning": (None if not enforce_active_share else
                                      min_as is not None and min_as < rules.ACTIVE_SHARE_MIN),
            "settlement_status": "invalidated" if invalidated else "executed",
            "transaction_invalidated": invalidated,
            "warning_number": risk.warning_count if reasons else 0,
            "warning_reasons": "|".join(sorted(set(reasons))),
            "passive_overweight_days": json.dumps(state.overweight_days, ensure_ascii=False, sort_keys=True),
            "active_share_confirmed_two_day_etfs": "|".join(as_status["confirmed_two_day_etfs"]),
            "active_share_estimated_two_day_etfs": "|".join(as_status["estimated_two_day_etfs"]),
        })
        for ticker, shares in state.holdings.items():
            holding_rows.append({"date": trade_date, "ticker": ticker, "shares": shares,
                                 "close": state.last_close[ticker], "market_value": shares * state.last_close[ticker],
                                 "weight": weights[ticker]})
        diag = targets.scored.reset_index(names="ticker").copy()
        diag.insert(0, "date", trade_date)
        diag["selected"] = diag["ticker"].isin(targets.picks.index)
        diag["target_weight"] = diag["ticker"].map(targets.picks["weight"]).fillna(0.0)
        factor_rows.append(diag)

    if not daily_rows:
        raise ValueError('沒有有效回測交易日，不能產出成功報告')
    if terminal_date:
        if daily_rows[-1]['date'] != terminal_date:
            raise ValueError('期末日未成功回測，不得結算股利')
        terminal = dividend_book.finalize(terminal_date, inputs.close_prices(terminal_date))
        adjustment = float(terminal['nav_adjustment'])
        last = daily_rows[-1]
        last['terminal_dividend_adjustment'] = adjustment
        last['nav'] += adjustment
        last['daily_return'] = last['nav'] / last['prior_nav'] - 1
        last['drawdown'] = last['nav'] / max(peak_nav, last['nav']) - 1
    daily = pd.DataFrame(daily_rows)
    if not daily.empty:
        daily["cumulative_return"] = daily["nav"] / rules.INITIAL_CAPITAL - 1
    summary = _summary(daily, skipped_dates)
    active_share_detail = pd.DataFrame(active_share_rows)
    strategy_manifest = strategy_adapter.manifest() if strategy_adapter else production_manifest()
    summary.update({'strategy_manifest': strategy_manifest,
                    'turnover_definition': 'gross_two_sided_notional_over_prior_nav_including_cash_excluding_fees',
                    'news_policy': ('time_contract_checked_not_source_certified' if hasattr(news_loader, 'sha256') else 'injected_loader_unverified') if news_loader else ('archived_snapshot' if any('news' in d['frames'] for d in archived_days) else 'neutral_ablation_not_full_live_strategy'),
                    'submission_validation': 'not_full_dplan_validation',
                    'dividend_policy': 'contest_deferred_dividends_v1', 'terminal_date': terminal_date,
                    'dividend_data_coverage': 'user_supplied_events_not_certified_complete',
                    'terminal_nav_includes_dividends': terminal_date is not None,
                    'daily_risk_basis': 'trading_nav；期末股利調整不回寫可交易現金與風控分母',
                    'source_warning': '公司事件明細需人工核對完整性；分割等未支持事件遇曝險時停止',
                    'daily_warning_count': risk.warning_count, 'daily_warning_days': risk.warning_days,
                    'disqualified_after_three_warnings': risk.disqualified,
                    'disqualification_reason': risk.disqualification_reason,
                    'active_share_rule_note': '連續風險僅以本地收盤與ETF披露資料估計；非主辦方裁定，未自動宣告取消資格'})
    summary['active_share_enforced'] = enforce_active_share
    if not enforce_active_share:
        summary['active_share_note'] = '研究比較已停用 Active Share 約束；結果不代表符合競賽規範'
    summary['price_only'] = price_only
    if price_only:
        summary['dividend_policy'] = 'excluded_price_only_comparison'
        summary['price_only_note'] = '僅按原始收盤價估值，不含現金股利、股票股利與股數調整'
    artifacts['replay_inputs.json'] = archive.json_bytes({'days': archived_days,
        'active_share_trackable': enforce_active_share and not getattr(inputs, 'is_mock', False),
        'corporate_actions': [asdict(a) for a in actions], 'terminal_date': terminal_date,
        'known_exrights': sorted(known_exrights), 'strategy_manifest': strategy_manifest})
    if hasattr(news_loader, 'raw'):
        artifacts['news_snapshot.csv'] = news_loader.raw
        summary['news_snapshot_sha256'] = news_loader.sha256
    archive.check_unchanged(source_hashes)
    return BacktestResult(daily, pd.DataFrame(holding_rows), pd.DataFrame(trade_rows),
                          pd.DataFrame(compliance_rows),
                          pd.concat(factor_rows, ignore_index=True) if factor_rows else pd.DataFrame(),
                          summary, skipped_dates, dividend_book.snapshot(), artifacts, active_share_detail)


def add_0050_benchmark(result: BacktestResult, *, cache_dir: Path | None = None) -> None:
    """Attach same-period, close-price-only 0050 comparison without blocking the strategy result."""
    from ..datafeed.benchmark_prices import fetch_prices

    daily = result.daily_nav
    if not hasattr(result, 'artifacts'):
        result.artifacts = {}
    dates = daily['date'].astype(str).tolist() if 'date' in daily else []
    start_data_date = str(daily['data_date'].iloc[0]) if dates and 'data_date' in daily else None
    requested = list(dict.fromkeys(([start_data_date] if start_data_date else []) + dates))
    sources: list[dict] = []
    try:
        prices, sources = fetch_prices(requested, cache_dir or ROOT / 'output' / 'benchmark_cache',
                                       tickers=('0050',))
    except Exception as exc:
        prices = pd.DataFrame(columns=['date', 'ticker', 'close'])
        sources = [{'ticker': '0050', 'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}]

    prices = prices[prices['ticker'].astype(str) == '0050'].copy() if not prices.empty else prices
    close = dict(zip(prices['date'].astype(str), pd.to_numeric(prices['close'], errors='coerce')))
    missing = [d for d in requested if d not in close or pd.isna(close[d])]
    if not start_data_date:
        missing.append('首個回測交易日前一交易日')
    benchmark = {'ticker': '0050', 'status': 'available' if not missing and len(dates) else 'unavailable',
                 'start_date': start_data_date, 'end_date': dates[-1] if dates else None,
                 'start_close_date': start_data_date, 'end_close_date': dates[-1] if dates else None,
                 'start_close': float(close[start_data_date]) if start_data_date and start_data_date in close else None,
                 'end_close': float(close[dates[-1]]) if dates and dates[-1] in close else None,
                 'price_return': None, 'dividends_included': False, 'costs_included': False,
                 'missing_dates': missing}
    if benchmark['status'] == 'available':
        benchmark['price_return'] = benchmark['end_close'] / benchmark['start_close'] - 1
    result.summary['benchmark_0050'] = benchmark
    result.artifacts['benchmark_0050_prices.csv'] = prices.to_csv(index=False).encode('utf-8-sig')
    result.artifacts['benchmark_0050_sources.json'] = json.dumps(
        sources, ensure_ascii=False, indent=2).encode('utf-8')


def pd_to_decimal(value: float):
    """延後匯入 Decimal，讓回測邏輯與正式 plan_trades 的型別契約一致。"""
    from decimal import Decimal
    return Decimal(str(value))


def write_result(result: BacktestResult, out_dir: Path, *, config: dict) -> None:
    """寫出獨立 run 的所有稽核檔；不得傳入 output/live。"""
    out_dir = archive.safe_output(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    result.daily_nav.to_csv(out_dir / "daily_nav.csv", index=False, encoding="utf-8-sig")
    result.active_share_detail.to_csv(out_dir / "active_share_detail.csv", index=False, encoding="utf-8-sig")
    result.holdings.to_csv(out_dir / "holdings.csv", index=False, encoding="utf-8-sig")
    result.trades.to_csv(out_dir / "trades.csv", index=False, encoding="utf-8-sig")
    result.compliance.to_csv(out_dir / "compliance.csv", index=False, encoding="utf-8-sig")
    result.factor_diagnostics.to_csv(out_dir / "factor_diagnostics.csv", index=False, encoding="utf-8-sig")
    (out_dir / "summary.json").write_text(json.dumps(result.summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / 'dividend_ledger.json').write_bytes(archive.json_bytes(result.dividend_ledger))
    pd.DataFrame(result.dividend_ledger.get('entitlements', [])).to_csv(out_dir / 'dividend_entitlements.csv', index=False, encoding='utf-8-sig')
    for name, data in result.artifacts.items():
        if Path(name).name != name:
            raise ValueError('封存檔名不得含路徑')
        (out_dir / name).write_bytes(data)
    replay = json.loads(result.artifacts['replay_inputs.json'])
    snapshot = {**config, "strategy_manifest": replay['strategy_manifest'],
                "code_version": json.loads(result.artifacts['source_manifest.json'])['code_version'],
                "source_snapshot_sha256": hashlib.sha256(result.artifacts['source_snapshot.zip']).hexdigest()}
    (out_dir / "config_snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s = result.summary
    report_lines = [
        "# 正式策略回測報告", "",
        "本報告重用正式策略引擎；不產生 D-Plan。只允許開始前缺檔暖機，回測途中缺資料即停止。", "",
        "股利明細完整性未經系統認證；未指定 terminal_date 時權益不納入 NAV。分割等未支持事件遇曝險時停止。", "",
        f"- 交易日：{s.get('start_date', '—')} 至 {s.get('end_date', '—')}（{s['records']} 日）",
        f"- 累積報酬：{s.get('cumulative_return', 0):.2%}",
    ]
    benchmark_0050 = s.get('benchmark_0050')
    if benchmark_0050:
        if benchmark_0050.get('status') == 'available':
            report_lines.append(
                f"- 0050 同區段價格報酬（{benchmark_0050['start_close_date']} 收盤至 "
                f"{benchmark_0050['end_close_date']} 收盤）：{benchmark_0050['price_return']:.2%}（不含配息與交易成本）")
        else:
            missing = ', '.join(benchmark_0050.get('missing_dates', [])) or '行情來源不可用'
            report_lines.append(f"- 0050 同區段報酬：無法計算，缺少收盤價日期：{missing}；詳見 benchmark_0050_sources.json")
    report_lines += [f"- 最大回撤：{s.get('max_drawdown', 0):.2%}",
                     f"- 總換手率：{s.get('total_turnover', 0):.2%}",
                     f"- 總成本：{s.get('total_cost', 0):,.0f}",
                     f"- 略過日期：{len(result.skipped_dates)}（詳見 summary.json）"]
    report_lines += ["", "## 每日持倉與風險", "",
                     "| 日期 | 持股檔數 | 現金比 | 最大單檔權重 | 最低 Active Share | 最低 AS ETF／快照日 |",
                     "|---|---:|---:|---:|---:|---|"]
    for row in result.daily_nav.itertuples():
        details = result.active_share_detail
        day_details = details[details["date"] == row.date] if not details.empty else details
        if not day_details.empty:
            lowest = day_details.sort_values("active_share").iloc[0]
            as_text = f"{lowest.active_share:.2%}"
            etf_text = f"{lowest.etf_id}／{lowest.etf_snapshot_date or '缺'}"
        else:
            as_text, etf_text = "無資料", "無資料"
        report_lines.append(
            f"| {row.date} | {int(row.n_holdings)} | {row.cash_pct:.2%} | {row.max_weight:.2%} | "
            f"{as_text} | {etf_text} |")
    report_lines += ["", "逐 ETF 完整 Active Share、重疊持股及快照日期見 `active_share_detail.csv`；",
                     "每日各檔持倉股數、市值與權重見 `holdings.csv`。快照較舊時，Active Share 僅代表使用該快照的估計值。"]
    (out_dir / 'report.md').write_text('\n'.join(report_lines) + '\n', encoding='utf-8')
    manifest = {'format': 'backtest-archive-v1', 'files': {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out_dir.iterdir() if p.is_file()}}
    (out_dir / 'integrity.json').write_bytes(archive.json_bytes(manifest))
    archive.verify_archive(out_dir)
    if out_dir.is_relative_to(VALIDATION_ROOT):
        update_index()


def main() -> None:
    parser = argparse.ArgumentParser(description="正式策略的 point-in-time 回測；不產生 D-Plan")
    parser.add_argument("--start", help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--end", help="結束交易日 YYYY-MM-DD")
    parser.add_argument("--days", type=int, help="最多處理交易日數")
    parser.add_argument("--out-dir", type=Path, help="輸出目錄；預設名稱包含回測起訖日期")
    parser.add_argument('--corporate-actions', type=Path, help='已核實的股利／公司事件 JSON 明細')
    parser.add_argument('--terminal-date', help='指定競賽／研究期末日，僅該日將累計股利納入最終 NAV')
    parser.add_argument('--replay', type=Path, help='驗證並重播封存輸入；不讀現行行情或股票主檔')
    parser.add_argument('--news-snapshot', type=Path)
    parser.add_argument('--strategy', choices=['production', 'research-equal'], default='production')
    parser.add_argument('--research-count', type=int, help='研究投組實驗明確選定 20–30；不改原 45 檔訊號研究')
    args = parser.parse_args()
    action_source = None
    if args.replay:
        if any([args.start, args.end, args.days is not None, args.corporate_actions, args.terminal_date,
                args.news_snapshot, args.strategy != 'production', args.research_count]):
            parser.error('--replay 不可覆寫封存的日期、事件與期末設定')
        inputs = archive.ArchivedInputs(args.replay)
        actions = [CorporateAction(**r) for r in inputs.payload['corporate_actions']]
        terminal_date = inputs.payload['terminal_date']
        known = {tuple(r) for r in inputs.payload['known_exrights']}
        manifest = inputs.payload['strategy_manifest']
        if manifest.get('version') in ('research-portfolio-equal-v1', 'research-portfolio-equal-v2-buffer55'):
            args.strategy, args.research_count = 'research-equal', manifest['selected_count']
    else:
        inputs = StoreInputs(history=True, strict_history=True)
        actions, action_source = load_actions(args.corporate_actions) if args.corporate_actions else ([], None)
        terminal_date, known = args.terminal_date, None
    dates = list(inputs.dates() if hasattr(inputs, 'dates') else inputs.days)[1:]
    if args.start:
        dates = [d for d in dates if d >= args.start]
    if args.end:
        dates = [d for d in dates if d <= args.end]
    if args.days is not None:
        dates = dates[:args.days]
    if not dates:
        raise ValueError('指定區間沒有可回測的交易日')
    out_dir = args.out_dir or VALIDATION_ROOT / 'backtest' / dated_run_name('formal', dates[0], dates[-1])
    out_dir = archive.safe_output(out_dir)
    from ..news_contract import NewsSnapshots
    from ..strategy_interface import ResearchPortfolio
    if args.strategy == 'research-equal' and args.research_count is None:
        parser.error('研究投組須 --research-count；不替使用者猜測原策略的配置規則')
    adapter = ResearchPortfolio(args.research_count) if args.strategy == 'research-equal' else None
    if args.replay and adapter is not None:
        adapter = ResearchPortfolio(manifest['selected_count'], target_cash=manifest['target_cash'],
                                    version=manifest['version'],
                                    holding_buffer_rank=manifest.get('holding_buffer_rank'))
    result = run_formal_backtest(inputs, start=args.start, end=args.end, days=args.days,
                                corporate_actions=actions, terminal_date=terminal_date, known_exrights=known,
                                news_loader=NewsSnapshots(args.news_snapshot) if args.news_snapshot else None,
                                strategy_adapter=adapter)
    add_0050_benchmark(result)
    if action_source:
        result.artifacts['corporate_actions_source.json'] = archive.json_bytes(action_source)
    if args.replay:
        for name, frame in [('daily_nav', result.daily_nav), ('holdings', result.holdings),
                            ('trades', result.trades), ('compliance', result.compliance),
                            ('factor_diagnostics', result.factor_diagnostics)]:
            if len(frame.columns) == 0:
                if (args.replay / f'{name}.csv').read_text(encoding='utf-8-sig').strip():
                    raise ValueError(f'重播 {name} 空表不一致')
                continue
            expected = pd.read_csv(args.replay / f'{name}.csv', dtype={'ticker': str})
            # 使用相同 CSV round-trip 比較空字串／NA，不將缺值推論誤判成策略差異。
            got = pd.read_csv(StringIO(frame.to_csv(index=False)), dtype={'ticker': str})
            pd.testing.assert_frame_equal(expected, got, check_dtype=False, rtol=1e-10, atol=1e-6)
        expected_book = json.loads((args.replay / 'dividend_ledger.json').read_text(encoding='utf-8'))
        if expected_book != result.dividend_ledger:
            raise ValueError('重播股利權益不一致')
        result.summary['replay_verified'] = True
    write_result(result, out_dir, config={"start": args.start, "end": args.end, "days": args.days,
                                          "history": True, "strict_history": True, "terminal_date": terminal_date,
                                          "news_policy": result.summary['news_policy'],
                                          "strategy_manifest": result.summary['strategy_manifest']})
    print(f"正式回測完成：{out_dir}；有效交易日 {result.summary['records']}，略過 {len(result.skipped_dates)} 日")
    from ..validation_outputs import print_output_paths
    print_output_paths('正式回測', out_dir, files=(('回測報告', 'report.md'), ('每日 NAV', 'daily_nav.csv'),
                                                   ('合規明細', 'compliance.csv')))


if __name__ == "__main__":
    main()
