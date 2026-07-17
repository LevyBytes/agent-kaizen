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

## Skill-store trust boundary

Treat the configured external skill store as trusted, operator-controlled instruction and code input. `SK7` can place a package's full `SKILL.md` prose into model context; containment, validation, live package hashes, a correct selected-host surface, and effective host policy `on` gate that return but cannot make untrusted instructions safe. Claude project policy has a supported four-state writer; Codex policy is currently audit-only/default-on because no supported project-local writer exists. A `published` label means only that a configured package Git remote validates as GitHub; `staged` means it does not. Publication state is neither a trust decision nor an activation decision, and a staged package is not automatically disabled. Skill inventory and context queries do not fetch or publish repositories, install links, change host policy, enable skills, or execute package scripts.
