from __future__ import annotations

from src.agent import rules, strategy
from src.agent.governance import production_manifest, write_manifest


def test_manifest_is_stable_and_records_contest_limits(tmp_path):
    first = production_manifest()
    second = production_manifest()
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["theme"] == strategy.THEME_NAME
    assert first["production_model"]["score_weights"] == strategy.SCORE_WEIGHTS
    assert first["production_model"]["fundamental_score_weights"] == strategy.FUND_SCORE_WEIGHTS
    assert first["production_model"]["selection"]["holding_buffer_rank"] == 50
    assert first["production_model"]["selection"]["no_trade_band"] == 0.03
    assert first["contest_limits"]["cash_max"] == rules.CASH_MAX

    path = tmp_path / "strategy_manifest.json"
    assert write_manifest(path)["manifest_sha256"] == first["manifest_sha256"]
    assert path.exists()
