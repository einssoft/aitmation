"""Action ``file``: the case's document goes into the filing folder under
the folder and name the process binds - cleaned, never outside, never
overwritten."""

from __future__ import annotations

import textwrap
from pathlib import Path

from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]

PROCESS = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
      xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
      <bpmn:process id="file-it" isExecutable="true" aimn:version="1">
        <bpmn:startEvent id="s"><bpmn:extensionElements><aimn:trigger source="manual"/>
        </bpmn:extensionElements></bpmn:startEvent>
        <bpmn:serviceTask id="put" name="Ablegen"><bpmn:extensionElements>
          <aimn:toolCall action="file"><aimn:property name="inputs">
            <aimn:property name="folder"
              value="template:{trigger.year}/../Rechnungen/{trigger.who}"/>
            <aimn:property name="name" value="template:{trigger.year}-11 Rechnung {trigger.who}"/>
          </aimn:property></aimn:toolCall></bpmn:extensionElements></bpmn:serviceTask>
        <bpmn:endEvent id="e"/>
        <bpmn:sequenceFlow id="f1" sourceRef="s" targetRef="put"/>
        <bpmn:sequenceFlow id="f2" name="done" sourceRef="put" targetRef="e"/>
        <bpmn:sequenceFlow id="f3" name="failed" sourceRef="put" targetRef="e"/>
      </bpmn:process>
    </bpmn:definitions>
    """
)


def test_file_action_files_into_a_clean_folder(tmp_path):
    data = tmp_path / "data"
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "file-it.bpmn").write_text(PROCESS, encoding="utf-8")
    drive = tmp_path / "drive"
    settings = Settings(data_dir=data, templates_dir=templates, filing_dir=drive)
    rt = Runtime(settings)
    try:
        doc = settings.files_dir / "scan_0001.PDF"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"%PDF-1.4")
        payload = {"file": str(doc), "year": "2026", "who": "Swisscom/AG"}
        first = rt.run_now("file-it", "alex", payload)
        second = rt.run_now("file-it", "alex", payload)
        rt.tick()
        files = sorted(p.relative_to(drive).as_posix() for p in drive.rglob("*") if p.is_file())
        # ".." is dropped, a "/" splits the folder (it stays inside the
        # filing folder), the name has no separator, nothing is overwritten
        assert files == [
            "2026/Rechnungen/Swisscom/AG/2026-11 Rechnung Swisscom-AG-2.pdf",
            "2026/Rechnungen/Swisscom/AG/2026-11 Rechnung Swisscom-AG.pdf",
        ]
        for case_id in (first, second):
            row = rt.conn.execute(
                "SELECT outcome FROM steps WHERE case_id = ? AND step_id = 'put'", (case_id,)
            ).fetchone()
            assert row["outcome"] == "done"
        # without a document the step fails instead of filing nothing
        third = rt.run_now("file-it", "alex", {"year": "2026", "who": "x"})
        rt.tick()
        row = rt.conn.execute(
            "SELECT outcome FROM steps WHERE case_id = ? AND step_id = 'put'", (third,)
        ).fetchone()
        assert row["outcome"] == "failed"
    finally:
        rt.close()


PDF_PROCESS = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
      xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
      <bpmn:process id="make-pdf" isExecutable="true" aimn:version="1">
        <bpmn:startEvent id="s"><bpmn:extensionElements><aimn:trigger source="manual"/>
        </bpmn:extensionElements></bpmn:startEvent>
        <bpmn:serviceTask id="pdf"><bpmn:extensionElements>
          <aimn:toolCall action="pdf"><aimn:property name="inputs">
            <aimn:property name="title" value="template:Angebot {trigger.no}"/>
            <aimn:property name="text" value="trigger.text"/>
            <aimn:property name="name" value="template:Angebot {trigger.no}"/>
          </aimn:property></aimn:toolCall></bpmn:extensionElements></bpmn:serviceTask>
        <bpmn:serviceTask id="put"><bpmn:extensionElements>
          <aimn:toolCall action="file"><aimn:property name="media" value="true"/>
          <aimn:property name="inputs"><aimn:property name="folder" value="literal:Angebote"/>
          </aimn:property></aimn:toolCall></bpmn:extensionElements></bpmn:serviceTask>
        <bpmn:endEvent id="e"/>
        <bpmn:sequenceFlow id="f1" sourceRef="s" targetRef="pdf"/>
        <bpmn:sequenceFlow id="f2" name="done" sourceRef="pdf" targetRef="put"/>
        <bpmn:sequenceFlow id="f3" name="failed" sourceRef="pdf" targetRef="e"/>
        <bpmn:sequenceFlow id="f4" name="done" sourceRef="put" targetRef="e"/>
        <bpmn:sequenceFlow id="f5" name="failed" sourceRef="put" targetRef="e"/>
      </bpmn:process>
    </bpmn:definitions>
    """
)


def test_pdf_action_makes_a_case_file_that_file_can_put_away(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "make-pdf.bpmn").write_text(PDF_PROCESS, encoding="utf-8")
    drive = tmp_path / "drive"
    settings = Settings(data_dir=tmp_path / "data", templates_dir=templates, filing_dir=drive)
    rt = Runtime(settings)
    try:
        long_line = "Position " + "sehr lange Beschreibung " * 20
        payload = {"no": "2026-17", "text": f"Guten Tag\n\n{long_line}"}
        case_id = rt.run_now("make-pdf", "alex", payload)
        rt.tick()
        rt.tick()
        made = settings.files_dir / "media" / case_id / "Angebot 2026-17.pdf"
        assert made.read_bytes().startswith(b"%PDF")
        assert (drive / "Angebote" / "Angebot 2026-17.pdf").is_file()
    finally:
        rt.close()
