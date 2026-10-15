"""D-Plan Writer：把各層草稿組成 schema v4.2 的 D-Plan JSON。

只收錄實際被引用的事實與來源，並依引用順序統一編號
（S/O/I/D 從 1 開始、連續、不跳號、不加前導零），文字中的 [[key]] 換成正式編號。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import rules
from .models import (DecisionDraft, Evidence, InferenceDraft, MarketViewDraft, NoTradeDraft,
                     OrderDraft)

TOKEN = re.compile(r"\[\[([^\]]+)\]\]")


def _ordered_unique(keys):
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def assemble(
    *,
    team_id: str,
    trade_date: str,
    evidence: Evidence,
    market_view: MarketViewDraft,
    inferences: dict[str, InferenceDraft],
    decisions: list[DecisionDraft],
    no_trades: list[NoTradeDraft],
    orders: list[OrderDraft],
    agent_metadata: dict,
    note: str | None = None,
) -> dict:
    inf_keys = _ordered_unique(
        [k for d in decisions for k in d.inference_keys] + [k for n in no_trades for k in n.reason_keys])
    obs_keys = _ordered_unique(
        list(market_view.basis_keys) + [k for ik in inf_keys for k in inferences[ik].premise_keys])
    src_keys = _ordered_unique([k for ok in obs_keys for k in evidence.observations[ok].source_keys])

    ids = {}
    ids.update({k: f"S{i}" for i, k in enumerate(src_keys, 1)})
    ids.update({k: f"O{i}" for i, k in enumerate(obs_keys, 1)})
    ids.update({k: f"I{i}" for i, k in enumerate(inf_keys, 1)})
    ids.update({d.key: f"D{i}" for i, d in enumerate(decisions, 1)})

    def text(s: str | None) -> str | None:
        if s is None:
            return None
        return TOKEN.sub(lambda m: ids[m.group(1)], s)

    def drop_none(d: dict) -> dict:
        return {k: v for k, v in d.items() if v is not None}

    sources = []
    for k in src_keys:
        s = evidence.sources[k]
        sources.append(drop_none({
            "source_id": ids[k], "authority": s.authority, "name": s.name, "url": s.url,
            "archive_url": s.archive_url, "published_at": s.published_at,
            "content_as_of": s.content_as_of, "fetched_at": s.fetched_at}))

    observations = []
    for k in obs_keys:
        o = evidence.observations[k]
        observations.append(drop_none({
            "obs_id": ids[k], "source_ref": [ids[x] for x in o.source_keys], "topic": o.topic,
            "statement": o.statement, "values": o.values}))

    mv = market_view
    plan = {}
    if note:
        plan["_NOTE"] = note
    plan.update({
        "schema_version": rules.SCHEMA_VERSION,
        "doc_type": "D-Plan",
        "team_id": team_id,
        "trade_date": trade_date,
        "sources": sources,
        "observations": observations,
        "market_view": drop_none({
            "basis_refs": [ids[k] for k in mv.basis_keys],
            "logic": text(mv.logic),
            "regime": mv.regime,
            "stance": mv.stance,
            "posture": {"net_exposure_intent": mv.net_exposure_intent,
                        "target_cash_pct_range": [mv.cash_range[0], mv.cash_range[1]]},
            "counter_evidence": text(mv.counter_evidence),
            "confidence": mv.confidence,
        }),
        "inferences": [],
        "decisions": [],
        "no_trade_decisions": [],
        "orders": [],
        "agent_metadata": agent_metadata,
    })
    plan["market_view"].setdefault("counter_evidence", None)

    for k in inf_keys:
        inf = inferences[k]
        plan["inferences"].append(drop_none({
            "inf_id": ids[k], "premise_refs": [ids[x] for x in inf.premise_keys],
            "logic": text(inf.logic), "conclusion": text(inf.conclusion),
            "counter_evidence": text(inf.counter_evidence), "confidence": inf.confidence}))
        plan["inferences"][-1].setdefault("counter_evidence", None)

    for d in decisions:
        plan["decisions"].append(drop_none({
            "decision_id": ids[d.key], "ticker": d.ticker, "action": d.action,
            "target_weight": d.target_weight, "inference_refs": [ids[x] for x in d.inference_keys],
            "risk_check": d.risk_check,
            "funding_for": [ids[x] for x in d.funding_for_keys] if d.funding_for_keys else None}))
    for n in no_trades:
        plan["no_trade_decisions"].append(drop_none({
            "ticker": n.ticker, "reason_refs": [ids[x] for x in n.reason_keys], "reason": n.reason}))
    for o in orders:
        plan["orders"].append(drop_none({
            "ticker": o.ticker, "side": o.side, "shares": o.shares,
            "decision_ref": ids[o.decision_key], "derivation_detail": o.derivation_detail}))
    return plan


def filename(team_id: str, trade_date: str) -> str:
    return f"D-Plan_{team_id}_{trade_date}.json"


def write(plan: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename(plan["team_id"], plan["trade_date"])
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
