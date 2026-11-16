from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

from .contracts import Offer, Requirement, PortfolioError


CENT = Decimal("0.01")


def money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_EVEN), "f")


@dataclass(frozen=True)
class Quote:
    package: str
    provider: str
    mode: str
    quantity: str
    unit_price: str
    amount: str
    permitted: bool
    reason: str | None

    def document(self) -> dict[str, Any]:
        return asdict(self)


class PriceEngine:
    def quote(self, offer: Offer, requirement: Requirement) -> Quote:
        reasons = []
        if not offer.permits(requirement.client):
            reasons.append("license_does_not_permit_client")
        if offer.path not in requirement.allowed_paths:
            reasons.append("collection_path_not_permitted")
        if offer.price_mode == "per_record":
            quantity = Decimal(offer.sample_count)
        elif offer.price_mode == "per_hour":
            quantity = Decimal(offer.hours)
        else:
            quantity = Decimal(1)
        amount = quantity * Decimal(offer.unit_price)
        return Quote(offer.package, offer.provider, offer.price_mode, str(quantity),
                     offer.unit_price, money(amount), not reasons, ",".join(reasons) or None)

    def all_quotes(self, offers: list[Offer], requirement: Requirement) -> list[Quote]:
        return [self.quote(offer, requirement) for offer in offers]

    def total(self, quotes: list[Quote]) -> str:
        return money(sum((Decimal(item.amount) for item in quotes), Decimal(0)))

    def within_budget(self, quotes: list[Quote], requirement: Requirement) -> bool:
        return Decimal(self.total(quotes)) <= Decimal(requirement.budget)

    def remaining(self, quotes: list[Quote], requirement: Requirement) -> str:
        return money(Decimal(requirement.budget) - Decimal(self.total(quotes)))

    def compare_modes(self, offer: Offer) -> dict[str, Any]:
        quantity = offer.sample_count if offer.price_mode == "per_record" else (
            offer.hours if offer.price_mode == "per_hour" else "1")
        total = Decimal(str(quantity)) * Decimal(offer.unit_price)
        return {"package": offer.package, "mode": offer.price_mode,
                "quantity": str(quantity), "unit_price": offer.unit_price,
                "amount": money(total), "sample_count": offer.sample_count,
                "hours": offer.hours}

    def cost_per_credited_sample(self, amount: str, credited: int) -> str | None:
        if credited <= 0:
            return None
        return money(Decimal(amount) / Decimal(credited))

    def provider_breakdown(self, quotes: list[Quote]) -> list[dict[str, Any]]:
        amounts: dict[str, Decimal] = {}
        packages: dict[str, list[str]] = {}
        for quote in quotes:
            amounts[quote.provider] = amounts.get(quote.provider, Decimal(0)) + Decimal(quote.amount)
            packages.setdefault(quote.provider, []).append(quote.package)
        return [{"provider": provider, "amount": money(amounts[provider]),
                 "packages": sorted(packages[provider])} for provider in sorted(amounts)]

    def budget_pressure(self, quotes: list[Quote], requirement: Requirement) -> dict[str, Any]:
        budget = Decimal(requirement.budget)
        total = Decimal(self.total(quotes))
        ratio = total / budget if budget else (Decimal(0) if total == 0 else None)
        return {"budget": money(budget), "cost": money(total),
                "remaining": money(budget - total),
                "utilization": str(ratio) if ratio is not None else None,
                "within_budget": total <= budget}
