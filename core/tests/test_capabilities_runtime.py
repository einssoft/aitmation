"""Capability modules wired into the runtime: LLM agent steps, mail intake."""

import json
from pathlib import Path

import pytest

from aitmation_core.config import Settings
from aitmation_core.mail import MailMessage, parse_message
from aitmation_core.model import ModelError, ModelResponse
from aitmation_core.runtime import Runtime
from aitmation_core.template import parse_template

LLM_TEMPLATE = """
id = "llm"
version = 1

[steps.intake]
kind = "trigger"
config = { source = "folder" }
next = { fired = "analyze" }

[steps.analyze]
kind = "agent_step"
next = { done = "approve", failed = "end" }
[steps.analyze.config]
task = "Summarize the document."
read_document = true
output = { summary = "one sentence" }

[steps.approve]
kind = "human_gate"
next = { approved = "export", rejected = "end" }

[steps.export]
kind = "system_action"
config = { action = "export" }
next = { done = "end" }
"""

MAIL_TEMPLATE = """
id = "mailflow"
version = 1

[steps.arrival]
kind = "trigger"
config = { source = "email" }
next = { fired = "check" }

[steps.check]
kind = "human_gate"
next = { approved = "end", rejected = "end" }
"""


class RecordingModel:
    def __init__(self, error=False):
        self.error = error
        self.seen_payloads = []

    def complete_structured(self, task, payload, schema_hint=None):
        self.seen_payloads.append(payload)
        if self.error:
            raise ModelError("provider down")
        return ModelResponse(
            data={"summary": "a short summary", "confidence": 0.9},
            model_ref="fake-model@1",
            confidence=0.9,
        )


class FakeMail:
    def __init__(self, messages):
        self.messages = list(messages)

    def fetch_new(self, attachment_dir: Path):
        out, self.messages = self.messages, []
        return out


def _runtime(tmp_path, template, **kwargs) -> Runtime:
    settings = Settings(data_dir=tmp_path / "data", templates_dir=tmp_path / "unused")
    definition = parse_template(template)
    return Runtime(settings, definitions={definition.id: definition}, **kwargs)


def test_llm_agent_step_reads_document_and_records_model_ref(tmp_path):
    model = RecordingModel()
    runtime = _runtime(tmp_path, LLM_TEMPLATE, model_client=model)
    try:
        (runtime.settings.inbox_dir / "doc.txt").write_text("document body", encoding="utf-8")
        runtime.tick()

        # The model saw trigger data plus the extracted document text.
        [payload] = model.seen_payloads
        assert payload["documents"][0]["text"] == "document body"
        assert payload["trigger"]["source"] == "folder"

        [gate] = runtime.open_gates()
        step = runtime.conn.execute(
            "SELECT result, model_ref, input_digest FROM steps"
            " WHERE case_id = ? AND step_id = 'analyze'",
            (gate["case_id"],),
        ).fetchone()
        assert json.loads(step["result"])["summary"] == "a short summary"
        assert step["model_ref"] == "fake-model@1"
        assert step["input_digest"]  # comparability, ADR-0013
    finally:
        runtime.close()


def test_llm_failure_takes_failed_outcome(tmp_path):
    runtime = _runtime(tmp_path, LLM_TEMPLATE, model_client=RecordingModel(error=True))
    try:
        (runtime.settings.inbox_dir / "doc.txt").write_text("x", encoding="utf-8")
        runtime.tick()
        row = runtime.conn.execute("SELECT outcome FROM steps WHERE step_id = 'analyze'").fetchone()
        assert row["outcome"] == "failed"
        assert runtime.open_gates() == []  # failed -> end, never a gate
    finally:
        runtime.close()


def test_without_model_client_stub_keeps_running(tmp_path):
    runtime = _runtime(tmp_path, LLM_TEMPLATE)  # no model configured
    try:
        (runtime.settings.inbox_dir / "doc.txt").write_text("x", encoding="utf-8")
        runtime.tick()
        [gate] = runtime.open_gates()
        step = runtime.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND step_id = 'analyze'",
            (gate["case_id"],),
        ).fetchone()
        assert json.loads(step["result"])["stub"] is True
    finally:
        runtime.close()


def test_mail_intake_starts_case(tmp_path):
    message = MailMessage(
        message_id="<m1@example>",
        sender="a@example.com",
        subject="new document",
        body_text="see attachment",
        attachments=[],
    )
    runtime = _runtime(tmp_path, MAIL_TEMPLATE, mail_source=FakeMail([message]))
    try:
        runtime.tick()
        [gate] = runtime.open_gates()
        assert gate["template_id"] == "mailflow"
        trigger = runtime.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'",
            (gate["case_id"],),
        ).fetchone()
        payload = json.loads(trigger["result"])
        assert payload["subject"] == "new document"
        runtime.tick()  # a second tick must not duplicate the case
        assert len(runtime.open_gates()) == 1
    finally:
        runtime.close()


def test_mail_with_several_attachments_starts_one_case_each(tmp_path):
    """Vermittler 9b: every attachment is its own document and gets its own
    case; the envelope (sender, subject, message id) travels with each."""
    files = []
    for name in ("rechnung.pdf", "lieferschein.pdf"):
        stored = tmp_path / f"{'a' * 32}_{name}"
        stored.write_text("x", encoding="utf-8")
        files.append(stored)
    message = MailMessage(
        message_id="<m2@example>",
        sender="a@example.com",
        subject="zwei Belege",
        body_text="anbei",
        attachments=files,
    )
    runtime = _runtime(tmp_path, MAIL_TEMPLATE, mail_source=FakeMail([message]))
    try:
        runtime.tick()
        gates = runtime.open_gates()
        assert len(gates) == 2
        payloads = []
        for gate in gates:
            trigger = runtime.conn.execute(
                "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'",
                (gate["case_id"],),
            ).fetchone()
            payloads.append(json.loads(trigger["result"]))
            assert runtime._case_files(gate["case_id"]) == [Path(payloads[-1]["file"])]
        by_name = {p["original_name"]: p for p in payloads}
        assert set(by_name) == {"rechnung.pdf", "lieferschein.pdf"}
        assert by_name["rechnung.pdf"]["attachment_index"] == 1
        assert by_name["lieferschein.pdf"]["attachment_count"] == 2
        assert all(p["message_id"] == "<m2@example>" for p in payloads)
        assert all(p["subject"] == "zwei Belege" for p in payloads)
    finally:
        runtime.close()


@pytest.fixture()
def rfc822_with_attachment() -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Sender <s@example.com>"
    msg["Subject"] = "Beleg"
    msg["Message-Id"] = "<abc@example>"
    msg.set_content("body text")
    msg.add_attachment(
        b"%fakepdf", maintype="application", subtype="pdf", filename="../evil/../doc.pdf"
    )
    return bytes(msg)


def test_parse_message_saves_attachment_with_safe_name(tmp_path, rfc822_with_attachment):
    message = parse_message(rfc822_with_attachment, tmp_path)
    assert message.subject == "Beleg"
    assert message.body_text.strip() == "body text"
    [attachment] = message.attachments
    # Path components from the wire are stripped; file lands in our dir.
    assert attachment.parent == tmp_path
    assert attachment.name.endswith("_doc.pdf")
    assert attachment.read_bytes() == b"%fakepdf"
