"""Fail-closed validations for fixed-universe files."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .contracts import COMMON_STOCK, MARKETS, SECURITY_MASTER_COLUMNS, UNIVERSE_AUDIT_COLUMNS


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str


def _blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _valid_datetime(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def validate_security_master(rows: list[dict[str, str]], profile: str = "example") -> list[Issue]:
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for number, row in enumerate(rows, start=2):
        missing = [column for column in SECURITY_MASTER_COLUMNS if column not in row]
        if missing:
            issues.append(Issue("error", "missing_columns", f"row {number}: missing columns {missing}"))
            continue
        key = (row["universe_id"], row["ticker"])
        if key in seen:
            issues.append(Issue("error", "duplicate_key", f"row {number}: duplicate universe_id+ticker {key}"))
        seen.add(key)
        for column in ("universe_id", "ticker", "name", "industry", "security_type", "source", "source_as_of_date", "updated_at"):
            if _blank(row[column]):
                issues.append(Issue("error", "required", f"row {number}: {column} is required"))
        if not row["ticker"].isdigit() or not 4 <= len(row["ticker"]) <= 6:
            issues.append(Issue("error", "ticker", f"row {number}: ticker must be a 4–6 digit text identifier"))
        if row["market"] not in MARKETS:
            issues.append(Issue("error", "market", f"row {number}: market must be TWSE or TPEX"))
        if row["security_type"] != COMMON_STOCK:
            issues.append(Issue("error", "security_type", f"row {number}: only COMMON_STOCK is eligible"))
        if profile == "production" and row["source"] == "MOCK_NOT_OFFICIAL":
            issues.append(Issue("error", "non_official_source", f"row {number}: mock data cannot form a production universe"))
        if row["is_eligible"].lower() != "true":
            issues.append(Issue("error", "eligibility", f"row {number}: is_eligible must be true"))
        if not _valid_date(row["source_as_of_date"]):
            issues.append(Issue("error", "source_as_of_date", f"row {number}: invalid ISO date"))
        if not _valid_datetime(row["updated_at"]):
            issues.append(Issue("error", "updated_at", f"row {number}: timezone-aware ISO datetime required"))
        if _blank(row["max_weight"]):
            severity = "error" if profile == "production" else "warning"
            issues.append(Issue(severity, "weight_rule_pending", f"row {number}: max_weight is required for a competition-compliant universe"))
        else:
            try:
                weight = float(row["max_weight"])
                if not 0 < weight <= 1:
                    raise ValueError
                if profile == "production":
                    expected = 0.25 if row["ticker"] == "2330" else 0.10
                    if not math.isclose(weight, expected, abs_tol=1e-12):
                        issues.append(Issue("error", "competition_weight", f"row {number}: {row['ticker']} max_weight must be {expected}"))
            except ValueError:
                issues.append(Issue("error", "max_weight", f"row {number}: max_weight must be in (0, 1]"))
    if profile == "production":
        counts = {market: sum(row.get("market") == market for row in rows) for market in MARKETS}
        if len(rows) != 150 or counts["TWSE"] != 100 or counts["TPEX"] != 50:
            issues.append(Issue("error", "universe_size", f"production universe must be 150 rows (TWSE=100, TPEX=50), got {len(rows)} / {counts}"))
    return issues


def validate_universe_audit(rows: list[dict[str, str]]) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[tuple[str, str, str]] = set()
    for number, row in enumerate(rows, start=2):
        missing = [column for column in UNIVERSE_AUDIT_COLUMNS if column not in row]
        if missing:
            issues.append(Issue("error", "missing_columns", f"row {number}: missing columns {missing}"))
            continue
        key = (row["universe_id"], row["cutoff_date"], row["ticker"])
        if key in seen:
            issues.append(Issue("error", "duplicate_key", f"row {number}: duplicate audit key {key}"))
        seen.add(key)
        try:
            cap = float(row["market_cap"])
            rank = int(row["market_cap_rank"])
            if not math.isfinite(cap) or cap <= 0 or rank < 1:
                raise ValueError
        except ValueError:
            issues.append(Issue("error", "market_cap", f"row {number}: positive market_cap and rank are required"))
        method = row["market_cap_method"]
        if method not in {"CLOSE_X_COMMON_SHARES", "OFFICIAL_MARKET_CAP_REPORT"}:
            issues.append(Issue("error", "market_cap_method", f"row {number}: unsupported market-cap method"))
        if method == "CLOSE_X_COMMON_SHARES":
            try:
                expected = float(row["closing_price"]) * float(row["shares_outstanding"])
                if not math.isclose(expected, cap, rel_tol=1e-9):
                    issues.append(Issue("error", "market_cap_reconciliation", f"row {number}: market_cap does not equal close × shares"))
            except (TypeError, ValueError):
                issues.append(Issue("error", "market_cap_inputs", f"row {number}: close and shares required for calculation method"))
    return issues


def load_and_validate(path: str | Path, table: str, profile: str = "example") -> list[Issue]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if table == "security_master":
        return validate_security_master(rows, profile)
    if table == "universe_audit":
        return validate_universe_audit(rows)
    raise ValueError(f"unsupported table: {table}")
