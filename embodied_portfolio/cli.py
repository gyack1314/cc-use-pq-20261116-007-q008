from __future__ import annotations

from argparse import ArgumentParser, Namespace
from copy import deepcopy
from pathlib import Path
from typing import Any
import json
import sys

from .contracts import PortfolioError
from .decision import PortfolioService


def parser() -> ArgumentParser:
    main = ArgumentParser(prog="embodied_portfolio", description="离线具身数据采购组合分析")
    main.add_argument("--db", default="portfolio.sqlite3", help="本地SQLite文件")
    commands = main.add_subparsers(dest="command", required=True)
    incoming = commands.add_parser("import")
    incoming.add_argument("kind", choices=("offer", "requirement", "source"))
    incoming.add_argument("file")
    batch = commands.add_parser("batch")
    batch.add_argument("file")
    plan = commands.add_parser("plan")
    plan.add_argument("requirement")
    plan.add_argument("--as-of", dest="as_of", default=None,
                      help="评估证据时效的日期 YYYY-MM-DD，默认今天")
    approve = commands.add_parser("approve")
    approve.add_argument("analysis", type=int)
    approve.add_argument("input_digest")
    approve.add_argument("--as-of", dest="as_of", default=None)
    analysis = commands.add_parser("analysis")
    analysis.add_argument("id", type=int)
    verify = commands.add_parser("verify")
    verify.add_argument("id", type=int)
    history = commands.add_parser("history")
    history.add_argument("--requirement")
    documents = commands.add_parser("versions")
    documents.add_argument("kind", choices=("offer", "requirement", "source"))
    documents.add_argument("key")
    revisions = commands.add_parser("compare-versions")
    revisions.add_argument("kind", choices=("offer", "requirement", "source"))
    revisions.add_argument("key")
    revisions.add_argument("earlier", type=int)
    revisions.add_argument("later", type=int)
    gaps = commands.add_parser("gaps")
    gaps.add_argument("requirement")
    gaps.add_argument("packages", nargs="*")
    marginal = commands.add_parser("marginal")
    marginal.add_argument("requirement")
    marginal.add_argument("candidate")
    marginal.add_argument("current", nargs="*")
    frontier = commands.add_parser("frontier")
    frontier.add_argument("requirement")
    readiness = commands.add_parser("readiness")
    readiness.add_argument("requirement")
    brief = commands.add_parser("brief")
    brief.add_argument("requirement")
    sourcing = commands.add_parser("sourcing")
    sourcing.add_argument("requirement")
    sourcing.add_argument("--as-of", dest="as_of", default=None)
    budgets = commands.add_parser("budgets")
    budgets.add_argument("requirement")
    budgets.add_argument("amounts", nargs="+")
    targets = commands.add_parser("targets")
    targets.add_argument("requirement")
    targets.add_argument("slice")
    targets.add_argument("minimums", nargs="+", type=int)
    withdrawal = commands.add_parser("withdrawal")
    withdrawal.add_argument("requirement")
    withdrawal.add_argument("package")
    price = commands.add_parser("price-scenario")
    price.add_argument("requirement")
    price.add_argument("package")
    price.add_argument("unit_price")
    audit = commands.add_parser("audit")
    audit.add_argument("id", type=int)
    compare = commands.add_parser("compare")
    compare.add_argument("earlier", type=int)
    compare.add_argument("later", type=int)
    groups = commands.add_parser("compare-packages")
    groups.add_argument("requirement")
    groups.add_argument("groups", nargs="+", help="comma-separated package keys per portfolio")
    events = commands.add_parser("events")
    events.add_argument("--key")
    commands.add_parser("status")
    demo = commands.add_parser("demo")
    demo.add_argument("--workspace", default="fixtures")
    sourcing_demo = commands.add_parser("demo-sourcing")
    sourcing_demo.add_argument("--workspace", default="fixtures_sourcing")
    sourcing_demo.add_argument("--as-of", dest="as_of", default="2026-11-17")
    return main


def demo_payloads() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = {"scenario": "warehouse_pick", "stage": "grasp", "outcome": "failure", "robot": "arm_a"}
    requirement = {"key": "warehouse_trial", "client": "client_a", "title": "合成仓储抓取失败样本采购",
                   "budget": "180", "targets": [{"slice": target, "minimum": 4,
                                                  "mandatory": True, "weight": "2"}],
                   "allowed_paths": ["teleoperation", "portable"], "maximum_packages": 3}
    first = {"provider": "provider_a", "package": "remote_01", "path": "teleoperation",
             "robots": ["arm_a"], "observations": [{"slice": target, "count": 3,
                                                  "episode_ids": ["trial_a", "trial_b", "trial_c"]}],
             "hours": "1", "price_mode": "per_hour", "unit_price": "100",
             "license": "nonexclusive", "allowed_clients": [], "validity": "2026", "notes": "合成数据"}
    second = {"provider": "provider_b", "package": "portable_02", "path": "portable",
              "robots": ["arm_a"], "observations": [{"slice": target, "count": 2,
                                                   "episode_ids": ["trial_c", "trial_d"]}],
              "hours": "0", "price_mode": "flat", "unit_price": "50",
              "license": "single_customer", "allowed_clients": ["client_a"],
              "validity": "2026", "notes": "合成数据"}
    return requirement, [first, second]


