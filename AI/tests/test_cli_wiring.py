"""CLI wiring: help/version, denials, ALIASES <-> dispatch <-> README parity."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from _harness import IsolatedDBTest, alias_codes, kaizen, readme_codes, readme_purposes, registry_purposes, run


class HelpVersionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_help_exits_zero(self):
        proc = run(self.root, "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Command families", proc.stdout)

    def test_version(self):
        proc = run(self.root, "--version", "--json")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("tool_version", proc.stdout)

    def test_no_operation_prints_help(self):
        proc = run(self.root)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Command families", proc.stdout)

    def test_unknown_operation_denied(self):
        rc, payload = kaizen(self.root, "ZZ9")
        self.assertEqual(rc, 2)
        self.assertEqual(payload.get("status"), "DENIED")


class ParityTest(unittest.TestCase):
    def test_aliases_match_readme_table(self):
        aliases = set(alias_codes())
        readme = set(readme_codes())
        self.assertTrue(aliases, "failed to parse ALIASES codes")
        self.assertTrue(readme, "failed to parse README codes")
        self.assertEqual(
            aliases,
            readme,
            f"ALIASES vs README mismatch: only-in-args={aliases - readme}, only-in-readme={readme - aliases}",
        )


class RegistryParityTest(unittest.TestCase):
    def test_registry_matches_aliases_and_readme_purposes(self):
        """args.py REGISTRY is the single source of truth: it must cover every op, and the
        README Purpose column must match it byte-for-byte (K0 answers from REGISTRY)."""
        aliases = set(alias_codes())
        registry = registry_purposes()
        self.assertTrue(registry, "failed to parse REGISTRY purposes")
        self.assertEqual(set(registry), aliases, "REGISTRY keys must equal ALIASES codes")
        readme = readme_purposes()
        self.assertEqual(set(readme), aliases, "README purpose rows must cover every op")
        mismatches = {c: (registry[c], readme[c]) for c in aliases if registry[c] != readme[c]}
        self.assertEqual(mismatches, {}, f"REGISTRY vs README purpose drift: {mismatches}")


class K0LookupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_intent_query_finds_gotcha_add(self):
        rc, p = kaizen(self.root, "K0", "--query", "record a pitfall")
        self.assertEqual(rc, 0, p)
        top_codes = [r["code"] for r in p["records"][:3]]
        self.assertIn("G1", top_codes, p)
        self.assertIn("example", p["records"][0], p)

    def test_bare_k0_returns_full_index(self):
        rc, p = kaizen(self.root, "K0")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], len(alias_codes()), p)
        first = p["records"][0]
        self.assertEqual(sorted(first), ["alias", "code", "example", "purpose"], first)

    def test_no_match_points_back_at_index(self):
        rc, p = kaizen(self.root, "K0", "--query", "zzqqxxunmatchable")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["count"], 0, p)
        self.assertIn("K0", p.get("required_action", ""), p)


class DenialRemedyTest(IsolatedDBTest):
    def test_missing_title_denial_carries_example(self):
        rc, p = self.kz("G1", "--summary", "One sentence.")
        self.assertEqual(rc, 2, p)
        self.assertIn("G1 --title", p.get("example", ""), p)

    def test_invalid_json_denial_names_file_fallback(self):
        rc, p = self.kz("W5", "--summary", "Packet.", "--payload-json", "{not json")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_JSON_INVALID", p)
        self.assertIn("--payload-json-file", p.get("example", ""), p)

    def test_argparse_hint_on_shattered_json(self):
        proc = run(self.root, "W5", "--payload-json", "{a", "{b}")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--payload-json-file", proc.stderr, proc.stderr)


class DispatchTest(IsolatedDBTest):
    def test_every_alias_dispatches_without_unexpected_error(self):
        """Each op must reach a real dispatch branch: OK or a structured DENIED, never
        ERROR_UNEXPECTED and never a missing 'recognized but not implemented' denial."""
        failures = []
        for code in alias_codes():
            rc, payload = self.kz(code)
            status = payload.get("status")
            if status not in ("OK", "DENIED"):
                failures.append((code, rc, payload))
            elif payload.get("code") == "ERROR_UNEXPECTED":
                failures.append((code, rc, payload))
        self.assertEqual(failures, [], f"ops with unexpected errors: {failures}")
