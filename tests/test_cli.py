from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from io import StringIO
import json
import unittest

from embodied_portfolio.cli import demo_payloads, main


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "cli.sqlite3"
        requirement, offers = demo_payloads()
        (self.root / "requirement.json").write_text(json.dumps(requirement), encoding="utf-8")
        for number, offer in enumerate(offers):
            (self.root / f"offer{number}.json").write_text(json.dumps(offer), encoding="utf-8")

    def invoke(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", str(self.db), *args])
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def seed(self):
        self.assertEqual(self.invoke("import", "requirement", str(self.root / "requirement.json"))[0], 0)
        self.assertEqual(self.invoke("import", "offer", str(self.root / "offer0.json"))[0], 0)
        self.assertEqual(self.invoke("import", "offer", str(self.root / "offer1.json"))[0], 0)

    def test_cli_import_plan_approve_verify(self):
        self.seed()
        code, analysis, error = self.invoke("plan", "warehouse_trial")
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(analysis["report"]["cost"], "150.00")
        code, approved, _ = self.invoke("approve", str(analysis["id"]), analysis["input_digest"])
        self.assertEqual(code, 0)
        self.assertEqual(approved["state"], "approved")
        self.assertTrue(self.invoke("verify", str(analysis["id"]))[1]["verified"])

    def test_cli_batch_import_is_idempotent(self):
        requirement, offers = demo_payloads()
        batch = {"documents": [{"kind": "requirement", "body": requirement},
                               *[{"kind": "offer", "body": offer} for offer in offers]]}
        path = self.root / "batch.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        self.assertEqual(self.invoke("batch", str(path))[1]["changed"], 3)
        self.assertEqual(self.invoke("batch", str(path))[1]["changed"], 0)
        self.assertEqual(self.invoke("status")[1]["offer_count"], 2)

    def test_cli_brief_and_scenario_commands(self):
        self.seed()
        self.assertEqual(self.invoke("brief", "warehouse_trial")[1]["known_duplicate_records"], 1)
        self.assertEqual(self.invoke("budgets", "warehouse_trial", "140", "150")[1][1]["feasible"], True)
        self.assertEqual(self.invoke("withdrawal", "warehouse_trial", "remote_01")[1]["after_feasible"], False)
        self.assertEqual(self.invoke("compare-packages", "warehouse_trial",
                                     "remote_01", "remote_01,portable_02")[1]["portfolios"][1]["feasible"], True)

    def test_cli_reports_structured_input_error(self):
        code, value, error = self.invoke("plan", "missing_req")
        self.assertEqual(code, 2)
        self.assertIsNone(value)
        self.assertIn("missing_requirement", error)

    def test_demo_runs_in_isolated_workspace(self):
        code, result, error = self.invoke("demo", "--workspace", str(self.root / "demo"))
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(result["state"], "approved")
        self.assertEqual(result["duplicate_count"], 1)
        self.assertTrue(result["verified"])


if __name__ == "__main__":
    unittest.main()