def sourcing_payloads() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """两家名称不同的报价商共用同一上游采集队；第三家才真正独立。

    覆盖意义上 remote_01 + portable_02 已足够，但两者同源；独立保障要求引入
    portable_03，成本上升。更正证据后才可能用更便宜的独立组合。
    """
    target = {"scenario": "warehouse_pick", "stage": "grasp", "outcome": "failure", "robot": "arm_a"}
    requirement = {"key": "warehouse_trial", "client": "client_a",
                   "title": "合成仓储抓取失败样本采购（要求双来源）",
                   "budget": "280",
                   "targets": [{"slice": target, "minimum": 7, "mandatory": True,
                                "weight": "2", "minimum_independent_sources": 2}],
                   "allowed_paths": ["teleoperation", "portable"], "maximum_packages": 3}
    first = {"provider": "provider_a", "package": "remote_01", "path": "teleoperation",
             "robots": ["arm_a"], "observations": [{"slice": target, "count": 5,
                                                  "episode_ids": ["trial_1", "trial_2", "trial_3",
                                                                  "trial_4", "trial_5"]}],
             "hours": "1", "price_mode": "per_hour", "unit_price": "100",
             "license": "nonexclusive", "allowed_clients": [], "validity": "2026", "notes": "合成数据"}
    second = {"provider": "provider_b", "package": "portable_02", "path": "portable",
              "robots": ["arm_a"], "observations": [{"slice": target, "count": 3,
                                                   "episode_ids": ["trial_5", "trial_6", "trial_7"]}],
              "hours": "0", "price_mode": "flat", "unit_price": "50",
              "license": "single_customer", "allowed_clients": ["client_a"],
              "validity": "2026", "notes": "合成数据"}
    third = {"provider": "provider_c", "package": "portable_03", "path": "portable",
             "robots": ["arm_a"], "observations": [{"slice": target, "count": 1,
                                                  "episode_ids": ["trial_8"]}],
             "hours": "0", "price_mode": "flat", "unit_price": "120",
             "license": "nonexclusive", "allowed_clients": [], "validity": "2026",
             "notes": "合成数据，独立采集队"}
    evidence = {"key": "upstream_2026", "title": "上游采集队关系证据",
                "claims": [{"package": "remote_01", "collector": "field_crew_alpha",
                            "basis": "contract", "strength": "verified",
                            "reference": "采购合同附件A", "observed_on": "2026-10-01"},
                           {"package": "portable_02", "collector": "field_crew_alpha",
                            "basis": "disclosure", "strength": "reported",
                            "reference": "供应商书面说明", "observed_on": "2026-10-02"},
                           {"package": "portable_03", "collector": "field_crew_beta",
                            "basis": "audit", "strength": "verified",
                            "reference": "现场审计记录", "observed_on": "2026-10-03"}],
                "valid_from": "2026-10-01", "valid_until": "2027-09-30", "notes": "合成证据"}
    return requirement, [first, second, third], [evidence]


def demo(workspace: str) -> dict[str, Any]:
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    requirement, offers = demo_payloads()
    with PortfolioService(root / "portfolio.sqlite3") as service:
        req = service.import_requirement(requirement)
        for offer in offers:
            service.import_offer(offer)
        analysis = service.plan(req["key"])
        if not analysis["report"]["feasible"]:
            raise PortfolioError("demo_infeasible", "synthetic example unexpectedly infeasible")
        approved = service.approve(analysis["id"], analysis["input_digest"])
        verified = service.verify(approved["id"])
        if not verified["verified"]:
            raise PortfolioError("demo_verify", "synthetic analysis snapshot did not verify")
        return {"analysis_id": approved["id"], "state": approved["state"],
                "selection": approved["report"]["selection"], "cost": approved["report"]["cost"],
                "credited": approved["report"]["coverage"]["total_credited"],
                "duplicate_count": approved["report"]["coverage"]["total_duplicate"],
                "verified": verified["verified"]}


