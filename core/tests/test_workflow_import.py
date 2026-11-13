"""ADR-0060: workflows from n8n, Activepieces and Make become drafts -
translated where possible, approximated with a note, placeholders where
not; every outside effect behind a gate; no credentials carried over."""

import json
from pathlib import Path

import pytest

from aitmation_core import workflow_import as wi
from aitmation_core.apps import validate_app

SOLUTION = """
format = 2
id = "imports"
version = "0.1.0"
name = "Importe"
license = "proprietary"
[requires]
core = "0.0.1"
data_class = "internal"
[model_roles.imported]
structured_output = true
"""

N8N = {
    "name": "Lead triage",
    "nodes": [
        {"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {"path": "lead"}},
        {
            "name": "Classify",
            "type": "@n8n/n8n-nodes-langchain.openAi",
            "parameters": {
                "model": {"value": "gpt-4o-mini"},
                "messages": {
                    "values": [
                        {"role": "system", "content": "You sort leads."},
                        {"content": "=Lead from {{ $json.body.email }}: {{ $json.body.message }}"},
                    ]
                },
            },
            "credentials": {"openAiApi": {"id": "1", "name": "OpenAI account"}},
        },
        {
            "name": "Is hot?",
            "type": "n8n-nodes-base.if",
            "parameters": {
                "conditions": {
                    "conditions": [
                        {
                            "leftValue": "={{ $('Webhook').item.json.body.budget }}",
                            "rightValue": 10000,
                            "operator": {"type": "number", "operation": "gt"},
                        }
                    ],
                    "combinator": "and",
                }
            },
        },
        {
            "name": "Create deal",
            "type": "n8n-nodes-base.httpRequest",
            "parameters": {
                "method": "POST",
                "url": "https://crm.example.com/api/deals",
                "authentication": "genericCredentialType",
                "sendBody": True,
                "bodyParameters": {
                    "parameters": [
                        {"name": "email", "value": "={{ $('Webhook').item.json.body.email }}"},
                        {"name": "stage", "value": "new"},
                    ]
                },
            },
            "credentials": {"httpHeaderAuth": {"id": "2", "name": "CRM"}},
        },
        {
            "name": "Reply",
            "type": "n8n-nodes-base.emailSend",
            "parameters": {
                "toEmail": "={{ $('Webhook').item.json.body.email }}",
                "subject": "Danke",
                "text": "=Hallo, wir melden uns.",
            },
        },
        {"name": "Score", "type": "n8n-nodes-base.code", "parameters": {"jsCode": "return items;"}},
        {"name": "Notify", "type": "n8n-nodes-base.slack", "parameters": {"channel": "#sales"}},
        {"name": "Note", "type": "n8n-nodes-base.stickyNote", "parameters": {"content": "hi"}},
    ],
    "connections": {
        "Webhook": {"main": [[{"node": "Classify", "type": "main", "index": 0}]]},
        "Classify": {"main": [[{"node": "Is hot?", "type": "main", "index": 0}]]},
        "Is hot?": {
            "main": [
                [{"node": "Create deal", "type": "main", "index": 0}],
                [{"node": "Reply", "type": "main", "index": 0}],
            ]
        },
        "Create deal": {"main": [[{"node": "Score", "type": "main", "index": 0}]]},
        "Score": {"main": [[{"node": "Notify", "type": "main", "index": 0}]]},
    },
}

ACTIVEPIECES = {
    "name": "Daily digest",
    "template": {
        "displayName": "Daily digest",
        "trigger": {
            "name": "trigger",
            "type": "PIECE_TRIGGER",
            "displayName": "Every day",
            "settings": {
                "pieceName": "@activepieces/piece-schedule",
                "triggerName": "every_day",
                "input": {"hour_of_the_day": 7},
            },
            "nextAction": {
                "name": "step_1",
                "type": "PIECE",
                "displayName": "Fetch news",
                "settings": {
                    "pieceName": "@activepieces/piece-http",
                    "actionName": "send_request",
                    "input": {
                        "method": "GET",
                        "url": "https://news.example.org/api/top",
                        "queryParams": {"limit": "5"},
                    },
                },
                "nextAction": {
                    "name": "step_2",
                    "type": "ROUTER",
                    "displayName": "Anything?",
                    "settings": {
                        "executionType": "EXECUTE_FIRST_MATCH",
                        "branches": [
                            {
                                "branchName": "Has news",
                                "branchType": "CONDITION",
                                "conditions": [
                                    [
                                        {
                                            "firstValue": "{{step_1.body.count}}",
                                            "operator": "NUMBER_IS_GREATER_THAN",
                                            "secondValue": "0",
                                        }
                                    ]
                                ],
                            },
                            {"branchName": "Otherwise", "branchType": "FALLBACK"},
                        ],
                    },
                    "children": [
                        {
                            "name": "step_3",
                            "type": "PIECE",
                            "displayName": "Summarize",
                            "settings": {
                                "pieceName": "@activepieces/piece-claude",
                                "actionName": "ask_claude",
                                "input": {"prompt": "Summarize: {{step_1.body}}"},
                            },
                            "nextAction": {
                                "name": "step_4",
                                "type": "PIECE",
                                "displayName": "Send digest",
                                "settings": {
                                    "pieceName": "@activepieces/piece-smtp",
                                    "actionName": "send-email",
                                    "input": {
                                        "to": ["team@example.com"],
                                        "subject": "Digest",
                                        "body": "{{step_3}}",
                                    },
                                },
                            },
                        },
                        None,
                    ],
                },
            },
        },
    },
}

MAKE = {
    "name": "Video generator",
    "flow": [
        {
            "id": 2,
            "module": "util:SetVariables",
            "mapper": {
                "variables": [
                    {"name": "prompt", "value": "a cat"},
                    {"name": "loop", "value": "false"},
                ]
            },
        },
        {
            "id": 4,
            "module": "http:ActionSendDataAPIKeyAuth",
            "parameters": {"auth": 53877},
            "mapper": {
                "url": "https://api.lumalabs.ai/dream-machine/v1/generations",
                "method": "post",
                "data": '{"prompt": "{{2.prompt}}", "loop": "{{2.loop}}"}',
                "headers": [{"name": "accept", "value": "application/json"}],
            },
        },
        {"id": 6, "module": "util:FunctionSleep", "mapper": {"duration": "30"}},
        {
            "id": 8,
            "module": "http:ActionGetFile",
            "filter": {
                "name": "completed?",
                "conditions": [[{"a": "{{4.data.state}}", "b": "completed", "o": "text:equal"}]],
            },
            "mapper": {"url": "{{4.data.assets.video}}", "method": "get"},
        },
        {
            "id": 9,
            "module": "google-drive:uploadAFile",
            "parameters": {"__IMTCONN__": 123},
            "mapper": {"filename": "video.mp4"},
        },
    ],
    "metadata": {"version": 1},
}


def _solution(tmp_path: Path, result: wi.ImportResult) -> Path:
    root = tmp_path / "imports"
    root.mkdir()
    (root / "solution.toml").write_text(SOLUTION, encoding="utf-8")
    for rel, text in result.files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _status(result: wi.ImportResult) -> dict[str, str]:
    return {i.node: i.status for i in result.report}


def test_n8n_workflow_becomes_a_valid_draft_with_gates_and_placeholders(tmp_path):
    result = wi.translate(json.dumps(N8N))
    assert result.source == "n8n" and result.process_id == "lead-triage"
    report = validate_app(_solution(tmp_path, result))
    assert report.ok, [str(e) for e in report.errors]
    st = _status(result)
    assert st["Classify"] == wi.TRANSLATED and st["Is hot?"] == wi.TRANSLATED
    assert st["Webhook"] == wi.APPROXIMATED  # webhook → manual start
    assert st["Score"] == wi.PLACEHOLDER and st["Notify"] == wi.PLACEHOLDER
    bpmn = result.files["processes/lead-triage.bpmn"]
    # the deal and the reply each wait for a gate; the code node is a gate "rebuild by hand"
    assert bpmn.count("<aimn:gate") >= 4
    assert 'test="&gt; 10000"' in bpmn and 'input="trigger.budget"' in bpmn
    assert set(result.inputs) >= {"email", "message", "budget"}
    tool = result.files["tools/lead-triage-create-deal.toml"]
    assert 'effect = "external"' in tool and "{secret:crm}" in tool
    assert "AITMATION_SECRET_CRM" in result.summary()["secrets"]
    brief = result.files["ai/lead-triage-classify.aimn.toml"]
    assert (
        "{trigger.email}" in brief
        and "You sort leads." in brief
        and 'model_role = "imported"' in brief
    )
    # the code itself never comes along, no credentials either
    text = json.dumps(result.files)
    assert "return items" not in text.replace("\\", "") or "Original" in text
    assert "OpenAI account" not in text and '"id": "2"' not in text


def test_activepieces_flow_reads_without_a_gate_and_routes(tmp_path):
    result = wi.translate(json.dumps(ACTIVEPIECES))
    assert result.source == "activepieces"
    report = validate_app(_solution(tmp_path, result))
    assert report.ok, [str(e) for e in report.errors]
    bpmn = result.files[f"processes/{result.process_id}.bpmn"]
    assert 'source="schedule"' in bpmn and 'value="07:00"' in bpmn
    fetch = result.files["tools/daily-digest-fetch-news.toml"]
    assert 'effect = "read"' in fetch and 'format = "json"' in fetch and '"limit" = "5"' in fetch
    assert 'input="steps.fetch_news.outputs.data.count"' in bpmn
    assert 'action="mail"' in bpmn and "Freigabe: Send digest" in bpmn
    st = _status(result)
    assert st["Summarize"] == wi.TRANSLATED and st["Fetch news"] == wi.TRANSLATED


def test_make_blueprint_with_variables_filter_and_unknown_module(tmp_path):
    result = wi.translate(json.dumps(MAKE))
    assert result.source == "make"
    report = validate_app(_solution(tmp_path, result))
    assert report.ok, [str(e) for e in report.errors]
    bpmn = result.files[f"processes/{result.process_id}.bpmn"]
    assert {"prompt", "loop"} <= set(result.inputs)
    assert "PT30S" in bpmn
    assert '"completed"' in bpmn and 'input="steps.http_api_lumalabs_ai.outputs.data.state"' in bpmn
    assert 'id="filtered"' in bpmn
    st = _status(result)
    assert st["Google Drive"] == wi.PLACEHOLDER
    assert result.summary()["counts"][wi.PLACEHOLDER] >= 1
    tool = next(t for p, t in result.files.items() if p.startswith("tools/") and "lumalabs" in t)
    assert "{input:prompt}" in tool and "Bearer {secret:" in tool


def test_refuses_what_is_not_an_export():
    with pytest.raises(wi.WorkflowImportError, match="unknown export"):
        wi.translate(json.dumps({"hello": 1}))
    with pytest.raises(wi.WorkflowImportError, match="not JSON"):
        wi.translate("<xml/>")
    with pytest.raises(wi.WorkflowImportError, match="too large"):
        wi.translate(" " * (wi.MAX_EXPORT_BYTES + 1))


def test_a_loop_back_edge_is_cut_and_reported(tmp_path):
    flow = {
        "name": "loop",
        "nodes": [
            {"name": "Start", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
            {"name": "Batches", "type": "n8n-nodes-base.splitInBatches", "parameters": {}},
            {
                "name": "Wait",
                "type": "n8n-nodes-base.wait",
                "parameters": {"amount": 2, "unit": "minutes"},
            },
        ],
        "connections": {
            "Start": {"main": [[{"node": "Batches"}]]},
            "Batches": {"main": [[{"node": "Wait"}]]},
            "Wait": {"main": [[{"node": "Batches"}]]},
        },
    }
    result = wi.translate(json.dumps(flow))
    assert validate_app(_solution(tmp_path, result)).ok
    assert any("Rückkante" in i.note for i in result.report)
    assert "PT120S" in result.files["processes/loop.bpmn"]


# -- Zapier -------------------------------------------------------------------

ZAPFILE = {
    "metadata": {"version": "1"},
    "zaps": [
        {
            "id": 101,
            "title": "New lead to CRM",
            "nodes": {
                "1": {
                    "id": 1,
                    "parent_id": None,
                    "root_id": None,
                    "selected_api": "WebHookCLIAPI",
                    "action": "hook_v2",
                    "type_of": "read",
                    "params": {},
                    "title": "Catch Hook",
                },
                "2": {
                    "id": 2,
                    "parent_id": 1,
                    "root_id": 1,
                    "selected_api": "FilterAPI",
                    "action": "filter",
                    "type_of": "filter",
                    "params": {
                        "filter_criteria": [
                            {"key": "1__plan", "match": "exact", "value": "pro", "group": "0"}
                        ]
                    },
                    "title": "Only pro",
                },
                "3": {
                    "id": 3,
                    "parent_id": 2,
                    "root_id": 1,
                    "selected_api": "ChatGPTCLIAPI",
                    "action": "conversation",
                    "type_of": "write",
                    "params": {
                        "user_message": "Summarise the lead {{1__message}} from {{1__email}}.",
                        "system_message": "You write short CRM notes.",
                        "model": "gpt-4o-mini",
                    },
                    "title": "Summarise",
                },
                "4": {
                    "id": 4,
                    "parent_id": 3,
                    "root_id": 1,
                    "selected_api": "WebHookCLIAPI",
                    "action": "post",
                    "type_of": "write",
                    "params": {
                        "url": "https://crm.example.com/api/leads",
                        "data": {"email": "{{1__email}}", "note": "{{3__choices__text}}"},
                        "headers": {"Authorization": "Bearer abc123"},
                    },
                    "title": "Create lead",
                },
                "5": {
                    "id": 5,
                    "parent_id": 4,
                    "root_id": 1,
                    "selected_api": "BranchingAPI",
                    "action": "branch",
                    "type_of": "branch",
                    "params": {},
                    "title": "Paths",
                },
                "6": {
                    "id": 6,
                    "parent_id": 5,
                    "root_id": 1,
                    "selected_api": "BranchingAPI",
                    "action": "filter",
                    "params": {
                        "path_eval_index": 0,
                        "filter_criteria": [
                            {"key": "{{1__budget}}", "match": "gt", "value": "10000", "group": "0"}
                        ],
                    },
                    "title": "Big deal",
                },
                "7": {
                    "id": 7,
                    "parent_id": 6,
                    "root_id": 1,
                    "selected_api": "GoogleMailV2CLIAPI",
                    "action": "message",
                    "type_of": "write",
                    "params": {
                        "to": "sales@example.com",
                        "subject": "Big lead {{1__email}}",
                        "body": "{{3__choices__text}}",
                    },
                    "title": "Tell sales",
                },
                "8": {
                    "id": 8,
                    "parent_id": 5,
                    "root_id": 1,
                    "selected_api": "BranchingAPI",
                    "action": "filter",
                    "params": {"path_eval_index": 1, "filter_criteria": []},
                    "title": "Everything else",
                },
                "9": {
                    "id": 9,
                    "parent_id": 8,
                    "root_id": 1,
                    "selected_api": "SlackCLIAPI",
                    "action": "channel_message",
                    "type_of": "write",
                    "params": {"channel": "#leads", "text": "{{1__email}}", "token": "xoxb-1"},
                    "title": "Post to Slack",
                },
                "10": {
                    "id": 10,
                    "parent_id": 9,
                    "root_id": 1,
                    "selected_api": "CodeCLIAPI",
                    "action": "run_javascript",
                    "type_of": "write",
                    "params": {"code": "return {x: 1}"},
                    "title": "Tidy",
                },
            },
        },
        {
            "id": 102,
            "title": "Weekly report",
            "nodes": {
                "1": {
                    "id": 1,
                    "parent_id": None,
                    "selected_api": "ScheduleCLIAPI",
                    "action": "everyWeek",
                    "params": {"day_of_week": "Friday", "time_of_day": "4pm"},
                    "title": "Every Friday",
                },
                "2": {
                    "id": 2,
                    "parent_id": 1,
                    "selected_api": "DelayCLIAPI",
                    "action": "delay_for",
                    "params": {"value": "5", "unit": "minutes"},
                    "title": "Wait a bit",
                },
                "3": {
                    "id": 3,
                    "parent_id": 2,
                    "selected_api": "WebHookCLIAPI",
                    "action": "get",
                    "params": {
                        "url": "https://stats.example.com/v1/week",
                        "params": {"team": "sales"},
                    },
                    "title": "Fetch stats",
                },
            },
        },
    ],
}


def test_zapier_zap_with_filter_paths_ai_http_mail_and_placeholders(tmp_path):
    result = wi.translate(json.dumps(ZAPFILE))
    assert result.source == "zapier" and result.process_id == "new-lead-to-crm"
    report = validate_app(_solution(tmp_path, result))
    assert report.ok, [str(e) for e in report.errors]
    bpmn = result.files["processes/new-lead-to-crm.bpmn"]
    st = _status(result)
    assert st["Catch Hook"] == wi.APPROXIMATED  # webhook → manual start
    assert st["Summarise"] == wi.TRANSLATED and st["Only pro"] == wi.TRANSLATED
    assert st["Post to Slack"] == wi.PLACEHOLDER and st["Tidy"] == wi.PLACEHOLDER
    assert st["Weekly report"] == wi.APPROXIMATED  # the second zap is reported
    # filter and paths become conditions on the inputs of the trigger
    assert 'input="trigger.plan"' in bpmn and "test='\"pro\"'" in bpmn
    assert 'input="trigger.budget"' in bpmn and 'test="&gt; 10000"' in bpmn
    assert 'id="filtered"' in bpmn
    assert {"plan", "message", "email", "budget"} <= set(result.inputs)
    # the POST waits for a gate and asks for a secret, never carries the token
    tool = result.files["tools/new-lead-to-crm-create-lead.toml"]
    assert 'effect = "external"' in tool and "Bearer {secret:" in tool
    assert "abc123" not in json.dumps(result.files) and "xoxb" not in json.dumps(result.files)
    assert "{input:note}" in tool and 'value="steps.summarise.text"' in bpmn
    brief = result.files["ai/new-lead-to-crm-summarise.aimn.toml"]
    assert "{trigger.message}" in brief and "You write short CRM notes." in brief
    assert 'action="mail"' in bpmn and "Freigabe: Tell sales" in bpmn
    assert "Zapier MCP" in bpmn  # the placeholder names the MCP route
    assert "return {x: 1}" in bpmn  # only as a note on the rebuild gate


def test_zapier_second_zap_schedule_delay_and_read(tmp_path):
    result = wi.translate(json.dumps(ZAPFILE), index=1)
    assert result.process_id == "weekly-report"
    report = validate_app(_solution(tmp_path, result))
    assert report.ok, [str(e) for e in report.errors]
    bpmn = result.files["processes/weekly-report.bpmn"]
    assert 'source="schedule"' in bpmn and 'value="friday"' in bpmn and 'value="16:00"' in bpmn
    assert "PT300S" in bpmn
    fetch = result.files["tools/weekly-report-fetch-stats.toml"]
    assert 'effect = "read"' in fetch and '"team" = "sales"' in fetch
    assert "<aimn:gate" not in bpmn  # a read and a wait need no gate
    with pytest.raises(wi.WorkflowImportError, match="zap 5"):
        wi.translate(json.dumps(ZAPFILE), index=5)


def test_zapier_zip_export_and_detection():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.json", "{}")
        zf.writestr("zapfile.json", json.dumps(ZAPFILE))
    text = wi.export_text(buf.getvalue())
    assert wi.detect(json.loads(text)) == "zapier"
    assert wi.export_text(b'{"a": 1}') == '{"a": 1}'
    with pytest.raises(wi.WorkflowImportError):
        wi.export_text(b"PK\x03\x04broken")
    # a single zap as the API returns it (steps, no params) is a zap too
    api = {"id": 1, "title": "From API", "steps": [
        {"id": "a", "selected_api": "ScheduleCLIAPI", "action": "everyDay", "params": {}},
        {"id": "b", "selected_api": "SlackCLIAPI", "action": "channel_message"},
    ]}
    result = wi.translate(json.dumps(api))
    assert result.source == "zapier" and result.process_id == "from-api"
    assert _status(result)["SlackCLIAPI channel_message".replace("CLIAPI", "")] == wi.PLACEHOLDER
