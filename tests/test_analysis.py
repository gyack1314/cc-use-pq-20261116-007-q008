from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import unittest

from embodied_portfolio.cli import demo_payloads
from embodied_portfolio.contracts import Offer, Requirement, PortfolioError
from embodied_portfolio.coverage import CoverageEngine, effective_count
from embodied_portfolio.diagnostics import SupplyDiagnostics
from embodied_portfolio.economics import PriceEngine
from embodied_portfolio.optimizer import PortfolioOptimizer
from embodied_portfolio.sensitivity import SensitivityAnalyzer


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        req, offers = demo_payloads()
        self.requirement = Requirement.parse(req)
        self.offers = [Offer.parse(item) for item in offers]
        self.coverage = CoverageEngine()
        self.prices = PriceEngine()
        self.optimizer = PortfolioOptimizer(self.coverage, self.prices)
        self.diagnostics = SupplyDiagnostics(self.coverage, self.prices)
        self.sensitivity = SensitivityAnalyzer(self.optimizer, self.coverage, self.prices)

    def test_known_overlap_is_deduplicated(self):
        result = self.coverage.evaluate(self.requirement, self.offers)
        self.assertEqual(result.cells[0].available, 4)
        self.assertEqual(result.cells[0].duplicate_count, 1)
        self.assertEqual(result.total_credited, 4)
        self.assertEqual(result.total_shortage, 0)
        self.assertTrue(result.mandatory_complete)

    def test_anonymous_counts_are_not_guessed_duplicates(self):
        original = self.offers[1].document()
        original["observations"][0]["episode_ids"] = []
        second = Offer.parse(original)
        result = self.coverage.evaluate(self.requirement, [self.offers[0], second])
        self.assertEqual(result.cells[0].available, 5)
        self.assertEqual(result.total_duplicate, 0)

    def test_known_ids_deduplicate_across_three_offers(self):
        third = self.offers[1].document()
        third["package"] = "portable_03"
        third["observations"][0]["episode_ids"] = ["trial_a", "trial_d"]
        result = self.coverage.evaluate(self.requirement, [*self.offers, Offer.parse(third)])
        self.assertEqual(result.cells[0].available, 4)
        self.assertEqual(result.total_duplicate, 3)

    def test_effective_count_handles_empty_and_anonymous(self):
        self.assertEqual(effective_count([]), (0, 0))
        anonymous = Offer.parse({**self.offers[1].document(),
                                 "observations": [{**self.offers[1].document()["observations"][0],
                                                    "episode_ids": []}]}).observations[0]
        self.assertEqual(effective_count([anonymous, anonymous]), (4, 0))

    def test_gap_report_shows_missing_mandatory_slice(self):
        report = self.coverage.gap_report(self.requirement, [self.offers[0]])
        self.assertFalse(report["mandatory_complete"])
        self.assertEqual(report["gaps"][0]["missing"], 1)
        self.assertEqual(report["gaps"][0]["providers"], ["remote_01"])

    def test_marginal_gain_counts_duplicate_once(self):
        value = self.coverage.marginal(self.requirement, [self.offers[0]], self.offers[1])
        self.assertEqual(value["credited_gain"], 1)
        self.assertEqual(value["duplicate_increase"], 1)
        self.assertEqual(value["changes"][0]["remaining_shortage"], 0)

    def test_pair_overlap_uses_matching_slice(self):
        report = self.coverage.pair_overlap(*self.offers)
        self.assertEqual(report["known_overlap_total"], 1)
        self.assertTrue(report["shared_slices"][0]["identifiers_complete"])

    def test_attribution_identifies_mandatory_contributors(self):
        rows = self.coverage.attribution(self.requirement, self.offers)
        self.assertEqual([row["exclusive_credited"] for row in rows], [2, 1])
        self.assertTrue(all(row["mandatory_would_fail"] for row in rows))

    def test_hourly_and_flat_prices_add_to_150(self):
        quotes = self.prices.all_quotes(self.offers, self.requirement)
        self.assertEqual([quote.amount for quote in quotes], ["100.00", "50.00"])
        self.assertEqual(self.prices.total(quotes), "150.00")
        self.assertEqual(self.prices.remaining(quotes, self.requirement), "30.00")

    def test_per_record_price_counts_all_records(self):
        body = self.offers[0].document()
        body.update(price_mode="per_record", unit_price="0.335")
        quote = self.prices.quote(Offer.parse(body), self.requirement)
        self.assertEqual(quote.quantity, "3")
        self.assertEqual(quote.amount, "1.00")

    def test_restricted_client_is_not_permitted(self):
        changed = self.offers[1].document()
        changed["allowed_clients"] = ["other_client"]
        offer = Offer.parse(changed)
        quote = self.prices.quote(offer, self.requirement)
        self.assertFalse(quote.permitted)
        self.assertIn("license_does_not_permit_client", quote.reason)
        eligible, filtered = self.optimizer.eligible(self.requirement, [offer])
        self.assertEqual(eligible, [])
        self.assertEqual(filtered[0]["package"], "portable_02")

    def test_disallowed_path_is_not_permitted(self):
        changed = self.offers[1].document()
        changed["path"] = "internet"
        offer = Offer.parse(changed)
        quote = self.prices.quote(offer, self.requirement)
        self.assertFalse(quote.permitted)
        self.assertIn("collection_path_not_permitted", quote.reason)

    def test_zero_budget_pressure_is_defined(self):
        requirement = Requirement.parse({**self.requirement.document(), "budget": "0"})
        no_spend = self.prices.budget_pressure([], requirement)
        self.assertEqual(no_spend["utilization"], "0")
        spend = self.prices.budget_pressure(self.prices.all_quotes(self.offers, requirement), requirement)
        self.assertIsNone(spend["utilization"])
        self.assertFalse(spend["within_budget"])

    def test_provider_breakdown_preserves_two_suppliers(self):
        rows = self.prices.provider_breakdown(self.prices.all_quotes(self.offers, self.requirement))
        self.assertEqual([(row["provider"], row["amount"]) for row in rows],
                         [("provider_a", "100.00"), ("provider_b", "50.00")])

    def test_optimizer_selects_both_for_mandatory_coverage(self):
        result = self.optimizer.search(self.requirement, self.offers)
        self.assertEqual(result.best.packages, ("portable_02", "remote_01"))
        self.assertEqual(result.best.cost, "150.00")
        self.assertEqual(result.feasible, 1)
        self.assertEqual(result.evaluated, 3)

    def test_optimizer_reports_budget_infeasibility(self):
        requirement = Requirement.parse({**self.requirement.document(), "budget": "140"})
        result = self.optimizer.search(requirement, self.offers)
        self.assertIsNone(result.best)
        self.assertEqual(result.reason, "mandatory_targets_uncovered")
        self.assertTrue(result.alternatives)

    def test_optimizer_reports_no_affordable_combination(self):
        requirement = Requirement.parse({**self.requirement.document(), "budget": "1"})
        result = self.optimizer.search(requirement, self.offers)
        self.assertEqual(result.reason, "all_combinations_exceed_budget")
        self.assertEqual(result.alternatives, ())

    def test_optimizer_reports_no_eligible_offer(self):
        requirement = Requirement.parse({**self.requirement.document(), "allowed_paths": ["simulation"]})
        result = self.optimizer.search(requirement, self.offers)
        self.assertEqual(result.reason, "no_eligible_offers")
        self.assertEqual(len(result.filtered), 2)

    def test_optimizer_filters_irrelevant_slice(self):
        changed = self.offers[0].document()
        changed["observations"][0]["slice"]["scenario"] = "another_scene"
        result = self.optimizer.search(self.requirement, [Offer.parse(changed)])
        self.assertEqual(result.filtered[0]["reason"], "no_requested_slice")

    def test_optimizer_candidate_limit_is_explicit(self):
        optimizer = PortfolioOptimizer(self.coverage, self.prices, maximum_candidates=1)
        with self.assertRaises(PortfolioError) as caught:
            optimizer.search(self.requirement, self.offers)
        self.assertEqual(caught.exception.code, "candidate_limit")

    def test_frontier_keeps_nondominated_alternatives(self):
        rows = self.optimizer.frontier(self.requirement, self.offers)
        self.assertTrue(rows)
        self.assertTrue(all(Decimal(item["cost"]) <= Decimal(self.requirement.budget) for item in rows))
        self.assertIn(["portable_02"], [list(item["packages"]) for item in rows])

    def test_exclusion_reasons_include_no_marginal_gain(self):
        additional = self.offers[1].document()
        additional["package"] = "portable_03"
        offers = [*self.offers, Offer.parse(additional)]
        explanations = self.optimizer.exclusion_analysis(self.requirement, offers, self.offers)
        self.assertEqual(explanations[0]["package"], "portable_03")
        self.assertEqual(explanations[0]["marginal_credited"], 0)

    def test_readiness_counts_sparse_target_and_duplicate(self):
        report = self.diagnostics.data_readiness(self.requirement, self.offers)
        self.assertTrue(report["mandatory_possible"])
        self.assertEqual(report["known_duplicates"], 1)
        self.assertEqual(report["sparse_targets"][0]["available"], 4)

    def test_brief_discloses_overlap_and_supplier_cost(self):
        report = self.diagnostics.procurement_brief(self.requirement, self.offers)
        self.assertEqual(report["overlaps"]["known_overlap_total"], 1)
        self.assertEqual(report["eligible_offer_count"], 2)
        self.assertEqual(report["offer_scorecards"][0]["price"], "50.00")
        self.assertEqual(report["target_supply"][0]["contributor_count"], 2)

    def test_budget_grid_changes_feasibility_without_writing_catalog(self):
        rows = self.sensitivity.budget_grid(self.requirement, self.offers, ["140", "150", "180"])
        self.assertEqual([row["feasible"] for row in rows], [False, True, True])
        self.assertEqual(rows[1]["selected"], ["portable_02", "remote_01"])
        self.assertEqual(self.requirement.budget, "180")

    def test_target_grid_changes_minimum_without_writing_requirement(self):
        token = self.requirement.targets[0].slice.token()
        rows = self.sensitivity.target_grid(self.requirement, self.offers, token, [3, 4, 5])
        self.assertEqual([row["feasible"] for row in rows], [True, True, False])
        self.assertEqual(self.requirement.targets[0].minimum, 4)

    def test_remove_offer_shows_loss_of_feasibility(self):
        report = self.sensitivity.remove_offer(self.requirement, self.offers, "remote_01")
        self.assertFalse(report["after_feasible"])
        self.assertEqual(report["before_cost"], "150.00")

    def test_price_change_can_break_budget(self):
        report = self.sensitivity.price_change(self.requirement, self.offers, "portable_02", "100")
        self.assertFalse(report["after_feasible"])
        self.assertEqual(report["before_cost"], "150.00")
        self.assertEqual(self.offers[1].unit_price, "50")

    def test_invalid_grid_is_rejected(self):
        with self.assertRaises(PortfolioError):
            self.sensitivity.budget_grid(self.requirement, self.offers, [])
        with self.assertRaises(PortfolioError):
            self.sensitivity.target_grid(self.requirement, self.offers, "missing", [4])
        with self.assertRaises(PortfolioError):
            self.sensitivity.remove_offer(self.requirement, self.offers, "missing")


if __name__ == "__main__":
    unittest.main()
