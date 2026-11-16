"""Extension points: a solution docks a process of its own onto a point of
another solution's process. The pre-filter asks no model and never
decides; one model call decides in context, only when a topic comes into
question."""

# ruff: noqa: E501 - XML and TOML fixtures read better unwrapped
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aitmation_core import extensions, providers
from aitmation_core.apps import load_apps, validate_app
from aitmation_core.bpmn_graph import apply_graph, read_graph
from aitmation_core.config import Settings
from aitmation_core.model import ModelResponse
from aitmation_core.runtime import Runtime
from aitmation_core.template import load_templates

CORE_TEMPLATES = Path(__file__).resolve().parents[2] / "core" / "templates"
HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
"""
ROLE = (
    '[model_roles.{name}]\nstructured_output = true\nvision = false\nmax_data_class = "internal"\n'
)

# The host: a desk that answers questions itself unless a module takes over.
DESK = """
  <bpmn:startEvent id="start"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:callActivity id="topic"><bpmn:extensionElements>
    <aimn:extensionPoint name="topic" mode="takeover" text="trigger.q" last="trigger.topic"
      history="trigger.history" modelRole="router" returns="answer"/>
  </bpmn:extensionElements></bpmn:callActivity>
  <bpmn:task id="answer"><bpmn:extensionElements><aimn:aiTask ref="answer"/></bpmn:extensionElements></bpmn:task>
  <bpmn:endEvent id="by_module"/><bpmn:endEvent id="by_desk"/>
  <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="topic"/>
  <bpmn:sequenceFlow id="f2" name="done" sourceRef="topic" targetRef="by_module"/>
  <bpmn:sequenceFlow id="f3" name="none" sourceRef="topic" targetRef="answer"/>
  <bpmn:sequenceFlow id="f4" name="done" sourceRef="answer" targetRef="by_desk"/>
  <bpmn:sequenceFlow id="f5" name="failed" sourceRef="answer" targetRef="by_desk"/>
"""
OFFER = """
  <bpmn:startEvent id="start"><bpmn:extensionElements><aimn:trigger source="chain"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:task id="offer"><bpmn:extensionElements><aimn:aiTask ref="offer"/></bpmn:extensionElements></bpmn:task>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="start" targetRef="offer"/>
  <bpmn:sequenceFlow id="f2" name="done" sourceRef="offer" targetRef="finished"/>
  <bpmn:sequenceFlow id="f3" name="failed" sourceRef="offer" targetRef="finished"/>
"""
BOOKING = """extends = "ask"
point = "topic"
process = "booking-offer"
priority = 60

[returns]
answer = "offer.text"

