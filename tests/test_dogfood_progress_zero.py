#!/usr/bin/env python3
"""test_dogfood_progress_zero.py — regression test for scripts/dogfood_fixture.py.

format_progress(0, 0) must return "0%" instead of raising ZeroDivisionError;
negative done/total or done > total must be rejected as invalid input; other
inputs must keep the rounded-percentage behavior.

Usage:
  python3 tests/test_dogfood_progress_zero.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.dogfood_fixture import format_progress


class FormatProgressZeroTotalTest(unittest.TestCase):
    def test_zero_total_returns_zero_percent(self):
        self.assertEqual(format_progress(0, 0), "0%")

    def test_negative_done_rejected(self):
        with self.assertRaises(ValueError):
            format_progress(-1, 10)

    def test_negative_total_rejected(self):
        with self.assertRaises(ValueError):
            format_progress(1, -10)

    def test_done_greater_than_total_rejected(self):
        with self.assertRaises(ValueError):
            format_progress(11, 10)

    def test_normal_rounding_preserved(self):
        self.assertEqual(format_progress(1, 3), "33%")
        self.assertEqual(format_progress(10, 10), "100%")
        self.assertEqual(format_progress(0, 10), "0%")


if __name__ == "__main__":
    unittest.main()
