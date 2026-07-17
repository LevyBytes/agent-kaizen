"""C6 mode-profiles (v8 H2.1) round-trip through the real CLI.

Exit criteria proven here:
- C6 list/show/set round-trip via the CLI (--payload-json-file), name-keyed;
- the default 'plan' profile seeds its defaults (AI/work, AI/generation) on first read (idempotent);
- AI/db can NEVER be a designated write root;
- a ../ traversal escape and a nonexistent root are rejected;
- an empty designated_write_roots list = read-only Plan (accepted);
- set is an idempotent upsert (a second set overwrites, does not duplicate);
- a --test profile is purged by K7 (mode_profiles is in the purge SQL).
"""

from __future__ import annotations

import json

from _harness import IsolatedDBTest


class ModeProfilesTest(IsolatedDBTest):
    def _write_payload(self, obj: dict) -> str:
        path = self.root / "profile_payload.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return str(path)

    def _mkdir(self, rel: str) -> None:
        (self.root / rel).mkdir(parents=True, exist_ok=True)

    def test_list_seeds_default_plan(self):
        # AI/work + AI/generation exist after K1 (ensure_runtime_dirs), so the plan seed carries both.
        rc, p = self.kz("C6", "--action", "list")
        self.assertEqual(rc, 0, p)
        names = {prof["name"]: prof for prof in p["profiles"]}
        self.assertIn("plan", names, p)
        self.assertEqual(names["plan"]["permission_mode"], "plan", p)
        self.assertEqual(
            sorted(names["plan"]["designated_write_roots"]),
            ["AI/generation", "AI/work"],
            p,
        )

    def test_seed_is_idempotent(self):
        rc, _ = self.kz("C6", "--action", "list")
        self.assertEqual(rc, 0)
        rc, _ = self.kz("C6", "--action", "list")
        self.assertEqual(rc, 0)
        # Exactly one 'plan' row (no duplicate seed).
        rc, p = self.kz("C6", "--action", "list")
        self.assertEqual(rc, 0, p)
        plans = [prof for prof in p["profiles"] if prof["name"] == "plan"]
        self.assertEqual(len(plans), 1, p)

    def test_show_named_profile(self):
        rc, p = self.kz("C6", "--action", "show", "--name", "plan")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["profile"]["name"], "plan", p)
        self.assertEqual(p["profile"]["permission_mode"], "plan", p)

    def test_show_missing_profile_denied(self):
        rc, p = self.kz("C6", "--action", "show", "--name", "nonexistent")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_MODE_PROFILE_NOT_FOUND", p)

    def test_set_round_trip(self):
        self._mkdir("AI/work")
        payload = self._write_payload({"permission_mode": "agent", "designated_write_roots": ["AI/work"]})
        rc, p = self.kz("C6", "--action", "set", "--name", "myagent", "--payload-json-file", payload)
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["created"], p)
        rc, p = self.kz("C6", "--action", "show", "--name", "myagent")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["profile"]["permission_mode"], "agent", p)
        self.assertEqual(p["profile"]["designated_write_roots"], ["AI/work"], p)

    def test_set_idempotent_upsert(self):
        payload1 = self._write_payload({"permission_mode": "plan", "designated_write_roots": ["AI/work"]})
        rc, p1 = self.kz("C6", "--action", "set", "--name", "upserted", "--payload-json-file", payload1)
        self.assertEqual(rc, 0, p1)
        self.assertTrue(p1["created"], p1)
        rid = p1["id"]
        payload2 = self._write_payload({"permission_mode": "agent", "designated_write_roots": ["AI/generation"]})
        rc, p2 = self.kz("C6", "--action", "set", "--name", "upserted", "--payload-json-file", payload2)
        self.assertEqual(rc, 0, p2)
        self.assertFalse(p2["created"], p2)  # updated in place, not a new row
        self.assertEqual(p2["id"], rid, p2)
        rc, p = self.kz("C6", "--action", "show", "--name", "upserted")
        self.assertEqual(p["profile"]["permission_mode"], "agent", p)
        self.assertEqual(p["profile"]["designated_write_roots"], ["AI/generation"], p)

    def test_empty_roots_is_read_only_plan(self):
        payload = self._write_payload({"permission_mode": "plan", "designated_write_roots": []})
        rc, p = self.kz("C6", "--action", "set", "--name", "readonly", "--payload-json-file", payload)
        self.assertEqual(rc, 0, p)
        rc, p = self.kz("C6", "--action", "show", "--name", "readonly")
        self.assertEqual(p["profile"]["designated_write_roots"], [], p)

    def test_ai_db_can_never_be_designated(self):
        payload = self._write_payload({"permission_mode": "agent", "designated_write_roots": ["AI/db"]})
        rc, p = self.kz("C6", "--action", "set", "--name", "bad", "--payload-json-file", payload)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_MODE_PROFILE_ROOT_PROTECTED", p)

    def test_traversal_escape_rejected(self):
        payload = self._write_payload({"permission_mode": "agent", "designated_write_roots": ["../outside"]})
        rc, p = self.kz("C6", "--action", "set", "--name", "bad", "--payload-json-file", payload)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_MODE_PROFILE_ROOT_ESCAPE", p)

    def test_nonexistent_root_rejected(self):
        payload = self._write_payload({"permission_mode": "agent", "designated_write_roots": ["AI/does-not-exist"]})
        rc, p = self.kz("C6", "--action", "set", "--name", "bad", "--payload-json-file", payload)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_MODE_PROFILE_ROOT_MISSING", p)

    def test_invalid_permission_mode_rejected(self):
        payload = self._write_payload({"permission_mode": "nonsense", "designated_write_roots": []})
        rc, p = self.kz("C6", "--action", "set", "--name", "bad", "--payload-json-file", payload)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PERMISSION_MODE_INVALID", p)

    def test_set_requires_name(self):
        payload = self._write_payload({"permission_mode": "plan", "designated_write_roots": []})
        rc, p = self.kz("C6", "--action", "set", "--payload-json-file", payload)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_MODE_PROFILE_NAME_REQUIRED", p)

    def test_test_flag_purges(self):
        payload = self._write_payload({"permission_mode": "plan", "designated_write_roots": []})
        rc, p = self.kz("C6", "--action", "set", "--name", "throwaway", "--payload-json-file", payload, "--test")
        self.assertEqual(rc, 0, p)
        rc, purged = self.kz("K7")
        self.assertEqual(rc, 0, purged)
        self.assertEqual(purged["purged"].get("mode_profiles"), 1, purged)
        # The default seeded 'plan' (non-test) survives.
        rc, p = self.kz("C6", "--action", "show", "--name", "throwaway")
        self.assertEqual(rc, 1, p)
