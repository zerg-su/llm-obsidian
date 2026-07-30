"""Importable support for the hyphenated release-acceptance entrypoint."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).with_name("release-acceptance.py")
_SPEC = importlib.util.spec_from_file_location("_release_acceptance", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load release acceptance contract")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

AcceptanceError = _MODULE.AcceptanceError
CELL_IDS = _MODULE.CELL_IDS
contract = _MODULE.contract
validate_report = _MODULE.validate_report
