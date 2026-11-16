from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from embodied_portfolio.cli import main, sourcing_payloads
from embodied_portfolio.contracts import (Offer, Requirement, SourceEvidence, PortfolioError,
                                          parse_document)
from embodied_portfolio.coverage import CoverageEngine
from embodied_portfolio.decision import PortfolioService
from embodied_portfolio.economics import PriceEngine
from embodied_portfolio.ledger import Ledger
from embodied_portfolio.optimizer import PortfolioOptimizer
from embodied_portfolio.sourcing import SourceAnalyzer, SourceContext, contributors

AS_OF = "2026-11-17"


def corrected_evidence():
    _, _, evidence = sourcing_payloads()
    corrected = deepcopy(evidence[0])
    corrected["claims"] = [claim for claim in corrected["claims"]
                           if claim["package"] != "portable_02"]
    corrected["claims"].append({"package": "portable_02", "collector": "field_crew_beta",
                                "basis": "audit", "strength": "verified",
                                "reference": "更正后的审计记录", "observed_on": "2026-11-16"})
    return corrected


class SourceAnalyzerTests(unittest.TestCase):
    def setUp(self):
        requirement, offers, evidence = sourcing_payloads()
        self.requirement = Requirement.parse(requirement)
        self.offers = [Offer.parse(item) for item in offers]
        self.evidence = [SourceEvidence.parse(item) for item in evidence]
        self.context = SourceContext(tuple(self.evidence), AS_OF)
        self.sourcing = SourceAnalyzer()
        self.coverage = CoverageEngine()
        self.prices = PriceEngine()
        self.optimizer = PortfolioOptimizer(self.coverage, self.prices, sourcing=self.sourcing)
        self.by_package = {offer.package: offer for offer in self.offers}

    def assess(self, packages, context=None):
        return self.sourcing.assess(self.requirement,
                                    [self.by_package[item] for item in packages],
                                    context or self.context)

    def test_different_providers_share_upstream_collector(self):
        report = self.assess(["remote_01", "portable_02"])
        self.assertEqual(report["distinct_providers"], 2)
        self.assertEqual(report["distinct_collectors"], 1)
        self.assertTrue(report["provider_diversity_misleading"])
        self.assertFalse(report["assurance_met"])
        self.assertEqual(report["shared_source_groups"],
                         [{"collector": "field_crew_alpha",
                           "packages": ["portable_02", "remote_01"]}])
        self.assertEqual(report["targets"][0]["reason"], "insufficient_independent_collectors")

    def test_independent_collector_satisfies_assurance_at_a_cost(self):
        report = self.assess(["remote_01", "portable_02", "portable_03"])
        self.assertTrue(report["assurance_met"])
        self.assertEqual(report["distinct_collectors"], 2)
        self.assertEqual(report["shared_source_groups"][0]["collector"], "field_crew_alpha")

    def test_unconfirmed_package_is_not_an_independent_source(self):
        context = SourceContext((), AS_OF)
        report = self.assess(["remote_01", "portable_02"], context)
        self.assertFalse(report["assurance_met"])
        self.assertEqual(report["targets"][0]["independent_sources"], 0)
        self.assertEqual(report["targets"][0]["reason"],
                         "insufficient_evidenced_collectors_and_unconfirmed_packages")
        self.assertEqual(sorted(report["unconfirmed_packages"]), ["portable_02", "remote_01"])

    def test_expired_evidence_does_not_support_independence(self):
        context = SourceContext(tuple(self.evidence), "2028-01-01")
        report = self.assess(["remote_01", "portable_03"], context)
        self.assertFalse(report["assurance_met"])
        states = {item["package"]: item["state"] for item in report["resolutions"]}
        self.assertEqual(states, {"portable_03": "expired", "remote_01": "expired"})
        self.assertEqual(report["evidence_validity"][0]["status"], "expired")

    def test_evidence_before_its_window_is_not_yet_valid(self):
        context = SourceContext(tuple(self.evidence), "2026-09-01")
        report = self.assess(["remote_01", "portable_03"], context)
        states = {item["package"]: item["state"] for item in report["resolutions"]}
        self.assertEqual(states["remote_01"], "not_yet_valid")
        self.assertFalse(report["assurance_met"])

    def test_conflicting_collectors_in_same_window_are_disputed(self):
        _, _, evidence = sourcing_payloads()
        conflicting = deepcopy(evidence[0])
        conflicting["claims"].append({"package": "remote_01", "collector": "field_crew_beta",
                                      "basis": "audit", "strength": "verified",
                                      "reference": "冲突审计", "observed_on": "2026-10-05"})
        context = SourceContext((SourceEvidence.parse(evidence[0]),
                                 SourceEvidence.parse(conflicting)), AS_OF)
        report = self.assess(["remote_01", "portable_03"], context)
        resolution = next(item for item in report["resolutions"] if item["package"] == "remote_01")
        self.assertEqual(resolution["state"], "disputed")
        self.assertIn("remote_01", report["unconfirmed_packages"])
        self.assertFalse(report["assurance_met"])

    def test_evidence_expiry_is_reported_with_days_remaining(self):
        report = self.assess(["remote_01"])
        row = report["evidence_validity"][0]
        self.assertEqual(row["valid_until"], "2027-09-30")
        self.assertEqual(row["days_remaining"], 317)
        self.assertEqual(row["status"], "current")

    def test_target_without_independence_request_is_not_blocked(self):
        body = self.requirement.document()
        body["targets"][0]["minimum_independent_sources"] = 0
        requirement = Requirement.parse(body)
        report = self.sourcing.assess(requirement, self.offers, SourceContext((), AS_OF))
        self.assertTrue(report["assurance_met"])
        self.assertEqual(report["targets"][0]["reason"], "not_requested")

    def test_contributors_ignore_packages_repeating_only_known_ids(self):
        first = self.by_package["remote_01"].observations[0]
        copied = Offer.parse({**self.by_package["portable_02"].document(),
                              "package": "portable_09",
                              "observations": [{"slice": self.requirement.targets[0].slice.__dict__,
                                                "count": 2,
                                                "episode_ids": ["trial_1", "trial_2"]}]}).observations[0]
        self.assertEqual(contributors([("remote_01", first), ("portable_09", copied)]),
                         ["remote_01"])


class PortfolioSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "portfolio.db"
        self.service = PortfolioService(self.path)
        self.addCleanup(self.service.close)
        self.requirement, self.offers, self.evidence = sourcing_payloads()
        self.service.import_requirement(self.requirement)
        for offer in self.offers:
            self.service.import_offer(offer)
        self.service.import_source(self.evidence[0])

    def plan(self, as_of=AS_OF):
        return self.service.plan("warehouse_trial", as_of)

    def test_plan_separates_coverage_from_independent_assurance(self):
        report = self.plan()["report"]
        self.assertTrue(report["feasible"])
        self.assertEqual(report["coverage"]["mandatory_complete"], True)
        self.assertEqual(report["coverage_only_selection"], ["portable_02", "remote_01"])
        self.assertFalse(report["coverage_only_source_assurance"]["assurance_met"])
        self.assertTrue(report["source_assurance"]["assurance_met"])
        self.assertEqual(report["selection"], ["portable_02", "portable_03", "remote_01"])
        self.assertEqual(report["assurance_premium"], "120.00")
        self.assertEqual(report["source_assurance"]["distinct_providers"], 3)
        self.assertEqual(report["source_assurance"]["distinct_collectors"], 2)

    def test_independent_alternative_answers_budget_question(self):
        report = self.plan()["report"]
        alternative = report["independent_alternative"]
        self.assertTrue(alternative["available"])
        self.assertEqual(alternative["additional_cost"], "120.00")
        self.assertTrue(alternative["within_budget"])
        self.assertEqual(alternative["budget_remaining"], "10.00")

    def test_independent_alternative_reports_budget_overrun(self):
        self.service.import_requirement(dict(self.requirement, budget="200"))
        report = self.plan()["report"]
        self.assertFalse(report["feasible"])
        self.assertEqual(report["search"]["reason"], "source_assurance_unmet")
        alternative = report["independent_alternative"]
        self.assertEqual(alternative["cost"], "270.00")
        self.assertFalse(alternative["within_budget"])
        self.assertEqual(alternative["budget_remaining"], "-70.00")
        self.assertIsNone(report["source_assurance"])

    def test_evidence_revision_makes_pending_analysis_unapprovable(self):
        analysis = self.plan()
        self.service.import_source(corrected_evidence())
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "stale_input")

    def test_corrected_evidence_changes_recommendation(self):
        first = self.plan()
        self.service.import_source(corrected_evidence())
        second = self.plan()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["report"]["selection"],
                         ["portable_02", "portable_03", "remote_01"])
        self.assertEqual(second["report"]["selection"], ["portable_02", "remote_01"])
        self.assertEqual(second["report"]["cost"], "150.00")
        self.assertEqual(second["report"]["assurance_premium"], "0.00")
        self.assertTrue(second["report"]["source_assurance"]["assurance_met"])

    def test_approved_snapshot_keeps_original_supply_chain_basis(self):
        first = self.plan()
        approved = self.service.approve(first["id"], first["input_digest"], AS_OF)
        self.service.import_source(corrected_evidence())
        audit = self.service.audit_analysis(approved["id"])
        self.assertTrue(audit["historical_valid"])
        self.assertEqual(audit["selection"], ["portable_02", "portable_03", "remote_01"])
        self.assertFalse(audit["input_current"])
        self.assertFalse(audit["evidence_current"])
        portable_02 = next(item for item in audit["source_assurance"]["resolutions"]
                           if item["package"] == "portable_02")
        self.assertEqual(portable_02["collectors"], ["field_crew_alpha"])
        self.assertTrue(audit["source_assurance"]["assurance_met"])
        stored = self.service.analysis(approved["id"])["report"]
        self.assertEqual([item["revision"] for item in stored["source_versions"]], [1])

    def test_old_approval_cannot_be_upgraded_to_new_evidence(self):
        first = self.plan()
        approved = self.service.approve(first["id"], first["input_digest"], AS_OF)
        self.service.import_source(corrected_evidence())
        again = self.service.approve(approved["id"], approved["input_digest"], AS_OF)
        self.assertEqual(again["approved_at"], approved["approved_at"])
        audit = self.service.audit_analysis(approved["id"])
        self.assertFalse(audit["evidence_current"])

    def test_compare_analyses_exposes_evidence_revision(self):
        first = self.plan()
        self.service.import_source(corrected_evidence())
        second = self.plan()
        comparison = self.service.compare_analyses(first["id"], second["id"])
        self.assertEqual(comparison["source_evidence_changed"],
                         [{"key": "upstream_2026", "earlier_revision": 1, "later_revision": 2}])
        self.assertEqual(comparison["packages_removed"], ["portable_03"])
        self.assertEqual(comparison["cost_delta"], "-120.00")

    def test_approval_rechecks_evidence_validity_on_approval_date(self):
        analysis = self.plan()
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"], "2028-01-01")
        self.assertEqual(caught.exception.code, "source_assurance_unmet")
        self.assertEqual(self.service.analysis(analysis["id"])["state"], "draft")

    def test_approved_plan_still_verifies_after_evidence_revision(self):
        analysis = self.plan()
        approved = self.service.approve(analysis["id"], analysis["input_digest"], AS_OF)
        self.service.import_source(corrected_evidence())
        verified = self.service.verify(approved["id"])
        self.assertTrue(verified["verified"])

    def test_sourcing_report_is_read_only(self):
        before = self.service.status()
        report = self.service.sourcing("warehouse_trial", AS_OF)
        self.assertTrue(report["assurance_met"])
        self.assertEqual(report["distinct_collectors"], 2)
        self.assertEqual(self.service.status(), before)

    def test_batch_import_accepts_source_documents(self):
        with TemporaryDirectory() as directory:
            service = PortfolioService(Path(directory) / "batch.db")
            self.addCleanup(service.close)
            corrected = corrected_evidence()
            imported = service.catalog.import_batch({"documents": [
                {"kind": "requirement", "body": self.requirement},
                {"kind": "source", "body": corrected}]})
            self.assertEqual(imported["changed"], 2)
            self.assertEqual(service.status()["source_count"], 1)
            self.assertEqual(service.document_history("source", "upstream_2026")[0]["revision"], 1)

    def test_compare_versions_shows_evidence_claim_change(self):
        self.service.import_source(corrected_evidence())
        comparison = self.service.compare_versions("source", "upstream_2026", 1, 2)
        self.assertTrue(comparison["changed"])
        self.assertEqual(comparison["changes"][0]["field"], "claims")
        self.assertIn("portable_02", str(comparison["changes"][0]["later"]))

    def test_snapshot_verifies_when_evidence_key_sorts_before_offers(self):
        early = deepcopy(self.evidence[0])
        early["key"] = "aa_evidence"
        self.service.import_source(early)
        analysis = self.plan()
        self.assertTrue(self.service.verify(analysis["id"])["verified"])
        approved = self.service.approve(analysis["id"], analysis["input_digest"], AS_OF)
        self.assertTrue(self.service.verify(approved["id"])["verified"])
        audit = self.service.audit_analysis(approved["id"])
        self.assertTrue(audit["historical_valid"], audit["mismatches"])
        self.assertEqual([item["kind"] for item in audit["lineage"]],
                         ["requirement", "offer", "offer", "offer", "source", "source"])

    def test_legacy_v1_snapshot_remains_auditable_after_upgrade(self):
        from embodied_portfolio.cli import demo_payloads
        from embodied_portfolio.diagnostics import SupplyDiagnostics

        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            requirement_body, offer_bodies = demo_payloads()
            requirement = Requirement.parse(requirement_body)
            offers = sorted((Offer.parse(item) for item in offer_bodies),
                            key=lambda offer: offer.package)
            coverage_engine, price_engine = CoverageEngine(), PriceEngine()
            optimizer = PortfolioOptimizer(coverage_engine, price_engine)
            search = optimizer.search(requirement, offers)
            selected = [offer for offer in offers if offer.package in search.best.packages]
            quotes = price_engine.all_quotes(selected, requirement)
            coverage = coverage_engine.evaluate(requirement, selected)
            diagnostics = SupplyDiagnostics(coverage_engine, price_engine)
            ledger = Ledger(path)
            requirement_doc = ledger.insert_document("requirement", requirement.key,
                                                      requirement.document())
            offer_docs = [ledger.insert_document("offer", offer.package, offer.document())
                          for offer in offers]
            search_doc = search.document()
            for candidate in [search_doc["best"], *search_doc["alternatives"]]:
                if candidate:
                    candidate.pop("source_assurance")
            report = {"schema_version": 1, "requirement": requirement.key,
                      "requirement_revision": 1, "client": requirement.client,
                      "budget": requirement.budget, "feasible": True,
                      "selection": list(search.best.packages), "cost": search.best.cost,
                      "coverage": coverage.document(), "search": search_doc,
                      "quotes": [quote.document() for quote in quotes],
                      "budget_pressure": price_engine.budget_pressure(quotes, requirement),
                      "provider_spend": price_engine.provider_breakdown(quotes),
                      "selected_attribution": coverage_engine.attribution(requirement, selected),
                      "cost_per_credited_sample": price_engine.cost_per_credited_sample(
                          search.best.cost, coverage.total_credited),
                      "target_supply": diagnostics.target_supply(requirement, offers),
                      "eligible_supply_readiness": diagnostics.data_readiness(requirement, offers),
                      "exclusions": optimizer.exclusion_analysis(requirement, offers, selected),
                      "catalog_versions": [{"id": doc["id"], "key": doc["key"],
                                            "revision": doc["revision"], "digest": doc["digest"]}
                                           for doc in offer_docs]}
            analysis = ledger.insert_analysis(requirement_doc, offer_docs, report)
            ledger.approve(analysis["id"], analysis["input_digest"])
            ledger.close()

            service = PortfolioService(path)
            self.addCleanup(service.close)
            audit = service.audit_analysis(analysis["id"])
            self.assertTrue(audit["historical_valid"], audit["mismatches"])
            self.assertEqual(audit["selection"], ["portable_02", "remote_01"])
            self.assertIsNone(audit["source_assurance"])
            self.assertTrue(audit["evidence_current"])
            self.assertEqual(audit["lineage"][0]["kind"], "requirement")


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        _, _, evidence = sourcing_payloads()
        self.evidence = evidence[0]

    def rejects(self, body, code):
        with self.assertRaises(PortfolioError) as caught:
            SourceEvidence.parse(body)
        self.assertEqual(caught.exception.code, code)

    def test_source_document_roundtrip(self):
        parsed = SourceEvidence.parse(self.evidence)
        self.assertEqual(SourceEvidence.parse(parsed.document()), parsed)
        self.assertEqual(parsed.claims_for("remote_01")[0].collector, "field_crew_alpha")

    def test_claims_are_required(self):
        self.rejects(dict(self.evidence, claims=[]), "claims")

    def test_claim_rejects_unknown_basis_and_strength(self):
        claim = dict(self.evidence["claims"][0], basis="rumour")
        self.rejects(dict(self.evidence, claims=[claim]), "evidence_basis")
        claim = dict(self.evidence["claims"][0], strength="assumed")
        self.rejects(dict(self.evidence, claims=[claim]), "evidence_strength")

    def test_duplicate_package_claim_is_rejected(self):
        claim = dict(self.evidence["claims"][0])
        self.rejects(dict(self.evidence, claims=[claim, claim]), "duplicate_claim")

    def test_validity_window_must_be_ordered(self):
        self.rejects(dict(self.evidence, valid_from="2027-01-01", valid_until="2026-01-01"),
                     "validity_window")

    def test_bad_dates_are_rejected(self):
        for value in ("2026-13-01", "26-01-01", "2026-1-1", None):
            with self.subTest(value=value):
                self.rejects(dict(self.evidence, valid_from=value), "invalid_date")

    def test_parse_document_supports_source(self):
        self.assertIsInstance(parse_document("source", self.evidence), SourceEvidence)

    def test_requirement_target_accepts_independent_source_minimum(self):
        requirement, _, _ = sourcing_payloads()
        parsed = Requirement.parse(requirement)
        self.assertEqual(parsed.targets[0].minimum_independent_sources, 2)
        changed = deepcopy(requirement)
        changed["targets"][0]["minimum_independent_sources"] = -1
        with self.assertRaises(PortfolioError):
            Requirement.parse(changed)


class SourceCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "cli.db"
        self.requirement, self.offers, self.evidence = sourcing_payloads()
        for name, value in [("requirement", self.requirement),
                            *[(f"offer{number}", offer) for number, offer in enumerate(self.offers)],
                            ("source", self.evidence[0])]:
            (self.root / f"{name}.json").write_text(__import__("json").dumps(value),
                                                    encoding="utf-8")

    def invoke(self, *args):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        import json
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", str(self.db), *args])
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def seed(self):
        self.assertEqual(self.invoke("import", "requirement", str(self.root / "requirement.json"))[0], 0)
        for number in range(len(self.offers)):
            self.assertEqual(self.invoke("import", "offer", str(self.root / f"offer{number}.json"))[0], 0)
        self.assertEqual(self.invoke("import", "source", str(self.root / "source.json"))[0], 0)

    def test_cli_sourcing_and_as_of_plan(self):
        self.seed()
        code, report, _ = self.invoke("sourcing", "warehouse_trial", "--as-of", AS_OF)
        self.assertEqual(code, 0)
        self.assertTrue(report["assurance_met"])
        self.assertEqual(report["distinct_providers"], 3)
        self.assertEqual(report["distinct_collectors"], 2)
        self.assertTrue(report["provider_diversity_misleading"])
        code, analysis, _ = self.invoke("plan", "warehouse_trial", "--as-of", AS_OF)
        self.assertEqual(code, 0)
        self.assertEqual(analysis["report"]["assurance_premium"], "120.00")
        self.assertEqual(analysis["report"]["as_of"], AS_OF)

    def test_cli_reports_evidence_contract_errors(self):
        bad = dict(self.evidence[0], valid_until="2026-01-01")
        path = self.root / "bad.json"
        path.write_text(__import__("json").dumps(bad), encoding="utf-8")
        code, value, error = self.invoke("import", "source", str(path))
        self.assertEqual(code, 2)
        self.assertIsNone(value)
        self.assertIn("validity_window", error)

    def test_cli_demo_sourcing_is_end_to_end(self):
        code, result, error = self.invoke("demo-sourcing", "--workspace", str(self.root / "demo"),
                                          "--as-of", AS_OF)
        self.assertEqual(code, 0, error)
        self.assertFalse(result["coverage_only_assurance_met"])
        self.assertTrue(result["provider_diversity_misleading"])
        self.assertTrue(result["first_assurance_met"])
        self.assertEqual(result["second_selection"], ["portable_02", "remote_01"])
        self.assertTrue(result["old_approval_still_valid"])
        self.assertFalse(result["old_approval_evidence_current"])
        self.assertTrue(result["second_evidence_current"])


if __name__ == "__main__":
    unittest.main()
