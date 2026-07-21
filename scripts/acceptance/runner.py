#!/usr/bin/env python3
"""Repo-shipped interactive runner for one release-acceptance matrix row."""

from __future__ import annotations

import argparse
import hashlib
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
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = ROOT / ".vault-meta" / "acceptance" / "runs"
SCENARIOS = ROOT / "evals" / "acceptance" / "scenarios.json"
SKILLS = ROOT / "evals" / "acceptance" / "skills.json"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")

sys.path.insert(0, str(ROOT / "scripts"))
from lib_sanitize import residual_credential_kinds, sanitize  # noqa: E402
from model_routing import capture_session, load_config  # noqa: E402
from pipeline_events import emit_event  # noqa: E402
from task_sessions import TaskSessionError, close_surface_exact, spawn_right  # noqa: E402
from vault_schema import FrontmatterError, parse_frontmatter, split_frontmatter  # noqa: E402
from cmux_agent_support import (  # noqa: E402
    SupervisorError,
    resolved_git_common_dir,
    task_codex_config_values,
    validated_cmux_socket_path,
)
from cmux_trust_prompt import (  # noqa: E402
    claude_background_exit_prompt_visible,
    workspace_trust_prompt_visible,
)


from acceptance.adapters import (
    autoresearch_acceptance_cleanup, bind_review_acceptance_fixture,
    close_acceptance_fixture, close_acceptance_proof, close_fixture_prompt,
    daily_acceptance_cleanup, dispatch_acceptance_fixture,
    dispatch_acceptance_proof, dispatch_fixture_prompt,
    is_disposable_bookkeeping, lifecycle_acceptance_cleanup_proof, prompt_text,
    review_acceptance_fixture, review_fixture_prompt,
    sandbox_cleanup_proof, write_dispatch_acceptance_request,
)
from acceptance.contracts import (
    AcceptanceRunnerError, AcceptanceTransientError, atomic_json, blocked, die, heartbeat, load_scenarios,
    load_skill_fixtures, read_json, result_payload, validate_agent_result,
    validate_row,
)
from acceptance.launchers import (
    AGENT_EXIT_GRACE_SECONDS, CHILD_SURFACE_SETTLE_SECONDS, OUTBOX_MAX_BYTES,
    agent_argv, close_operation_children, close_surface,
    operation_child_surfaces, run_agent_process, send_surface,
    settle_operation_surfaces, settled_outbox, surface_is_open,
    wait_for_operation_children, wait_for_outbox,
)
from acceptance.sandbox import (
    acceptance_seed_sha256, commit_file, create_sandbox,
    disable_acceptance_autocommit, git_head,
    git_output, install_acceptance_model_overrides,
    install_acceptance_runtime_fixture, materialize_seed_commit, run_checked,
    safe_cleanup, scratch_root_for,
)


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


