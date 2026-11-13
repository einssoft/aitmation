# aitmation

**An AI operations system for small and medium-sized businesses.**

aitmation automates bounded business processes — incoming documents, mail,
deadlines, approvals — with AI agents that prepare and people who decide.
Every action with an effect outside the system needs an approval. Every
step is logged.

It runs on a computer in your office or as a hosted instance. Same
software, same data, different way of operating it.

> **Status: early.** The core runs end to end and is used in daily
> operation by its authors, but interfaces still change and the installers
> are not signed yet. Expect rough edges.

## Who it is for

Businesses with 5–200 people and no IT department, freelancers and sole
proprietors — and private households that no longer want to do their
paperwork (taxes, insurance, letters from authorities, invoices) by hand.
One installation is a *household* with one or more *mandants*, for example
"Private" and "Company X". If you have an IT department, a full BPM suite
will serve you better.

## Principles

1. **One use case solved completely** beats ten that half work.
2. **The person decides.** The agent prepares, proposes and explains. It
   never acts towards the outside on its own.
3. **Deny by default.** A fresh installation can read and nothing else.
4. **Installable without IT knowledge.** If a terminal command is needed,
   we have failed. (Self-hosting with `docker compose` is deliberately for
   technical users.)
5. **Traceable.** Every case can be reconstructed afterwards, also by an
   auditor.

## What it is not

- Not a chatbot that can do everything
- Not an autonomous swarm of agents
- No payments — it books, it never pays
- Not a replacement for an ERP or accounting software

## How it works

- **Processes are BPMN 2.0** with a small extension (AIMN) for AI steps;
  decisions are DMN tables. The engine is our own and deliberately small:
  five primitives — trigger, agent step, human gate, system action, timer.
- **Human gates cannot be bypassed.** The loader proves statically that no
  action with an outside effect is reachable without a passed gate (or a
  standing mandate a person granted).
- **Solutions are declarations, never code.** A solution is a folder with
  processes, AI briefings, decision tables, tools, document types and its
  own tests with recorded model answers. One validator checks them all.
- **Models are yours to choose.** Anthropic, any OpenAI-compatible
  endpoint, or a local model through Ollama. Before every call the core
  checks, fail-closed, whether the data class of the step may go to that
  model in your jurisdiction.
- **Local first.** SQLite, files on disk, OCR and transcription on your
  machine. Documents never leave the instance unless a step you approved
  sends them.

## Repository layout

```
core/            process core, agent runtime, JSON API (Python)
launcher/        installs, starts and supervises the instances of a household (Go)
ui/              the product interface (React, TypeScript, Vite, Mantine)
apps/            the base solutions that ship with the core
docs/examples/   example solutions — illustrations, not a roadmap
deploy/          container entrypoint, release bundle files, transcription service
scripts/         check.* (build and all tests), start.* (core with UI), bundle.sh
```

The base solutions are `document-basics` (read, review and file a
document), `company-profile` (master data for a company or a household,
with variants for CH/DE/AT) and `tasks` (one shared task board).

## Build and test

One command checks everything the way CI does (core: ruff, pytest,
validating solutions and their proving runs; UI: `npm ci` and build;
launcher: `go vet` / `go test` if Go is installed):

```
scripts/check.sh               # macOS / Linux
.\scripts\check.ps1            # Windows (PowerShell)
```

`scripts/start.sh` (or `.\scripts\start.ps1`) starts the core with the
built UI and the example solutions on `http://127.0.0.1:8477/app/`, using
a data directory `_dev` next to the repository. Requirements:
[uv](https://docs.astral.sh/uv/), Node.js 22+, optionally Go.

To run a whole household with sign-in, see [`launcher/README.md`](launcher/README.md).
The core on its own is described in [`core/README.md`](core/README.md).

## Documentation

The architecture decision records and the specification of the solution
format were written in German and are being translated; they will appear
under `docs/` as they are ready. Until then the READMEs in each directory
and the code are the reference.

## Community and support

Questions, ideas and help from other users: the community at
[skool.com/ailaunch](https://www.skool.com/ailaunch). Bugs and concrete
proposals: GitHub issues in this repository.

## Contributing

Issues and pull requests are welcome. Please keep source code, comments,
commit messages and documentation in English, run `scripts/check.sh`
before you open a pull request, and do not add copyleft dependencies.

## License

[Apache License 2.0](LICENSE).
