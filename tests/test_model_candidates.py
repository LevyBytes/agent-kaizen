"""Model-candidate registry: manifest completeness + seeder ingest/idempotency into source_locks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import REPO_ROOT, IsolatedDBTest  # noqa: E402

_MANIFEST = REPO_ROOT / "evals" / "model-candidates.json"
_SEEDER = REPO_ROOT / "support_scripts" / "seed_model_candidates.py"
_TIERS = ("normative", "official_docs", "implementation", "design_guidance")


class ManifestTest(unittest.TestCase):
    def test_manifest_complete(self):
        data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(len(data["candidates"]), 13, "expected 13 registry entries")
        seen = set()
        for cand in data["candidates"]:
            for field in ("source_id", "url", "license", "version_or_commit", "authority_tier", "role", "disposition", "notes"):
                self.assertTrue(cand.get(field), f"{cand.get('source_id')} missing {field}")
            self.assertIn(cand["disposition"], data["dispositions"], cand["source_id"])
            self.assertIn(cand["authority_tier"], _TIERS, cand["source_id"])
            self.assertNotIn(cand["source_id"], seen, "duplicate source_id")
            seen.add(cand["source_id"])


class SeederTest(IsolatedDBTest):
    def test_seeder_ingests_and_is_idempotent(self):
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        for attempt in range(2):  # second run must be a no-op (idempotent)
            proc = subprocess.run([sys.executable, str(_SEEDER)], capture_output=True, text=True,
                                  cwd=str(REPO_ROOT), env=env, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if attempt == 1:
                self.assertIn("skip (exists)", proc.stdout)
        rc, p = self.kz("S2", "--query", "model-krea-2")
        self.assertEqual(rc, 0, p)
        matches = [r for r in p["records"] if r["source_id"] == "model-krea-2"]
        self.assertEqual(len(matches), 1, matches)


if __name__ == "__main__":
    unittest.main()
