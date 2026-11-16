from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from embodied_portfolio.cli import demo_payloads
from embodied_portfolio.contracts import PortfolioError
from embodied_portfolio.decision import PortfolioService


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "portfolio.db"
        self.service = PortfolioService(self.path)
        self.addCleanup(self.service.close)
        self.requirement, self.offers = demo_payloads()
        self.service.import_requirement(self.requirement)
        for offer in self.offers:
            self.service.import_offer(offer)

    def planned(self):
        return self.service.plan("warehouse_trial")

    def test_plan_records_coverages_and_quotes(self):
        analysis = self.planned()
        report = analysis["report"]
        self.assertEqual(analysis["state"], "draft")
        self.assertTrue(report["feasible"])
        self.assertEqual(report["cost"], "150.00")
        self.assertEqual(report["coverage"]["total_duplicate"], 1)
        self.assertEqual(len(report["catalog_versions"]), 2)
        self.assertEqual(report["cost_per_credited_sample"], "37.50")

    def test_approve_requires_matching_input_digest(self):
        analysis = self.planned()
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], "incorrect")
        self.assertEqual(caught.exception.code, "stale_input")
        self.assertEqual(self.service.analysis(analysis["id"])["state"], "draft")

    def test_approved_snapshot_verifies(self):
        analysis = self.planned()
        approved = self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(approved["state"], "approved")
        self.assertTrue(self.service.verify(analysis["id"])["verified"])
        self.assertEqual(self.service.status()["approved_count"], 1)

    def test_approve_is_idempotent(self):
        analysis = self.planned()
        first = self.service.approve(analysis["id"], analysis["input_digest"])
        second = self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(first["approved_at"], second["approved_at"])
        events = [event for event in self.service.ledger.events() if event["kind"] == "approve"]
        self.assertEqual(len(events), 1)

    def test_new_price_invalidates_pending_approval(self):
        analysis = self.planned()
        changed = dict(self.offers[1], unit_price="80")
        self.service.import_offer(changed)
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "stale_input")
        self.assertEqual(self.service.analysis(analysis["id"])["state"], "draft")

    def test_new_requirement_invalidates_pending_approval(self):
        analysis = self.planned()
        revised = dict(self.requirement, budget="210")
        self.service.import_requirement(revised)
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "stale_input")

    def test_new_offer_invalidates_pending_approval(self):
        analysis = self.planned()
        third = deepcopy(self.offers[1])
        third["package"] = "portable_03"
        self.service.import_offer(third)
        with self.assertRaises(PortfolioError):
            self.service.approve(analysis["id"], analysis["input_digest"])

    def test_infeasible_plan_cannot_be_approved(self):
        self.service.import_requirement(dict(self.requirement, budget="140"))
        analysis = self.planned()
        self.assertFalse(analysis["report"]["feasible"])
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "infeasible")

    def test_replanning_creates_new_analysis_without_overwriting_old(self):
        first = self.planned()
        self.service.import_offer(dict(self.offers[1], unit_price="60"))
        second = self.planned()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.service.analysis(first["id"])["report"]["cost"], "150.00")
        self.assertEqual(self.service.analysis(second["id"])["report"]["cost"], "160.00")
        self.assertEqual(len(self.service.history("warehouse_trial")), 2)

    def test_reopening_database_preserves_approved_snapshot(self):
        analysis = self.planned()
        self.service.approve(analysis["id"], analysis["input_digest"])
        self.service.close()
        self.service = PortfolioService(self.path)
        self.addCleanup(self.service.close)
        self.assertTrue(self.service.verify(analysis["id"])["verified"])
        self.assertEqual(self.service.analysis(analysis["id"])["report"]["cost"], "150.00")
        self.assertEqual(self.service.status()["offer_versions"], 2)

    def test_audit_recomputes_old_snapshot_after_catalog_revision(self):
        analysis = self.planned()
        self.service.approve(analysis["id"], analysis["input_digest"])
        self.service.import_offer(dict(self.offers[1], unit_price="70"))
        report = self.service.audit_analysis(analysis["id"])
        self.assertTrue(report["historical_valid"])
        self.assertFalse(report["input_current"])
        self.assertEqual(report["cost"], "150.00")
        self.assertEqual(len(report["lineage"]), 3)

    def test_audit_detects_modified_report_body(self):
        analysis = self.planned()
        self.service.ledger.db.execute("UPDATE analyses SET report='{}' WHERE id=?", (analysis["id"],))
        report = self.service.audit_analysis(analysis["id"])
        self.assertFalse(report["historical_valid"])
        self.assertIn("snapshot_digest", report["mismatches"])
        self.assertIn("selection", report["mismatches"])

    def test_audit_detects_changed_source_body(self):
        analysis = self.planned()
        doc = self.service.ledger.analysis_inputs(analysis["id"])[1]
        self.service.ledger.db.execute("UPDATE documents SET body='{}' WHERE id=?", (doc["id"],))
        self.assertFalse(self.service.verify(analysis["id"])["verified"])
        with self.assertRaises(PortfolioError):
            self.service.audit_analysis(analysis["id"])

    def test_compare_analyses_shows_price_revision(self):
        first = self.planned()
        self.service.import_offer(dict(self.offers[1], unit_price="60"))
        second = self.planned()
        report = self.service.compare_analyses(first["id"], second["id"])
        self.assertEqual(report["cost_delta"], "10.00")
        self.assertEqual(report["offer_revisions_changed"][0]["package"], "portable_02")
        self.assertEqual(report["packages_added"], [])

    def test_compare_analyses_rejects_different_requirements(self):
        first = self.planned()
        changed = dict(self.requirement, key="other_trial")
        self.service.import_requirement(changed)
        second = self.service.plan("other_trial")
        with self.assertRaises(PortfolioError) as caught:
            self.service.compare_analyses(first["id"], second["id"])
        self.assertEqual(caught.exception.code, "requirement_pair")

    def test_compare_analyses_rejects_same_id(self):
        analysis = self.planned()
        with self.assertRaises(PortfolioError):
            self.service.compare_analyses(analysis["id"], analysis["id"])

    def test_compare_packages_shows_unlicensed_group(self):
        changed = deepcopy(self.offers[1])
        changed["allowed_clients"] = ["other_client"]
        self.service.import_offer(changed)
        comparison = self.service.compare_packages("warehouse_trial", [["remote_01", "portable_02"]])
        portfolio = comparison["portfolios"][0]
        self.assertFalse(portfolio["eligible"])
        self.assertFalse(portfolio["feasible"])
        self.assertEqual(portfolio["restrictions"][0]["package"], "portable_02")

    def test_compare_packages_shows_marginal_loss(self):
        comparison = self.service.compare_packages("warehouse_trial",
                                                   [["remote_01"], ["remote_01", "portable_02"]])
        first, second = comparison["portfolios"]
        self.assertEqual(first["coverage"]["total_shortage"], 1)
        self.assertEqual(second["coverage"]["total_shortage"], 0)
        self.assertTrue(second["feasible"])
        self.assertEqual(len(second["attribution"]), 2)

    def test_budget_grid_is_read_only(self):
        before = self.service.status()
        results = self.service.budget_grid("warehouse_trial", ["140", "150"])
        self.assertEqual([row["feasible"] for row in results], [False, True])
        self.assertEqual(self.service.status(), before)

    def test_target_grid_is_read_only(self):
        before = self.service.document_history("requirement", "warehouse_trial")
        results = self.service.target_grid("warehouse_trial", "warehouse_pick|grasp|failure|arm_a", [4, 5])
        self.assertEqual([row["feasible"] for row in results], [True, False])
        self.assertEqual(self.service.document_history("requirement", "warehouse_trial"), before)

    def test_readiness_and_brief_use_latest_offer_revision(self):
        edited = dict(self.offers[1], unit_price="80")
        self.service.import_offer(edited)
        brief = self.service.brief("warehouse_trial")
        card = next(item for item in brief["offer_scorecards"] if item["package"] == "portable_02")
        self.assertEqual(card["price"], "80.00")
        self.assertEqual(self.service.readiness("warehouse_trial")["data"]["eligible_offers"], 2)

    def test_unknown_package_in_manual_group_is_rejected(self):
        with self.assertRaises(PortfolioError) as caught:
            self.service.compare_packages("warehouse_trial", [["missing_package"]])
        self.assertEqual(caught.exception.code, "unknown_offer")

    def test_event_history_retains_analysis_and_approval(self):
        analysis = self.planned()
        self.service.approve(analysis["id"], analysis["input_digest"])
        kinds = [event["kind"] for event in self.service.ledger.events()]
        self.assertEqual(kinds.count("analyze"), 1)
        self.assertEqual(kinds.count("approve"), 1)
        self.assertEqual(kinds.count("import_offer"), 2)


if __name__ == "__main__":
    unittest.main()
