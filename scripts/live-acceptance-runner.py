#!/usr/bin/env python3
"""Repo-shipped interactive runner for one release-acceptance matrix row."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = ROOT / ".vault-meta" / "acceptance" / "runs"
SCENARIOS = ROOT / "evals" / "acceptance" / "scenarios.json"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
SURFACE_RE = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
)

sys.path.insert(0, str(ROOT / "scripts"))
from lib_sanitize import residual_credential_kinds, sanitize  # noqa: E402
from model_routing import load_config  # noqa: E402
from task_sessions import TaskSessionError, spawn_right  # noqa: E402
from cmux_agent_supervisor import (  # noqa: E402
    SupervisorError,
    task_codex_config_values,
    validated_cmux_socket_path,
    workspace_trust_prompt_visible,
)


class AcceptanceRunnerError(ValueError):
    pass


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


def validate_row(value: Any, scenarios: dict[str, dict[str, Any]]) -> dict[str, Any]:
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
    return value


def result_payload(
    row: dict[str, Any], *, verdict: str, model: str, effort: str,
    actual: str, cleanup: str, evidence: str, defect: str = "", decision: str = "",
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
    return result


def git_head() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise AcceptanceRunnerError(
            "source checkout must be clean so live cells test the committed release candidate"
        )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}\n?", result.stdout):
        raise AcceptanceRunnerError("cannot resolve the committed source HEAD")
    return result.stdout.strip()


def create_sandbox(run_dir: Path) -> tuple[Path, str]:
    sandbox = run_dir / "sandbox"
    commit = git_head()
    cloned = subprocess.run(
        ["git", "clone", "--shared", "--no-hardlinks", "--quiet", str(ROOT), str(sandbox)],
        text=True,
        capture_output=True,
        check=False,
    )
    if cloned.returncode != 0:
        raise AcceptanceRunnerError(cloned.stderr.strip() or "local acceptance clone failed")
    checked = subprocess.run(
        ["git", "checkout", "--detach", "--quiet", commit], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if checked.returncode != 0:
        raise AcceptanceRunnerError(checked.stderr.strip() or "acceptance checkout failed")
    atomic_json(sandbox / ".acceptance-sandbox.json", {"schema_version": 1, "run_dir": str(run_dir), "commit": commit})
    return sandbox, commit


def prompt_text(
    row: dict[str, Any], scenario: dict[str, Any], sandbox: Path, outbox: Path,
    model: str, effort: str, commit: str,
) -> str:
    return f"""# Live release acceptance operation

You are running one real, bounded acceptance cell in a disposable local clone.

- Phase: `{row['phase']}`
- Runtime: `{row['runtime']}`
- Effective model: `{model}`
- Effective effort: `{effort}`
- Skill: `{row['skill']}`
- Scenario: `{row['scenario']}`
- Expected: {row['expected']}
- Network class: `{scenario['network']}`
- Source commit: `{commit}`

Read `{sandbox / 'skills' / row['skill'] / 'SKILL.md'}` completely and exercise that skill faithfully.
Scenario instructions: {scenario['instructions']}

Hard boundaries:

- Work only inside `{sandbox}` and disposable nested paths it creates.
- Never push, publish, deploy, send communication, use credentials, or mutate the source checkout.
- A public web read is allowed only when the declared network class permits it.
- If authentication is required, return `blocked` and name only the credential class; never print a value.
- Install nothing unless it is already covered by an explicit local noninteractive fixture. Missing optional dependencies must produce a visible blocked/degraded result.
- Clean every disposable page, branch, worktree, surface, process, and scratch file you create before reporting pass.
- Do not remove `.acceptance-sandbox.json`; it is the runner-owned cleanup marker.
- Preserve real first-failure evidence; do not turn a retry into a clean pass without mentioning it.

Finally write exactly one JSON object to `{outbox}` using this shape:

```json
{{
  "schema_version": 1,
  "phase": "{row['phase']}",
  "skill": "{row['skill']}",
  "runtime": "{row['runtime']}",
  "scenario": "{row['scenario']}",
  "verdict": "pass | fail | blocked | n-a",
  "model": "{model}",
  "effort": "{effort}",
  "actual": "bounded observed behavior",
  "cleanup": "bounded cleanup proof",
  "evidence": "bounded commands/artifacts/status proof without content or secrets",
  "defect": "required for fail/blocked",
  "decision": "required for n-a"
}}
```

