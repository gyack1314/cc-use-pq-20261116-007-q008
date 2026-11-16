from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .contracts import Offer, Requirement, Slice
from .coverage import CoverageEngine, effective_count
from .economics import PriceEngine


class SupplyDiagnostics:
    def __init__(self, coverage: CoverageEngine, prices: PriceEngine):
        self.coverage = coverage
        self.prices = prices

    def path_distribution(self, offers: list[Offer]) -> dict[str, Any]:
        grouped: dict[str, list[Offer]] = defaultdict(list)
        for offer in offers:
            grouped[offer.path].append(offer)
        return {path: {"offers": len(items), "records": sum(item.sample_count for item in items),
                       "providers": len({item.provider for item in items}),
                       "hours": str(sum((Decimal(item.hours) for item in items), Decimal(0)))}
                for path, items in sorted(grouped.items())}

    def outcome_distribution(self, offers: list[Offer]) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for offer in offers:
            for observation in offer.observations:
                counts[observation.slice.outcome] += observation.count
        total = sum(counts.values())
        return {"total": total, "by_outcome": dict(sorted(counts.items())),
                "failure_and_intervention": counts["failure"] + counts["intervention"]}

    def provider_exposure(self, requirement: Requirement, offers: list[Offer]) -> list[dict[str, Any]]:
        quotes = self.prices.all_quotes(offers, requirement)
        grouped: dict[str, list] = defaultdict(list)
        for quote in quotes:
            grouped[quote.provider].append(quote)
        results = []
        for provider, entries in sorted(grouped.items()):
            permitted = [entry for entry in entries if entry.permitted]
            results.append({"provider": provider, "offer_count": len(entries),
                            "permitted_count": len(permitted),
                            "permitted_cost": self.prices.total(permitted),
                            "restricted_packages": [entry.package for entry in entries if not entry.permitted]})
        return results

    def sparse_targets(self, requirement: Requirement, offers: list[Offer]) -> list[dict[str, Any]]:
        result = []
        for target in requirement.targets:
            matching = []
            for offer in offers:
                if not self.prices.quote(offer, requirement).permitted:
                    continue
                for observation in offer.observations:
                    if observation.slice == target.slice:
                        matching.append((offer, observation))
            available, duplicates = effective_count([obs for _, obs in matching])
            result.append({"slice": target.slice.token(), "minimum": target.minimum,
                           "available": available, "shortage": max(0, target.minimum - available),
                           "duplicate_count": duplicates,
                           "providers": sorted({offer.provider for offer, _ in matching}),
                           "packages": sorted({offer.package for offer, _ in matching})})
        return result

    def licensing_reasons(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        results: dict[str, list[str]] = defaultdict(list)
        for offer in offers:
            quote = self.prices.quote(offer, requirement)
            if quote.reason:
                for reason in quote.reason.split(","):
                    results[reason].append(offer.package)
        return {reason: sorted(packages) for reason, packages in sorted(results.items())}

    def data_readiness(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        permitted = [offer for offer in offers if self.prices.quote(offer, requirement).permitted]
        coverage = self.coverage.evaluate(requirement, permitted)
        outcomes = self.outcome_distribution(permitted)
        return {"requirement": requirement.key, "eligible_offers": len(permitted),
                "mandatory_possible": coverage.mandatory_complete,
                "target_shortage": coverage.total_shortage,
                "known_duplicates": coverage.total_duplicate,
                "outcomes": outcomes["by_outcome"],
                "sparse_targets": self.sparse_targets(requirement, permitted)}

    def offer_scorecards(self, requirement: Requirement, offers: list[Offer]) -> list[dict[str, Any]]:
        """逐包展示独立贡献，不能据此代替组合决策。"""
        cards = []
        for offer in offers:
            quote = self.prices.quote(offer, requirement)
            coverage = self.coverage.evaluate(requirement, [offer])
            matched = [cell for cell in coverage.cells if cell.available]
            missing = [cell.slice for cell in coverage.cells if cell.shortage and cell.mandatory]
            cards.append({"package": offer.package, "provider": offer.provider,
                          "path": offer.path, "sample_count": offer.sample_count,
                          "target_count": len(matched),
                          "credited": coverage.total_credited,
                          "mandatory_shortage": sum(cell.shortage for cell in coverage.cells if cell.mandatory),
                          "mandatory_gaps": missing,
                          "weighted_fraction": coverage.weighted_fraction,
                          "price": quote.amount, "price_mode": quote.mode,
                          "price_quantity": quote.quantity,
                          "permitted": quote.permitted, "restriction": quote.reason,
                          "cost_per_credited": self.prices.cost_per_credited_sample(
                              quote.amount, coverage.total_credited)})
        return sorted(cards, key=lambda item: item["package"])

    def overlap_network(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        """比较已声明的样本标识，并揭示无法确认的重复。"""
        eligible = [offer for offer in offers if self.prices.quote(offer, requirement).permitted]
        targets = {target.slice.token() for target in requirement.targets}
        edges = []
        for left_index, left in enumerate(eligible):
            for right in eligible[left_index + 1:]:
                raw = self.coverage.pair_overlap(left, right)
                relevant = [row for row in raw["shared_slices"] if row["slice"] in targets]
                if not relevant:
                    continue
                edges.append({"first": left.package, "second": right.package,
                              "known_overlap": sum(row["known_overlap"] for row in relevant),
                              "shared_target_count": len(relevant),
                              "unverified_target_count": sum(not row["identifiers_complete"]
                                                             for row in relevant),
                              "targets": relevant})
        edges.sort(key=lambda item: (item["first"], item["second"]))
        return {"eligible_packages": [offer.package for offer in eligible],
                "edges": edges,
                "known_overlap_total": sum(edge["known_overlap"] for edge in edges),
                "unverified_pairs": sum(edge["unverified_target_count"] > 0 for edge in edges)}

    def target_supply(self, requirement: Requirement, offers: list[Offer]) -> list[dict[str, Any]]:
        """并列展示逐包供给与去重后的总有效供给。"""
        eligible = [offer for offer in offers if self.prices.quote(offer, requirement).permitted]
        sparse = {item["slice"]: item for item in self.sparse_targets(requirement, eligible)}
        rows = []
        for target in requirement.targets:
            token = target.slice.token()
            contributors = []
            for offer in eligible:
                matching = next((obs for obs in offer.observations if obs.slice.token() == token), None)
                if matching is None:
                    continue
                quote = self.prices.quote(offer, requirement)
                contributors.append({"package": offer.package, "provider": offer.provider,
                                     "count": matching.count, "identifiers_known": bool(matching.episode_ids),
                                     "whole_package_price": quote.amount})
            aggregate = sparse[token]
            rows.append({"slice": token, "minimum": target.minimum,
                         "mandatory": target.mandatory, "weight": target.weight,
                         "available": aggregate["available"],
                         "shortage": aggregate["shortage"],
                         "duplicates": aggregate["duplicate_count"],
                         "contributor_count": len(contributors),
                         "contributors": sorted(contributors, key=lambda item: item["package"])})
        return rows

    def procurement_brief(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        readiness = self.data_readiness(requirement, offers)
        cards = self.offer_scorecards(requirement, offers)
        overlaps = self.overlap_network(requirement, offers)
        supply = self.target_supply(requirement, offers)
        return {"requirement": requirement.key, "client": requirement.client,
                "budget": requirement.budget, "offer_count": len(offers),
                "eligible_offer_count": readiness["eligible_offers"],
                "mandatory_possible_from_all_supply": readiness["mandatory_possible"],
                "supply_shortage": readiness["target_shortage"],
                "known_duplicate_records": readiness["known_duplicates"],
                "paths": self.path_distribution(offers),
                "outcomes": self.outcome_distribution(offers),
                "restrictions": self.licensing_reasons(requirement, offers),
                "offer_scorecards": cards, "target_supply": supply,
                "overlaps": overlaps}
