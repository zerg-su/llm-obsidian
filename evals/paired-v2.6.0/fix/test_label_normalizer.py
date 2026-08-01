#!/usr/bin/env python3
"""Pre-existing checks that are green despite the frozen Unicode defect."""

from label_normalizer import normalize_label


assert normalize_label("Hello World") == "hello-world"
assert normalize_label("  Alpha___Beta  ") == "alpha-beta"
assert normalize_label("!!!") == "untitled"

print("paired fix fixture: existing checks passed")
