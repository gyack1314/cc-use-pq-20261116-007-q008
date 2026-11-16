from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import Catalog
from .contracts import Offer, Requirement, PortfolioError
from .coverage import CoverageEngine
from .economics import PriceEngine
from .ledger import Ledger, digest
from .optimizer import PortfolioOptimizer
from .sensitivity import SensitivityAnalyzer
from .diagnostics import SupplyDiagnostics
from .audit import DecisionAudit


class PortfolioService:
    def __init__(self, db_path: str | Path):
        self.ledger = Ledger(db_path)
        self.catalog = Catalog(self.ledger)
        self.coverage = CoverageEngine()
        self.prices = PriceEngine()
        self.optimizer = PortfolioOptimizer(self.coverage, self.prices)
        self.sensitivity = SensitivityAnalyzer(self.optimizer, self.coverage, self.prices)
        self.diagnostics = SupplyDiagnostics(self.coverage, self.prices)
        self.audit = DecisionAudit(self.ledger, self.coverage, self.prices, self.optimizer)

    def close(self):
        self.ledger.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def import_offer(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_offer(body)

    def import_requirement(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_requirement(body)

    def import_file(self, kind: str, path: str | Path) -> dict[str, Any]:
        return self.catalog.import_file(kind, path)

    def import_batch_file(self, path: str | Path) -> dict[str, Any]:
        return self.catalog.import_batch_file(path)

    def plan(self, requirement_key: str) -> dict[str, Any]:
        requirement_doc, requirement = self.catalog.requirement(requirement_key)
        offer_pairs = self.catalog.offers()
        offers = [offer for _, offer in offer_pairs]
        search = self.optimizer.search(requirement, offers)
        selected = [offer for offer in offers if search.best and offer.package in search.best.packages]
        selected_quotes = self.prices.all_quotes(selected, requirement)
        selected_coverage = self.coverage.evaluate(requirement, selected)
        report = {"schema_version": 1, "requirement": requirement.key,
                  "requirement_revision": requirement_doc["revision"],
                  "client": requirement.client, "budget": requirement.budget,
                  "feasible": search.best is not None,
                  "selection": list(search.best.packages) if search.best else [],
                  "cost": search.best.cost if search.best else None,
                  "coverage": selected_coverage.document(),
                  "search": search.document(),
                  "quotes": [quote.document() for quote in selected_quotes],
                  "budget_pressure": self.prices.budget_pressure(selected_quotes, requirement),
                  "provider_spend": self.prices.provider_breakdown(selected_quotes),
                  "selected_attribution": self.coverage.attribution(requirement, selected),
                  "cost_per_credited_sample": self.prices.cost_per_credited_sample(
                      search.best.cost, selected_coverage.total_credited) if search.best else None,
                  "target_supply": self.diagnostics.target_supply(requirement, offers),
                  "eligible_supply_readiness": self.diagnostics.data_readiness(requirement, offers),
                  "exclusions": self.optimizer.exclusion_analysis(requirement, offers, selected),
                  "catalog_versions": [{"id": doc["id"], "key": doc["key"],
                                        "revision": doc["revision"], "digest": doc["digest"]}
                                       for doc, _ in offer_pairs]}
        return self.ledger.insert_analysis(requirement_doc, [doc for doc, _ in offer_pairs], report)

    def approve(self, analysis_id: int, expected_input_digest: str) -> dict[str, Any]:
        return self.ledger.approve(analysis_id, expected_input_digest)

    def analysis(self, analysis_id: int) -> dict[str, Any]:
        return self.ledger.analysis(analysis_id)

    def verify(self, analysis_id: int) -> dict[str, Any]:
        return self.ledger.verify(analysis_id)

    def history(self, requirement_key: str | None = None) -> list[dict[str, Any]]:
        return self.ledger.analyses(requirement_key)

    def document_history(self, kind: str, key: str) -> list[dict[str, Any]]:
        return self.catalog.history(kind, key)

    def compare_versions(self, kind: str, key: str, earlier: int, later: int) -> dict[str, Any]:
        return self.catalog.compare_versions(kind, key, earlier, later)

    def gaps(self, requirement_key: str, package_keys: list[str]) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        offers = {offer.package: offer for _, offer in self.catalog.offers()}
        if any(key not in offers for key in package_keys):
            raise PortfolioError("unknown_offer", "requested package not in catalog")
        chosen = [offers[key] for key in package_keys]
        return self.coverage.gap_report(requirement, chosen)

    def marginal(self, requirement_key: str, current_keys: list[str], candidate_key: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        offers = {offer.package: offer for _, offer in self.catalog.offers()}
        if candidate_key not in offers or any(key not in offers for key in current_keys):
            raise PortfolioError("unknown_offer", "requested package not in catalog")
        return self.coverage.marginal(requirement, [offers[key] for key in current_keys], offers[candidate_key])

    def frontier(self, requirement_key: str) -> list[dict[str, Any]]:
        _, requirement = self.catalog.requirement(requirement_key)
        return self.optimizer.frontier(requirement, [offer for _, offer in self.catalog.offers()])

    def status(self) -> dict[str, Any]:
        summary = self.catalog.summary()
        analyses = self.ledger.analyses()
        return {**summary, "analysis_count": len(analyses),
                "approved_count": sum(item["state"] == "approved" for item in analyses)}

    def readiness(self, requirement_key: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        offers = [offer for _, offer in self.catalog.offers()]
        return {"data": self.diagnostics.data_readiness(requirement, offers),
                "paths": self.diagnostics.path_distribution(offers),
                "providers": self.diagnostics.provider_exposure(requirement, offers),
                "restrictions": self.diagnostics.licensing_reasons(requirement, offers)}

    def brief(self, requirement_key: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        return self.diagnostics.procurement_brief(requirement,
                                                   [offer for _, offer in self.catalog.offers()])

    def budget_grid(self, requirement_key: str, budgets: list[str]) -> list[dict[str, Any]]:
        _, requirement = self.catalog.requirement(requirement_key)
        return self.sensitivity.budget_grid(requirement,
                                            [offer for _, offer in self.catalog.offers()], budgets)

    def target_grid(self, requirement_key: str, target: str, minimums: list[int]) -> list[dict[str, Any]]:
        _, requirement = self.catalog.requirement(requirement_key)
        return self.sensitivity.target_grid(requirement,
                                            [offer for _, offer in self.catalog.offers()], target, minimums)

    def withdrawal(self, requirement_key: str, package: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        return self.sensitivity.remove_offer(requirement,
                                             [offer for _, offer in self.catalog.offers()], package)

    def price_scenario(self, requirement_key: str, package: str, price: str) -> dict[str, Any]:
        _, requirement = self.catalog.requirement(requirement_key)
        return self.sensitivity.price_change(requirement,
                                             [offer for _, offer in self.catalog.offers()], package, price)

    def audit_analysis(self, analysis_id: int) -> dict[str, Any]:
        return self.audit.explain(analysis_id)

    def compare_analyses(self, earlier_id: int, later_id: int) -> dict[str, Any]:
        return self.audit.compare(earlier_id, later_id)

    def compare_packages(self, requirement_key: str, package_groups: list[list[str]]) -> dict[str, Any]:
        if not 1 <= len(package_groups) <= 12:
            raise PortfolioError("portfolio_count", "compare 1-12 groups")
        _, requirement = self.catalog.requirement(requirement_key)
        offers = {offer.package: offer for _, offer in self.catalog.offers()}
        preferred = self.optimizer.search(requirement, list(offers.values()))
        results = []
        for position, group in enumerate(package_groups):
            if not group or len(group) != len(set(group)):
                raise PortfolioError("portfolio_shape", "each group needs distinct packages")
            if len(group) > requirement.maximum_packages:
                raise PortfolioError("portfolio_size", "group exceeds package limit")
            if any(package not in offers for package in group):
                raise PortfolioError("unknown_offer", "group includes unknown package")
            chosen = [offers[package] for package in group]
            quotes = self.prices.all_quotes(chosen, requirement)
            coverage = self.coverage.evaluate(requirement, chosen)
            rejected = [{"package": quote.package, "reason": quote.reason}
                        for quote in quotes if not quote.permitted]
            results.append({"index": position, "packages": sorted(group),
                            "cost": self.prices.total(quotes), "coverage": coverage.document(),
                            "eligible": not rejected, "restrictions": rejected,
                            "within_budget": self.prices.within_budget(quotes, requirement),
                            "feasible": not rejected and coverage.mandatory_complete
                            and self.prices.within_budget(quotes, requirement),
                            "provider_spend": self.prices.provider_breakdown(quotes),
                            "attribution": self.coverage.attribution(requirement, chosen)})
        return {"requirement": requirement.key,
                "recommended": list(preferred.best.packages) if preferred.best else [],
                "portfolios": results}
