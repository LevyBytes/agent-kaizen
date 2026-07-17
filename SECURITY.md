# Security Policy

## Reporting a vulnerability

Found a sensitive security issue in the Agent Kaizen harness? Report it privately through GitHub [private vulnerability reporting](https://github.com/LevyBytes/agent-kaizen/security/advisories/new) — open the repository's **Security** tab and choose **Report a vulnerability**. This keeps the report private until a fix is ready.

For a non-sensitive bug with no exploit risk, a normal [public issue](https://github.com/LevyBytes/agent-kaizen/issues/new) is fine and gets triaged faster. There is no security email; please use the private advisory form instead.

Include:

- what you observed and why it is a security problem;
- the exact command or steps to reproduce;
- the commit you are on (`git rev-parse --short HEAD`) and your OS + Python version.

## Scope

In scope: the harness code in this repository — `kaizen.py`, `kaizen_components/`, test and acceptance infrastructure under `tests/`, auxiliary utilities under `support_scripts/`, and setup/installer scripts under `setup/`.

Out of scope:

- **Your data and chosen external services.** Project records default to the local database (`AI/db/`). Explicitly configured model, ComfyUI, vendor-agent, fleet sync/control, and Git-remote features may send operation-specific data to services or endpoints you select; setup may also download dependencies (see the [FAQ](README.md#faq)). Those services' own handling is out of scope, but a harness bug that transmits more than the selected operation or configuration requires is in scope.
- Third-party dependencies — report those upstream. On pushes to `main` and pull requests, the CI `audit` job runs `pip-audit` against the core, docs, and optional PyTorch requirements plus `npm audit` for extension dependencies.

## Related hardening

- `.github/secret_scanning.yml` — GitHub secret scanning is enabled; it excludes only `tests/test_redaction.py`, which holds secret-*shaped* fixtures used to prove the redaction gate denies them.
- `kaizen_components/redaction.py` — file and record write paths run through a redaction gate that denies secret-shaped strings before they land.
