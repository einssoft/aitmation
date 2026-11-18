# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: **Security → Report a vulnerability** on
this repository. If that is not available to you, write to
contact@aitmation.com with "security" in the subject.

Tell us what you found, how to reproduce it and which version or commit you
tested. You will get an answer within five working days. We will agree on a
date for disclosure with you and credit you in the release notes unless you
prefer otherwise.

## What is in scope

The code in this repository: the core (`core/`), the launcher (`launcher/`),
the web interface (`ui/`), the scripts and the bundled solutions (`apps/`).

Of particular interest:

- a way around a human approval (a step with outside effect that runs
  without an approval or a standing mandate),
- content from a document, a web page or a tool answer that is followed as
  an instruction,
- data reaching a model that its data class or the legal region does not
  allow,
- access across tenants in the shared host,
- secrets appearing in logs, answers or exported bundles.

## Supported versions

Fixes go into `main` and the latest release. There are no maintenance
branches for older releases yet.

## How this repository is checked

- Dependencies: Dependabot alerts and security updates.
- Our own code: CodeQL on every push and weekly; security rules of the
  linter (`ruff`, rule set `S`) in CI.
- Secrets: secret scanning with push protection.
- Licences: `scripts/check_licenses.py` keeps copyleft out of the
  dependency tree.
