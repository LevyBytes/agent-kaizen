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

BENCH = REPO_ROOT / "support_scripts" / "bench_kaizen.py"


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
        section = (out_dir / "readme-section.md").read_text(encoding="utf-8")
        self.assertIn("<!-- BENCHMARKS:BEGIN -->", section)
        self.assertIn("<!-- BENCHMARKS:END -->", section)
        self.assertIn("docs/images/bench-write-latency.svg", section)
        teaser = (out_dir / "teaser-section.md").read_text(encoding="utf-8")
        self.assertIn("<!-- BENCH-TEASER:BEGIN -->", teaser)
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


if __name__ == "__main__":
    unittest.main()
