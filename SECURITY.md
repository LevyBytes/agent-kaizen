# Security Policy

## Reporting a vulnerability

Found a security issue in the Agent Kaizen harness? Please [open a GitHub issue](https://github.com/LevyBytes/agent-kaizen/issues/new).

This is an open-source project with fully readable source — there is no closed binary to reverse and no private data at rest on my end. Public issues get triaged faster and let other users see the fix. There is no security email; please don't send one.

Include:

- what you observed and why it is a security problem;
- the exact command or steps to reproduce;
- the commit you are on (`git rev-parse --short HEAD`) and your OS + Python version.

## Scope

In scope: the harness code in this repository — `kaizen.py`, `kaizen_components/`, `support_scripts/`, and the setup/installer scripts under `setup/`.

Out of scope:

- **Your data.** Records, the local database (`AI/db/`), exports, and artifacts live on your machine and are never transmitted — nothing phones home (see the [FAQ](README.md#faq)). A harness bug does not expose your DB to anyone who is not already on your filesystem.
- Third-party dependencies — report those upstream. The CI `audit` job runs `pip-audit` on every push.

## Related hardening

- `.github/secret_scanning.yml` — GitHub secret scanning is enabled; it excludes only `AI/tests/test_redaction.py`, which holds secret-*shaped* fixtures used to prove the redaction gate denies them.
- `kaizen_components/redaction.py` — file and record write paths run through a redaction gate that denies secret-shaped strings before they land.
