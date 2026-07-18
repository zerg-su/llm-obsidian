#!/usr/bin/env python3
"""Cheap once-per-session readiness check; never installs or mutates dependencies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_routing import RoutingError, capture_session, load_config, routing_from_environment, sync_native


def probe(command: list[str], *, seconds: float = 2.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, text=True, capture_output=True, timeout=seconds, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def runtime_binary(runtime: str) -> str:
    return "codex" if runtime == "codex" else "claude"


def run_preflight(root: Path, *, session_id: str, runtime: str, model: str, effort: str) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    config = load_config(root)
    route, route_source = routing_from_environment(config, runtime, model, effort)
    if session_id and session_id != "unknown":
        capture_session(config, session_id, **route, source=route_source)

    if sys.version_info < (3, 11):
        issues.append({"id": "python", "severity": "required", "repair": "brew install python@3.14"})
    for binary, repair in (
        ("git", "xcode-select --install"),
        (runtime_binary(route["runtime"]), f"install {runtime_binary(route['runtime'])} CLI and restart the session"),
        ("cmux", "brew install cmux"),
    ):
        if shutil.which(binary) is None:
            issues.append({"id": binary, "severity": "required" if binary != "cmux" else "feature", "repair": repair})

    drift = sync_native(config, apply=False)
    if drift:
        issues.append({"id": "routing-drift", "severity": "required", "repair": "python3 scripts/model_routing.py sync-native --apply"})

    adapter = probe([sys.executable, str(root / "scripts/codex-adapter.py"), "--repo-root", str(root), "--check"])
    if adapter is None or adapter.returncode != 0:
        issues.append({"id": "codex-adapter", "severity": "feature", "repair": "python3 scripts/codex-adapter.py --apply"})

    ollama = shutil.which("ollama")
    if ollama is None:
        issues.append({"id": "ollama", "severity": "retrieval", "repair": "brew install ollama && ollama serve"})
        issues.append({"id": "bge-m3", "severity": "retrieval", "repair": "ollama pull bge-m3"})
    else:
        listed = probe([ollama, "list"])
        if listed is None or listed.returncode != 0:
            issues.append({"id": "ollama-service", "severity": "retrieval", "repair": "ollama serve"})
        elif not any(line.split()[0].split(":", 1)[0] == "bge-m3" for line in listed.stdout.splitlines()[1:] if line.split()):
            issues.append({"id": "bge-m3", "severity": "retrieval", "repair": "ollama pull bge-m3"})

    return {
        "schema_version": 1,
        "status": "ready" if not issues else "degraded",
        "routing": {**route, "source": route_source, "config_sha256": config.fingerprint, "local_override": config.local_override},
        "issues": issues,
        "retrieval": "sparse-fallback" if any(item["severity"] == "retrieval" for item in issues) else "hybrid",
        "blocking": False,
    }


def render(payload: dict[str, Any]) -> str:
    route = payload["routing"]
    issue_ids = ",".join(item["id"] for item in payload["issues"]) or "none"
    repairs = " | ".join(dict.fromkeys(item["repair"] for item in payload["issues"])) or "none"
    local = ",local-override" if route["local_override"] else ""
    return (
        f"SESSION_PREFLIGHT: {payload['status']} routing={route['runtime']}/{route['model']}/{route['effort']}"
        f" source={route['source']}{local} retrieval={payload['retrieval']} issues={issue_ids}; repair: {repairs}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--runtime", choices=["codex", "claude"], default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        payload = run_preflight(args.root.resolve(), session_id=args.session_id, runtime=args.runtime, model=args.model, effort=args.effort)
    except RoutingError as exc:
        payload = {"schema_version": 1, "status": "degraded", "routing": {"runtime": args.runtime or "unknown", "model": args.model or "unknown", "effort": args.effort or "unknown", "source": "error", "local_override": False}, "issues": [{"id": "routing", "severity": "required", "repair": "python3 scripts/model_routing.py check"}], "retrieval": "sparse-fallback", "blocking": False, "error": type(exc).__name__}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json else render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
