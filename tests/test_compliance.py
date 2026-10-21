"""本地驗證器：正常 D-Plan 全數通過；每種違規（含反例 043 三項教訓）都會被標為 REJECT。"""

from __future__ import annotations

import copy
from functools import lru_cache

import pytest

from src.agent import rules
from src.agent.compliance import ComplianceContext, run_checks, submit_window
from src.agent.tools.trading_calendar import is_submit_day
from src.agent.tools.universe import load_universe

BUSY = 4          # 模擬第 5 日（11/02）：有買進、減碼、出清與不交易


def context(run, i, *, live=False, filename=None, benches=None):
    trade_date, state, r = run.days[i]
    close = _close(run, trade_date)
    if benches is None:
        benches = {k: v.to_dict() for k, v in run.inputs.day(trade_date).benches.items()}
    return ComplianceContext(state=state, prev_close=close, universe=load_universe(), benchmarks=benches,
                             filename=filename or r.path.name, live=live, data_notes=[])


_cache = {}


def _close(run, trade_date):
    if trade_date not in _cache:
        _cache[trade_date] = {t: float(p) for t, p in run.inputs.day(trade_date).close.items()}
    return _cache[trade_date]


def rejected(checks) -> set[str]:
    return {c.rule for c in checks if c.level == "REJECT" and not c.passed}


def plan_of(run, i) -> dict:
    return copy.deepcopy(run.days[i][2].plan)


def test_every_mock_day_passes(mock_run):
    for trade_date, _, r in mock_run.days:
        assert not rejected(r.checks), (trade_date, rejected(r.checks))


def test_c1_dangling_reference(mock_run):
    """反例 043 ①：決策引用不存在的推論"""
    plan = plan_of(mock_run, 0)
    plan["decisions"][0]["inference_refs"] = ["I999"]
    assert "C1 引用完整（無孤兒）" in rejected(run_checks(plan, context(mock_run, 0)))


def test_c2_order_not_formula(mock_run):
    """反例 043 ②：委託股數不等於官方公式"""
    plan = plan_of(mock_run, 0)
    plan["orders"][0]["shares"] += 1000
    assert "C2 委託逐筆等於官方公式" in rejected(run_checks(plan, context(mock_run, 0)))


def test_c12_posture_contradiction(mock_run):
    """反例 043 ③：宣告與實際淨流向相反、現金不在宣告區間"""
    plan = plan_of(mock_run, 0)                    # 首日全數買進，淨買超
    plan["market_view"]["posture"]["net_exposure_intent"] = "reduce"
    plan["market_view"]["posture"]["target_cash_pct_range"] = [0.15, 0.25]
    bad = rejected(run_checks(plan, context(mock_run, 0)))
    assert {"C12 淨曝險意圖與委託一致", "C12 預估現金落在宣告區間"} <= bad


def test_c13_oversell(mock_run):
    plan = plan_of(mock_run, 0)
    t = plan["orders"][0]["ticker"]
    plan["orders"].append({"ticker": t, "side": "SELL", "shares": 1000,
                           "decision_ref": plan["decisions"][0]["decision_id"]})
    assert "C13 賣出不超過持股" in rejected(run_checks(plan, context(mock_run, 0)))


def test_every_holding_must_be_explained(mock_run):
    plan = plan_of(mock_run, BUSY)
    assert plan["no_trade_decisions"], "應有不交易的持股"
    plan["no_trade_decisions"].pop()
    assert "昨日持股全數交代（擇一不重複）" in rejected(run_checks(plan, context(mock_run, BUSY)))


def test_holdings_count_below_20(mock_run):
    plan = plan_of(mock_run, 0)
    drop = {d["decision_id"] for d in plan["decisions"][:6]}
    plan["decisions"] = [d for d in plan["decisions"] if d["decision_id"] not in drop]
    plan["orders"] = [o for o in plan["orders"] if o["decision_ref"] not in drop]
    renum = {d["decision_id"]: f"D{i}" for i, d in enumerate(plan["decisions"], 1)}   # 維持 ID 連續
    for d in plan["decisions"]:
        d["decision_id"] = renum[d["decision_id"]]
    for o in plan["orders"]:
        o["decision_ref"] = renum[o["decision_ref"]]
    assert "六(十四)(3) 持股 20–30 檔" in rejected(run_checks(plan, context(mock_run, 0)))


