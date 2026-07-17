"""Bench-script smoke: --quick runs green in an isolated root and never touches repo docs."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import REPO_ROOT

BENCH = REPO_ROOT / "tests" / "bench_kaizen.py"

# bench_kaizen imports the engine lazily (inside main), so importing it here is side-effect-free.
from bench_kaizen import replace_heading_section  # noqa: E402


def _sha(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BenchSmokeTest(unittest.TestCase):
    maxDiff = None

    def test_quick_bench_green_and_repo_untouched(self):
        readme_before = _sha(REPO_ROOT / "README.md")
        docs_before = _sha(REPO_ROOT / "docs" / "BENCHMARKS.md")
        out_dir = Path(tempfile.mkdtemp(prefix="kaizen-bench-test-"))
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)

        proc = subprocess.run(
            [sys.executable, str(BENCH), "--quick", "--out", str(out_dir)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, f"bench --quick failed:\n{proc.stderr}")

        raw = (out_dir / "benchmarks.json").read_text(encoding="utf-8")
        results = json.loads(raw)
        self.assertEqual(results["schema"], "kaizen-bench/v1")
        self.assertEqual(results["mode"], "quick")
        for op in ("W1", "G1", "Q2", "X1", "T2"):
            self.assertGreater(results["write_ops"][op]["median_ms"], 0, op)
        self.assertTrue(results["digest_at_scale"])
        for entry in results["digest_at_scale"]:
            self.assertTrue(entry["counts"], f"R0 counts missing at scale {entry['scale']}")
        hit = results["retrieval"]["hit_rate"]
        self.assertEqual(hit["top_5"], 1.0, "marker query missed its own document")
        self.assertTrue(hit["negative_control_clean"])
        self.assertTrue(results["retrieval"]["semantic"]["skipped"])

        md = (out_dir / "BENCHMARKS.md").read_text(encoding="utf-8")
        self.assertIn("xychart-beta", md)
        self.assertIn("```powershell\npython tests\\bench_kaizen.py\n```", md)
        self.assertIn("```sh\npython tests/bench_kaizen.py\n```", md)
        section = (out_dir / "readme-section.md").read_text(encoding="utf-8")
        # Heading-delimited (no marker comments): the section IS the `## Benchmarks` heading.
        self.assertEqual(section.splitlines()[0], "## Benchmarks", section[:40])
        self.assertNotIn("<!--", section)
        self.assertIn("docs/images/bench-write-latency.svg", section)
        teaser = (out_dir / "teaser-section.md").read_text(encoding="utf-8")
        self.assertEqual(teaser.splitlines()[0], "## Benchmarks Preview", teaser[:40])
        self.assertNotIn("<!--", teaser)
        self.assertIn("[Benchmarks](#benchmarks)", teaser)
        svg = (out_dir / "bench-write-latency.svg").read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertIn("context_recovery", results)

        # --quick with --out must never rewrite the repo's own docs.
        self.assertEqual(_sha(REPO_ROOT / "README.md"), readme_before)
        self.assertEqual(_sha(REPO_ROOT / "docs" / "BENCHMARKS.md"), docs_before)

        # Committed results must stay free of personal paths (JSON escapes backslashes,
        # so one-or-more matches both raw and escaped forms).
        self.assertIsNone(re.search(r"[A-Za-z]:\\+Users", raw), "Windows home path leaked")
        self.assertNotIn("/home/", raw)


class ReplaceHeadingSectionTest(unittest.TestCase):
    """The heading-delimited README injector (replaces marker-comment regions)."""

    def _write(self, text: str) -> Path:
        d = Path(tempfile.mkdtemp(prefix="kaizen-heading-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = d / "README.md"
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def test_replaces_section_up_to_next_heading(self):
        p = self._write("# Title\n\nintro\n\n## Benchmarks Preview\n\nold teaser\n\n## Contents\n\n- toc\n")
        replace_heading_section(p, "## Benchmarks Preview", "## Benchmarks Preview\n\nNEW teaser")
        out = p.read_text(encoding="utf-8")
        self.assertIn("## Benchmarks Preview\n\nNEW teaser\n", out)
        self.assertNotIn("old teaser", out)
        self.assertIn("## Contents\n\n- toc\n", out)  # following section preserved
        self.assertNotIn("<!--", out)

    def test_exact_match_avoids_prefix_collision(self):
        # `## Benchmarks` must target the full section, NOT the earlier `## Benchmarks Preview`.
        p = self._write(
            "## Benchmarks Preview\n\nteaser stays\n\n## Contents\n\n- toc\n\n"
            "## Benchmarks\n\nold full section\n\n## Daily Workflow\n\nwork\n"
        )
        replace_heading_section(p, "## Benchmarks", "## Benchmarks\n\nNEW full section")
        out = p.read_text(encoding="utf-8")
        self.assertIn("teaser stays", out)  # preview untouched
        self.assertIn("## Benchmarks\n\nNEW full section\n", out)
        self.assertNotIn("old full section", out)
        self.assertIn("## Daily Workflow\n\nwork\n", out)  # following section preserved

    def test_deeper_subheading_stays_content(self):
        p = self._write("## Benchmarks\n\nintro\n\n### Sub\n\ndetail\n\n## Next\n\nafter\n")
        replace_heading_section(p, "## Benchmarks", "## Benchmarks\n\nREPLACED")
        out = p.read_text(encoding="utf-8")
        self.assertNotIn("### Sub", out)  # the H3 was inside the replaced section
        self.assertIn("## Next\n\nafter\n", out)

    def test_missing_heading_exits_nonzero(self):
        p = self._write("# Title\n\nno benchmarks here\n")
        with self.assertRaises(SystemExit) as ctx:
            replace_heading_section(p, "## Benchmarks", "## Benchmarks\n\nx")
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
