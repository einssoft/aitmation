"""DMN decision tables with S-FEEL (ADR-0034 §2).

Profile: one or more ``decision`` elements per file, each with a
``decisionTable``; hit policies ``UNIQUE``, ``FIRST`` and ``COLLECT``;
input entries in S-FEEL unary tests (``-``, comparisons, ranges, lists,
literals, ``not(...)``); output entries as literals or variable
references. Full FEEL (functions, contexts, iteration, boxed
expressions) is deliberately outside the profile - a table is what a
business user can read and what the DMN TCK can check.

The evaluator is pure: :func:`evaluate` takes a decision and an input
context (``{"trigger": …, "steps": …, "records": …}`` at runtime, plain
values in a sidecar test) and returns the output values. A process calls
a decision through a ``businessRuleTask`` (``aimn:decision ref="…"``),
which the runtime executes as the internal action ``decide``; an output
named ``outcome`` picks the outgoing flow.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

HIT_POLICIES = ("UNIQUE", "FIRST", "COLLECT")
_IGNORED_TOP = ("inputData", "knowledgeSource", "extensionElements", "description", "DMNDI")
_DMN_NS_PREFIXES = ("http://www.omg.org/spec/DMN/", "https://www.omg.org/spec/DMN/")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class DmnError(ValueError):
    """The DMN file is outside the profile, invalid, or the evaluation failed."""


@dataclass(frozen=True)
class InputClause:
    id: str
    label: str
    expression: str
    type_ref: str = ""


@dataclass(frozen=True)
class OutputClause:
    id: str
    name: str
    type_ref: str = ""


@dataclass(frozen=True)
class Rule:
    id: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class Decision:
    id: str
    name: str
    hit_policy: str
    inputs: tuple[InputClause, ...]
    outputs: tuple[OutputClause, ...]
    rules: tuple[Rule, ...]
    description: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_dmn(tag: str) -> bool:
    return tag.startswith("{") and any(tag[1:].startswith(p) for p in _DMN_NS_PREFIXES)


def parse_dmn(text: str, source: str = "<string>") -> Decision:
    """Parse one DMN file holding exactly one decision table."""
    decisions = parse_dmn_all(text, source)
    if len(decisions) != 1:
        raise DmnError(f"{source}: exactly one decision per file, found {len(decisions)}")
    return next(iter(decisions.values()))


def parse_dmn_all(text: str, source: str = "<string>") -> dict[str, Decision]:
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise DmnError(f"{source}: DOCTYPE/ENTITY declarations are not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DmnError(f"{source}: invalid XML: {exc}") from exc
    if not _is_dmn(root.tag) or _local(root.tag) != "definitions":
        raise DmnError(f"{source}: root element must be dmn:definitions")
    out: dict[str, Decision] = {}
    for child in root:
        local = _local(child.tag)
        if local in ("decision",):
            decision = _parse_decision(child, source)
            if decision.id in out:
                raise DmnError(f"{source}: duplicate decision id {decision.id!r}")
            out[decision.id] = decision
        elif local in _IGNORED_TOP or local.endswith("DMNDI"):
            continue
        elif local in ("businessKnowledgeModel", "decisionService", "itemDefinition"):
            raise DmnError(f"{source}: dmn:{local} is not in the aitmation profile")
        else:
            raise DmnError(f"{source}: dmn:{local} is not in the aitmation profile")
    if not out:
        raise DmnError(f"{source}: no decision found")
    return out


def _parse_decision(element: ET.Element, source: str) -> Decision:
    decision_id = element.get("id", "")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", decision_id):
        raise DmnError(f"{source}: decision id {decision_id!r} must be a lowercase identifier")
    table = None
    description = ""
    for child in element:
        local = _local(child.tag)
        if local == "decisionTable":
            table = child
        elif local == "description":
            description = (child.text or "").strip()
        elif local in ("informationRequirement", "extensionElements", "variable"):
            continue
        elif local in ("literalExpression", "context", "invocation", "list", "relation"):
            raise DmnError(
                f"{source}: decision {decision_id!r} uses {local} - only decision tables are in"
                " the profile"
            )
    if table is None:
        raise DmnError(f"{source}: decision {decision_id!r} has no decisionTable")
    hit_policy = table.get("hitPolicy", "UNIQUE").upper()
    if hit_policy not in HIT_POLICIES:
        raise DmnError(
            f"{source}: decision {decision_id!r}: hit policy {hit_policy!r} is not in the"
            f" profile {list(HIT_POLICIES)}"
        )
    if table.get("aggregation"):
        raise DmnError(f"{source}: decision {decision_id!r}: COLLECT aggregation is not supported")
    inputs: list[InputClause] = []
    outputs: list[OutputClause] = []
    rules: list[Rule] = []
    for child in table:
        local = _local(child.tag)
        if local == "input":
            expr_el = next((c for c in child if _local(c.tag) == "inputExpression"), None)
            text_el = (
                next((c for c in expr_el if _local(c.tag) == "text"), None)
                if expr_el is not None
                else None
            )
            expression = (text_el.text or "").strip() if text_el is not None else ""
            if not _NAME.match(expression):
                raise DmnError(
                    f"{source}: decision {decision_id!r}: input expression {expression!r} must be"
                    " a variable name (optionally dotted)"
                )
            inputs.append(
                InputClause(
                    id=child.get("id", f"input{len(inputs) + 1}"),
                    label=child.get("label", expression),
                    expression=expression,
                    type_ref=(expr_el.get("typeRef", "") if expr_el is not None else ""),
                )
            )
        elif local == "output":
            name = child.get("name", "")
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                raise DmnError(
                    f"{source}: decision {decision_id!r}: output name {name!r} must be a lowercase"
                    " identifier"
                )
            outputs.append(
                OutputClause(
                    id=child.get("id", f"output{len(outputs) + 1}"),
                    name=name,
                    type_ref=child.get("typeRef", ""),
                )
            )
        elif local == "rule":
            in_entries = [_entry_text(c) for c in child if _local(c.tag) == "inputEntry"]
            out_entries = [_entry_text(c) for c in child if _local(c.tag) == "outputEntry"]
            desc = next(
                ((c.text or "").strip() for c in child if _local(c.tag) == "description"), ""
            )
            rules.append(
                Rule(
                    id=child.get("id", f"rule{len(rules) + 1}"),
                    inputs=tuple(in_entries),
                    outputs=tuple(out_entries),
                    description=desc,
                )
            )
        elif local == "annotation":
            continue
        else:
            raise DmnError(f"{source}: decision {decision_id!r}: unexpected element {local}")
    if not outputs:
        raise DmnError(f"{source}: decision {decision_id!r} has no outputs")
    for rule in rules:
        if len(rule.inputs) != len(inputs) or len(rule.outputs) != len(outputs):
            raise DmnError(
                f"{source}: decision {decision_id!r}: rule {rule.id!r} has"
                f" {len(rule.inputs)} input / {len(rule.outputs)} output entries,"
                f" table has {len(inputs)} / {len(outputs)}"
            )
        for entry in rule.inputs:
            _compile_unary(entry, f"{source}: rule {rule.id!r}")  # syntax check at load
        for entry in rule.outputs:
            _compile_output(entry, f"{source}: rule {rule.id!r}")
    return Decision(
        id=decision_id,
        name=element.get("name", decision_id),
        hit_policy=hit_policy,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        rules=tuple(rules),
        description=description,
        source=source,
    )


def _entry_text(entry: ET.Element) -> str:
    text_el = next((c for c in entry if _local(c.tag) == "text"), None)
    return (text_el.text or "").strip() if text_el is not None else ""


# -- S-FEEL ----------------------------------------------------------------------

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_STRING = re.compile(r'^"((?:[^"\\]|\\.)*)"$')
_DATE = re.compile(r'^date\("(\d{4}-\d{2}-\d{2})"\)$')
_RANGE = re.compile(r"^([\[(\]])\s*(.+?)\s*\.\.\s*(.+?)\s*([\])\[])$")
_COMPARISON = re.compile(r"^(<=|>=|<|>)\s*(.+)$")


def _literal(text: str, where: str):
    text = text.strip()
    if _NUMBER.match(text):
        return float(text) if "." in text else int(text)
    match = _STRING.match(text)
    if match:
        return match.group(1).replace('\\"', '"')
    if text in ("true", "false"):
        return text == "true"
    if text == "null":
        return None
    match = _DATE.match(text)
    if match:
        return date.fromisoformat(match.group(1))
    raise DmnError(f"{where}: {text!r} is not an S-FEEL literal")


def _is_literal(text: str) -> bool:
    try:
        _literal(text, "")
        return True
    except DmnError:
        return False


def _split_list(text: str) -> list[str]:
    """Split on top-level commas (not inside strings, parentheses or ranges)."""
    parts: list[str] = []
    depth = 0
    quote = False
    current = ""
    for ch in text:
        if ch == '"':
            quote = not quote
        if not quote:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(current.strip())
                current = ""
                continue
        current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def _compile_unary(entry: str, where: str):
    """A unary test as a predicate over the input value."""
    text = entry.strip()
    if text in ("", "-"):
        return lambda value: True
    parts = _split_list(text)
    if len(parts) > 1:
        tests = [_compile_unary(part, where) for part in parts]
        return lambda value: any(test(value) for test in tests)
    if text.startswith("not(") and text.endswith(")"):
        inner = _compile_unary(text[4:-1], where)
        return lambda value: not inner(value)
    match = _RANGE.match(text)
    if match:
        open_br, low_t, high_t, close_br = match.groups()
        low, high = _literal(low_t, where), _literal(high_t, where)
        low_incl, high_incl = open_br == "[", close_br == "]"

        def in_range(value):
            if value is None:
                return False
            value = _coerce(value, low)
            try:
                if low_incl and not value >= low:
                    return False
                if not low_incl and not value > low:
                    return False
                if high_incl and not value <= high:
                    return False
                if not high_incl and not value < high:
                    return False
            except TypeError:
                return False
            return True

        return in_range
    match = _COMPARISON.match(text)
    if match:
        op, rhs_t = match.groups()
        rhs = _literal(rhs_t, where)
        ops = {
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
        }

        def compare(value):
            if value is None:
                return False
            try:
                return bool(ops[op](_coerce(value, rhs), rhs))
            except TypeError:
                return False

        return compare
    literal = _literal(text, where)
    return lambda value: _same(_coerce(value, literal), literal)


def _same(value, literal) -> bool:
    """Literal equality with booleans distinct from numbers: ``true`` is
    not ``1`` and ``0`` is not ``false`` (FEEL types, not Python's)."""
    if isinstance(value, bool) != isinstance(literal, bool):
        return False
    return value == literal


def _typed(value, type_ref: str):
    """Coerce an input value by the clause's ``typeRef`` (S-FEEL types),
    so a numeric string from a form compares as a number and a stray
    unit ("12 CHF") is a visible miss, not a silent one."""
    if value is None or not type_ref:
        return value
    kind = type_ref.split(":")[-1].lower()
    try:
        if kind in ("number", "integer", "decimal", "double"):
            if isinstance(value, bool):
                return value
            if isinstance(value, int | float):
                return value
            text = str(value).strip().replace("'", "")
            if kind == "integer":
                return int(text)
            return float(text) if "." in text or "e" in text.lower() else int(text)
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            return {"true": True, "false": False}.get(text, value)
        if kind == "string":
            return str(value)
        if kind == "date":
            return _coerce(value, date.today())
    except (TypeError, ValueError):
        return value
    return value


def _coerce(value, like):
    """Compare like with like: numbers in strings, dates in ISO strings."""
    if isinstance(like, int | float) and isinstance(value, str) and _NUMBER.match(value.strip()):
        return float(value) if "." in value else int(value)
    if isinstance(like, date) and isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).date()
        except ValueError:
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return value
    if isinstance(like, date) and isinstance(value, datetime):
        return value.date()
    return value