def test_outside_universe(mock_run):
    plan = plan_of(mock_run, 0)
    plan["decisions"][0]["ticker"] = "9999"
    assert "六(七) 僅限 150 檔" in rejected(run_checks(plan, context(mock_run, 0)))


def test_filename(mock_run):
    plan = plan_of(mock_run, BUSY)
    assert "檔名規約" in rejected(run_checks(plan, context(mock_run, BUSY, filename="D-Plan_MOCK_2026-10-28_v2.json")))


def test_submit_window(mock_run):
    plan = plan_of(mock_run, 0)                    # 10/27
    assert plan["trade_date"] == "2026-10-27"
    plan["agent_metadata"]["run_completed_at"] = "2026-10-27T09:00:00+08:00"
    assert "六(三) 繳交時窗" in rejected(run_checks(plan, context(mock_run, 0, live=True)))
    plan["agent_metadata"]["run_completed_at"] = "2026-10-27T08:30:00+08:00"
    assert "六(三) 繳交時窗" not in rejected(run_checks(plan, context(mock_run, 0, live=True)))


def test_window_is_previous_calendar_day():
    """10/05 更新版正文範例：10/27 報告時窗為 10/26 19:30～10/27 08:55"""
    from datetime import date, timedelta, timezone
    open_, close_ = submit_window(date(2026, 10, 27), timezone(timedelta(hours=8)))
    assert (open_.isoformat(), close_.isoformat()) == ("2026-10-26T19:30:00+08:00", "2026-10-27T08:55:00+08:00")


def test_no_dplan_for_oct26():
    """Slack 答覆：10/26 休市，沒有 D-Plan"""
    assert not is_submit_day("2026-10-26") and is_submit_day("2026-10-27")


def test_active_share_low_is_warning_only(mock_run):
    """連續 2 日低於 15% 才取消資格：單日偏低只警示（仍須繳交，不交會記未繳交警告）"""
    trade_date, state, r = mock_run.days[BUSY]
    plan = plan_of(mock_run, BUSY)
    ctx = context(mock_run, BUSY)
    from src.agent.compliance import estimate_post_trade
    post, _, nav, _, _ = estimate_post_trade({t: s for t, s in state.holdings.items() if s > 0}, state.cash,
                                             plan["orders"], ctx.prev_close)
    ours = dict(sorted(((t, s * ctx.prev_close[t] / nav) for t, s in post.items()), key=lambda kv: -kv[1])[:rules.ACTIVE_SHARE_TOP_N])
    checks = run_checks(plan, context(mock_run, BUSY, benches={"CLONE": ours}))
    warn = next(c for c in checks if c.rule == f"六(十三) Active Share ≥ {rules.ACTIVE_SHARE_MIN:.0%}")
    assert warn.level == "WARN" and not warn.passed and not rejected(checks)


def test_missing_benchmarks_rejected(mock_run):
    assert f"六(十三) Active Share ≥ {rules.ACTIVE_SHARE_MIN:.0%}" in rejected(run_checks(plan_of(mock_run, BUSY), context(mock_run, BUSY, benches={})))


def test_cash_limit_inclusive():
    """Slack 答覆：現金恰好 25% 不違規"""
    assert rules.CASH_MAX == 0.25


def test_sell_all_must_have_zero_weight(mock_run):
    plan = plan_of(mock_run, BUSY)
    held = set(mock_run.days[BUSY][1].holdings)
    d = next((d for d in plan["decisions"] if d["ticker"] in held), None)
    if d is None:
        pytest.skip("沒有對持股的決策")
    d["action"], d["target_weight"] = "SELL_ALL", 0.01
    assert "action 與持股一致" in rejected(run_checks(plan, context(mock_run, BUSY)))
