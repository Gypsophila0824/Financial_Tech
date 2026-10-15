"""模組間傳遞的資料結構。

所有工具都回傳 ToolResult：data 是工具結果，evidence 是這份結果背後的資料來源與事實。
各層用暫時 key 互相引用（例如 "obs:tw_market"），最後由 dplan_writer 統一編成 S1/O1/I1/D1。
文字裡要引用其他項目時寫 [[key]]，dplan_writer 會換成正式編號。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class Source:
    key: str
    authority: str              # twse / tpex / taifex / mops / fininst / media / vendor / other
    url: str
    content_as_of: str          # 內容描述的時點（非抓取時間），+08:00
    name: str | None = None
    archive_url: str | None = None
    published_at: str | None = None
    fetched_at: str | None = None


@dataclass
class Observation:
    key: str
    source_keys: list[str]
    statement: str              # 只寫可核對的事實，不寫判斷
    values: dict[str, float]    # 只放數字
    topic: str | None = None


@dataclass
class Evidence:
    sources: dict[str, Source] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)

    def add_source(self, source: Source) -> str:
        self.sources[source.key] = source
        return source.key

    def add_observation(self, obs: Observation) -> str:
        missing = [k for k in obs.source_keys if k not in self.sources]
        if missing:
            raise KeyError(f"{obs.key} 引用了不存在的來源 {missing}")
        self.observations[obs.key] = obs
        return obs.key

    def merge(self, other: Evidence) -> None:
        for layer in ("sources", "observations"):
            mine, theirs = getattr(self, layer), getattr(other, layer)
            clash = set(mine) & set(theirs)
            if clash:
                raise KeyError(f"{layer} key 重複：{sorted(clash)}")
            mine.update(theirs)


@dataclass
class ToolResult(Generic[T]):
    data: T
    evidence: Evidence = field(default_factory=Evidence)
    is_mock: bool = False


@dataclass
class MarketViewDraft:
    basis_keys: list[str]
    logic: str
    regime: str                 # risk_on / neutral / risk_off
    stance: str                 # aggressive / neutral / defensive
    net_exposure_intent: str    # increase / hold / reduce
    cash_range: tuple[float, float]
    counter_evidence: str | None
    confidence: float | None = None


@dataclass
class InferenceDraft:
    key: str
    premise_keys: list[str]
    logic: str
    conclusion: str | None
    counter_evidence: str | None
    confidence: float | None = None


@dataclass
class TargetPosition:
    ticker: str
    target_weight: float
    tag: str                    # new / rebalance / hold_band / exit / cap_trim / hold_invalid


@dataclass
class DecisionDraft:
    key: str
    ticker: str
    action: str                 # BUY / ADD / TRIM / SELL_ALL
    target_weight: float
    inference_keys: list[str]
    risk_check: str | None = None
    funding_for_keys: list[str] | None = None


@dataclass
class NoTradeDraft:
    ticker: str
    reason_keys: list[str]
    reason: str | None = None


@dataclass
class OrderDraft:
    ticker: str
    side: str                   # BUY / SELL
    shares: int
    decision_key: str
    derivation_detail: str | None = None
