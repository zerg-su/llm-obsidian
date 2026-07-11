#!/usr/bin/env python3
"""Compatibility wrapper for the section-level ``retrieve.py`` CLI."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieve import *  # noqa: F401,F403 — preserve import compatibility
from retrieve import main as retrieve_main


# Import-level compatibility for callers that used the old fusion helper.
DENSE_WEIGHT = 3.0
BM25_OOS_WEIGHT = 2.9


def hybrid_fuse(dense_ranked, bm25_ranked, dense_scope):
    scores = {}
    for rank, path in enumerate(dense_ranked, 1):
        scores[path] = scores.get(path, 0.0) + DENSE_WEIGHT / (RRF_K + rank)
    for rank, path in enumerate(bm25_ranked, 1):
        if path not in dense_scope:
            scores[path] = scores.get(path, 0.0) + BM25_OOS_WEIGHT / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


if __name__ == "__main__":
    # ``--hybrid`` used to opt in; hybrid is now the sparse-first default.
    raise SystemExit(retrieve_main([arg for arg in sys.argv[1:] if arg != "--hybrid"]))
