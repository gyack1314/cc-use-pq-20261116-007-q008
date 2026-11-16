from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .contracts import Offer, Observation, Requirement, SourceEvidence
from .coverage import observed_for


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def days_between(earlier: str, later: str) -> int:
    return (parse_day(later) - parse_day(earlier)).days


@dataclass(frozen=True)
class SourceContext:
    """一次分析所看到的证据与评估日。冻结后不随后续修订变化。"""

    evidence: tuple[SourceEvidence, ...] = ()
    as_of: str = ""

    def claim_count(self) -> int:
        return sum(len(item.claims) for item in self.evidence)


@dataclass(frozen=True)
class Resolution:
    package: str
    provider: str
    state: str
    collectors: tuple[str, ...]
    strength: str | None
    evidence_keys: tuple[str, ...]
    observed_on: str | None
    valid_until: str | None

    def document(self) -> dict[str, Any]:
        return {"package": self.package, "provider": self.provider, "state": self.state,
                "collectors": list(self.collectors), "strength": self.strength,
                "evidence_keys": list(self.evidence_keys),
                "observed_on": self.observed_on, "valid_until": self.valid_until}


def contributors(entries: list[tuple[str, Observation]]) -> list[str]:
    """只重复他人标识的包不构成独立贡献者；匿名数量无法核实，按贡献计。"""
    owners: dict[str, set[str]] = {}
    for package, observation in entries:
        for identifier in observation.episode_ids:
            owners.setdefault(identifier, set()).add(package)
    result = []
    for package, observation in sorted(entries, key=lambda item: item[0]):
        anonymous = observation.count - len(observation.episode_ids)
        exclusive = any(owner == {package} for owner in
                        (owners[identifier] for identifier in observation.episode_ids))
        if anonymous > 0 or exclusive:
            result.append(package)
    return result


class SourceAnalyzer:
    """把供应商名称与实际上游采集方分开评估。未证实的关系不算独立来源。"""

    STRENGTH_ORDER = {"verified": 2, "reported": 1}

    def resolve(self, offer: Offer, context: SourceContext) -> Resolution:
        claims = [(doc, claim) for doc in context.evidence
                  for claim in doc.claims_for(offer.package)]
        if not claims:
            return Resolution(offer.package, offer.provider, "unconfirmed", (), None, (), None, None)
        windowed = [(doc, claim) for doc, claim in claims
                    if doc.valid_from <= context.as_of <= doc.valid_until]
        if not windowed:
            latest = max(claims, key=lambda item: item[1].observed_on)
            doc, claim = latest
            state = "not_yet_valid" if context.as_of < doc.valid_from else "expired"
            return Resolution(offer.package, offer.provider, state, (), None,
                              (doc.key,), claim.observed_on, doc.valid_until)
        collectors = tuple(sorted({claim.collector for _, claim in windowed}))
        strength = max((claim.strength for _, claim in windowed),
                       key=lambda value: self.STRENGTH_ORDER[value])
        evidence_keys = tuple(sorted({doc.key for doc, _ in windowed}))
        observed_on = min(claim.observed_on for _, claim in windowed)
        valid_until = min(doc.valid_until for doc, _ in windowed)
        if len(collectors) > 1:
            return Resolution(offer.package, offer.provider, "disputed", collectors, strength,
                              evidence_keys, observed_on, valid_until)
        return Resolution(offer.package, offer.provider, "resolved", collectors, strength,
                          evidence_keys, observed_on, valid_until)

    def index(self, requirement: Requirement, offers: list[Offer],
              context: SourceContext) -> "AssuranceIndex":
        """按一次分析预解析证据，候选组合只需对样本贡献与采集队做布尔判定。"""
        return AssuranceIndex(self, requirement, offers, context)

    def assess(self, requirement: Requirement, offers: list[Offer],
               context: SourceContext) -> dict[str, Any]:
        index = self.index(requirement, offers, context)
        selected = {offer.package for offer in offers}
        by_package = {offer.package: offer for offer in offers}
        cells = []
        for target, entries in index.targets:
            cells.append(index.cell(target, entries, selected))
        all_contributors = sorted({package for cell in cells
                                   for package in cell["contributing_packages"]})
        providers = sorted({by_package[package].provider for package in all_contributors})
        collectors = sorted({group["collector"] for cell in cells
                             for group in cell["collector_groups"]})
        used_evidence = sorted({key for package in all_contributors
                                for key in index.resolutions[package].evidence_keys})
        expiring = []
        for key in used_evidence:
            document = next((item for item in context.evidence if item.key == key), None)
            if document is None:
                continue
            remaining = days_between(context.as_of, document.valid_until)
            status = "expired" if remaining < 0 else "expiring" if remaining <= 30 else "current"
            expiring.append({"key": key, "valid_until": document.valid_until,
                             "days_remaining": remaining, "status": status})
        return {"as_of": context.as_of,
                "targets": cells,
                "assurance_met": all(cell["assurance_met"] or not cell["mandatory"]
                                     for cell in cells),
                "distinct_providers": len(providers),
                "distinct_collectors": len(collectors),
                "provider_diversity_misleading": bool(providers) and len(collectors) < len(providers),
                "shared_source_groups": [group for cell in cells
                                         for group in cell["shared_source_groups"]],
                "unconfirmed_packages": sorted({item["package"] for cell in cells
                                                for item in cell["unconfirmed_packages"]}),
                "evidence_validity": expiring,
                "resolutions": [index.resolutions[package].document()
                                for package in all_contributors],
                "claim_count": context.claim_count()}

    def blocks(self, requirement: Requirement, offers: list[Offer],
               context: SourceContext) -> bool:
        return not self.assess(requirement, offers, context)["assurance_met"]