Do not merely describe a hypothetical test. The outbox is the final action after cleanup.
"""


def agent_argv(runtime: str, sandbox: Path, model: str, effort: str, prompt: str) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    env["LLM_OBSIDIAN_ACCEPTANCE"] = "1"
    env["LLM_OBSIDIAN_WORKTREES"] = str(sandbox / ".vault-meta" / "acceptance-worktrees")
    env["DCG_CONFIG"] = str(sandbox / "config" / "dcg" / "task.toml")
    if runtime == "claude":
        return ["claude", "--permission-mode", "auto", "--model", model, "--effort", effort, prompt], env
    socket = validated_cmux_socket_path()
    argv = [
        "codex", "--cd", str(sandbox), "-a", "never", "-s", "workspace-write",
        "--model", model,
    ]
    for value in task_codex_config_values(socket, effort):
        argv.extend(["-c", value])
    dispatch_env = sandbox / ".codex" / "dispatch-env.toml"
    if dispatch_env.is_file() and sys.version_info >= (3, 11):
        import tomllib

        raw = tomllib.loads(dispatch_env.read_text(encoding="utf-8")).get("codex_dispatch", {})
        profile = str(raw.get("profile") or "").strip() if isinstance(raw, dict) else ""
        codex_home = str(raw.get("codex_home") or "").strip() if isinstance(raw, dict) else ""
        if profile:
            argv.extend(["--profile", profile])
        if codex_home:
            env["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
    env["CMUX_SOCKET_PATH"] = str(socket)
    argv.append(prompt)
    return argv, env


def run_agent_process(spec_path: Path) -> int:
    spec = read_json(spec_path)
    run_dir = spec_path.parent.resolve()
    sandbox = Path(str(spec.get("sandbox") or "")).resolve()
    prompt_path = Path(str(spec.get("prompt_file") or "")).resolve()
    if sandbox.parent != run_dir or not (sandbox / ".acceptance-sandbox.json").is_file():
        raise AcceptanceRunnerError("acceptance sandbox is not bound to its run directory")
    if prompt_path != run_dir / "prompt.md" or not prompt_path.is_file():
        raise AcceptanceRunnerError("acceptance prompt is not operation-scoped")
    runtime = str(spec.get("runtime") or "")
    config = load_config(sandbox)
    route = config.runtime_default(runtime)
    if route["model"] != spec.get("model") or route["effort"] != spec.get("effort"):
        raise AcceptanceRunnerError("acceptance route drifted after preparation")
    argv, env = agent_argv(runtime, sandbox, route["model"], route["effort"], prompt_path.read_text(encoding="utf-8"))
    try:
        return subprocess.run(argv, cwd=sandbox, env=env, check=False).returncode
    finally:
        atomic_json(run_dir / "agent-exit.json", {"schema_version": 1, "finished": True})


def send_surface(surface: str, text: str, *, submit_key: str = "Enter") -> None:
    for argv in (
        ["cmux", "send", "--surface", surface, text],
        ["cmux", "send-key", "--surface", surface, submit_key],
    ):
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise AcceptanceRunnerError((result.stdout + result.stderr).strip() or "cmux send failed")


def wait_for_outbox(
    outbox: Path, exit_marker: Path, timeout: int, *, surface: str, runtime: str
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    trust_accepted = False
    while time.monotonic() < deadline:
        if outbox.is_file():
            return read_json(outbox)
        if exit_marker.is_file():
            raise AcceptanceRunnerError("acceptance agent exited before writing its outbox")
        if not trust_accepted:
            screen = subprocess.run(
                ["cmux", "read-screen", "--surface", surface, "--lines", "80"],
                text=True,
                capture_output=True,
                check=False,
            )
            if screen.returncode == 0 and workspace_trust_prompt_visible(runtime, screen.stdout):
                accepted = subprocess.run(
                    ["cmux", "send-key", "--surface", surface, "Enter"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if accepted.returncode != 0:
                    raise AcceptanceRunnerError("exact workspace trust prompt could not be accepted")
                trust_accepted = True
        time.sleep(1)
    raise AcceptanceRunnerError("acceptance agent timed out")


def close_surface(surface: str, runtime: str, exit_marker: Path) -> str:
    try:
        if runtime == "codex":
            for _ in range(40):
                subprocess.run(["cmux", "send-key", "--surface", surface, "backspace"], capture_output=True, check=False)
            send_surface(surface, "/exit", submit_key="tab")
            subprocess.run(["cmux", "send-key", "--surface", surface, "Enter"], capture_output=True, check=False)
        else:
            send_surface(surface, "/exit")
    except AcceptanceRunnerError:
        return "exit-request-failed; surface left visible"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not exit_marker.is_file():
        time.sleep(0.5)
    if not exit_marker.is_file():
        return "agent did not exit; surface left visible"
    closed = subprocess.run(
        ["cmux", "close-surface", "--surface", surface], text=True, capture_output=True, check=False
    )
    text = (closed.stdout + closed.stderr).lower()
    if closed.returncode != 0 and not any(token in text for token in ("not found", "not_found", "unknown surface")):
        return "exact surface close failed; surface left visible"
    return "exact surface closed"


def safe_cleanup(run_dir: Path) -> None:
    sandbox = run_dir / "sandbox"
    marker = sandbox / ".acceptance-sandbox.json"
    if sandbox.is_dir() and marker.is_file() and sandbox.parent == run_dir:
        shutil.rmtree(sandbox)


def sandbox_cleanup_proof(sandbox: Path, commit: str) -> tuple[bool, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if head.returncode != 0 or head.stdout.strip() != commit:
        return False, "disposable clone HEAD changed"
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if status.returncode != 0:
        return False, "disposable clone status is unreadable"
    dirty = [
        line for line in status.stdout.splitlines()
        if line[3:] != ".acceptance-sandbox.json"
    ]
    if dirty:
        return False, "disposable clone retained product or vault changes"
    nested = sandbox / ".vault-meta" / "acceptance-worktrees"
    if nested.is_dir() and any(nested.iterdir()):
        return False, "nested acceptance worktrees remain"
    return True, "committed HEAD and worktree restored"


def run_with_backend(row: dict[str, Any], command: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        command, input=json.dumps(row, ensure_ascii=False) + "\n", text=True,
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AcceptanceRunnerError(f"test backend exited {proc.returncode}")
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceRunnerError(f"test backend returned invalid JSON: {exc}") from exc
    return validate_agent_result(row, raw)


def run_live(row: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    origin = str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
    if not origin or shutil.which("cmux") is None:
        raise AcceptanceRunnerError("cmux and CMUX_SURFACE_ID are required for live acceptance")
    config = load_config(ROOT)
    route = config.runtime_default(row["runtime"])
    run_id = str(uuid.uuid4())
    run_dir = STATE_ROOT / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    surface = ""
    cleanup = "sandbox retained for diagnosis"
    try:
        sandbox, commit = create_sandbox(run_dir)
        outbox = sandbox / ".vault-meta" / "acceptance-outbox.json"
        prompt = prompt_text(row, scenario, sandbox, outbox, route["model"], route["effort"], commit)
        prompt_path = run_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        spec = {
            "schema_version": 1,
            "runtime": row["runtime"],
            "model": route["model"],
            "effort": route["effort"],
            "sandbox": str(sandbox),
            "prompt_file": str(prompt_path),
        }
        spec_path = run_dir / "operation.json"
        atomic_json(spec_path, spec)
        try:
            created = spawn_right(origin)
        except TaskSessionError as exc:
            raise AcceptanceRunnerError(str(exc)) from exc
        surface = created["surface"]
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "agent", "--spec", str(spec_path)])
        send_surface(surface, command)
        raw = wait_for_outbox(
            outbox,
            run_dir / "agent-exit.json",
            int(scenario["timeout_seconds"]),
            surface=surface,
            runtime=row["runtime"],
        )
        result = validate_agent_result(row, raw)
        close = close_surface(surface, row["runtime"], run_dir / "agent-exit.json")
        if close != "exact surface closed":
            result["verdict"] = "blocked"
            result["defect"] = close
        elif result["verdict"] in {"pass", "n-a"}:
            clean, proof = sandbox_cleanup_proof(sandbox, commit)
            if not clean:
                result["verdict"] = "blocked"
                result["defect"] = proof
                result["cleanup"] = f"{result['cleanup']}; diagnostic clone retained"[:600]
            else:
                safe_cleanup(run_dir)
                cleanup = "disposable clone removed; exact surface closed"
                result["cleanup"] = f"{result['cleanup']}; {proof}; {cleanup}"[:600]
        else:
            result["cleanup"] = f"{result['cleanup']}; diagnostic clone retained; exact surface closed"[:600]
        return result
    except BaseException:
        if surface:
            close_surface(surface, row["runtime"], run_dir / "agent-exit.json")
        raise


def blocked(row: dict[str, Any], message: str) -> dict[str, Any]:
    try:
        route = load_config(ROOT).runtime_default(row["runtime"])
    except Exception:
        route = {"model": "unknown", "effort": "unknown"}
    clean, _ = sanitize(message[:300])
    return result_payload(
        row, verdict="blocked", model=route["model"], effort=route["effort"],
        actual="Live acceptance cell did not complete.", cleanup="diagnostic state retained",
        evidence="runner boundary", defect=clean,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--backend-command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    agent = sub.add_parser("agent", help=argparse.SUPPRESS)
    agent.add_argument("--spec", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "agent":
            return run_agent_process(args.spec.resolve())
        scenarios = load_scenarios(args.scenarios.resolve())
        row = validate_row(json.load(sys.stdin), scenarios)
        if args.backend_command:
            result = run_with_backend(row, args.backend_command)
        else:
            result = run_live(row, scenarios[row["scenario"]])
    except (AcceptanceRunnerError, SupervisorError, json.JSONDecodeError, OSError, ValueError) as exc:
        if "row" not in locals():
            die(str(exc))
        result = blocked(row, str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # A valid fail/blocked result is data for release-acceptance.py, not a
    # runner mechanism failure. The matrix aggregator owns the final exit code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