def demo_sourcing(workspace: str, as_of: str) -> dict[str, Any]:
    """演示：名称分散但上游共享 → 推荐独立组合 → 更正证据后重新分析 → 旧批准快照仍可审计。"""
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    requirement, offers, evidence = sourcing_payloads()
    with PortfolioService(root / "portfolio.sqlite3") as service:
        req = service.import_requirement(requirement)
        for offer in offers:
            service.import_offer(offer)
        service.import_source(evidence[0])
        first = service.plan(req["key"], as_of)
        report = first["report"]
        misleading = (report["coverage_only_source_assurance"] or {})
        if not misleading.get("shared_source_groups"):
            raise PortfolioError("demo_sourcing", "coverage-only choice should look diversified but share upstream")
        approved = service.approve(first["id"], first["input_digest"])
        if approved["state"] != "approved":
            raise PortfolioError("demo_sourcing", "approval should record current evidence")
        corrected = deepcopy(evidence[0])
        corrected["claims"] = [claim for claim in corrected["claims"]
                               if claim["package"] != "portable_02"]
        corrected["claims"].append({"package": "portable_02", "collector": "field_crew_beta",
                                    "basis": "audit", "strength": "verified",
                                    "reference": "更正后的审计记录", "observed_on": "2026-11-16"})
        service.import_source(corrected)
        second = service.plan(req["key"], as_of)
        history = service.audit_analysis(approved["id"])
        if not history["historical_valid"]:
            raise PortfolioError("demo_audit", "approved snapshot should stay auditable")
        if history["evidence_current"]:
            raise PortfolioError("demo_audit", "old approval must not claim current evidence")
        if not service.audit_analysis(second["id"])["evidence_current"]:
            raise PortfolioError("demo_audit", "new analysis should bind corrected evidence")
        return {"first_analysis": first["id"], "first_selection": report["selection"],
                "first_cost": report["cost"],
                "first_assurance_met": report["source_assurance"]["assurance_met"],
                "coverage_only_selection": report["coverage_only_selection"],
                "coverage_only_cost": report["coverage_only_cost"],
                "coverage_only_assurance_met":
                    report["coverage_only_source_assurance"]["assurance_met"],
                "coverage_only_shared_upstream":
                    report["coverage_only_source_assurance"]["shared_source_groups"],
                "provider_diversity_misleading":
                    report["coverage_only_source_assurance"]["provider_diversity_misleading"],
                "independent_alternative": report["independent_alternative"],
                "assurance_premium": report["assurance_premium"],
                "second_analysis": second["id"], "second_selection": second["report"]["selection"],
                "second_cost": second["report"]["cost"],
                "second_assurance_met": second["report"]["source_assurance"]["assurance_met"],
                "old_approval_still_valid": history["historical_valid"],
                "old_approval_evidence_current": history["evidence_current"],
                "second_evidence_current": service.audit_analysis(second["id"])["evidence_current"],
                "old_approval_selection": approved["report"]["selection"]}


def execute(args: Namespace) -> Any:
    if args.command == "demo":
        return demo(args.workspace)
    if args.command == "demo-sourcing":
        return demo_sourcing(args.workspace, args.as_of)
    with PortfolioService(args.db) as service:
        if args.command == "import":
            return service.import_file(args.kind, args.file)
        if args.command == "batch":
            return service.import_batch_file(args.file)
        if args.command == "plan":
            return service.plan(args.requirement, args.as_of)
        if args.command == "approve":
            return service.approve(args.analysis, args.input_digest, args.as_of)
        if args.command == "analysis":
            return service.analysis(args.id)
        if args.command == "verify":
            return service.verify(args.id)
        if args.command == "history":
            return service.history(args.requirement)
        if args.command == "versions":
            return service.document_history(args.kind, args.key)
        if args.command == "compare-versions":
            return service.compare_versions(args.kind, args.key, args.earlier, args.later)
        if args.command == "gaps":
            return service.gaps(args.requirement, args.packages)
        if args.command == "marginal":
            return service.marginal(args.requirement, args.current, args.candidate)
        if args.command == "frontier":
            return service.frontier(args.requirement)
        if args.command == "readiness":
            return service.readiness(args.requirement)
        if args.command == "brief":
            return service.brief(args.requirement)
        if args.command == "sourcing":
            return service.sourcing(args.requirement, args.as_of)
        if args.command == "budgets":
            return service.budget_grid(args.requirement, args.amounts)
        if args.command == "targets":
            return service.target_grid(args.requirement, args.slice, args.minimums)
        if args.command == "withdrawal":
            return service.withdrawal(args.requirement, args.package)
        if args.command == "price-scenario":
            return service.price_scenario(args.requirement, args.package, args.unit_price)
        if args.command == "audit":
            return service.audit_analysis(args.id)
        if args.command == "compare":
            return service.compare_analyses(args.earlier, args.later)
        if args.command == "compare-packages":
            return service.compare_packages(args.requirement, [item.split(",") for item in args.groups])
        if args.command == "events":
            return service.ledger.events(args.key)
        if args.command == "status":
            return service.status()
    raise PortfolioError("command", "unknown command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print(json.dumps(execute(args), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (PortfolioError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
