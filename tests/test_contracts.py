from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from embodied_portfolio.catalog import read_json
from embodied_portfolio.cli import demo_payloads
from embodied_portfolio.contracts import (Offer, Observation, PortfolioError, Requirement, Slice,
                                          Target, count, decimal, identifiers, key, parse_document)
from embodied_portfolio.decision import PortfolioService


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.requirement, self.offers = demo_payloads()

    def rejects(self, fn, code):
        with self.assertRaises(PortfolioError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_reference_payloads_are_normalized(self):
        req = Requirement.parse(self.requirement)
        offer = Offer.parse(self.offers[0])
        self.assertEqual(req.key, "warehouse_trial")
        self.assertEqual(offer.sample_count, 3)
        self.assertEqual(req.document()["budget"], "180")
        self.assertEqual(Offer.parse(offer.document()), offer)
        self.assertEqual(Requirement.parse(req.document()), req)

    def test_identifier_accepts_lowercase_and_rejects_unsafe_keys(self):
        self.assertEqual(key("provider_01", "provider"), "provider_01")
        for value in ("A", "a", "../bad", "with space", 1, None):
            with self.subTest(value=value):
                self.rejects(lambda value=value: key(value, "provider"), "invalid_key")

    def test_decimal_rejects_nonfinite_and_negative(self):
        for value in ("NaN", "Infinity", "-Infinity", None, True, []):
            with self.subTest(value=value):
                self.rejects(lambda value=value: decimal(value, "price"), "invalid_number")
        self.rejects(lambda: decimal("-1", "price", nonnegative=True), "invalid_number")
        self.rejects(lambda: decimal("0", "weight", positive=True), "invalid_number")
        self.assertEqual(str(decimal("0.025", "price", positive=True)), "0.025")

    def test_count_rejects_booleans_and_fractions(self):
        self.assertEqual(count(5, "records", minimum=1), 5)
        for value in (True, 1.0, "2", 0, -1):
            with self.subTest(value=value):
                self.rejects(lambda value=value: count(value, "records", minimum=1), "invalid_count")

    def test_identifier_list_is_unique(self):
        self.assertEqual(identifiers(["alpha", "beta"], "providers"), ("alpha", "beta"))
        self.rejects(lambda: identifiers(["alpha", "alpha"], "providers"), "duplicate_item")
        self.rejects(lambda: identifiers([], "providers"), "invalid_list")
        self.assertEqual(identifiers([], "providers", empty=True), ())

    def test_slice_checks_outcome(self):
        good = self.requirement["targets"][0]["slice"]
        self.assertEqual(Slice.parse(good).token(), "warehouse_pick|grasp|failure|arm_a")
        changed = dict(good, outcome="unknown")
        self.rejects(lambda: Slice.parse(changed), "outcome")
        self.rejects(lambda: Slice.parse([]), "slice_shape")

    def test_observation_ids_match_count(self):
        observation = self.offers[0]["observations"][0]
        self.assertEqual(Observation.parse(observation).count, 3)
        changed = dict(observation, episode_ids=["one"])
        self.rejects(lambda: Observation.parse(changed), "episode_count")
        changed = dict(observation, count=0)
        self.rejects(lambda: Observation.parse(changed), "invalid_count")

    def test_offer_rejects_undeclared_robot(self):
        changed = deepcopy(self.offers[0])
        changed["robots"] = ["arm_b"]
        self.rejects(lambda: Offer.parse(changed), "robot_scope")

    def test_offer_rejects_duplicate_slice(self):
        changed = deepcopy(self.offers[0])
        changed["observations"].append(deepcopy(changed["observations"][0]))
        self.rejects(lambda: Offer.parse(changed), "duplicate_slice")

    def test_offer_rejects_duplicate_episode_across_slices(self):
        changed = deepcopy(self.offers[0])
        second = deepcopy(changed["observations"][0])
        second["slice"]["stage"] = "place"
        changed["observations"].append(second)
        self.rejects(lambda: Offer.parse(changed), "duplicate_episode")

    def test_hourly_offer_requires_hours(self):
        changed = dict(self.offers[0], hours="0")
        self.rejects(lambda: Offer.parse(changed), "hours")
        changed = dict(self.offers[0], hours="-1")
        self.rejects(lambda: Offer.parse(changed), "invalid_number")

    def test_license_scope_requires_clients(self):
        changed = dict(self.offers[1], allowed_clients=[])
        self.rejects(lambda: Offer.parse(changed), "license_scope")
        self.assertTrue(Offer.parse(self.offers[1]).permits("client_a"))
        self.assertFalse(Offer.parse(self.offers[1]).permits("client_b"))

    def test_target_requires_positive_weight(self):
        target = deepcopy(self.requirement["targets"][0])
        target["weight"] = "0"
        self.rejects(lambda: Target.parse(target), "invalid_number")
        target["weight"] = "1"
        target["mandatory"] = 1
        self.rejects(lambda: Target.parse(target), "target_mandatory")

    def test_requirement_rejects_duplicate_targets(self):
        changed = deepcopy(self.requirement)
        changed["targets"].append(deepcopy(changed["targets"][0]))
        self.rejects(lambda: Requirement.parse(changed), "duplicate_target")

    def test_requirement_rejects_duplicate_paths(self):
        changed = dict(self.requirement, allowed_paths=["portable", "portable"])
        self.rejects(lambda: Requirement.parse(changed), "allowed_paths")

    def test_requirement_rejects_package_limit(self):
        changed = dict(self.requirement, maximum_packages=25)
        self.rejects(lambda: Requirement.parse(changed), "invalid_count")
        changed = dict(self.requirement, maximum_packages=0)
        self.rejects(lambda: Requirement.parse(changed), "invalid_count")

    def test_document_kind_is_explicit(self):
        self.assertIsInstance(parse_document("offer", self.offers[0]), Offer)
        self.assertIsInstance(parse_document("requirement", self.requirement), Requirement)
        self.rejects(lambda: parse_document("unknown", self.requirement), "document_kind")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = PortfolioService(Path(self.tmp.name) / "catalog.db")
        self.addCleanup(self.service.close)
        self.requirement, self.offers = demo_payloads()

    def test_idempotent_import_preserves_revision(self):
        first = self.service.import_offer(self.offers[0])
        second = self.service.import_offer(self.offers[0])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.document_history("offer", "remote_01")), 1)

    def test_offer_revision_is_immutable(self):
        old = self.service.import_offer(self.offers[0])
        changed = dict(self.offers[0], unit_price="120")
        new = self.service.import_offer(changed)
        self.assertEqual((old["revision"], new["revision"]), (1, 2))
        self.assertEqual(self.service.ledger.document(old["id"])["body"]["unit_price"], "100")
        self.assertEqual(self.service.ledger.document(new["id"])["body"]["unit_price"], "120")

    def test_requirement_revision_changes_only_on_new_body(self):
        first = self.service.import_requirement(self.requirement)
        self.assertEqual(self.service.import_requirement(self.requirement)["revision"], 1)
        edited = dict(self.requirement, budget="200")
        self.assertEqual(self.service.import_requirement(edited)["revision"], 2)
        self.assertEqual(first["body"]["budget"], "180")

    def test_batch_import_commits_all_documents(self):
        batch = {"documents": [{"kind": "requirement", "body": self.requirement},
                               {"kind": "offer", "body": self.offers[0]},
                               {"kind": "offer", "body": self.offers[1]}]}
        imported = self.service.catalog.import_batch(batch)
        self.assertEqual(imported["count"], 3)
        self.assertEqual(imported["changed"], 3)
        self.assertEqual(self.service.status()["offer_count"], 2)
        self.assertEqual(self.service.catalog.import_batch(batch)["changed"], 0)

    def test_batch_validation_prevents_partial_import(self):
        broken = dict(self.offers[0], unit_price="NaN")
        batch = {"documents": [{"kind": "requirement", "body": self.requirement},
                               {"kind": "offer", "body": broken}]}
        with self.assertRaises(PortfolioError):
            self.service.catalog.import_batch(batch)
        self.assertEqual(self.service.status()["requirement_count"], 0)

    def test_batch_duplicate_key_is_rejected(self):
        batch = {"documents": [{"kind": "offer", "body": self.offers[0]},
                               {"kind": "offer", "body": self.offers[0]}]}
        with self.assertRaises(PortfolioError) as caught:
            self.service.catalog.import_batch(batch)
        self.assertEqual(caught.exception.code, "duplicate_batch_key")
        self.assertEqual(self.service.status()["offer_count"], 0)

    def test_compare_offer_versions_shows_price(self):
        self.service.import_offer(self.offers[0])
        self.service.import_offer(dict(self.offers[0], unit_price="125"))
        report = self.service.compare_versions("offer", "remote_01", 1, 2)
        self.assertTrue(report["changed"])
        self.assertEqual(report["changes"][0]["field"], "unit_price")
        self.assertEqual(report["changes"][0]["later"], "125")

    def test_compare_requirement_targets_shows_minimum(self):
        self.service.import_requirement(self.requirement)
        changed = deepcopy(self.requirement)
        changed["targets"][0]["minimum"] = 5
        self.service.import_requirement(changed)
        comparison = self.service.compare_versions("requirement", "warehouse_trial", 1, 2)
        self.assertEqual(comparison["changes"][0]["field"], "targets")
        self.assertEqual(comparison["changes"][0]["target_changes"][0]["later"]["minimum"], 5)

    def test_file_reader_enforces_object_and_size(self):
        path = Path(self.tmp.name) / "input.json"
        path.write_text(json.dumps(self.requirement), encoding="utf-8")
        self.assertEqual(read_json(path)["key"], "warehouse_trial")
        with self.assertRaises(PortfolioError):
            read_json(path, limit=1)
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(PortfolioError):
            read_json(path)

    def test_events_record_actual_revisions(self):
        self.service.import_offer(self.offers[0])
        self.service.import_offer(dict(self.offers[0], unit_price="125"))
        events = self.service.ledger.events("remote_01")
        self.assertEqual([event["details"]["revision"] for event in events], [1, 2])
        self.assertEqual([event["kind"] for event in events], ["import_offer", "import_offer"])


if __name__ == "__main__":
    unittest.main()
