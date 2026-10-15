from __future__ import annotations

import json

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
