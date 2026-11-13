"""Data classes: the governance dimension of model access.

Three formerly entangled questions are separated (the key governance
lesson): WHAT may flow into a model (the data class declared by the
processing step), WHERE a model runs (which grounds its clearance in the
model registry), and WHICH model computes (a registry entry). The one hard
rule connecting them is the dominance rule, enforced fail-closed before
every call: **clearance >= demand** - otherwise no call happens, and a
fallback may only ever go toward HIGHER clearance, never lower.

The classes use German product terms (they surface in templates and UI);
the scale is ordered from public to specially protected personal data in
the sense of the Swiss DSG.
"""

from __future__ import annotations

CLASSES = ("public", "internal", "confidential", "sensitive")
"""Ordered ascending: index = strictness rank."""

DEFAULT_CLASS = "internal"


class DataClassError(ValueError):
    """An unknown data class name was used."""


def rank(data_class: str) -> int:
    try:
        return CLASSES.index(data_class)
    except ValueError as exc:
        raise DataClassError(
            f"unknown data class {data_class!r} (known: {', '.join(CLASSES)})"
        ) from exc


def clearance_suffices(clearance: str, demand: str) -> bool:
    """The dominance rule: clearance must be at least the demand."""
    return rank(clearance) >= rank(demand)


def demand(step_config: dict, policies: dict) -> str:
    """The data class a step's model call must satisfy.

    Precedence: explicit ``data_class`` in the step config, else the
    template's privacy policy category (the pre-existing declaration -
    its values are a subset of the class names), else the default.
    """
    declared = step_config.get("data_class")
    if declared is None:
        declared = policies.get("privacy", {}).get("category", DEFAULT_CLASS)
    rank(str(declared))  # validate loudly
    return str(declared)
