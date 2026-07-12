#!/usr/bin/env python3
"""Install and validate the pinned local Docling document runtime."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config" / "document-tools.json").read_text(encoding="utf-8"))
DOCLING_VERSION = str(CONFIG["docling"]["version"])
PYTHON_VERSION = str(CONFIG["docling"]["python"])
DOCLING_EXTRA = str(CONFIG["docling"]["extra"])
MODELS = [str(value) for value in CONFIG["docling"]["models"]]
LANGUAGES = [str(value) for value in CONFIG["docling"]["ocr_languages"]]
EASYOCR_REQUIRED_FILES = [
    str(value) for value in CONFIG["docling"]["easyocr_required_files"]
]


def home() -> Path:
    override = os.environ.get("LLM_OBSIDIAN_DOCLING_HOME", "")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "llm-obsidian" / "docling" / DOCLING_VERSION


def venv_python() -> Path:
    return home() / "venv" / "bin" / "python"


def docling_tools() -> Path:
    return home() / "venv" / "bin" / "docling-tools"


def models_path() -> Path:
    override = os.environ.get("LLM_OBSIDIAN_DOCLING_MODELS", "")
    return Path(override).expanduser() if override else home() / "models"


def marker_path() -> Path:
    return home() / "models-manifest.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(args: list[str]) -> None:
    print("+ " + " ".join(args))
    result = subprocess.run(args, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit {result.returncode}: {args[0]}")


def installed_version() -> str | None:
    python = venv_python()
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
    return result.stdout.strip() if result.returncode == 0 else None


def installed_python_version() -> str | None:
    python = venv_python()
    if not python.is_file():
        return None
    result = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def marker() -> dict[str, object] | None:
    try:
        data = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def model_stats() -> tuple[int, int]:
    root = models_path()
    if not root.is_dir():
        return (0, 0)
    count = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            count += 1
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return (count, total)


def easyocr_languages_ready() -> bool:
    root = models_path() / "EasyOcr"
    return all((root / name).is_file() for name in EASYOCR_REQUIRED_FILES)


def status() -> dict[str, object]:
    version = installed_version()
    python_version = installed_python_version()
    data = marker()
    count, total = model_stats()
    models_ok = bool(
        data
        and data.get("docling_version") == DOCLING_VERSION
        and data.get("python_version") == PYTHON_VERSION
        and data.get("models") == MODELS
        and data.get("languages") == LANGUAGES
        and data.get("easyocr_required_files") == EASYOCR_REQUIRED_FILES
        and count > 0
        and easyocr_languages_ready()
    )
    ok = (
        version == DOCLING_VERSION
        and python_version == PYTHON_VERSION
        and docling_tools().is_file()
        and models_ok
    )
    return {
        "version": 1,
        "status": "ok" if ok else "dependency_missing",
        "home": str(home()),
        "python": str(venv_python()),
        "expected_python_version": PYTHON_VERSION,
        "actual_python_version": python_version,
        "expected_docling_version": DOCLING_VERSION,
        "actual_docling_version": version,
        "models_path": str(models_path()),
        "models": MODELS,
        "model_files": count,
        "model_bytes": total,
        "ocr_languages": LANGUAGES,
        "easyocr_required_files": EASYOCR_REQUIRED_FILES,
        "install_command": "python3 scripts/install-docling.py install",
    }


def emit(payload: dict[str, object], json_output: bool, code: int = 0) -> NoReturn:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Docling: {payload['status']}")
        print(f"  runtime: {payload['home']}")
        print(
            "  version: "
            f"{payload['actual_docling_version'] or 'missing'} "
            f"(expected {payload['expected_docling_version']})"
        )
        print(
            "  python: "
            f"{payload['actual_python_version'] or 'missing'} "
            f"(expected {payload['expected_python_version']})"
        )
        print(f"  models: {payload['model_files']} files in {payload['models_path']}")
        if payload["status"] != "ok":
            print(f"  repair: {payload['install_command']}")
    raise SystemExit(code)


def plan() -> int:
    payload = status()
    if payload["status"] == "ok":
        print(f"keep: Docling {DOCLING_VERSION} with ru/en models at {home()}")
        return 0
    uv = shutil.which("uv") or "uv"
    print(f"+ {uv} venv --python {PYTHON_VERSION} {home() / 'venv'}")
    print(
        f"+ {uv} pip install --python {venv_python()} "
        f"docling[{DOCLING_EXTRA}]=={DOCLING_VERSION}"
    )
    print(
        f"+ {docling_tools()} models download --output-dir {models_path()} "
        + " ".join(MODELS)
    )
    return 0


def install() -> int:
    uv = shutil.which("uv")
    if not uv:
        print(
            "ERROR: uv is required. On macOS run 'brew install uv', then retry.",
            file=sys.stderr,
        )
        return 2
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    if installed_python_version() != PYTHON_VERSION:
        if (root / "venv").exists():
            print(f"replace: incompatible Docling Python environment at {root / 'venv'}")
            shutil.rmtree(root / "venv")
        run([uv, "venv", "--python", PYTHON_VERSION, str(root / "venv")])
    if installed_version() != DOCLING_VERSION:
        if not venv_python().is_file():
            run([uv, "venv", "--python", PYTHON_VERSION, str(root / "venv")])
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(venv_python()),
                f"docling[{DOCLING_EXTRA}]=={DOCLING_VERSION}",
            ]
        )
    else:
        print(f"keep: Docling {DOCLING_VERSION} runtime at {root / 'venv'}")

    data = marker()
    count, _ = model_stats()
    models_current = bool(
        data
        and data.get("docling_version") == DOCLING_VERSION
        and data.get("python_version") == PYTHON_VERSION
        and data.get("models") == MODELS
        and data.get("languages") == LANGUAGES
        and data.get("easyocr_required_files") == EASYOCR_REQUIRED_FILES
        and count > 0
        and easyocr_languages_ready()
    )
    if not models_current:
        models_path().mkdir(parents=True, exist_ok=True)
        run(
            [
                str(docling_tools()),
                "models",
                "download",
                "--output-dir",
                str(models_path()),
                *MODELS,
            ]
        )
        easyocr_dir = models_path() / "EasyOcr"
        easyocr_dir.mkdir(parents=True, exist_ok=True)
        if not easyocr_languages_ready():
            # Docling's generic EasyOCR downloader currently prefetches
            # English/Latin only. Instantiate the complete configured OCR
            # pipeline so both the CRAFT detector and Cyrillic recognizer are
            # independently guaranteed before ingest becomes networkless.
            run(
                [
                    str(venv_python()),
                    "-c",
                    (
                        "import easyocr,sys; "
                        "easyocr.Reader(sys.argv[2].split(','), "
                        "model_storage_directory=sys.argv[1], "
                        "download_enabled=True, detector=True, "
                        "recognizer=True, verbose=False)"
                    ),
                    str(easyocr_dir),
                    ",".join(LANGUAGES),
                ]
            )
        count, total = model_stats()
        if count == 0 or not easyocr_languages_ready():
            print(
                "ERROR: Docling model download completed without the configured ru/en EasyOCR files",
                file=sys.stderr,
            )
            return 3
        marker_payload = {
            "version": 1,
            "installed_at": utc_now(),
            "docling_version": DOCLING_VERSION,
            "python_version": PYTHON_VERSION,
            "models": MODELS,
            "languages": LANGUAGES,
            "easyocr_required_files": EASYOCR_REQUIRED_FILES,
            "models_path": str(models_path()),
            "model_files": count,
            "model_bytes": total,
        }
        temporary = marker_path().with_name(f"{marker_path().name}.tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(marker_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, marker_path())
    else:
        print(f"keep: Docling layout/table/EasyOCR models at {models_path()}")

    payload = status()
    if payload["status"] != "ok":
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    print(f"ready: Docling {DOCLING_VERSION}, OCR languages ru/en")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "check", "install"):
        item = subparsers.add_parser(command)
        if command == "check":
            item.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command == "plan":
        return plan()
    if args.command == "install":
        return install()
    payload = status()
    emit(payload, args.json, 0 if payload["status"] == "ok" else 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
