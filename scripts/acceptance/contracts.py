"""Typed input, output, and fixture-registry contracts for live acceptance."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evals" / "acceptance" / "scenarios.json"
SKILLS = ROOT / "evals" / "acceptance" / "skills.json"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")

sys.path.insert(0, str(ROOT / "scripts"))
from lib_sanitize import residual_credential_kinds, sanitize  # noqa: E402
from model_routing import load_config  # noqa: E402

class AcceptanceRunnerError(ValueError):
    pass


TRANSIENT_FAILURE_KINDS = {
    "agent-capacity",
    "cmux-launch-transient",
    "surface-allocation-transient",
}


class AcceptanceTransientError(AcceptanceRunnerError):
    def __init__(self, failure_kind: str, message: str):
        if failure_kind not in TRANSIENT_FAILURE_KINDS:
            raise ValueError(f"unsupported transient failure kind: {failure_kind}")
        super().__init__(message)
        self.failure_kind = failure_kind


def heartbeat(stage: str, *, status: str = "active", counts: dict[str, int] | None = None) -> None:
    """Write one content-free liveness record when a supervisor requested it."""

    target = str(os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_HEARTBEAT") or "").strip()
    if not target:
        return
    path = Path(target).resolve()
    atomic_json(path, {
        "schema_version": 1,
        "stage": stage[:40],
        "status": status[:24],
        "monotonic_ms": round(time.monotonic() * 1000),
        "counts": counts or {},
    })

def die(message: str, code: int = 3) -> NoReturn:
    print(f"live-acceptance-runner: {message}", file=sys.stderr)
    raise SystemExit(code)

def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceRunnerError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceRunnerError(f"{path} must contain an object")
    return value

def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def load_scenarios(path: Path = SCENARIOS) -> dict[str, dict[str, Any]]:
    raw = read_json(path)
    values = raw.get("scenarios")
    if raw.get("schema_version") != 1 or not isinstance(values, dict):
        raise AcceptanceRunnerError("scenario registry must use schema_version 1")
    result: dict[str, dict[str, Any]] = {}
    for name, item in values.items():
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not isinstance(item, dict):
            raise AcceptanceRunnerError(f"invalid scenario {name!r}")
        timeout = item.get("timeout_seconds")
        instructions = item.get("instructions")
        network = item.get("network")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 60 <= timeout <= 3600:
            raise AcceptanceRunnerError(f"{name}: timeout_seconds must be 60..3600")
        if network not in {"none", "protected", "direct-readonly"}:
            raise AcceptanceRunnerError(f"{name}: invalid network class")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 1000:
            raise AcceptanceRunnerError(f"{name}: invalid instructions")
        result[name] = dict(item)
    return result

def load_skill_fixtures(path: Path = SKILLS) -> dict[str, dict[str, str]]:
    raw = read_json(path)
    values = raw.get("skills")
    if raw.get("schema_version") != 1 or not isinstance(values, dict):
        raise AcceptanceRunnerError("skill fixture registry must use schema_version 1")
    result: dict[str, dict[str, str]] = {}
    for name, item in values.items():
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not isinstance(item, dict):
            raise AcceptanceRunnerError(f"invalid skill fixture {name!r}")
        scenario = item.get("scenario")
        expected = item.get("expected")
        fixture = item.get("fixture")
        if not isinstance(scenario, str) or not SAFE_ID.fullmatch(scenario):
            raise AcceptanceRunnerError(f"{name}: invalid fixture scenario")
        if not isinstance(expected, str) or not expected.strip() or len(expected) > 300:
            raise AcceptanceRunnerError(f"{name}: invalid fixture expectation")
        if not isinstance(fixture, str) or not fixture.strip() or len(fixture) > 1000:
            raise AcceptanceRunnerError(f"{name}: invalid live fixture")
        result[name] = {
            "scenario": scenario,
            "expected": expected.strip(),
            "fixture": fixture.strip(),
        }
    discovered = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
    if set(result) != discovered:
        raise AcceptanceRunnerError("skill fixture registry does not exactly cover installed skills")
    return result

def validate_row(
    value: Any,
    scenarios: dict[str, dict[str, Any]],
    fixtures: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise AcceptanceRunnerError("input row must be a schema_version 1 object")
    for key in ("phase", "skill", "runtime", "scenario", "expected"):
        if not isinstance(value.get(key), str) or not str(value[key]).strip():
            raise AcceptanceRunnerError(f"row {key} is required")
    if value["phase"] not in {"baseline", "final"}:
        raise AcceptanceRunnerError("row phase is invalid")
    if value["runtime"] not in {"claude", "codex"}:
        raise AcceptanceRunnerError("row runtime is invalid")
    if value["scenario"] not in scenarios:
        raise AcceptanceRunnerError("row scenario is not registered")
    if not SAFE_ID.fullmatch(value["skill"]):
        raise AcceptanceRunnerError("row skill is invalid")
    if not (ROOT / "skills" / value["skill"] / "SKILL.md").is_file():
        raise AcceptanceRunnerError("row skill is not installed in the source checkout")
    fixture = fixtures.get(value["skill"])
    if fixture is None:
        raise AcceptanceRunnerError("row skill has no registered live fixture")
    if fixture["scenario"] != value["scenario"]:
        raise AcceptanceRunnerError("row scenario does not match the registered live fixture")
    if fixture["expected"] != value["expected"]:
        raise AcceptanceRunnerError("row expected result does not match the registered live fixture")
    return value

def result_payload(
    row: dict[str, Any], *, verdict: str, model: str, effort: str,
    actual: str, cleanup: str, evidence: str, defect: str = "", decision: str = "",
    failure_kind: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        **{key: row[key] for key in ("schema_version", "phase", "skill", "runtime", "scenario", "expected")},
        "verdict": verdict,
        "model": model,
        "effort": effort,
        "actual": actual,
        "cleanup": cleanup,
        "evidence": evidence,
    }
    if defect:
        value["defect"] = defect
    if decision:
        value["decision"] = decision
    if failure_kind:
        value["failure_kind"] = failure_kind
    return value

def validate_agent_result(row: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise AcceptanceRunnerError("agent outbox must contain a schema_version 1 object")
    for key in ("phase", "skill", "runtime", "scenario"):
        if raw.get(key) != row[key]:
            raise AcceptanceRunnerError(f"agent outbox {key} does not match the operation")
    verdict = raw.get("verdict")
    if verdict not in {"pass", "fail", "blocked", "n-a"}:
        raise AcceptanceRunnerError("agent outbox verdict is invalid")
    result = result_payload(
        row,
        verdict=verdict,
        model=str(raw.get("model") or ""),
        effort=str(raw.get("effort") or ""),
        actual=str(raw.get("actual") or ""),
        cleanup=str(raw.get("cleanup") or ""),
        evidence=str(raw.get("evidence") or ""),
        defect=str(raw.get("defect") or ""),
        decision=str(raw.get("decision") or ""),
        failure_kind=str(raw.get("failure_kind") or ""),
    )
    for key, value in result.items():
        if isinstance(value, str):
            cleaned, _ = sanitize(value)
            if residual_credential_kinds(cleaned):
                raise AcceptanceRunnerError(f"agent outbox {key} contains credential-like text")
            result[key] = cleaned[:600]
    if verdict in {"pass", "fail"} and any(not result[key].strip() for key in ("model", "effort", "actual", "cleanup", "evidence")):
        raise AcceptanceRunnerError(f"{verdict} result lacks bounded evidence fields")
    if verdict in {"fail", "blocked"} and not result.get("defect"):
        raise AcceptanceRunnerError(f"{verdict} result lacks defect")
    if verdict == "n-a" and not result.get("decision"):
        raise AcceptanceRunnerError("n-a result lacks decision")
    failure_kind = str(result.get("failure_kind") or "")
    if failure_kind and failure_kind not in TRANSIENT_FAILURE_KINDS:
        raise AcceptanceRunnerError("agent outbox failure_kind is not retryable")
    if failure_kind and verdict != "blocked":
        raise AcceptanceRunnerError("failure_kind is valid only for blocked results")
    return result

def blocked(row: dict[str, Any], message: str, *, failure_kind: str = "") -> dict[str, Any]:
    try:
        route = load_config(ROOT).runtime_default(row["runtime"])
    except Exception:
        route = {"model": "unknown", "effort": "unknown"}
    override = str(
        os.environ.get(f"LLM_OBSIDIAN_ACCEPTANCE_{str(row['runtime']).upper()}_MODEL") or ""
    ).strip()
    if override:
        route = {**route, "model": override}
    effort_override = str(os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_EFFORT") or "").strip()
    if effort_override:
        route = {**route, "effort": effort_override}
    clean, _ = sanitize(message[:300])
    return result_payload(
        row, verdict="blocked", model=route["model"], effort=route["effort"],
        actual="Live acceptance cell did not complete.", cleanup="diagnostic state retained",
        evidence="runner boundary", defect=clean, failure_kind=failure_kind,
    )
