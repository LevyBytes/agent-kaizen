# Contributing to Agent Kaizen

Thanks for considering a contribution. This guide demonstrates and standardizes procedures to have a contribution accepted. In model terms, a contract of: how to scope a change, prove it works, sign it, and open a pull request. It is short on purpose.

Contributions are offered under the repository's [AGPL-3.0](LICENSE) license, and every commit must be signed off under the [Developer Certificate of Origin](#sign-your-commits-dco).

You do not need the Kaizen harness, its database, or any Kaizen record to contribute. Those are how the maintainer(s) runs the project internally; a pull request is judged on its diff, its tests, and CI — not on any Kaizen artifact. Please do not paste Kaizen record IDs, JSON records, tiers, or internal identifiers into a PR.

## How this project thinks about a change

The project organizes work as **SAVMI — Scope, Adapt, Verify, Manage, Improve** (see [`Kaizen_System.md`](Kaizen_System.md)). A good pull request walks the same five steps. This is the current project framework; it may evolve.

- **Scope** — one focused change. Say what it is, why it is needed, and which files or surfaces it touches. Open an issue first for anything large or ambiguous.
- **Adapt** — make the change. Describe the user-visible contract (new/changed behavior, flags, output) and any compatibility impact.
- **Verify** — reproduce the problem, add or update a test that fails before and passes after, and record the exact command you ran and its outcome. CI on your PR's current commit is the authoritative check; your local result is disclosure, not proof.
- **Manage** — state the risks, how to roll the change back, and any concern you have not resolved. A disclosed "this part is untested" is welcome and never held against you — do not relabel a known gap to look clean.
- **Improve** — leave the tree better: a regression test guarding the fix, updated docs, a short note on anything a future reader should know.

## What to include, by change type

| Change | Provide (Scope / Adapt) | Prove (Verify) | Close out (Manage / Improve) |
| --- | --- | --- | --- |
| Docs or non-behavioral asset | Summary, affected pages/assets, "no behavior change" | Link or rendering check | Keep docs consistent; no test needed |
| Bug fix | Reproduction and the focused fix | New/updated regression test; exact command + outcome | Risks, rollback; the test guards the fix |
| Feature | Scope, user-visible contract, compatibility impact | Tests for the new behavior; exact command + outcome | Rollback, docs, disclosed concerns |
| Sensitive surface (workflow, dependency, installer, policy, schema, validator, test-runner, security) | Rationale, affected manifests, permission/supply-chain impact | Full suite + exact local results | Rollback; expect maintainer review of the diff before CI runs on a fork |

## Set up and run the tests

Commands are audience-specific: **contributors, maintainers, and agents** use the shared-interpreter wrapper in the [README testing section](README.md#testing), while **CI** uses fixed commands from [`tests.yml`](.github/workflows/tests.yml) on GitHub-hosted Windows and Ubuntu runners. CI commands are maintainer-owned and never copied from pull request text.

Follow the setup in the [README](README.md#setup), which creates the shared Kaizen environment under `DEVROOT`, then run the canonical scratch-pinning wrapper:

```powershell
# Windows
& "$env:DEVROOT\Python\venvs\kaizen\Scripts\python.exe" tests/run_tests.py
```

```sh
# Linux / macOS
"$DEVROOT/Python/venvs/kaizen/bin/python" tests/run_tests.py
```

CI runs this same suite on Windows and Linux, plus a dependency audit. Put the command you ran and its result in the pull request.

## Sign your commits (DCO)

Every commit must carry a `Signed-off-by` line certifying you have the right to submit it under the project's license, per the [Developer Certificate of Origin 1.1](https://developercertificate.org/). Add it automatically with:

```sh
git commit -s -m "Your message"
```

This appends a trailer like `Signed-off-by: Your Name <you@example.com>`. To sign off commits you already made, use `git rebase --signoff` on the range, then update your branch with `git push --force-with-lease`. CI checks every commit; an unsigned commit fails the DCO check and must be fixed. Once configured as a required status check, that failure blocks merge; until then, maintainers enforce the same policy manually. The sign-off is an attestation, not a cryptographic identity check.

## Open the pull request

- Fill in the pull request template; it mirrors the SAVMI steps above.
- Keep the change focused; unrelated cleanups belong in their own PR.
- If you contribute from an external fork, expect maintainer approval before your code runs and after each new commit. This authorizes runner execution only — it is not code review or approval to merge.
- Changes to a **sensitive surface** (above) get a manual diff review before that approval.
- A maintainer makes the final merge decision. Expect a response within about two weeks; a ping after that is fine.

## Reporting security issues

Do not open a public issue for a sensitive vulnerability. Follow the [private reporting route](SECURITY.md#reporting-a-vulnerability). Non-sensitive bugs can go in a normal public issue.

## License

By contributing, you agree that your contributions are licensed under [AGPL-3.0](LICENSE), the same license as the project.
