"""Build auditable fixed-universe outputs from normalized official-source records."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .contracts import COMMON_STOCK, SECURITY_MASTER_COLUMNS, UNIVERSE_AUDIT_COLUMNS


def _market_cap(row: dict[str, object]) -> tuple[float, str]:
    reported = row.get("market_cap")
    if reported not in (None, ""):
        return float(reported), "OFFICIAL_MARKET_CAP_REPORT"
    close, shares = row.get("closing_price"), row.get("shares_outstanding")
    if close in (None, "") or shares in (None, ""):
        raise ValueError(f"{row.get('ticker')}: market cap requires official report or close × common shares")
    return float(close) * float(shares), "CLOSE_X_COMMON_SHARES"


def build_universe(records: Iterable[dict[str, object]], universe_id: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Filter ordinary shares, calculate market cap, and rank independently by market."""
    audit: list[dict[str, object]] = []
    for source in records:
        if source.get("security_type") != COMMON_STOCK:
            continue
        cap, method = _market_cap(source)
        row = dict(source)
        row["universe_id"] = universe_id
        row["market_cap"] = cap
        row["market_cap_method"] = method
        audit.append(row)

    per_market: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in audit:
        per_market[str(row["market"])].append(row)
    for rows in per_market.values():
        rows.sort(key=lambda row: float(row["market_cap"]), reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["market_cap_rank"] = rank

    market_order = {"TWSE": 0, "TPEX": 1}
    audit.sort(key=lambda row: (market_order.get(str(row["market"]), 99), int(row["market_cap_rank"])))
    master = [{column: row.get(column) for column in SECURITY_MASTER_COLUMNS} for row in audit]
    normalized_audit = [{column: row.get(column) for column in UNIVERSE_AUDIT_COLUMNS} for row in audit]
    return master, normalized_audit


def write_csv(path: str | Path, fieldnames: tuple[str, ...], rows: Iterable[dict[str, object]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
