"""共用：以模擬資料連跑數個交易日（MockInputs＋範本推論），供各測試重複使用。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.agent.inputs import MockInputs
from src.agent.reasoner import TemplateReasoner
from src.agent.run_daily import run_day
from src.agent.state import PortfolioState, settle

START = "2026-10-27"


@dataclass
class MockRun:
    inputs: MockInputs
    out_dir: object
    days: list = field(default_factory=list)       # (trade_date, 執行前帳本, DayResult)
    final: PortfolioState = None


def run_mock(inputs, out_dir, n_days: int, start: str = START, state: PortfolioState | None = None) -> MockRun:
    run = MockRun(inputs, out_dir)
    state = state or PortfolioState.initial()
    day = start
    for _ in range(n_days):
        r = run_day(day, mode="mock", team_id="MOCK", inputs=inputs, state=state, reasoner=TemplateReasoner(),
                    out_dir=out_dir)
        run.days.append((day, state, r))
        state = settle(state, day, r.orders, inputs.fill_prices(day), inputs.close_prices(day))
        day = inputs.next_trading_day(day)
    run.final = state
    return run


@pytest.fixture(scope="session")
def mock_inputs():
    return MockInputs()


@pytest.fixture(scope="session")
def mock_run(mock_inputs, tmp_path_factory):
    return run_mock(mock_inputs, tmp_path_factory.mktemp("mock"), 5)
