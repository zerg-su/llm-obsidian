"""Pinned Docling runtime readiness for document normalization."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

from document_normalize_config import (
    CHECK_COMMAND,
    DOCLING_MODELS,
    DOCLING_PYTHON_VERSION,
    DOCLING_VERSION,
    EASYOCR_REQUIRED_FILES,
    INSTALL_COMMAND,
    OCR_LANGUAGES,
    ROOT,
)


def default_docling_home() -> Path:
    value = os.environ.get("LLM_OBSIDIAN_DOCLING_HOME", "")
    if value:
        return Path(value).expanduser()
    return (
        Path.home()
        / ".local"
        / "share"
        / "llm-obsidian"
        / "docling"
        / DOCLING_VERSION
    )


def default_models_path() -> Path:
    value = os.environ.get("LLM_OBSIDIAN_DOCLING_MODELS", "")
    if value:
        return Path(value).expanduser()
    return default_docling_home() / "models"


def needs_docling_payload(reason: str) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "needs_user_action",
        "reason": reason,
        "action": {
            "kind": "install_or_repair_docling",
            "message": "Install the pinned local Docling runtime, then retry the ingest.",
            "install_command": INSTALL_COMMAND,
            "check_command": CHECK_COMMAND,
            "native_model_fallback_requires_confirmation": True,
        },
    }


def custom_docling_adapter() -> Path | None:
    value = os.environ.get("LLM_OBSIDIAN_DOCLING_ADAPTER", "").strip()
    return Path(value).expanduser() if value else None


def docling_python() -> Path:
    return default_docling_home() / "venv" / "bin" / "python"


def docling_adapter() -> Path:
    return custom_docling_adapter() or ROOT / "scripts" / "docling-adapter.py"


def runtime_version() -> str | None:
    override = os.environ.get("LLM_OBSIDIAN_DOCLING_VERSION", "").strip()
    if override:
        return override
    python = docling_python()
    if not python.is_file():
        return None
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('docling'))",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def models_ready() -> bool:
    if custom_docling_adapter() is not None:
        return True
    marker = default_docling_home() / "models-manifest.json"
    if not marker.is_file() or not default_models_path().is_dir():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        data.get("docling_version") == DOCLING_VERSION
        and data.get("python_version") == DOCLING_PYTHON_VERSION
        and data.get("languages") == OCR_LANGUAGES
        and data.get("easyocr_required_files") == EASYOCR_REQUIRED_FILES
        and data.get("models") == DOCLING_MODELS
        and any(default_models_path().iterdir())
        and all(
            (default_models_path() / "EasyOcr" / name).is_file()
            for name in EASYOCR_REQUIRED_FILES
        )
    )


def runtime_status() -> dict[str, Any]:
    adapter = docling_adapter()
    python = docling_python()
    version = runtime_version()
    if custom_docling_adapter() is not None:
        command_ok = adapter.is_file() and os.access(adapter, os.X_OK)
        command = adapter
    else:
        command_ok = python.is_file() and adapter.is_file()
        command = python
    model_ok = models_ready()
    ok = command_ok and version == DOCLING_VERSION and model_ok
    payload: dict[str, Any] = {
        "version": 1,
        "status": "ok" if ok else "dependency_missing",
        "docling": {
            "command": str(command),
            "adapter": str(adapter),
            "expected_version": DOCLING_VERSION,
            "actual_version": version,
            "command_ready": command_ok,
            "models_path": str(default_models_path()),
            "models_ready": model_ok,
            "ocr_languages": OCR_LANGUAGES,
        },
    }
    if not ok:
        payload.update(
            needs_docling_payload("Pinned Docling runtime or ru/en models are missing.")
        )
        payload["docling"] = {
            "command": str(command),
            "adapter": str(adapter),
            "expected_version": DOCLING_VERSION,
            "actual_version": version,
            "command_ready": command_ok,
            "models_path": str(default_models_path()),
            "models_ready": model_ok,
            "ocr_languages": OCR_LANGUAGES,
        }
    return payload
