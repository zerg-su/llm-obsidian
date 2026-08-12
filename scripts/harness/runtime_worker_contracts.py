"""Shared bounded identities for the short-lived runtime worker."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("receipts",)

import re


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)


class RuntimeWorkerError(RuntimeError):
    """The worker cannot advance without violating its launch contract."""
