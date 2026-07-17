"""Docs that cannot rot: every kaizen.py example in the public docs is executed.

Extracts fenced ``powershell``/``sh`` blocks from README.md, setup/SETUP.md, and
evals/README.md, runs each ``kaizen.py`` command in document order against one fresh
isolated data plane per document, threads placeholder ids (``TASK_ID_FROM_W1`` etc.)
from earlier outputs, and asserts every example succeeds. A DENIED example is a doc
bug. Also pins the CLAUDE.md/AGENTS.md "intentionally identical" claim byte-for-byte.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

from _harness import REPO_ROOT, run

DOC_FILES = ["README.md", "setup/SETUP.md", "evals/README.md"]

_FENCE_RE = re.compile(r"^[ \t]*```(?:powershell|sh)\b[^\n]*$(.*?)^[ \t]*```\s*$", re.MULTILINE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"^[A-Z][A-Z_]*_FROM_([A-Z]+\d+)$")
# Interpreter prefixes docs legitimately use; everything up to and including kaizen.py is
# replaced by the harness runner (sys.executable + the repo's kaizen.py).
_KAIZEN_TOKEN_RE = re.compile(r"(^|[\\/])kaizen\.py$")


def _doc_commands(text: str) -> tuple[list[list[str]], int, int]:
    """Return ([argv-after-kaizen.py, ...] in document order, skipped_placeholder_lines,
    skipped_service_lines). A foreground service example (``daemon run`` without the
    ``--exit-after-boot`` seam) never returns, so executing it deadlocks the suite -- it is
    documented for humans and skipped here by design (hung the full suite twice 2026-07-10)."""
    commands: list[list[str]] = []
    skipped = 0
    service_skipped = 0
    for block in _FENCE_RE.findall(text):
        for line in block.splitlines():
            line = line.strip()
            if "kaizen.py" not in line or line.startswith("#"):
                continue
            if "<" in line:
                skipped += 1  # template placeholder like <operation>; not runnable by design
                continue
            tokens = shlex.split(line, posix=True)
            starts = [i for i, tok in enumerate(tokens) if _KAIZEN_TOKEN_RE.search(tok)]
            if not starts:
                continue
            argv = tokens[starts[0] + 1 :]
            if not argv:
                continue
            if argv[:2] == ["daemon", "run"] and "--exit-after-boot" not in argv:
                service_skipped += 1  # forever-running foreground service; see docstring
                continue
            commands.append(argv)
    return commands, skipped, service_skipped


class DocExamplesTest(unittest.TestCase):
    maxDiff = None

    def _run_document(self, rel_path: str) -> None:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        commands, skipped, service_skipped = _doc_commands(text)
        self.assertTrue(commands, f"{rel_path}: no kaizen.py examples extracted (extractor broken?)")
        # Placeholder-skips should stay rare and deliberate; growth here means examples
        # are drifting toward unrunnable templates.
        self.assertLessEqual(skipped, 3, f"{rel_path}: too many <placeholder> example lines skipped")
        self.assertLessEqual(service_skipped, 1, f"{rel_path}: more than one foreground service example")

        root = Path(tempfile.mkdtemp(prefix="kaizen-docs-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        # Fixture for the evidence walkthrough: E1 resolves relative --path against the
        # isolated repo root, and E4's example queries the phrase "retry budget".
        spec = root / "docs" / "spec.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(
            "# Widget spec\n\nThe retry budget is four seconds total.\n\nSecond paragraph for chunking.\n",
            encoding="utf-8",
        )

        captured_ids: dict[str, str] = {}
        for argv in commands:
            op = argv[0] if argv else ""
            resolved = []
            for token in argv:
                match = _PLACEHOLDER_RE.match(token)
                if match:
                    source_op = match.group(1)
                    self.assertIn(
                        source_op,
                        captured_ids,
                        f"{rel_path}: placeholder {token} appears before any {source_op} example ran",
                    )
                    token = captured_ids[source_op]
                resolved.append(token)
            if "--help" in resolved:
                proc = run(root, *resolved, timeout=120)
                self.assertEqual(proc.returncode, 0, f"{rel_path}: {' '.join(resolved)} -> {proc.stderr}")
                continue
            if "--json" not in resolved:
                resolved.append("--json")
            # Bounded: a doc example that outlives the budget is a hang (loud TimeoutExpired), never a
            # silent suite deadlock.
            proc = run(root, *resolved, timeout=120)
            raw = proc.stdout.strip() or proc.stderr.strip()
            self.assertEqual(
                proc.returncode, 0, f"{rel_path}: documented example failed: {' '.join(resolved)} -> {raw}"
            )
            self.assertTrue(raw, f"{rel_path}: documented example emitted no JSON: {' '.join(resolved)}")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as error:
                self.fail(f"{rel_path}: documented example emitted non-JSON output: {' '.join(resolved)} -> {raw!r}: {error}")
            self.assertEqual(payload.get("status"), "OK", f"{rel_path}: {' '.join(resolved)} -> {payload}")
            if isinstance(payload.get("id"), str):
                captured_ids[op] = payload["id"]

    def test_readme_examples_run_green(self):
        self._run_document("README.md")

    def test_setup_examples_run_green(self):
        self._run_document("setup/SETUP.md")

    def test_evals_readme_examples_run_green(self):
        self._run_document("evals/README.md")


class AgentDocParityTest(unittest.TestCase):
    def test_claude_and_agents_docs_identical_below_title(self):
        """Both files claim to be intentionally identical below their first line."""
        claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split("\n", 1)[1]
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8").split("\n", 1)[1]
        self.assertEqual(claude, agents, "CLAUDE.md and AGENTS.md drifted below the title line")


if __name__ == "__main__":
    unittest.main()
