# core

Process core, agent runtime, connector layer and JSON API. Language: **Python**.

The core is the technical automation engine and stays neutral about any
business domain: reading and creating PDFs, OCR, mail intake and the model
step are capabilities of the core; business logic (an incoming invoice,
say) belongs in a solution, never here. Without a model key every part
still works — AI steps that need a model fail with a clear reason, and the
classifier at the entrance hands everything to a person.

## Configuration without a terminal

All configuration happens in the interface (`/app`): model keys, mailbox
with "test connection", web form token, addressees, master data, document
types. Secrets go to `secrets.toml` (file mode 0600, never into
`config.toml`, URLs, logs or responses); environment variables still win
(containers). A faulty template or solution leaves the previous state
active on reload.

## Modules

**Engine**

- `db.py` – connection (WAL, foreign keys, busy timeout), migration
  runner (numbered SQL files, run when the core starts), online backup
  through `VACUUM INTO`.
- `migrations/` – schema migrations. Every migration must leave the
  *previous* application version working (two-release rule).
- `definition.py` – a process definition over the five primitives
  (trigger, agent step, human gate, system action, timer) with static
  validation: it proves that no action with an outside effect is
  reachable without a passed human gate.
- `engine.py` – the state machine over the database: `start_case`,
  `complete_step`, `decide_gate`, timers, mandates, parallel branches and
  multi-instance steps. Stateless — the whole state is in the database, a
  restart loses nothing. Append-only audit log with application version
  and model per entry.
- `bpmn.py`, `dmn.py`, `aimn.py` – loaders for the process notation: a
  BPMN 2.0 profile, DMN decision tables with S-FEEL, and briefings (the
  declaration of an AI step: role, instructions, limits, model role,
  output, context). Anything outside the profile is a load error.
- `bpmn_graph.py`, `designer.py` – the graph view on a BPMN file that the
  process designer in the interface reads and writes.
- `template.py` – the earlier TOML format for process templates, still
  loaded.
- `runtime.py` – wires everything together; `tick()` drives triggers,
  agent steps, actions and timers, `run_through()` runs a case within a
  request as far as it may go.
- `schedule.py` – time as a trigger (`every`, `at`, `weekday`, `day`),
  never retroactive; a missed slot runs once.

**Solutions**

- `solution.py`, `apps.py` – solution bundles: manifest, processes,
  briefings, decisions, tools, document types, records, knowledge spaces,
  configurations, tests. One validator (`validate-solution`), a loader
  with catalogue-wide collision checks. Bundles never contain code —
  foreign file types are a load error.
- `testing.py`, `recordings.py` – the proving run: sidecar tests drive
  the real engine with recorded model answers and compare the resulting
  state.
- `trust.py`, `catalog.py` – signed bundles and trust levels (Ed25519), a
  signed catalogue to install solutions from, safe unpacking, recalls.
- `library.py` – the building-block view across solutions: every agent
  (briefing) and tool with its origin and where it is used, new blocks
  from short forms into the instance's own solution (`own`), and "adapt a
  copy" of a shipped block.
- `drafts.py`, `scaffold.py`, `workflow_import.py` – drafts with an
  approval step, scaffolds for new elements, and the translation of
  workflows from n8n, Activepieces, Make and Zapier into drafts.

**Models and safety**

- `model.py`, `providers.py` – our own small client abstraction:
  Anthropic, Ollama (local), any OpenAI-compatible endpoint; a router per
  model role with fallbacks. Inputs are marked as untrusted data.
- `model_registry.py`, `data_classes.py`, `legal.py` – the registry of
  deployments (provider, region, clearance, prices), the data classes
  (`public < internal < confidential < sensitive`) and the jurisdiction
  table. Before every call the core checks, fail-closed, that the
  clearance of the model covers the demand of the step and that the
  region is allowed for the country of the instance.
- `guardrails.py` – the guardrail pipeline before and after every model
  call (pass, redact, block, escalate, flag): suspected prompt injection
  is flagged, links, personal data and secrets are removed.
- `observability.py`, `metrics.py`, `operations.py` – the model log
  (redacted, with retention), diagnostic events, cost and approval
  figures, the operations overview, the monthly budget.
- `gateway.py` – an OpenAI-shaped endpoint in front of the registry, so
  other programs use the same rules, budget and log.

**Documents and knowledge**

- `document.py`, `formats.py` – text extraction (PDF, office formats,
  HTML), OCR with a local Tesseract and, only where the data class allows
  it, a vision model as escalation; simple PDF creation.
