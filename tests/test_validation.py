from pathlib import Path
import unittest

from tw_stock_agent.builder import build_universe
from tw_stock_agent.validation import load_and_validate, validate_security_master, validate_universe_audit


ROOT = Path(__file__).parents[1]


class UniverseValidationTests(unittest.TestCase):
    def test_example_security_master_warns_but_does_not_fail(self) -> None:
        issues = load_and_validate(ROOT / "data/examples/security_master.csv", "security_master", "example")
        self.assertTrue(issues)
        self.assertFalse(any(issue.severity == "error" for issue in issues))
        self.assertEqual({issue.code for issue in issues}, {"weight_rule_pending"})

    def test_production_requires_full_150_and_weights(self) -> None:
        issues = load_and_validate(ROOT / "data/examples/security_master.csv", "security_master", "production")
        self.assertTrue({issue.code for issue in issues} >= {"weight_rule_pending", "universe_size", "non_official_source"})
        self.assertTrue(any(issue.severity == "error" for issue in issues))

    def test_duplicate_ticker_is_rejected(self) -> None:
        row = {
            "universe_id": "u", "ticker": "0050", "name": "x", "market": "TWSE", "industry": "x",
            "security_type": "COMMON_STOCK", "listing_date": "", "is_eligible": "true", "max_weight": "0.1",
            "source": "organizer", "source_reference": "x", "source_as_of_date": "2026-07-31",
            "updated_at": "2026-10-06T09:00:00+08:00",
        }
        issues = validate_security_master([row, dict(row)])
        self.assertTrue(any(issue.code == "duplicate_key" for issue in issues))

    def test_production_enforces_published_weight_limits(self) -> None:
        row = {
            "universe_id": "u", "ticker": "2330", "name": "x", "market": "TWSE", "industry": "x",
            "security_type": "COMMON_STOCK", "listing_date": "", "is_eligible": "true", "max_weight": "0.10",
            "source": "TWSE", "source_reference": "official", "source_as_of_date": "2026-07-31",
            "updated_at": "2026-10-06T09:00:00+08:00",
        }
        issues = validate_security_master([row], "production")
        self.assertTrue(any(issue.code == "competition_weight" for issue in issues))

    def test_audit_reconciles_calculated_market_cap(self) -> None:
        issues = load_and_validate(ROOT / "data/examples/universe_audit.csv", "universe_audit")
        self.assertEqual(issues, [])
        bad = [{"universe_id": "u", "cutoff_date": "2026-07-31", "ticker": "0050", "name": "x", "market": "TWSE", "industry": "x", "closing_price": "10", "shares_outstanding": "100", "market_cap": "999", "market_cap_rank": "1", "security_type": "COMMON_STOCK", "listing_date": "", "market_cap_method": "CLOSE_X_COMMON_SHARES", "max_weight": "", "weight_rule_reference": "", "source": "x", "source_reference": "x", "retrieved_at": "2026-10-06T09:00:00+08:00"}]
        self.assertTrue(any(issue.code == "market_cap_reconciliation" for issue in validate_universe_audit(bad)))

    def test_builder_ranks_each_market_and_keeps_tpex_missing_inputs_blank(self) -> None:
        records = [
            {"ticker": "2330", "name": "a", "market": "TWSE", "industry": "x", "security_type": "COMMON_STOCK", "closing_price": 10, "shares_outstanding": 100, "is_eligible": "true"},
            {"ticker": "2317", "name": "b", "market": "TWSE", "industry": "x", "security_type": "COMMON_STOCK", "closing_price": 20, "shares_outstanding": 100, "is_eligible": "true"},
            {"ticker": "5274", "name": "c", "market": "TPEX", "industry": "x", "security_type": "COMMON_STOCK", "market_cap": 50, "is_eligible": "true"},
            {"ticker": "0050", "name": "excluded", "market": "TWSE", "industry": "x", "security_type": "ETF", "market_cap": 999},
        ]
        master, audit = build_universe(records, "u")
        self.assertEqual([row["ticker"] for row in audit], ["2317", "2330", "5274"])
        self.assertEqual([row["market_cap_rank"] for row in audit], [1, 2, 1])
        self.assertIsNone(audit[-1]["closing_price"])
        self.assertEqual(master[-1]["ticker"], "5274")