def run_live(row: dict[str, Any], scenario: dict[str, Any], fixture: str) -> dict[str, Any]:
    origin = str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
    if not origin or shutil.which("cmux") is None:
        raise AcceptanceRunnerError("cmux and CMUX_SURFACE_ID are required for live acceptance")
    run_id = str(uuid.uuid4())
    run_dir = STATE_ROOT / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    surface = ""
    cleanup = "sandbox retained for diagnosis"
    prepared_dispatch: dict[str, str] | None = None
    prepared_review: dict[str, str] | None = None
    prepared_close: dict[str, str] | None = None
    stage = "setup"
    stage_started = time.monotonic()
    heartbeat(stage)
    try:
        sandbox, commit = create_sandbox(run_dir)
        install_acceptance_model_overrides(sandbox)
        if row["scenario"] == "dispatch-review-reap":
            install_acceptance_runtime_fixture(sandbox)
        routing_config = load_config(sandbox)
        route = routing_config.runtime_default(row["runtime"])
        acceptance_session_id = f"acceptance-{run_id}"
        capture_session(
            routing_config,
            acceptance_session_id,
            route["runtime"],
            route["model"],
            route["effort"],
            source="acceptance-runner",
        )
        if row["skill"] in {"dispatch", "dispatch-workspace"}:
            prepared_dispatch = dispatch_acceptance_fixture(sandbox, run_id, row["runtime"])
            fixture = dispatch_fixture_prompt(prepared_dispatch)
        elif row["skill"] in {"review-dispatch", "review-send"}:
            prepared_review = review_acceptance_fixture(
                sandbox, run_id, row["runtime"], commit, acceptance_session_id
            )
            fixture = review_fixture_prompt(prepared_review, row["skill"])
        elif row["skill"] == "close":
            prepared_close = close_acceptance_fixture(run_id)
            fixture = close_fixture_prompt(prepared_close)
        scratch_root = scratch_root_for(run_dir)
        scratch_root.mkdir(mode=0o700)
        atomic_json(
            scratch_root / ".acceptance-scratch.json",
            {"schema_version": 1, "run_dir": str(run_dir)},
        )
        outbox = sandbox / ".vault-meta" / "acceptance" / "agent-outbox.json"
        prompt = prompt_text(
            row, scenario, sandbox, outbox, route["model"], route["effort"], commit, fixture,
            prepared_dispatch or prepared_review,
        )
        prompt_path = run_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        spec = {
            "schema_version": 1,
            "row": {key: row[key] for key in ("phase", "skill", "runtime", "scenario", "expected")},
            "runtime": row["runtime"],
            "model": route["model"],
            "effort": route["effort"],
            "session_id": acceptance_session_id,
            "sandbox": str(sandbox),
            "scratch_root": str(scratch_root),
            "prompt_file": str(prompt_path),
        }
        if prepared_dispatch is not None:
            spec["dispatch_fixture"] = prepared_dispatch
        if prepared_review is not None:
            spec["review_fixture"] = prepared_review
        spec_path = run_dir / "operation.json"
        atomic_json(spec_path, spec)
        try:
            created = spawn_right(origin)
        except TaskSessionError as exc:
            raise AcceptanceTransientError("surface-allocation-transient", str(exc)) from exc
        surface = created["surface"]
        spec.update(
            {
                "surface": surface,
                "surface_ref": created.get("surface_ref") or "",
                "status": "running",
            }
        )
        atomic_json(spec_path, spec)
        if prepared_review is not None:
            bind_review_acceptance_fixture(
                sandbox, prepared_review, surface, route, routing_config
            )
        if prepared_dispatch is not None:
            write_dispatch_acceptance_request(
                sandbox,
                prepared_dispatch,
                source_commit=commit,
                coordinator_surface=surface,
                coordinator_model=route["model"],
                coordinator_effort=route["effort"],
                placement="workspace" if row["skill"] == "dispatch-workspace" else "split",
            )
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "agent", "--spec", str(spec_path)])
        send_surface(surface, command)
        emit_event(
            "acceptance-cell-stage", actor="setup", session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)}, root=ROOT,
        )
        stage_started = time.monotonic()
        stage = "model-wait"
        heartbeat(stage)
        raw = wait_for_outbox(
            outbox,
            run_dir / "agent-exit.json",
            int(scenario["timeout_seconds"]),
            surface=surface,
            runtime=row["runtime"],
            activity_paths=(
                sandbox / ".vault-meta" / "pipeline-events.jsonl",
                run_dir / "agent-exit.json",
            ),
        )
        if prepared_close is not None:
            exit_deadline = time.monotonic() + 10.0
            while time.monotonic() < exit_deadline and not (run_dir / "agent-exit.json").is_file():
                time.sleep(0.25)
        emit_event(
            "acceptance-cell-stage", actor="model-wait", session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)}, root=ROOT,
        )
        stage_started = time.monotonic()
        stage = "proof"
        heartbeat(stage)
        result = validate_agent_result(row, raw)
        if result.get("model") != route["model"] or result.get("effort") != route["effort"]:
            result["verdict"] = "blocked"
            result["defect"] = "agent evidence did not report the exact launched model and effort"
        close, children_closed, child_failures = settle_operation_surfaces(
            sandbox, surface, row["runtime"], run_dir / "agent-exit.json"
        )
        if child_failures:
            result["verdict"] = "blocked"
            result["defect"] = "exact operation child surface close failed"
        elif children_closed and result["verdict"] in {"pass", "n-a"}:
            result["verdict"] = "blocked"
            result["defect"] = f"runner had to close {children_closed} leftover operation child surface(s)"
        elif close != "exact surface closed":
            result["verdict"] = "blocked"
            result["defect"] = close
        elif result["verdict"] in {"pass", "n-a"}:
            if prepared_dispatch is not None:
                clean, proof = dispatch_acceptance_proof(sandbox, commit, prepared_dispatch)
            elif prepared_close is not None:
                clean, proof = close_acceptance_proof(sandbox, prepared_close)
                if clean:
                    clean, cleanup_proof = sandbox_cleanup_proof(sandbox, commit)
                    proof = f"{proof}; {cleanup_proof}"
            elif row["skill"] == "autoresearch":
                clean, proof = autoresearch_acceptance_cleanup(sandbox, commit, surface)
                if clean:
                    clean, cleanup_proof = sandbox_cleanup_proof(sandbox, commit)
                    proof = f"{proof}; {cleanup_proof}"
            elif row["skill"] == "daily":
                clean, proof = daily_acceptance_cleanup(sandbox, commit)
            elif row["scenario"] == "dispatch-review-reap":
                clean, proof = lifecycle_acceptance_cleanup_proof(sandbox, commit)
            else:
                clean, proof = sandbox_cleanup_proof(sandbox, commit)
            if not clean:
                result["verdict"] = "blocked"
                result["defect"] = proof
                result["cleanup"] = f"{result['cleanup']}; diagnostic clone retained"[:600]
            else:
                cleanup_started = time.monotonic()
                heartbeat("cleanup")
                safe_cleanup(run_dir)
                emit_event(
                    "acceptance-cell-stage", actor="cleanup", session=run_id,
                    counts={"duration_ms": round((time.monotonic() - cleanup_started) * 1000)}, root=ROOT,
                )
                cleanup = "disposable clone removed; exact surface closed"
                result["cleanup"] = f"{result['cleanup']}; {proof}; {cleanup}"[:600]
                result["evidence"] = f"{result['evidence']}; runner proof: {proof}"[:600]
        else:
            result["cleanup"] = f"{result['cleanup']}; diagnostic clone retained; exact surface closed"[:600]
        spec["status"] = "complete" if result["verdict"] in {"pass", "n-a"} else "blocked"
        spec["verdict"] = result["verdict"]
        atomic_json(run_dir / "result.json", result)
        atomic_json(spec_path, spec)
        emit_event(
            "acceptance-cell-stage", actor="proof", session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
            status="ok" if result["verdict"] in {"pass", "n-a"} else "degraded", root=ROOT,
        )
        return result
    except BaseException as exc:
        emit_event(
            "acceptance-cell-stage", actor=stage, session=run_id,
            counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
            status="error", root=ROOT,
        )
        close = "surface was not created"
        if surface:
            close, _children_closed, _child_failures = settle_operation_surfaces(
                locals().get("sandbox", run_dir / "sandbox"),
                surface,
                row["runtime"],
                run_dir / "agent-exit.json",
                force=isinstance(exc, KeyboardInterrupt),
            )
        spec_path = run_dir / "operation.json"
        if spec_path.is_file():
            try:
                interrupted = read_json(spec_path)
                interrupted["status"] = "interrupted"
                interrupted["cleanup"] = close
                atomic_json(spec_path, interrupted)
            except (AcceptanceRunnerError, OSError):
                pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--skills", type=Path, default=SKILLS)
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
        fixtures = load_skill_fixtures(args.skills.resolve())
        row = validate_row(json.load(sys.stdin), scenarios, fixtures)
        if args.backend_command:
            result = run_with_backend(row, args.backend_command)
        else:
            result = run_live(row, scenarios[row["scenario"]], fixtures[row["skill"]]["fixture"])
    except (AcceptanceRunnerError, SupervisorError, json.JSONDecodeError, OSError, ValueError) as exc:
        if "row" not in locals():
            die(str(exc))
        result = blocked(
            row,
            str(exc),
            failure_kind=(exc.failure_kind if isinstance(exc, AcceptanceTransientError) else ""),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # A valid fail/blocked result is data for release-acceptance.py, not a
    # runner mechanism failure. The matrix aggregator owns the final exit code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
