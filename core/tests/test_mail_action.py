"""Outbound mail as a process action (ADR-0048): bound like a tool, only
after a gate, attachments only from the case's own files, recipients
within the operator's allowlist, stubbed under the test configuration."""

import email.policy
import json
from email import message_from_bytes
from pathlib import Path

import pytest

from aitmation_core import mail as outbound
from aitmation_core.apps import validate_app
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"

SOLUTION_TOML = """
format = 2
id = "mail-demo"
version = "0.1.0"
name = "Antwort per Mail"
license = "proprietary"
[requires]
core = "0.0.1"
data_class = "internal"
"""

PROCESS = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:aimn="https://aitmation.com/schema/aimn/1"
  xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  id="defs_reply" targetNamespace="https://aitmation.com/solutions">
  <bpmn:process id="reply" name="reply" isExecutable="true" aimn:version="1">
    <bpmn:extensionElements><aimn:meta><aimn:title lang="de">Antworten</aimn:title></aimn:meta>
    </bpmn:extensionElements>
    <bpmn:startEvent id="start" name="start"><bpmn:extensionElements>
      <aimn:trigger source="manual" /></bpmn:extensionElements></bpmn:startEvent>
    <bpmn:userTask id="approve" name="approve"><bpmn:extensionElements>
      <aimn:gate approving="approved" /></bpmn:extensionElements></bpmn:userTask>
    <bpmn:serviceTask id="send" name="send"><bpmn:extensionElements><aimn:toolCall action="mail">
      <aimn:property name="inputs">
        <aimn:property name="to" value="trigger.sender" />
        <aimn:property name="subject" value="trigger.subject" />
        <aimn:property name="body" value="trigger.answer" />
        <aimn:property name="in_reply_to" value="trigger.message_id" />
        <aimn:property name="attachments" value="trigger.attach" />
      </aimn:property>
    </aimn:toolCall></bpmn:extensionElements></bpmn:serviceTask>
    <bpmn:endEvent id="finished" /><bpmn:endEvent id="stopped" />
    <bpmn:sequenceFlow id="f1" name="fired" sourceRef="start" targetRef="approve" />
    <bpmn:sequenceFlow id="f2" name="approved" sourceRef="approve" targetRef="send" />
    <bpmn:sequenceFlow id="f3" name="rejected" sourceRef="approve" targetRef="stopped" />
    <bpmn:sequenceFlow id="f4" name="done" sourceRef="send" targetRef="finished" />
    <bpmn:sequenceFlow id="f5" name="failed" sourceRef="send" targetRef="stopped" />
  </bpmn:process>
