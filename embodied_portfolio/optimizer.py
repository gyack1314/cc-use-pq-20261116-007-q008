from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from itertools import combinations
from typing import Any

from .contracts import Offer, Requirement, PortfolioError
from .coverage import CoverageEngine, CoverageResult
from .economics import PriceEngine, Quote
from .sourcing import AssuranceIndex, SourceAnalyzer, SourceContext


@dataclass(frozen=True)
class Candidate:
    packages: tuple[str, ...]
    cost: str
    score: str
    credited: int
    shortage: int
    duplicates: int
    mandatory_complete: bool
    within_budget: bool
    source_assurance: bool = True

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    best: Candidate | None
    evaluated: int
    feasible: int
    filtered: tuple[dict[str, str], ...]
    alternatives: tuple[Candidate, ...]
    reason: str | None

    def document(self) -> dict[str, Any]:
        return {"best": self.best.document() if self.best else None,
                "evaluated": self.evaluated, "feasible": self.feasible,
                "filtered": list(self.filtered),
                "alternatives": [item.document() for item in self.alternatives],
                "reason": self.reason}


class PortfolioOptimizer:
    def __init__(self, coverage: CoverageEngine, prices: PriceEngine, maximum_candidates: int = 18,
                 sourcing: SourceAnalyzer | None = None):
        self.coverage = coverage
        self.prices = prices
        self.maximum_candidates = maximum_candidates
        self.sourcing = sourcing or SourceAnalyzer()

    def eligible(self, requirement: Requirement, offers: list[Offer]) -> tuple[list[Offer], list[dict[str, str]]]:
        eligible = []
        filtered = []
        target_tokens = {target.slice.token() for target in requirement.targets}
        for offer in offers:
            quote = self.prices.quote(offer, requirement)
            if not quote.permitted:
                filtered.append({"package": offer.package, "reason": quote.reason or "not_permitted"})
                continue
            if not any(observation.slice.token() in target_tokens for observation in offer.observations):
                filtered.append({"package": offer.package, "reason": "no_requested_slice"})
                continue
            eligible.append(offer)
        return eligible, filtered

    def evaluate(self, requirement: Requirement, offers: list[Offer],
                 context: SourceContext | None = None,
                 index: AssuranceIndex | None = None) -> Candidate:
        coverage = self.coverage.evaluate(requirement, offers)
        quotes = self.prices.all_quotes(offers, requirement)
        amount = self.prices.total(quotes)
        if index is not None:
            assurance = index.met({offer.package for offer in offers})
        elif context is not None:
            assurance = not self.sourcing.blocks(requirement, offers, context)
        else:
            assurance = True
        return Candidate(tuple(sorted(offer.package for offer in offers)), amount,
                         coverage.weighted_fraction, coverage.total_credited,
                         coverage.total_shortage, coverage.total_duplicate,
                         coverage.mandatory_complete, Decimal(amount) <= Decimal(requirement.budget),
                         assurance)

    @staticmethod
    def ranking(candidate: Candidate) -> tuple:
        return (-Decimal(candidate.score), Decimal(candidate.cost),
                candidate.duplicates, len(candidate.packages), candidate.packages)

    def _assured_cheapest(self, eligible: list[Offer], requirement: Requirement,
                          index: AssuranceIndex) -> Candidate | None:
        return self._enumerate(eligible, requirement, index)[3]

    def _enumerate(self, eligible: list[Offer], requirement: Requirement,
                   index: AssuranceIndex | None
                   ) -> tuple[list[Candidate], list[Candidate], list[Candidate], Candidate | None, int]:
        """单次枚举收集：来源保障组合、仅覆盖可行组合、预算内组合、最便宜来源保障组合、评估数。"""
        assured: list[Candidate] = []
        coverage_feasible: list[Candidate] = []
        affordable: list[Candidate] = []
        cheapest_assured: Candidate | None = None
        evaluated = 0
        for size in range(1, min(len(eligible), requirement.maximum_packages) + 1):
            for subset in combinations(eligible, size):
                evaluated += 1
                candidate = self.evaluate(requirement, list(subset), index=index)
                if candidate.within_budget:
                    affordable.append(candidate)
                    if candidate.mandatory_complete:
                        coverage_feasible.append(candidate)
                        if candidate.source_assurance:
                            assured.append(candidate)
                if candidate.mandatory_complete and candidate.source_assurance:
                    key = (Decimal(candidate.cost), -Decimal(candidate.score), candidate.packages)
                    if cheapest_assured is None or key < (
                            Decimal(cheapest_assured.cost), -Decimal(cheapest_assured.score),
                            cheapest_assured.packages):
                        cheapest_assured = candidate
        return assured, coverage_feasible, affordable, cheapest_assured, evaluated

    def survey(self, requirement: Requirement, offers: list[Offer], context: SourceContext | None
               ) -> tuple[SearchResult, SearchResult, Candidate | None]:
        """一次枚举同时给出：来源保障最优、仅覆盖最优、与最便宜的来源保障组合。

        规划报告需要同时回答“覆盖选了什么”“保障后选了什么”“为独立性多付多少”，
        分开调用会把同一组合空间枚举三遍。
        """
        eligible, filtered = self.eligible(requirement, offers)
        if len(eligible) > self.maximum_candidates:
            raise PortfolioError("candidate_limit", f"more than {self.maximum_candidates} eligible offers")
        index = self.sourcing.index(requirement, eligible, context) if context is not None else None
        assured, coverage_feasible, affordable, cheapest_assured, evaluated = self._enumerate(
            eligible, requirement, index)
        assured.sort(key=self.ranking)
        coverage_feasible.sort(key=self.ranking)
        affordable.sort(key=self.ranking)
        assured_result = self._result(assured, affordable, filtered, evaluated, len(eligible))
        coverage_result = self._result(coverage_feasible, affordable, filtered, evaluated,
                                       len(eligible))
        return assured_result, coverage_result, cheapest_assured

    def _result(self, feasible: list[Candidate], affordable: list[Candidate],
                filtered: list[dict[str, str]], evaluated: int, eligible_count: int) -> SearchResult:
        if feasible:
            return SearchResult(feasible[0], evaluated, len(feasible), tuple(filtered),
                                tuple(feasible[1:4]), None)
        if not eligible_count:
            reason = "no_eligible_offers"
        elif not affordable:
            reason = "all_combinations_exceed_budget"
        elif not any(item.mandatory_complete for item in affordable):
            reason = "mandatory_targets_uncovered"
        else:
            reason = "source_assurance_unmet"
        return SearchResult(None, evaluated, 0, tuple(filtered), tuple(affordable[:3]), reason)

    def search(self, requirement: Requirement, offers: list[Offer],
               context: SourceContext | None = None) -> SearchResult:
        return self.survey(requirement, offers, context)[0]

    def frontier(self, requirement: Requirement, offers: list[Offer],
                 context: SourceContext | None = None) -> list[dict[str, Any]]:
        eligible, _ = self.eligible(requirement, offers)
        if len(eligible) > self.maximum_candidates:
            raise PortfolioError("candidate_limit", "too many offers for frontier")
        index = self.sourcing.index(requirement, eligible, context) if context is not None else None
        candidates = []
        for size in range(1, min(len(eligible), requirement.maximum_packages) + 1):
            for subset in combinations(eligible, size):
                candidate = self.evaluate(requirement, list(subset), index=index)
                if candidate.within_budget:
                    candidates.append(candidate)
        frontier = []
        for candidate in candidates:
            dominated = any(other != candidate and
                            Decimal(other.cost) <= Decimal(candidate.cost) and
                            Decimal(other.score) >= Decimal(candidate.score) and
                            (Decimal(other.cost) < Decimal(candidate.cost) or
                             Decimal(other.score) > Decimal(candidate.score))
                            for other in candidates)
            if not dominated:
                frontier.append(candidate)
        frontier.sort(key=self.ranking)
        return [item.document() for item in frontier]

    def exclusion_analysis(self, requirement: Requirement, offers: list[Offer],
                           chosen: list[Offer], context: SourceContext | None = None) -> list[dict[str, Any]]:
        chosen_keys = {offer.package for offer in chosen}
        baseline = self.evaluate(requirement, chosen, context)
        explanations = []
        for offer in offers:
            if offer.package in chosen_keys:
                continue
            quote = self.prices.quote(offer, requirement)
            if not quote.permitted:
                reason = quote.reason
                gain = None
            else:
                with_offer = self.evaluate(requirement, [*chosen, offer], context)
                gain = with_offer.credited - baseline.credited
                if not with_offer.within_budget:
                    reason = "exceeds_budget_with_selection"
                elif gain == 0:
                    reason = "no_marginal_coverage"
                else:
                    reason = "lower_ranked_feasible_choice"
            explanations.append({"package": offer.package, "reason": reason,
                                 "marginal_credited": gain,
                                 "quote": quote.amount})
        return explanations

    def independent_from(self, requirement: Requirement, candidate: Candidate | None,
                         reference: Candidate | None) -> dict[str, Any]:
        """把来源保障组合与“只按覆盖选出的组合”做成本对照。"""
        reference_packages = reference.packages if reference is not None else ()
        if candidate is None:
            return {"available": False, "reason": "no_source_assured_portfolio",
                    "reference_packages": list(reference_packages),
                    "reference_cost": reference.cost if reference is not None else None}
        additional = (Decimal(candidate.cost) - Decimal(reference.cost)
                      if reference is not None else None)
        return {"available": True,
                "packages": list(candidate.packages), "cost": candidate.cost,
                "reference_packages": list(reference_packages),
                "reference_cost": reference.cost if reference is not None else None,
                "additional_cost": str(additional) if additional is not None else None,
                "within_budget": candidate.within_budget,
                "budget_remaining": str(Decimal(requirement.budget) - Decimal(candidate.cost)),
                "weighted_fraction": candidate.score,
                "credited": candidate.credited}

    def independent_alternative(self, requirement: Requirement, offers: list[Offer],
                               context: SourceContext, reference: Candidate | None
                               ) -> dict[str, Any]:
        """相对于“只按覆盖选出的组合”，满足来源保障的最低成本组合及其溢价。

        即使该组合超预算也返回，让调用方明确看到独立性成本是否仍在预算内。
        """
        eligible, _ = self.eligible(requirement, offers)
        index = self.sourcing.index(requirement, eligible, context)
        return self.independent_from(requirement, self._assured_cheapest(eligible, requirement, index),
                                     reference)
