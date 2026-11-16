from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from itertools import combinations
from typing import Any

from .contracts import Observation, Offer, Requirement, Slice, Target


@dataclass(frozen=True)
class CoverageCell:
    slice: str
    requested: int
    available: int
    credited: int
    shortage: int
    mandatory: bool
    weight: str
    packages: tuple[str, ...]
    duplicate_count: int

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageResult:
    cells: tuple[CoverageCell, ...]
    mandatory_complete: bool
    weighted_fraction: str
    total_credited: int
    total_shortage: int
    total_duplicate: int

    def document(self) -> dict[str, Any]:
        return {"cells": [cell.document() for cell in self.cells],
                "mandatory_complete": self.mandatory_complete,
                "weighted_fraction": self.weighted_fraction,
                "total_credited": self.total_credited, "total_shortage": self.total_shortage,
                "total_duplicate": self.total_duplicate}


def observed_for(offer: Offer, slice_key: str) -> Observation | None:
    for observation in offer.observations:
        if observation.slice.token() == slice_key:
            return observation
    return None


def effective_count(items: list[Observation]) -> tuple[int, int]:
    if not items:
        return 0, 0
    known: set[str] = set()
    anonymous = 0
    nominal = 0
    for item in items:
        nominal += item.count
        if item.episode_ids:
            known.update(item.episode_ids)
        else:
            anonymous += item.count
    unique = anonymous + len(known)
    return unique, nominal - unique


class CoverageEngine:
    def evaluate(self, requirement: Requirement, offers: list[Offer]) -> CoverageResult:
        cells = []
        earned = Decimal(0)
        possible = Decimal(0)
        for target in requirement.targets:
            token = target.slice.token()
            matched = [(offer.package, observed_for(offer, token)) for offer in offers]
            matched = [(name, obs) for name, obs in matched if obs is not None]
            available, duplicate = effective_count([obs for _, obs in matched])
            credited = min(available, target.minimum)
            shortage = target.minimum - credited
            weight = Decimal(target.weight)
            earned += weight * Decimal(credited) / Decimal(target.minimum)
            possible += weight
            cells.append(CoverageCell(token, target.minimum, available, credited, shortage,
                                      target.mandatory, target.weight,
                                      tuple(name for name, _ in matched), duplicate))
        complete = all(not cell.mandatory or cell.shortage == 0 for cell in cells)
        fraction = earned / possible if possible else Decimal(0)
        return CoverageResult(tuple(cells), complete, str(fraction),
                              sum(cell.credited for cell in cells),
                              sum(cell.shortage for cell in cells),
                              sum(cell.duplicate_count for cell in cells))

    def offer_matrix(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        rows = []
        for offer in offers:
            item = {"package": offer.package, "path": offer.path, "provider": offer.provider,
                    "slices": {target.slice.token(): (observed_for(offer, target.slice.token()).count
                                if observed_for(offer, target.slice.token()) else 0)
                               for target in requirement.targets}}
            rows.append(item)
        return {"targets": [target.slice.token() for target in requirement.targets], "offers": rows}

    def marginal(self, requirement: Requirement, current: list[Offer], candidate: Offer) -> dict[str, Any]:
        before = self.evaluate(requirement, current)
        after = self.evaluate(requirement, [*current, candidate])
        changes = []
        for old, new in zip(before.cells, after.cells):
            changes.append({"slice": old.slice, "credited_gain": new.credited - old.credited,
                            "duplicate_increase": new.duplicate_count - old.duplicate_count,
                            "remaining_shortage": new.shortage})
        return {"package": candidate.package, "credited_gain": after.total_credited - before.total_credited,
                "duplicate_increase": after.total_duplicate - before.total_duplicate,
                "weighted_fraction_before": before.weighted_fraction,
                "weighted_fraction_after": after.weighted_fraction,
                "changes": changes}

    def pair_overlap(self, first: Offer, second: Offer) -> dict[str, Any]:
        shared = []
        for left in first.observations:
            right = observed_for(second, left.slice.token())
            if right is None:
                continue
            repeated = len(set(left.episode_ids) & set(right.episode_ids))
            shared.append({"slice": left.slice.token(), "first_count": left.count,
                           "second_count": right.count, "known_overlap": repeated,
                           "identifiers_complete": bool(left.episode_ids and right.episode_ids)})
        return {"first": first.package, "second": second.package,
                "shared_slices": shared,
                "known_overlap_total": sum(item["known_overlap"] for item in shared)}

    def gap_report(self, requirement: Requirement, offers: list[Offer]) -> dict[str, Any]:
        result = self.evaluate(requirement, offers)
        gaps = [{"slice": cell.slice, "missing": cell.shortage,
                 "mandatory": cell.mandatory, "providers": list(cell.packages)}
                for cell in result.cells if cell.shortage]
        return {"requirement": requirement.key, "mandatory_complete": result.mandatory_complete,
                "gaps": gaps, "total_shortage": result.total_shortage}

    def attribution(self, requirement: Requirement, offers: list[Offer]) -> list[dict[str, Any]]:
        result = []
        for offer in offers:
            without = [item for item in offers if item.package != offer.package]
            before = self.evaluate(requirement, without)
            after = self.evaluate(requirement, offers)
            result.append({"package": offer.package,
                           "exclusive_credited": after.total_credited - before.total_credited,
                           "mandatory_would_fail": after.mandatory_complete and not before.mandatory_complete})
        return result


def describe_slice(token: str) -> dict[str, str]:
    scenario, stage, outcome, robot = token.split("|", 3)
    return {"scenario": scenario, "stage": stage, "outcome": outcome, "robot": robot}
