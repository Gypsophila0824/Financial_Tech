from __future__ import annotations

import json
import pandas as pd
import pytest

from src.agent.backtest import runner
from src.agent.backtest.runner import run_formal_backtest, write_result
from src.agent.inputs import MockInputs


def test_formal_backtest_reuses_strategy_without_dplan(tmp_path, monkeypatch):
    from src.agent.backtest import archive
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', tmp_path)
    inputs = MockInputs(seed=42)
    result = run_formal_backtest(inputs, start="2026-10-27", days=4)

    assert len(result.daily_nav) == 4
    assert set(result.daily_nav["n_holdings"]).issubset(set(range(20, 31)))
    assert result.daily_nav["cash_pct"].between(0, 0.25).all()
    assert not result.trades.empty
    assert len(result.factor_diagnostics) == 4 * 150
    assert (result.factor_diagnostics.groupby("date")["selected"].sum() == 25).all()

    out = tmp_path / "formal_run"
    write_result(result, out, config={"history": False, "test": True})
    assert not list(out.glob("D-Plan_*.json"))
    assert {"daily_nav.csv", "holdings.csv", "trades.csv", "compliance.csv", "factor_diagnostics.csv",
            "summary.json", "config_snapshot.json", "report.md"} <= {p.name for p in out.iterdir()}
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))["records"] == 4


def test_research_comparison_can_explicitly_skip_active_share(tmp_path, monkeypatch):
    from src.agent.backtest import archive
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', tmp_path)
    result = run_formal_backtest(MockInputs(seed=7), start="2026-10-27", days=2,
                                 enforce_active_share=False)

    assert len(result.daily_nav) == 2
    assert result.summary["active_share_enforced"] is False
    assert "不代表符合競賽規範" in result.summary["active_share_note"]
    assert result.compliance["active_share_ok"].isna().all()


def test_report_includes_same_period_0050_return(tmp_path, monkeypatch):
    from src.agent.backtest import archive
    monkeypatch.setattr(archive, 'OUTPUT_ROOT', tmp_path)
    result = run_formal_backtest(MockInputs(seed=11), start='2026-10-27', days=2)
    dates = result.daily_nav['date'].tolist()
    start_date = result.daily_nav['data_date'].iloc[0]
    frame = pd.DataFrame({
        'date': [start_date, *dates], 'ticker': ['0050'] * 3, 'close': [100.0, 102.0, 105.0]})
    monkeypatch.setattr('src.agent.datafeed.benchmark_prices.fetch_prices',
                        lambda requested, cache, tickers: (frame, [{'ticker': '0050', 'status': 'ok'}]))
    runner.add_0050_benchmark(result, cache_dir=tmp_path / 'benchmark_cache')
    out = tmp_path / 'with_benchmark'
    write_result(result, out, config={'test': True})
    report = (out / 'report.md').read_text(encoding='utf-8')
    assert '0050 同區段價格報酬' in report
    assert '5.00%' in report
    assert (out / 'benchmark_0050_prices.csv').is_file()
    summary = __import__('json').loads((out / 'summary.json').read_text(encoding='utf-8'))
    assert summary['benchmark_0050']['price_return'] == pytest.approx(0.05)
