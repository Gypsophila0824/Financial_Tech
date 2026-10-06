from __future__ import annotations

SECURITY_MASTER_COLUMNS = (
    "universe_id", "ticker", "name", "market", "industry", "security_type",
    "listing_date", "is_eligible", "max_weight", "source", "source_reference",
    "source_as_of_date", "updated_at",
)

UNIVERSE_AUDIT_COLUMNS = (
    "universe_id", "cutoff_date", "ticker", "name", "market", "industry",
    "closing_price", "shares_outstanding", "market_cap", "market_cap_rank",
    "security_type", "listing_date", "market_cap_method", "max_weight",
    "weight_rule_reference", "source", "source_reference", "retrieved_at",
)

MARKETS = frozenset({"TWSE", "TPEX"})
COMMON_STOCK = "COMMON_STOCK"
