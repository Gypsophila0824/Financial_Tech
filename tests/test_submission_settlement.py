"""離線整合測試：真實提交封存／結算函式，僅替換行情、時鐘與輸出路徑。"""
import json
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from src.agent import live, submissions
from src.agent.active_share_risk import EXPECTED_ETFS
from src.agent.inputs import StoreInputs, MockInputs
from src.agent.models import OrderDraft
from src.agent.state import PortfolioState, settle
from src.agent.backtest.runner import run_formal_backtest

DAY, TEAM = "2026-10-28", "TEST_TEAM"


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 1, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(submissions, "datetime", Clock)
    monkeypatch.setattr(live, "ROOT", tmp_path)
    monkeypatch.setattr(live, "OUT", tmp_path / "live")
    monkeypatch.setattr(live, "LEDGER", tmp_path / "state" / "ledger.json")
    monkeypatch.setattr(live, "LOG_DIR", tmp_path / "logs")
    # 測試只跑離線入口；誤觸網路抓取立即失敗。
    monkeypatch.setattr(live, "run_module", lambda *a: pytest.fail("不得呼叫外部抓取"))
    state = PortfolioState("2026-10-27", 1_000_000, 900_000,
                           {"2330": 1000}, last_close={"2330": 100})
    state.save(live.LEDGER)
    prices = {"fill": {"2330": 100.0}, "close": {"2330": 110.0}}

    class Inputs:
        def __init__(self, **kwargs):
            self.etf_dates = {code: DAY for code in EXPECTED_ETFS}

        def fill_prices(self, day):
            return prices["fill"]

        def close_prices(self, day):
            return prices["close"]

        def _benches(self, day, notes):
            return {code: pd.Series({"OTHER": .5}) for code in EXPECTED_ETFS}, DAY

    monkeypatch.setattr(live, "StoreInputs", Inputs)
    receipt = tmp_path / "receipt.txt"
    receipt.write_text("synthetic platform receipt", encoding="utf-8")
    official = tmp_path / "official.json"
    args = SimpleNamespace(trade_date=DAY, team_id=TEAM, skip_fetch=True,
                           official_state=official, receipt_file=receipt,
                           no_trades=False, invalidated=False, reason=None)

    def run(name, shares=1000, passed=True):
        path = live.OUT / "runs" / name
        path.mkdir(parents=True)
        plan = {"team_id": TEAM, "trade_date": DAY,
                "orders": [{"ticker": "2330", "side": "BUY", "shares": shares, "decision_ref": "D1"}]}
        write_json(path / f"D-Plan_{TEAM}_{DAY}.json", plan)
        write_json(path / f"inputs_{TEAM}_{DAY}.json", {"state": asdict(state)})
        submissions.seal_run(path, TEAM, DAY, passed)
        return path

    def confirm(path, time="08:00:00"):
        return submissions.confirm_submission(live.OUT, path, TEAM, DAY, receipt,
                                              f"{DAY}T{time}+08:00")

    def official_for(shares=1000, status=None):
        orders = [OrderDraft("2330", "BUY", shares, "D1")] if shares else []
        expected = settle(state, DAY, orders, prices["fill"], prices["close"])
        data = {"team_id": TEAM, "as_of": DAY, "holdings": expected.holdings,
                "cash": expected.cash, "nav": expected.nav}
        if status:
            data["trade_status"] = status
        write_json(official, data)
        return data

    return SimpleNamespace(run=run, confirm=confirm, official_for=official_for, args=args,
                           state=state, prices=prices, receipt=receipt)


def assert_blocked(env):
    before = live.LEDGER.read_bytes()
    assert live.cmd_settle(env.args) != 0
    assert live.LEDGER.read_bytes() == before
    assert not (live.LEDGER.parent / "history" / f"settlement_{DAY}.json").exists()


def test_latest_platform_time_not_registration_or_generation_order(env):
    old = env.run("old", 1000)
    new = env.run("new", 2000)
    accepted = env.confirm(new, "08:30:00")
    env.confirm(old, "08:00:00")  # 晚登錄的較早提交不可覆蓋較晚提交
    env.run("never_submitted", 9000)
    assert submissions.latest_submission(live.OUT, TEAM, DAY)[0] == accepted
    env.official_for(2000)
    assert live.cmd_settle(env.args) == 0
    assert PortfolioState.load(live.LEDGER).holdings == {"2330": 3000}


def test_generated_without_submission_cannot_settle(env):
    env.run("unsubmitted")
    env.official_for()
    assert_blocked(env)


@pytest.mark.parametrize("filename", ["receipt.bin", f"D-Plan_{TEAM}_{DAY}.json",
                                     f"inputs_{TEAM}_{DAY}.json"])
def test_tampered_submission_blocks_without_ledger_write(env, filename):
    accepted = env.confirm(env.run("first"))
    (accepted / filename).write_bytes(b"tampered")
    env.official_for()
    assert_blocked(env)


@pytest.mark.parametrize("case", ["failed_checks", "tampered_run", "empty_receipt", "duplicate_time"])
def test_submission_registration_guards(env, case):
    path = env.run("run", passed=case != "failed_checks")
    if case == "tampered_run":
        (path / f"D-Plan_{TEAM}_{DAY}.json").write_bytes(b"tampered")
    elif case == "empty_receipt":
        env.receipt.write_bytes(b"")
    elif case == "duplicate_time":
        env.confirm(path)
    with pytest.raises(ValueError):
        env.confirm(path)