[topic]
title = { de = "Termin buchen", en = "Book an appointment" }
describe = { de = "Fragen nach freien Terminen, Buchen und Verschieben.", en = "Free slots, booking, rescheduling." }
examples = { de = ["Wann ist der nächste Termin frei?"], en = ["When is the next free slot?"] }
keywords = { de = ["termin", "buch"], en = ["appointment", "book", "slot"] }
"""


def _write(
    root: Path, sid: str, process_id: str, body: str, briefing: str, role: str, deps: str = ""
) -> Path:
    (root / "processes").mkdir(parents=True)
    (root / "ai").mkdir()
    (root / "solution.toml").write_text(
        f'format = 2\nid = "{sid}"\nversion = "0.1.0"\nname = "{sid}"\n'
        + (f"[dependencies]\n{deps}\n" if deps else "")
        + ROLE.format(name=role)
        + (ROLE.format(name="router") if sid == "desk" else ""),
        encoding="utf-8",
    )
    (root / "processes" / f"{process_id}.bpmn").write_text(
        HEAD
        + f'<bpmn:process id="{process_id}" isExecutable="true">{body}</bpmn:process></bpmn:definitions>\n',
        encoding="utf-8",
    )
    (root / "ai" / f"{briefing}.aimn.toml").write_text(
        f'id = "{briefing}"\nmodel_role = "{role}"\ninstructions = "Answer the visitor."\n[output]\ntext = "s"\n',
        encoding="utf-8",
    )
    return root


def _apps(tmp_path: Path, booking: str | None = BOOKING) -> Path:
    apps = tmp_path / "apps"
    if apps.exists():
        return apps
    _write(apps / "desk", "desk", "ask", DESK, "answer", "writer")
    if booking is not None:
        module = _write(
            apps / "booking", "booking", "booking-offer", OFFER, "offer", "writer", 'desk = "^0.1"'
        )
        (module / "extensions").mkdir()
        (module / "extensions" / "booking.toml").write_text(booking, encoding="utf-8")
    return apps


class Model:
    """Answers the router with ``topic`` and every other task with a text."""

    clearance = "sensitive"
    local = True
    entries = ()

    def __init__(self, topic: str = "booking", confidence: float = 0.9) -> None:
        self.topic, self.confidence = topic, confidence
        self.routed: list[dict] = []
        self.tasks: list[str] = []

    def complete_structured(self, task, payload, schema_hint=None):
        self.tasks.append(task)
        if "Topics:" in task:
            self.routed.append(payload)
            data = {"topic": self.topic, "confidence": self.confidence}
        else:
            data = {"text": "Free on Thursday at 10:00."}
        return ModelResponse(
            data=data, confidence=0.9, model_ref="fake@1", tokens_in=1, tokens_out=1
        )


@pytest.fixture()
def world(tmp_path, monkeypatch):
    def build(model: Model | None = None, booking: str | None = BOOKING) -> tuple[Runtime, Model]:
        model = model or Model()
        data = tmp_path / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "models.toml").write_text(
            '[house]\nmodel = "llama-house"\nprovider = "ollama"\nclearance = "sensitive"\n'
            'role = "cheap"\nregion = "local"\nlocal = true\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(providers, "client_for", lambda entry, settings: model)
        runtime = Runtime(
            Settings(data_dir=data, templates_dir=CORE_TEMPLATES, apps_dir=_apps(tmp_path, booking))
        )
        return runtime, model

    return build


def _ask(runtime: Runtime, q: str, **more) -> tuple[str, dict, dict]:
    case = runtime.engine.start_case("ask", {"source": "manual", "q": q, **more})
    for _ in range(6):
        runtime.tick()
    row = runtime.conn.execute(
        "SELECT status, outcome, result FROM steps WHERE case_id = ? AND step_id = 'topic'", (case,)
    ).fetchone()
    steps = {
        r["step_id"]: r["outcome"]
        for r in runtime.conn.execute(
            "SELECT step_id, outcome FROM steps WHERE case_id = ?", (case,)
        )
    }
    return row["outcome"], json.loads(row["result"]), steps


def test_the_prefilter_finds_word_beginnings_and_never_decides():
    booking = extensions.parse_extension(BOOKING, "extensions/booking.toml", "booking")
    hit = extensions.candidates([booking], "Kann ich für morgen etwas BUCHEN?")
    assert [(c.extension.id, c.reason) for c in hit] == [("booking", "keyword:buch")]
    assert (
        extensions.candidates([booking], "Do you have a free slot on Friday?")[0].reason
        == "keyword:slot"
    )
    assert extensions.candidates([booking], "Wie sind Ihre Öffnungszeiten?") == []
    assert extensions.candidates([booking], "Und am Donnerstag?") == []
    assert (
        extensions.candidates([booking], "Und am Donnerstag?", last="booking")[0].reason
        == "conversation"
    )
    assert (
        extensions.candidates([booking], "Das Sachbuch ist vergriffen") == []
    )  # not the beginning of a word
    assert extensions.fold("Réservation für Übermorgen") == "reservation fuer uebermorgen"
    task = extensions.router_task(hit, "de")
    assert (
        "- booking: Termin buchen." in task
        and "Wann ist der nächste Termin frei?" in task
        and '"none"' in task
    )


def test_declarations_are_checked(tmp_path):
    parse = extensions.parse_extension
    with pytest.raises(extensions.ExtensionError, match="needs keywords"):
        parse(BOOKING.split("keywords")[0], "x", "booking")
    with pytest.raises(extensions.ExtensionError, match="shorter than three"):
        parse(BOOKING.replace('"termin", "buch"', '"zu"'), "x", "booking")
    with pytest.raises(extensions.ExtensionError, match="unknown key"):
        parse(BOOKING.replace("priority = 60", "code = 1"), "x", "booking")
    core = load_templates(CORE_TEMPLATES)
    apps = load_apps(_apps(tmp_path), core_definitions=core)
    assert apps["booking"].extensions["booking"].returns == {"answer": "offer.text"}
    assert apps["desk"].templates["ask"].extension_points()["topic"].config["returns"] == ["answer"]
    # alone, the host is not in sight: a warning; installed next to it, errors are errors
    alone = validate_app(tmp_path / "apps" / "booking", core_definitions=core)
    assert alone.ok and any("extends process 'ask' not in sight" in str(w) for w in alone.warnings)
    for change, message in (
        (('point = "topic"', 'point = "elsewhere"'), "has no extension point 'elsewhere'"),
        (('answer = "offer.text"', 'other = "offer.text"'), "the point asks for answer"),
        (("offer.text", "missing.text"), "reads step 'missing'"),
        (('process = "booking-offer"', 'process = "ask"'), "is not a process of this solution"),
    ):
        other = tmp_path / change[1].replace('"', "").replace(" ", "").replace("=", "")
        with pytest.raises(Exception, match=message):
            load_apps(_apps(other, BOOKING.replace(*change)), core_definitions=core)


def test_a_point_without_its_outcomes_or_with_a_called_process_is_refused(tmp_path):
    core = load_templates(CORE_TEMPLATES)
    apps = tmp_path / "apps"
    _write(
        apps / "desk",
        "desk",
        "ask",
        DESK.replace('name="none" sourceRef="topic"', 'name="skip" sourceRef="topic"'),
        "answer",
        "writer",
    )
    assert any(
        "needs the outcome(s) none" in str(e)
        for e in validate_app(apps / "desk", core_definitions=core).errors
    )
    other = tmp_path / "b"
    _write(
        other / "desk",
        "desk",
        "ask",
        DESK.replace(
            '<bpmn:callActivity id="topic">', '<bpmn:callActivity id="topic" calledElement="x">'
        ),
        "answer",
        "writer",
    )
    assert any(
        "takes no calledElement" in str(e)
        for e in validate_app(other / "desk", core_definitions=core).errors
    )
    third = tmp_path / "c"
    _write(
        third / "desk",
        "desk",
        "ask",
        DESK.replace('mode="takeover"', 'mode="sideways"'),
        "answer",
        "writer",
    )
    assert any(
        "mode is one of" in str(e)
        for e in validate_app(third / "desk", core_definitions=core).errors
    )


def test_no_candidate_no_model_call_for_the_decision(world):
    runtime, model = world()
    outcome, result, steps = _ask(runtime, "Wie sind Ihre Öffnungszeiten?")
    assert outcome == "none" and steps["answer"] == "done"
    assert result["extension"] == {
        "point": "topic",
        "docked": ["booking"],
        "candidates": {},
        "chosen": None,
        "stage": "prefilter",
    }
    assert model.routed == [] and len(model.tasks) == 1  # only the desk's own answer
    runtime.close()


def test_a_candidate_is_decided_by_the_model_and_the_module_takes_over(world):
    runtime, model = world()
    outcome, result, steps = _ask(
        runtime, "Wann ist der nächste Termin frei?", history=["Hallo"], lang="de"
    )
    assert outcome == "done" and "answer" not in steps  # the desk's own answer never ran
    assert result["answer"] == "Free on Thursday at 10:00."
    assert result["extension"]["chosen"] == "booking" and result["extension"]["app"] == "booking"
    assert result["extension"]["candidates"] == {"booking": "keyword:termin"}
    assert result["extension"]["router"] == {"topic": "booking", "confidence": 0.9}
    assert model.routed[0]["text"] == "Wann ist der nächste Termin frei?" and model.routed[0][
        "history"
    ] == ["Hallo"]
    audit = [
        json.loads(r["details"])
        for r in runtime.conn.execute(
            "SELECT details FROM audit_log WHERE event = 'subprocess_called'"
        )
    ]
    assert audit[0]["process"] == "booking-offer"
    assert "Termin" not in json.dumps(audit)  # names only, no content
    # a conversation kept as one text reaches the decision with its recent end
    _ask(runtime, "Termin frei?", history="x" * 2000 + " Besucher: Hallo")
    assert model.routed[-1]["history"].endswith("Besucher: Hallo") and len(model.routed[-1]["history"]) == 1500
    runtime.close()


def test_a_keyword_alone_does_not_trigger_the_module(world):
    """ "I missed my appointment, do I get a refund?" has the keyword but
    asks for something else: the model says none, the desk answers."""
    runtime, model = world(Model(topic="none"))
    outcome, result, steps = _ask(
        runtime, "Ich habe meinen Termin verpasst, bekomme ich Geld zurück?"
    )
    assert outcome == "none" and steps["answer"] == "done"
    assert result["extension"]["stage"] == "router" and result["extension"]["chosen"] is None
    assert result["extension"]["router"]["topic"] == "none" and len(model.routed) == 1
    runtime.close()


def test_an_unsure_or_invented_answer_of_the_model_is_no_takeover(world):
    runtime, _ = world(Model(confidence=0.3))
    assert _ask(runtime, "Termin?")[0] == "none"
    runtime.close()
    runtime, _ = world(Model(topic="refunds"))
    assert _ask(runtime, "Termin?")[0] == "none"
    runtime.close()


def test_a_follow_up_without_keyword_is_a_candidate_through_the_conversation(world):
    runtime, model = world()
    assert _ask(runtime, "Und am Donnerstag?")[0] == "none" and model.routed == []
    outcome, result, _ = _ask(runtime, "Und am Donnerstag?", topic="booking")
    assert outcome == "done" and result["extension"]["candidates"] == {"booking": "conversation"}
    runtime.close()


def test_without_modules_the_process_goes_its_own_way(world):
    runtime, model = world(booking=None)
    outcome, result, steps = _ask(runtime, "Wann ist der nächste Termin frei?")
    assert outcome == "none" and result["extension"]["docked"] == [] and steps["answer"] == "done"
    assert model.routed == []
    runtime.close()


def test_a_failing_decision_never_fails_the_process(world):
    class Broken(Model):
        def complete_structured(self, task, payload, schema_hint=None):
            if "Topics:" in task:
                from aitmation_core.model import ModelError

                raise ModelError("HTTP 503")
            return super().complete_structured(task, payload, schema_hint)

    runtime, _ = world(Broken())
    outcome, result, steps = _ask(runtime, "Termin frei?")
    assert outcome == "none" and steps["answer"] == "done"
    assert result["extension"]["note"] == "the decision could not be made"
    runtime.close()


def test_the_graph_view_keeps_the_point(tmp_path):
    path = _apps(tmp_path) / "desk" / "processes" / "ask.bpmn"
    text = path.read_text(encoding="utf-8")
    graph = read_graph(text)
    node = next(n for n in graph["nodes"] if n["id"] == "topic")
    assert node["kind"] == "call"
    saved = apply_graph(text, graph)
    assert 'name="topic"' in saved and "extensionPoint" in saved and "calledElement" not in saved
    path.write_text(saved, encoding="utf-8")
    assert validate_app(
        tmp_path / "apps" / "desk", core_definitions=load_templates(CORE_TEMPLATES)
    ).ok
