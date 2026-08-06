from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from task_review_context import _bounded_review_diff  # noqa: E402


class ReviewDiffBoundaryTest(unittest.TestCase):
    def test_normalizes_invalid_utf8(self) -> None:
        normalized = _bounded_review_diff(b"review fixture\n\xd0\n")

        self.assertIn("\ufffd", normalized.decode("utf-8"))

    def test_truncates_on_utf8_character_boundary(self) -> None:
        raw = ("a" * 64_999 + "Ж" * 1_000).encode("utf-8")

        bounded = _bounded_review_diff(raw)

        self.assertLessEqual(len(bounded), 65_536)
        self.assertTrue(
            bounded.decode("utf-8").endswith(
                "[diff truncated; inspect product HEAD]\n"
            )
        )


if __name__ == "__main__":
    unittest.main()
