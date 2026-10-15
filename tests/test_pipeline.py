"""每日管線：多日連跑零 REJECT（posture 回歸）、黑箱重現、正式模式防呆、策略引擎規則。"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from src.agent import rules, strategy as S
from src.agent.factor_engine import build_targets, cap_weights, target_shares
from src.agent.reasoner import TemplateReasoner
from src.agent.reproduce import reproduce
from src.agent.run_daily import run_day
from src.agent.state import PortfolioState

from conftest import run_mock


def test_contest_length_zero_reject(mock_inputs, tmp_path):
    """初賽 24 個交易日：每天零 REJECT，結算後持股 20–30 檔、現金 0–25%（C12 posture 回歸）"""
    run = run_mock(mock_inputs, tmp_path, 24)
    for trade_date, _, r in run.days:
        bad = [c.rule for c in r.checks if c.level == "REJECT" and not c.passed]
        assert not bad, (trade_date, bad)
    s = run.final
    assert rules.MIN_HOLDINGS <= len(s.held_tickers()) <= rules.MAX_HOLDINGS
    assert 0 <= s.cash / s.nav <= rules.CASH_MAX


def test_reproduce_from_bundle(mock_run):
    trade_date, _, r = mock_run.days[1]
    assert reproduce(r.path.parent / f"inputs_MOCK_{trade_date}.json")


def test_live_rejects_mock_components(mock_inputs, tmp_path):
    with pytest.raises(RuntimeError, match="模擬元件"):
        run_day("2026-10-27", mode="live", team_id="TEAM_11161", inputs=mock_inputs,
                state=PortfolioState.initial(), reasoner=TemplateReasoner(), out_dir=tmp_path)


def test_mock_team_id_guard(mock_inputs, tmp_path):
    with pytest.raises(RuntimeError, match="team_id"):
        run_day("2026-10-27", mode="mock", team_id="TEAM_11161", inputs=mock_inputs,
                state=PortfolioState.initial(), reasoner=TemplateReasoner(), out_dir=tmp_path)


def test_live_rejects_holiday(tmp_path):
    class RealLooking:
        is_mock = False
    with pytest.raises(RuntimeError, match="休市"):
        run_day("2026-10-26", mode="live", team_id="TEAM_11161", inputs=RealLooking(),
                state=PortfolioState.initial(), reasoner=TemplateReasoner(), out_dir=tmp_path)


def test_official_order_formula():
    assert target_shares(0.05, Decimal(1_000_000_000), 1000) == 50_000
    # 無條件捨去到整張：0.0899999 × 10 億 ÷ 1500 ÷ 1000 = 59.99… → 59 張
    assert target_shares(0.0899999, Decimal(1_000_000_000), 1500) == 59_000


def test_cap_weights_respects_caps():
    raw = pd.Series([10.0, 1, 1, 1, 1], index=list("abcde"))
    w = cap_weights(raw, pd.Series(0.3, index=raw.index), 0.95)
    assert abs(w.sum() - 0.95) < 1e-9 and (w <= 0.3 + 1e-12).all()


def test_targets_active_share_and_caps(mock_inputs):
    day = mock_inputs.day("2026-10-27")
    t = build_targets(day.master, day.pv, day.fund, None, [], day.benches)
    assert len(t.picks) == S.N_HOLDINGS
    assert t.as_report["active_share"].min() >= S.AS_THRESHOLD
    caps = t.picks.index.map(lambda x: S.CAP_2330 if x == rules.TSMC else S.CAP_OTHER)
    assert (t.picks["weight"] <= caps + 1e-4).all()


def test_veto_high_negative_news_below_ma(mock_inputs):
    """重大利空＋跌破 60 日均價者不得買進"""
    day = mock_inputs.day("2026-10-27")
    news = pd.DataFrame({"news_score": 0.0, "worst_event": "high:法律監管:測試"}, index=day.master.index)
    t = build_targets(day.master, day.pv, day.fund, news, [], day.benches)
    below = set(day.pv.index[day.pv["A3"] < 0])
    assert below and set(t.vetoed.index) == below and not (set(t.picks.index) & below)