class AssuranceIndex:
    """证据解析结果的复用视图：组合搜索时避免重复解析证据与重建分组。"""

    def __init__(self, analyzer: SourceAnalyzer, requirement: Requirement,
                 offers: list[Offer], context: SourceContext):
        self.resolutions = {offer.package: analyzer.resolve(offer, context) for offer in offers}
        self.targets: list[tuple[Any, list[tuple[str, Observation]]]] = []
        for target in requirement.targets:
            token = target.slice.token()
            entries = [(offer.package, observation) for offer in offers
                       if (observation := observed_for(offer, token)) is not None]
            self.targets.append((target, entries))

    def met(self, packages: set[str]) -> bool:
        for target, entries in self.targets:
            if target.minimum_independent_sources == 0:
                continue
            selected = [(package, observation) for package, observation in entries
                        if package in packages]
            collectors, unconfirmed = self.classify(contributors(selected))
            met = len(collectors) >= target.minimum_independent_sources and not unconfirmed
            if not met and target.mandatory:
                return False
        return True

    def classify(self, packages: list[str]) -> tuple[list[str], list[str]]:
        collectors: set[str] = set()
        unconfirmed = []
        for package in packages:
            resolution = self.resolutions[package]
            if resolution.state == "resolved":
                collectors.add(resolution.collectors[0])
            else:
                unconfirmed.append(package)
        return sorted(collectors), unconfirmed

    def cell(self, target: Any, entries: list[tuple[str, Observation]],
             packages: set[str]) -> dict[str, Any]:
        selected = [(package, observation) for package, observation in entries
                    if package in packages]
        contributing = contributors(selected)
        groups: dict[str, list[str]] = {}
        unconfirmed = []
        for package in contributing:
            resolution = self.resolutions[package]
            if resolution.state == "resolved":
                groups.setdefault(resolution.collectors[0], []).append(package)
            else:
                unconfirmed.append({"package": package, "state": resolution.state,
                                    "valid_until": resolution.valid_until,
                                    "collectors": list(resolution.collectors)})
        collectors = sorted(groups)
        minimum = target.minimum_independent_sources
        met = len(collectors) >= minimum and (minimum == 0 or not unconfirmed)
        if minimum == 0:
            reason = "not_requested"
        elif len(collectors) < minimum and unconfirmed:
            reason = "insufficient_evidenced_collectors_and_unconfirmed_packages"
        elif len(collectors) < minimum:
            reason = "insufficient_independent_collectors"
        elif unconfirmed:
            reason = "unconfirmed_packages"
        else:
            reason = "met"
        return {"slice": target.slice.token(), "minimum_independent_sources": minimum,
                "mandatory": target.mandatory,
                "independent_sources": len(collectors),
                "collector_groups": [{"collector": key, "packages": sorted(groups[key])}
                                     for key in collectors],
                "shared_source_groups": [{"collector": key, "packages": sorted(groups[key])}
                                         for key in collectors if len(groups[key]) > 1],
                "unconfirmed_packages": unconfirmed,
                "assurance_met": met, "reason": reason,
                "contributing_packages": sorted(contributing)}