def _compile_output(entry: str, where: str):
    text = entry.strip()
    if text in ("", "-"):
        return lambda ctx: None
    if _is_literal(text):
        value = _literal(text, where)
        return lambda ctx: value
    if _NAME.match(text):
        return lambda ctx: lookup(ctx, text)
    raise DmnError(f"{where}: output entry {text!r} must be a literal or a variable name")


def lookup(context: dict, path: str):
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def evaluate(decision: Decision, context: dict) -> dict:
    """Run the table. UNIQUE: at most one rule may match (none -> all
    outputs None); FIRST: the first match wins; COLLECT: lists."""
    values = [
        _typed(lookup(context, clause.expression), clause.type_ref) for clause in decision.inputs
    ]
    matches: list[Rule] = []
    for rule in decision.rules:
        if all(
            _compile_unary(entry, f"{decision.source}: rule {rule.id!r}")(value)
            for entry, value in zip(rule.inputs, values, strict=True)
        ):
            matches.append(rule)
            if decision.hit_policy == "FIRST":
                break
    if decision.hit_policy == "UNIQUE" and len(matches) > 1:
        raise DmnError(
            f"{decision.id}: hit policy UNIQUE but rules {[r.id for r in matches]} all match"
        )
    names = [clause.name for clause in decision.outputs]
    if decision.hit_policy == "COLLECT":
        out: dict = {name: [] for name in names}
        for rule in matches:
            for name, entry in zip(names, rule.outputs, strict=True):
                out[name].append(_compile_output(entry, decision.source)(context))
        return out
    if not matches:
        return dict.fromkeys(names)
    rule = matches[0]
    return {
        name: _compile_output(entry, decision.source)(context)
        for name, entry in zip(names, rule.outputs, strict=True)
    }


def matched_rules(decision: Decision, context: dict) -> list[str]:
    """Rule ids that match - for the trace in the audit entry."""
    values = [
        _typed(lookup(context, clause.expression), clause.type_ref) for clause in decision.inputs
    ]
    return [
        rule.id
        for rule in decision.rules
        if all(
            _compile_unary(entry, decision.source)(value)
            for entry, value in zip(rule.inputs, values, strict=True)
        )
    ]
