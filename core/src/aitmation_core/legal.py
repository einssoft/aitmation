"""Legal space of model access (ADR-0027): where may which data go?

A deliberately small table, keyed by the country of the instance
(:meth:`Runtime.country`, from the company or household profile,
ADR-0026). For each country it names the data protection law that
applies, the regions a remote model may run in for confidential data,
and the class from which a data processing agreement (DPA,
Auftragsverarbeitungsvertrag) with the provider is required.

The rule is applied fail-closed together with the dominance rule (K2)
before every model call: a step's demand is compared against the
deployment's clearance (ADR-0017) AND against this table. Without a
known country only the dominance rule applies - the UI says so.

This is a product rule, not legal advice: the owner remains responsible
for the providers and agreements they choose; the table makes the
consequence of that choice visible and mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass

from .data_classes import rank
from .model_registry import ModelEntry


@dataclass(frozen=True)
class Jurisdiction:
    country: str
    law: str
    allowed_regions: tuple[str, ...]  # remote regions acceptable for confidential data
    dpa_required_from: str  # data class from which a DPA is required for remote models

    def as_dict(self) -> dict:
        return {
            "country": self.country,
            "law": self.law,
            "allowed_regions": list(self.allowed_regions),
            "dpa_required_from": self.dpa_required_from,
        }


JURISDICTIONS: dict[str, Jurisdiction] = {
    # Switzerland: revised FADP (DSG, in force 2023-09-01). Transfers to
    # countries with adequate protection (EU/EEA) are permitted; the US
    # only under the Swiss-U.S. Data Privacy Framework per certified
    # provider - the owner records that as region "US" + dpa = true and
    # decides consciously; for confidential data we stay conservative.
    "CH": Jurisdiction("CH", "DSG (revidiert, seit 2023)", ("CH", "EU"), "confidential"),
    # Germany and Austria: GDPR plus national law (BDSG / DSG).
    "DE": Jurisdiction("DE", "DSGVO / BDSG", ("EU", "CH"), "confidential"),
    "AT": Jurisdiction("AT", "DSGVO / DSG", ("EU", "CH"), "confidential"),
}


def jurisdiction_for(country: str | None) -> Jurisdiction | None:
    return JURISDICTIONS.get(country or "")


def violation(country: str | None, entry: ModelEntry, demand: str) -> str | None:
    """Why this deployment may NOT process data of class ``demand`` for an
    instance in ``country`` - or None when it may. Local models pass
    (the data never leaves the machine); the clearance itself is checked
    by the dominance rule, not here."""
    if entry.local:
        return None
    jurisdiction = jurisdiction_for(country)
    if jurisdiction is None:
        return None
    if rank(demand) < rank(jurisdiction.dpa_required_from):
        return None
    if entry.region not in jurisdiction.allowed_regions:
        return (
            f"data class {demand!r} may not leave {jurisdiction.country} for region"
            f" {entry.region!r} ({jurisdiction.law}); allowed:"
            f" {', '.join(jurisdiction.allowed_regions)} or a local model"
        )
    if not entry.dpa:
        return (
            f"data class {demand!r} needs a data processing agreement with the provider"
            f" of {entry.id!r} ({jurisdiction.law}); none is recorded"
        )
    return None


def permitted_up_to(country: str | None, entry: ModelEntry) -> str:
    """The highest data class this deployment may process for the country -
    the minimum of its clearance and what the legal space allows."""
    from .data_classes import CLASSES

    best = "public"
    for data_class in CLASSES:
        if rank(data_class) > rank(entry.clearance):
            break
        if violation(country, entry, data_class):
            break
        best = data_class
    return best