- `transcribe.py` – local transcription of audio and video (whisper.cpp,
  faster-whisper or a transcription service you run), key frames through
  ffmpeg.
- `knowledge.py`, `knowledge_web.py` – knowledge spaces with a data
  class, versioned documents, BM25 plus optional local embeddings, web
  sources that are crawled on a schedule. A step binds a space
  explicitly; an unknown space fails the step.
- `records.py` – declared records and collections (schema with
  multilingual fields and country variants, versioned values).

**Channels and tools**

- `mail.py` – mail intake (IMAP) and outgoing mail as a gated action.
- `connectors.py`, `mcp_client.py`, `mcp_servers.py` – the only place
  that talks to foreign hosts: declarative HTTP tools, read-only fetch and
  search, MCP servers. Host policy, secrets in headers only, every answer
  is treated as foreign content.
- `notify.py` – notifications for new, reminded and escalated decisions.
- `chat.py`, `language.py` – the chat as a way in (deterministic intent
  recognition, no model, never an action without a gate) and the
  detection of a document's language.
- `mcp_server.py` – the MCP channel: desktop assistants are a way in,
  never the model.

**Surface**

- `api.py`, `web.py`, `__main__.py` – the JSON API `/api/v1`, the health
  endpoint `/healthz`, serving the built interface under `/app`, the
  scheduler as a background task in the same process, and the command
  line.
- `auth.py` – the core only verifies session tokens; users and passwords
  live in the launcher. Without a household secret the instance runs open
  on 127.0.0.1 (tests, development, single use).
- `config.py`, `secrets.py`, `i18n.py` – configuration from `config.toml`
  plus environment variables (`AITMATION_*` wins), the secrets store, and
  multilingual text fields (a string is German, or a table
  `{ de, fr, it, en }`).

## The MCP channel (Claude Desktop and other stdio assistants)

Add a server to the assistant's MCP configuration; `AITMATION_URL` points
to the mailroom of the running launcher or to a single instance:

```json
{
  "mcpServers": {
    "aitmation": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/aitmation/core", "--extra", "mcp", "python", "-m", "aitmation_core", "mcp"],
      "env": { "AITMATION_URL": "http://127.0.0.1:8420", "AITMATION_TOKEN": "<output of: aitmation-launcher token -user alex>" }
    }
  }
}
```

`AITMATION_TOKEN` is needed as soon as the household has users;
`aitmation-launcher token -user <name> -days 365` creates it — it is a
credential, keep it out of repositories and logs. The assistant never
decides by itself: the API only accepts decisions at open gates, and only
envelopes (sender, subject, file name) travel, never document contents.

## Develop

You need [uv](https://docs.astral.sh/uv/); the Python version comes from
`.python-version`, the dependencies from `uv.lock` (checked in).

```
cd core
uv sync                 # build the environment from the lockfile
uv run pytest           # tests
uv run ruff check .     # lint (the gate; the code base is not run through ruff format)
uv run python -m aitmation_core          # start; interface on http://127.0.0.1:8420/app/ once ui/ is built
```

Comments, docstrings, log messages and CLI help are English. No copyleft
dependencies.

Some tests drive the engine through solutions that are not part of this
repository; they are skipped unless that solution sits in `apps/`.

## Hard requirements for the core

1. Runs **without** the launcher — start, read configuration, open the port
2. Configuration from environment variables **and** a configuration file
   (environment wins)
3. No hard-wired paths; the data directory comes from the configuration
4. Health endpoint at `/healthz`
5. Schema migrations run when the core starts, not in the launcher
6. Clean shutdown on SIGTERM: running cases are not cut off
7. **Does not know the concept of a tenant.** No `tenant_id`, no logic
   across tenants — separation comes from deployment: one instance per
   mandant
8. The data access layer carries SQLite and Postgres without
   database-specific constructs in the schema
9. Background work runs in the same process as the web interface without
   blocking it

## Solutions

```
uv run python -m aitmation_core validate-solution ../apps/document-basics
uv run python -m aitmation_core test ../apps/document-basics            # proving run under "test"
uv run python -m aitmation_core test ../apps/document-basics --only <case> --repeat 3
uv run python -m aitmation_core test ../apps/document-basics --record   # record answers with a real model
AITMATION_CONFIGURATION=shadow uv run python -m aitmation_core          # shadow operation
```
