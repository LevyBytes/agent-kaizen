"""Conformance matrix: every op code in ALIASES must be exercised somewhere in this suite."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from _harness import IsolatedDBTest, alias_codes

TESTS_DIR = Path(__file__).resolve().parent

# Ops that legitimately cannot run in CI. Keep this EMPTY unless an op truly cannot
# be exercised here; every entry needs a one-line justification comment beside it.
ALLOWED_UNTESTED: set[str] = set()


def _op_code_arg(call: ast.Call) -> ast.expr | None:
    """Return the AST node in the op-code position of a kz()/kaizen() call, else None.

    ``self.kz("G1", ...)`` puts the op first; ``kaizen(root, "G1", ...)`` puts it second.
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "kz" and call.args:
        return call.args[0]
    if isinstance(func, ast.Name) and func.id == "kaizen" and len(call.args) > 1:
        return call.args[1]
    return None


def _invoked_op_literals(tree: ast.AST) -> set[str]:
    """Collect string literals passed in the op-code position of kz()/kaizen() calls.

    Counts direct calls plus the loop idiom used by the report tests:
    ``for code in ("R9", "R10"): ... self.kz(code)``. Downstream matching uses exact
    string equality against ALIASES, so T1 can never be mistaken for T10.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            arg = _op_code_arg(node)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
        elif isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name) or not isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
                continue
            literals = {
                elt.value
                for elt in node.iter.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
            if not literals:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    arg = _op_code_arg(inner)
                    if isinstance(arg, ast.Name) and arg.id == node.target.id:
                        found |= literals
                        break
    return found


def _natural(code: str) -> tuple[str, int]:
    m = re.fullmatch(r"([A-Z]+)(\d+)", code)
    return (m.group(1), int(m.group(2))) if m else (code, 0)


class OpCoverageTest(IsolatedDBTest):
    def test_every_alias_op_is_exercised_by_the_suite(self):
        codes = set(alias_codes())
        self.assertGreaterEqual(len(codes), 80, f"alias_codes() parse looks broken: {sorted(codes)}")

        covered: set[str] = set()
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            covered |= _invoked_op_literals(tree)

        stale = sorted(ALLOWED_UNTESTED - codes, key=_natural)
        self.assertFalse(stale, f"ALLOWED_UNTESTED entries no longer exist in ALIASES: {stale}")
        pruneable = sorted(ALLOWED_UNTESTED & covered, key=_natural)
        self.assertFalse(pruneable, f"ALLOWED_UNTESTED ops are now covered; prune them: {pruneable}")

        uncovered = sorted(codes - covered - ALLOWED_UNTESTED, key=_natural)
        self.assertFalse(
            uncovered,
            "Ops with no test coverage. Add a test that invokes self.kz(\"<OP>\", ...) in "
            "tests, or (last resort, with a justification comment) add the op to "
            f"ALLOWED_UNTESTED in {Path(__file__).name}: {uncovered}",
        )

    def test_g2_gotcha_list_round_trip(self):
        # G2 lives here because no other suite file invokes it; this closes the matrix
        # without an ALLOWED_UNTESTED entry and gives gotcha-list a real round-trip.
        rc, p = self.kz(
            "G1",
            "--title", "Listable pitfall",
            "--summary", "Fixture for the list op.",
            "--body", "Short body.",
        )
        self.assertEqual(rc, 0, p)
        gid = p["id"]
        rc, p = self.kz("G2")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p.get("count"), 1, p)
        record = p["records"][0]
        self.assertEqual(record["id"], gid, p)
        self.assertEqual(record["title"], "Listable pitfall", p)


if __name__ == "__main__":
    unittest.main()
