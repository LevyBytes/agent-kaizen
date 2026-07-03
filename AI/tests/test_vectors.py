"""Turso-native vector verification.

Backs the project claim that the pinned Turso engine (the ``turso`` / pyturso package in
``requirements-kaizen.txt``) ships NATIVE vector storage and similarity search. Anyone who
clones the repo and runs the suite reproduces this check.

The engine provides native vector column types (``F32_BLOB(n)``, ``F8_BLOB(n)``), native
constructors (``vector32`` / ``vector8``), and ``vector_distance_cos`` for cosine distance.
Nearest-neighbour search is the documented brute-force scan
``ORDER BY vector_distance_cos(...) LIMIT k`` (the libSQL ANN index ``libsql_vector_idx`` /
``vector_top_k`` table function is absent in this build, which is fine for per-project sets).

These tests run entirely in a throwaway temp database opened through the same ``turso``
dependency the harness uses; they never touch the project's real ``AI/db``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import turso


class TursoNativeVectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="kaizen-vectest-")
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.conn = turso.connect(os.path.join(self._dir, "vectors.db"))
        self.addCleanup(self.conn.close)

    def test_float32_nearest_neighbour_ranks_correctly(self):
        self.conn.execute("CREATE TABLE vt (id INTEGER PRIMARY KEY, embedding F32_BLOB(4))")
        self.conn.execute(
            "INSERT INTO vt VALUES "
            "(1, vector32('[1,0,0,0]')), (2, vector32('[0,1,0,0]')), (3, vector32('[0.9,0.1,0,0]'))"
        )
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT id, vector_distance_cos(embedding, vector32('[1,0,0,0]')) AS d "
            "FROM vt ORDER BY d ASC"
        ).fetchall()
        # Cosine distance must rank the identical vector first, the near vector second,
        # the orthogonal vector last — proving the engine computes over stored float32s.
        self.assertEqual([r[0] for r in rows], [1, 3, 2], f"native cosine NN mis-ranked: {rows}")
        self.assertAlmostEqual(rows[0][1], 0.0, places=5)  # identical
        self.assertAlmostEqual(rows[2][1], 1.0, places=5)  # orthogonal

    def test_int8_quantized_vectors_rank_correctly(self):
        self.conn.execute("CREATE TABLE vt8 (id INTEGER PRIMARY KEY, embedding F8_BLOB(4))")
        self.conn.execute(
            "INSERT INTO vt8 VALUES "
            "(1, vector8('[1,0,0,0]')), (2, vector8('[0,1,0,0]')), (3, vector8('[0.9,0.1,0,0]'))"
        )
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT id, vector_distance_cos(embedding, vector8('[1,0,0,0]')) AS d "
            "FROM vt8 ORDER BY d ASC LIMIT 2"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [1, 3], f"int8 NN mis-ranked: {rows}")

    def test_scalar_vector_functions(self):
        identical = self.conn.execute(
            "SELECT vector_distance_cos(vector32('[1,0,0,0]'), vector32('[1,0,0,0]'))"
        ).fetchone()[0]
        self.assertAlmostEqual(identical, 0.0, places=5)
        orthogonal = self.conn.execute(
            "SELECT vector_distance_cos(vector32('[1,0,0,0]'), vector32('[0,1,0,0]'))"
        ).fetchone()[0]
        self.assertAlmostEqual(orthogonal, 1.0, places=5)
        extracted = self.conn.execute("SELECT vector_extract(vector32('[1.5,2.5]'))").fetchone()[0]
        self.assertEqual(extracted, "[1.5,2.5]")


if __name__ == "__main__":
    unittest.main()
