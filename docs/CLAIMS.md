# Claims and Proof

Every public performance/quality claim, mapped to its source and how to reproduce it. Benchmark numbers are the committed reference run in [BENCHMARKS.md](BENCHMARKS.md) — repository-local and illustrative (reference machine: Windows 11, AMD64, Python 3.12.10, pyturso 0.6.1), regenerable with one command.

| Claim (as stated publicly) | Where stated | Source of truth | Reproduce / verify |
| --- | --- | --- | --- |
| Record writes land in under 30 ms | README hero | `W1 task-start` median **24.25 ms** (n=50) — [Op Write Latency](BENCHMARKS.md#op-write-latency) | `python support_scripts/bench_kaizen.py` |
| Session-start digest reads back in ~0.11 s at 5,000 records | README hero | `R0` at 5,000 seeded records median **105.79 ms** — [Session Digest At Scale](BENCHMARKS.md#session-digest-at-scale) | same command |
| Context restore ~28× cheaper than replaying a transcript | README hero + Benchmarks | `R0` digest ≈ **1,825 tokens** vs median transcript ≈ **51,558 tokens** (7,299 B vs 206,232 chars) — [Context Recovery](BENCHMARKS.md#context-recovery) | same command; baseline = this repo's own median session transcript |
| 260+ tests across 30 modules | README Testing | [AI/tests/README.md](../AI/tests/README.md) | `python -m unittest discover -s AI/tests` |
| CI on Windows + Ubuntu | README Testing / badge | `os: [windows-latest, ubuntu-latest]` in [tests.yml](../.github/workflows/tests.yml) | GitHub Actions, every push |
| Cross-agent continuity: Claude Code + Codex, one DB, one CLI | README "Why Not Just Built-In Agent Memory?" | both hosts write through `kaizen.py`; read back with `R0` | `CLAUDE.md` / `AGENTS.md` |
| Verification is a recorded go/no-go, not a vibe | README | `Q2` writes conclusions (queried with `Q9`), linked to task + artifact hashes | `python kaizen.py Q2 --help` |
| Lessons are gated with full lineage | README | GOTCHA `g_20260703083749_7af0ead4be` → LEARNING `l_20260703083759_4ea0351f7e` → LEARNED `ld_20260703083810_2b4b61a94f`, read back with `L10` | README "Does It Actually Pay Off?" (real record IDs) |
| Nothing phones home / local-first | README FAQ | no network code; DB in `AI/db/` | inspect source; CI `audit` runs `pip-audit` |

Qualifier: latency is machine- and state-dependent (MVCC write-log compaction can make a larger record count read faster). Treat the reference run as a shape, not a guarantee — see [Limits](BENCHMARKS.md#limits).
