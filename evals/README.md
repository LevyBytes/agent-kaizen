# Agent Kaizen Evals

This folder is the portable fixture surface for project-level evals and DB-backed learning command stubs.

Kaizen DB records own eval metadata and run history. Files here should be portable inputs, schemas, or fixtures that a verifier can execute or inspect.

Use:

```powershell
python kaizen.py Q3 --help
python kaizen.py Q4 --help
python kaizen.py G2 --help
python kaizen.py L4 --help
```

Initial eval categories:

- trigger
- behavior
- verification/proof
- learning-regression

Learning command stubs:

- `GOTCHA.md`
- `LEARNING.md`
- `LEARNED.md`

## Seed cases

Eight behavioral eval cases that seed the improvement lab (`O1` assembles case sets from `eval_cases` + LEARNED exemplars + active GOTCHAs). They encode the judgment calls the harness depends on: when a record is warranted, where it routes, and what the promotion gate protects. Run once per data plane; each command is copy-paste runnable.

```sh
python kaizen.py Q3 --category trigger --title "Should trigger G1: mid-task failure with evidence" --summary "A reproducible failure with evidence surfaced during task work should be recorded as a GOTCHA." --body "Agent hits a reproducible error twice while implementing a feature and has the command output. Expected: G1 with the evidence in the body. Not expected: silently working around it or leaving it in chat only." --expected-json '{"expected_op":"G1"}' --json
python kaizen.py Q3 --category trigger --title "Should trigger W2: scope or acceptance change" --summary "New evidence that changes scope, risk, or acceptance criteria should produce a task ledger update." --body "Mid-task, the user approves a narrower scope than originally planned. Expected: W2 on the active task recording the decision. Not expected: proceeding with the change undocumented." --expected-json '{"expected_op":"W2"}' --json
python kaizen.py Q3 --category trigger --title "Should NOT trigger: typo fix or chat-only exchange" --summary "Trivial mechanics and conversational back-and-forth do not warrant durable records." --body "Agent fixes a comment typo and answers a clarifying question. Expected: no harness record. Not expected: a G1 or W2 for noise-level events." --expected-json '{"expected_op":"none"}' --json
python kaizen.py Q3 --category trigger --title "Should NOT promote: unvalidated hypothesis" --summary "A plausible but unvalidated root-cause guess stays a GOTCHA; promotion to LEARNING waits for validation." --body "Agent suspects a race condition but has not reproduced or confirmed it. Expected: G1 recorded, L2 withheld until evidence confirms the cause. This is the promotion gate the system exists to protect." --expected-json '{"expected_op":"G1","withhold":"L2"}' --json
python kaizen.py Q3 --category behavior --title "Routing: record proof that checks passed" --summary "A claim that tests or checks passed should route to a verification record with a go/no-go conclusion." --body "Agent finishes work and states the suite is green. Expected: Q2 with conclusion VERIFIED_ACCEPTABLE and the commands run in the body, linked to the task with --task-id." --expected-json '{"expected_op":"Q2"}' --json
python kaizen.py Q3 --category behavior --title "Routing: consult lessons before re-deriving" --summary "A question about past pitfalls or decisions should query the record plane before re-deriving from scratch." --body "User asks whether this failure mode was seen before. Expected: L10 or L8/G3 first; re-derivation only if the records are empty." --expected-json '{"expected_op":"L10"}' --json
python kaizen.py Q3 --category verification --title "Proof: success claimed with nothing run" --summary "A success claim with no commands executed should be recorded as VERIFICATION_FAILED, not accepted." --body "A change is declared done but no test, linter, or smoke command ran. Expected: Q2 with conclusion VERIFICATION_FAILED (or NEEDS_HUMAN_DECISION) naming what was not run." --expected-json '{"expected_conclusion":"VERIFICATION_FAILED"}' --json
python kaizen.py Q3 --category learning-regression --title "Regression: promoted GOTCHA leaves the digest" --summary "After L2 promotion the source GOTCHA is marked promoted and must no longer appear in R0 active_gotchas." --body "Record a GOTCHA, promote it with L2, then run R0. Expected: the gotcha absent from active_gotchas and its status shown as promoted by G4. Guards the lifecycle transition shipped with G5." --expected-json '{"expected_status":"promoted"}' --json
```

The full operating model is in [`../Kaizen_System.md`](../Kaizen_System.md).