</bpmn:definitions>
"""


def _solution(tmp_path: Path, process: str = PROCESS) -> Path:
    root = tmp_path / "apps" / "mail-demo"
    (root / "processes").mkdir(parents=True)
    (root / "solution.toml").write_text(SOLUTION_TOML, encoding="utf-8")
    (root / "processes" / "reply.bpmn").write_text(process, encoding="utf-8")
    return root


def _runtime(tmp_path: Path, configuration: str = "standard", **extra) -> Runtime:
    root = _solution(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=root.parent,
        configuration=configuration,
        smtp_host="smtp.example",
        smtp_from="aitmation@example.ch",
        **extra,
    )
    return Runtime(settings)


def _run_to_mail(runtime: Runtime, payload: dict) -> tuple[str, dict]:
    case_id = runtime.engine.start_case("reply", {"source": "manual", **payload})
    runtime.tick()
    gate = next(g for g in runtime.open_gates() if g["case_id"] == case_id)
    runtime.engine.decide_gate(case_id, gate["step_id"], "approved", actor_id="alex")
    runtime.tick()
    row = runtime.conn.execute(
        "SELECT outcome, result FROM steps WHERE case_id = ? AND step_id = 'send'", (case_id,)
    ).fetchone()
    return row["outcome"], json.loads(row["result"])


def test_validator_knows_the_mail_action(tmp_path):
    assert validate_app(_solution(tmp_path)).ok
    report = validate_app(
        _solution(
            tmp_path / "b",
            PROCESS.replace(
                'name="body" value="trigger.answer"', 'name="text" value="trigger.answer"'
            ),
        )
    )
    assert any("no input 'text'" in str(e) for e in report.errors)
    assert any("leaves ['body'] unbound" in str(e) for e in report.errors)
    no_error_path = PROCESS.replace(
        '<bpmn:sequenceFlow id="f5" name="failed" sourceRef="send" targetRef="stopped" />', ""
    )
    report = validate_app(_solution(tmp_path / "c", no_error_path))
    assert any("'failed' outcome" in str(e) for e in report.errors)
    # Reachable without a gate: refused like every outside effect (ADR-0007).
    ungated = PROCESS.replace('targetRef="approve" />', 'targetRef="send" />', 1)
    report = validate_app(_solution(tmp_path / "d", ungated))
    assert any("ADR-0007" in str(e) for e in report.errors)


def test_mail_goes_out_after_the_gate_with_case_file_attached(tmp_path):
    runtime = _runtime(tmp_path)
    sent: list = []
    runtime.outbound_mailer = sent.append
    try:
        attachment = runtime.settings.files_dir / "offer.pdf"
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_bytes(b"%PDF-1.4 offer")
        outcome, result = _run_to_mail(
            runtime,
            {
                "sender": "Anna Kunde <Anna@Kunde.ch>",
                "subject": "Re: Offerte",
                "answer": "Guten Tag\n\nanbei die Offerte.",
                "message_id": "<abc@kunde.ch>",
                "attach": "offer.pdf",
                "file": str(attachment),
            },
        )
        assert outcome == "done", result
        assert result == {
            "to": ["anna@kunde.ch"],
            "cc": [],
            "subject": "Re: Offerte",
            "attachments": ["offer.pdf"],
        }
        (message,) = sent
        assert message["From"] == "aitmation@example.ch" and message["To"] == "anna@kunde.ch"
        assert (
            message["In-Reply-To"] == "<abc@kunde.ch>" and message["References"] == "<abc@kunde.ch>"
        )
        parsed = message_from_bytes(message.as_bytes(), policy=email.policy.default)
        parts = list(parsed.walk())
        assert any(
            p.get_filename() == "offer.pdf" and p.get_payload(decode=True) == b"%PDF-1.4 offer"
            for p in parts
        )
        assert "anbei die Offerte" in parsed.get_body(preferencelist=("plain",)).get_content()
    finally:
        runtime.close()


def test_mail_refuses_foreign_files_bad_addresses_and_recipients_outside_the_allowlist(tmp_path):
    runtime = _runtime(tmp_path, mail_recipients=("@kunde.ch", "chef@firma.ch"))
    sent: list = []
    runtime.outbound_mailer = sent.append
    try:
        secret = tmp_path / "secret.txt"
        secret.write_text("x")
        outcome, result = _run_to_mail(
            runtime,
            {
                "sender": "a@kunde.ch",
                "subject": "s",
                "answer": "b",
                "message_id": "",
                "attach": str(secret),
            },
        )
        assert outcome == "failed" and "not a file of this case" in result["error"]
        outcome, result = _run_to_mail(
            runtime,
            {
                "sender": "someone@elsewhere.org",
                "subject": "s",
                "answer": "b",
                "message_id": "",
                "attach": [],
            },
        )
        assert outcome == "failed" and "outside mail_recipients" in result["error"]
        outcome, result = _run_to_mail(
            runtime,
            {
                "sender": "not-an-address",
                "subject": "s",
                "answer": "b",
                "message_id": "",
                "attach": [],
            },
        )
        assert outcome == "failed" and "not an address" in result["error"]
        outcome, result = _run_to_mail(
            runtime,
            {
                "sender": "chef@firma.ch",
                "subject": "s",
                "answer": "b",
                "message_id": "",
                "attach": [],
            },
        )
        assert outcome == "done" and len(sent) == 1
    finally:
        runtime.close()


def test_under_the_test_configuration_nothing_leaves(tmp_path):
    runtime = _runtime(tmp_path, configuration="test")
    sent: list = []
    runtime.outbound_mailer = sent.append
    try:
        outcome, result = _run_to_mail(
            runtime,
            {"sender": "a@kunde.ch", "subject": "s", "answer": "b", "message_id": "", "attach": []},
        )
        assert outcome == "done" and result["tool_mode"] == "stub" and sent == []
    finally:
        runtime.close()


def test_address_helpers():
    assert outbound.addresses("A <a@x.ch>; b@y.ch, a@x.ch") == ("a@x.ch", "b@y.ch")
    with pytest.raises(outbound.MailError):
        outbound.addresses([f"a{i}@x.ch" for i in range(11)])
    assert outbound.recipients_allowed(("a@x.ch",), ()) is None
    assert outbound.recipients_allowed(("a@x.ch", "b@y.ch"), ("@x.ch",)) == "b@y.ch"
    msg = outbound.build_message(
        "me@x.ch",
        outbound.OutgoingMail(("a@x.ch",), "  Hi  there ", "body"),
        max_attachment_bytes=10,
    )
    assert msg["Subject"] == "Hi there"
    with pytest.raises(outbound.MailError, match="subject"):
        outbound.build_message(
            "me@x.ch", outbound.OutgoingMail(("a@x.ch",), " ", "body"), max_attachment_bytes=10
        )
