from __future__ import annotations

from decimal import Decimal
from typing import Any

from .contracts import Offer, Requirement, PortfolioError, SourceEvidence, canonical
from .coverage import CoverageEngine
from .economics import PriceEngine
from .ledger import Ledger, digest
from .optimizer import PortfolioOptimizer
from .sourcing import SourceAnalyzer, SourceContext


class DecisionAudit:
    """仅使用冻结输入重算历史决策，不依赖当前报价清单或当前关系证据。"""

    def __init__(self, ledger: Ledger, coverage: CoverageEngine, prices: PriceEngine,
                 optimizer: PortfolioOptimizer, sourcing: SourceAnalyzer | None = None):
        self.ledger = ledger
        self.coverage = coverage
        self.prices = prices
        self.optimizer = optimizer
        self.sourcing = sourcing or SourceAnalyzer()

    def _inputs(self, analysis_id: int) -> tuple[dict[str, Any], Requirement, list[dict[str, Any]],
                                                 list[Offer], list[dict[str, Any]], list[SourceEvidence]]:
        documents = self.ledger.analysis_inputs(analysis_id)
        requirements = [doc for doc in documents if doc["kind"] == "requirement"]
        if len(requirements) != 1:
            raise PortfolioError("snapshot_inputs", "analysis needs one requirement revision")
        offer_docs = [doc for doc in documents if doc["kind"] == "offer"]
        source_docs = [doc for doc in documents if doc["kind"] == "source"]
        return (requirements[0], Requirement.parse(requirements[0]["body"]), offer_docs,
                [Offer.parse(doc["body"]) for doc in offer_docs], source_docs,
                [SourceEvidence.parse(doc["body"]) for doc in source_docs])

    def explain(self, analysis_id: int) -> dict[str, Any]:
        analysis = self.ledger.analysis(analysis_id)
        (requirement_doc, requirement, offer_docs, offers,
         source_docs, evidence) = self._inputs(analysis_id)
        stored = analysis["report"]
        version = stored.get("schema_version", 1)
        snapshot = self.ledger.verify(analysis_id)
        source_integrity = all(digest(doc["body"]) == doc["digest"]
                               for doc in [requirement_doc, *offer_docs, *source_docs])
        report_selection = stored.get("selection", [])
        selected = [offer for offer in offers if offer.package in report_selection]
        unknown = sorted(set(report_selection) - {offer.package for offer in offers})
        as_of = analysis["as_of"] or stored.get("as_of") or ""
        context = SourceContext(tuple(evidence), as_of) if as_of else None
        # v1 报告生成时还没有来源证据层，重算时必须按当时的语义还原，而不是用新规则改写历史。
        historical = self.optimizer.search(requirement, offers, context if version >= 2 else None)
        coverage_only = self.optimizer.search(requirement, offers)
        recomputed = list(historical.best.packages) if historical.best else []
        recomputed_premium = (str(Decimal(historical.best.cost) - Decimal(coverage_only.best.cost))
                              if historical.best and coverage_only.best else None)
        selected_coverage = self.coverage.evaluate(requirement, selected)
        quotes = self.prices.all_quotes(selected, requirement)
        recomputed_cost = self.prices.total(quotes) if historical.best else None
        recomputed_sourcing = (self.sourcing.assess(requirement, selected, context)
                               if selected and context else None)
        mismatches = []
        if not snapshot["verified"]:
            mismatches.append("snapshot_digest")
        if not source_integrity:
            mismatches.append("source_digest")
        if unknown:
            mismatches.append("unknown_selected_package")
        if report_selection != recomputed:
            mismatches.append("selection")
        if stored.get("feasible") != (historical.best is not None):
            mismatches.append("feasibility")
        if stored.get("cost") != recomputed_cost:
            mismatches.append("cost")
        if canonical(stored.get("coverage")) != canonical(selected_coverage.document()):
            mismatches.append("coverage")
        if stored.get("quotes") != [quote.document() for quote in quotes]:
            mismatches.append("quotes")
        if canonical(self._normalize_search(stored.get("search") or {}, version)) != canonical(
                self._normalize_search(historical.document(), version)):
            mismatches.append("search")
        if stored.get("provider_spend") != self.prices.provider_breakdown(quotes):
            mismatches.append("provider_spend")
        if stored.get("selected_attribution") != self.coverage.attribution(requirement, selected):
            mismatches.append("attribution")
        if version >= 2:
            if context is not None and stored.get("source_assurance") != recomputed_sourcing:
                mismatches.append("source_assurance")
            if context is None and stored.get("source_assurance") is not None:
                mismatches.append("source_assurance")
            if stored.get("assurance_premium") != recomputed_premium:
                mismatches.append("assurance_premium")
            if coverage_only.best:
                coverage_only_selected = [offer for offer in offers
                                          if offer.package in coverage_only.best.packages]
                recomputed_coverage_assurance = (
                    self.sourcing.assess(requirement, coverage_only_selected, context)
                    if context else None)
                if stored.get("coverage_only_source_assurance") != recomputed_coverage_assurance:
                    mismatches.append("coverage_only_assurance")
            if stored.get("as_of") != as_of or not as_of:
                mismatches.append("as_of")
        if stored.get("cost_per_credited_sample") != (
                self.prices.cost_per_credited_sample(recomputed_cost, selected_coverage.total_credited)
                if recomputed_cost is not None else None):
            mismatches.append("unit_cost")
        lineage = [{"kind": doc["kind"], "key": doc["key"],
                    "revision": doc["revision"], "digest": doc["digest"]}
                   for doc in [requirement_doc, *offer_docs, *source_docs]]
        current = self.ledger.latest_input_digest(requirement.key)
        current_evidence = [{"key": doc["key"], "revision": doc["revision"]}
                            for doc in self.ledger.documents("source")]
        return {"analysis_id": analysis_id, "state": analysis["state"],
                "historical_valid": not mismatches, "mismatches": mismatches,
                "input_current": current == analysis["input_digest"],
                "evidence_current": current_evidence == [
                    {"key": doc["key"], "revision": doc["revision"]} for doc in source_docs],
                "as_of": as_of,
                "lineage": lineage, "selection": recomputed,
                "cost": recomputed_cost, "coverage": selected_coverage.document(),
                "source_assurance": recomputed_sourcing,
                "snapshot_digest_valid": snapshot["verified"],
                "source_digest_valid": source_integrity}

    @staticmethod
    def _normalize_search(document: dict[str, Any], version: int) -> dict[str, Any]:
        """v1 的候选没有 source_assurance 字段；忽略它才不至于把旧快照判成被篡改。"""
        if version >= 2:
            return document
        def strip(candidate):
            if candidate is None:
                return None
            value = dict(candidate)
            value.pop("source_assurance", None)
            return value
        value = dict(document)
        value["best"] = strip(value.get("best"))
        value["alternatives"] = [strip(item) for item in value.get("alternatives", [])]
        return value

    def compare(self, earlier_id: int, later_id: int) -> dict[str, Any]:
        if earlier_id == later_id:
            raise PortfolioError("analysis_pair", "choose two distinct analyses")
        earlier = self.ledger.analysis(earlier_id)
        later = self.ledger.analysis(later_id)
        if earlier["requirement_key"] != later["requirement_key"]:
            raise PortfolioError("requirement_pair", "analyses must share requirement key")
        first = self.explain(earlier_id)
        second = self.explain(later_id)
        if not first["historical_valid"] or not second["historical_valid"]:
            raise PortfolioError("invalid_snapshot", "cannot compare invalid analysis")
        a = {doc["key"]: doc for doc in first["lineage"] if doc["kind"] == "offer"}
        b = {doc["key"]: doc for doc in second["lineage"] if doc["kind"] == "offer"}
        a_sources = {doc["key"]: doc for doc in first["lineage"] if doc["kind"] == "source"}
        b_sources = {doc["key"]: doc for doc in second["lineage"] if doc["kind"] == "source"}
        a_selected = set(first["selection"])
        b_selected = set(second["selection"])
        changes = []
        for package in sorted(a.keys() | b.keys()):
            left = a.get(package)
            right = b.get(package)
            if left is None or right is None or left["digest"] != right["digest"]:
                changes.append({"package": package,
                                "earlier_revision": left["revision"] if left else None,
                                "later_revision": right["revision"] if right else None})
        evidence_changes = [{"key": source_key,
                             "earlier_revision": a_sources.get(source_key, {}).get("revision"),
                             "later_revision": b_sources.get(source_key, {}).get("revision")}
                            for source_key in sorted(a_sources.keys() | b_sources.keys())
                            if a_sources.get(source_key, {}).get("digest")
                            != b_sources.get(source_key, {}).get("digest")]
        left_cost = earlier["report"].get("cost")
        right_cost = later["report"].get("cost")
        cost_delta = str(Decimal(right_cost) - Decimal(left_cost)) if left_cost and right_cost else None
        a_cells = {cell["slice"]: cell for cell in first["coverage"]["cells"]}
        b_cells = {cell["slice"]: cell for cell in second["coverage"]["cells"]}
        coverage_changes = [{"slice": token,
                             "credited_before": a_cells.get(token, {}).get("credited", 0),
                             "credited_after": b_cells.get(token, {}).get("credited", 0),
                             "shortage_before": a_cells.get(token, {}).get("shortage", 0),
                             "shortage_after": b_cells.get(token, {}).get("shortage", 0)}
                            for token in sorted(a_cells.keys() | b_cells.keys())
                            if a_cells.get(token) != b_cells.get(token)]
        return {"requirement": earlier["requirement_key"], "earlier": earlier_id,
                "later": later_id, "requirement_revision_changed":
                earlier["requirement_revision"] != later["requirement_revision"],
                "offer_revisions_changed": changes,
                "source_evidence_changed": evidence_changes,
                "source_assurance_changed":
                (first.get("source_assurance") or {}).get("assurance_met")
                != (second.get("source_assurance") or {}).get("assurance_met"),
                "packages_added": sorted(b_selected - a_selected),
                "packages_removed": sorted(a_selected - b_selected),
                "cost_delta": cost_delta, "coverage_changes": coverage_changes}