@pytest.mark.parametrize("field,value", [
    ("holdings", {"2330": 3000}), ("cash", 1), ("nav", 1),
    ("team_id", "OTHER"), ("as_of", "2026-10-27"),
])
def test_official_mismatch_leaves_ledger_unchanged(env, field, value):
    env.confirm(env.run("first"))
    data = env.official_for()
    data[field] = value
    write_json(env.args.official_state, data)
    assert_blocked(env)


def test_no_submission_marks_to_market_without_average_price(env):
    env.args.no_trades, env.args.reason = True, "未提交；已核對平台"
    env.prices["fill"] = {}
    env.official_for(0)
    assert live.cmd_settle(env.args) == 0
    state = PortfolioState.load(live.LEDGER)
    assert (state.as_of, state.holdings, state.cash, state.nav) == (
        DAY, env.state.holdings, env.state.cash, 1_010_000)


def test_submitted_cannot_use_no_trades_shortcut(env):
    env.confirm(env.run("first"))
    env.args.no_trades, env.args.reason = True, "attempt shortcut"
    env.official_for(0)
    assert_blocked(env)


def test_invalidated_preserves_submitted_orders_and_marks_to_market(env):
    accepted = env.confirm(env.run("first"))
    sealed = (accepted / f"D-Plan_{TEAM}_{DAY}.json").read_bytes()
    env.args.invalidated, env.args.reason = True, "官方通知當日交易無效"
    env.official_for(0, "invalidated")
    env.prices["fill"] = {}  # 無成交不要求委託均價
    assert live.cmd_settle(env.args) == 0
    state = PortfolioState.load(live.LEDGER)
    assert state.holdings == env.state.holdings
    assert state.cash == env.state.cash
    assert state.nav == 1_010_000 and state.as_of == DAY
    audit = json.loads((live.LEDGER.parent / "history" / f"settlement_{DAY}.json").read_text())
    assert audit["orders"] == []
    assert audit["submitted_orders"][0]["shares"] == 1000
    assert audit["settlement_status"] == "invalidated"
    assert audit["submission_dir"] == str(accepted)
    assert (accepted / f"D-Plan_{TEAM}_{DAY}.json").read_bytes() == sealed


@pytest.mark.parametrize("case", ["no_submission", "no_status", "no_reason", "no_evidence", "no_flag"])
def test_invalidation_requires_submission_status_reason_evidence_and_flag(env, case):
    if case != "no_submission":
        env.confirm(env.run("first"))
    env.args.invalidated = case != "no_flag"
    env.args.reason = "" if case == "no_reason" else "官方判定無效"
    env.official_for(0, None if case == "no_status" else "invalidated")
    if case == "no_evidence":
        env.receipt.write_bytes(b"")
    assert_blocked(env)


def test_duplicate_settlement_cannot_deduct_twice(env):
    env.confirm(env.run("first"))
    env.official_for()
    assert live.cmd_settle(env.args) == 0
    before = live.LEDGER.read_bytes()
    assert live.cmd_settle(env.args) != 0
    assert live.LEDGER.read_bytes() == before


def test_missing_fill_stops_before_ledger_write(env):
    env.confirm(env.run("first"))
    env.official_for()
    env.prices["fill"] = {}
    assert_blocked(env)


def test_missing_close_stops_even_on_no_trade_day(env):
    env.args.no_trades, env.args.reason = True, "未提交"
    env.official_for(0)
    env.prices["close"] = {}
    assert_blocked(env)


def test_store_average_price_never_falls_back_to_close():
    inputs = StoreInputs.__new__(StoreInputs)
    inputs._daily = pd.DataFrame({
        "date": [DAY] * 5, "ticker": ["1", "2", "3", "4", "5"],
        "avg_price": [float("nan"), 0, -1, float("inf"), 100], "close": [110] * 5,
    })
    assert inputs.fill_prices(DAY) == {"5": 100}


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf")])
def test_settlement_rejects_invalid_fill(price):
    fill = {} if price is None else {"2330": price}
    with pytest.raises(ValueError, match="成交均價"):
        settle(PortfolioState.initial(), DAY, [OrderDraft("2330", "BUY", 1000, "D1")],
               fill, {"2330": 110})


def test_backtest_rejects_missing_average():
    class MissingAverage(MockInputs):
        def fill_prices(self, day):
            return {}
    with pytest.raises(ValueError, match="成交均價"):
        run_formal_backtest(MissingAverage(), start="2026-10-27", days=1)


def test_recover_entry_is_idempotent_after_settlement(env):
    env.confirm(env.run('recover_entry'))
    env.official_for()
    assert live.cmd_settle(env.args) == 0
    before = live.LEDGER.read_bytes()
    assert live.cmd_recover(env.args) == 0
    assert live.cmd_recover(env.args) == 0
    assert live.LEDGER.read_bytes() == before


def test_recover_entry_after_interrupted_live_commit(env, monkeypatch):
    from src.agent import settlement_transaction as tx
    env.confirm(env.run('crash_entry'))
    env.official_for()
    real = tx.atomic_write
    def crash(path, data):
        if path == live.LEDGER:
            raise OSError('synthetic interruption before ledger replace')
        real(path, data)
    monkeypatch.setattr(tx, 'atomic_write', crash)
    assert live.cmd_settle(env.args) != 0
    assert PortfolioState.load(live.LEDGER).as_of == env.state.as_of
    monkeypatch.setattr(tx, 'atomic_write', real)
    assert live.cmd_recover(env.args) == 0
    assert PortfolioState.load(live.LEDGER).holdings == {'2330': 2000}
