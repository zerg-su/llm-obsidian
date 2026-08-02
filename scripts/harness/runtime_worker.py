"""Short-lived supervisor for one provider process inside an owned cmux surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters.cmux import CmuxAdapter
from .adapters.process import ProcessAdapter, ProcessError, ProcessHandle
from .callbacks import (
    REVIEWER_PROFILES,
    CallbackBroker,
    CallbackError,
    CallbackTimeoutError,
)
from .contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError as HarnessContractError,
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    to_dict,
)
from .prompts import PromptDecision, classify
from .pipeline_builtins import compiled_executable_for_contract
from .pipeline_builtins import builtin_registry
from .custom_pipelines import (
    CustomPipelinePolicy,
    resolve_custom_executable,
)
from .liveness import (
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
)
from .pipelines import reconcile_pipeline
from .review_finalization import task_review_status
from .state_machine import TERMINAL
from .store import OperationStore, StoreError
from .supervisor import OperationSupervisor, SupervisorError
from .runtime_worker_contracts import (
    IDENTIFIER,
    SURFACE_UUID,
    RuntimeWorkerError,
)
from .runtime_worker_spec import load_spec
from .verification import (
    VerificationError,
    compose_commands,
    load_profiles,
    run_profile,
)
from .workflows.engineering_fix import (
    FixStepReceipt,
    FixWorkflowError,
    accept_phase,
    load_receipt,
    prepare_next_phase,
    prepare_retry_phase,
    reconcile_fix,
    reconcile_retry_fix,
)
from .workflows.custom_sequence import (
    CustomSequenceError,
    CustomStepReceipt,
    accept_custom_step,
    custom_step_request,
    load_custom_receipt,
    prepare_custom_step,
    reconcile_custom_sequence,
)
from research_contract import (
    ResearchContractError,
    load_artifact,
    validate_result_artifact,
)
from lifecycle_telemetry import emit_compiled_pipeline_event, emit_lifecycle_event
from review_resolution import (
    DISPOSITIONS,
    MATERIAL_SEVERITIES,
    ResolutionError,
    review_transport_identity_sha256,
)
from task_contract import ContractError, validate_handoff
from wiki_summary_contract import WikiSummaryError, validate_summary_for_task


MAX_OUTBOX_BYTES = 70_000
MAX_SCREEN_BYTES = 70_000
MAX_PIPELINE_VERIFY_RESUBMITS = 1
RESEARCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
CALLBACK_WAIT_STATES = frozenset(
    {"running", "awaiting-callback", "verifying"}
)


def _review_resolution_handoff_ready(
    *,
    worktree: Path,
    operation_id: str,
    gate_state: Mapping[str, object],
    current_head: str,
) -> bool:
    """Return true only after the executor publishes one complete fix handoff."""

    awaiting = gate_state.get("awaiting_resolution")
    if not isinstance(awaiting, dict) or not awaiting:
        return False
    reviewed_heads = {
        str(boundary.get("reviewed_head_sha") or "")
        for boundary in awaiting.values()
        if isinstance(boundary, dict)
    }
    expected_finding_ids: list[str] = []
    review_operation_ids: set[str] = set()
    review_callbacks: list[dict[str, object]] = []
    for axis in sorted(awaiting):
        boundary = awaiting[axis]
        if not isinstance(boundary, dict):
            return False
        material_ids = boundary.get("material_finding_ids")
        if (
            not isinstance(material_ids, list)
            or any(
                not isinstance(finding_id, str) or not finding_id
                for finding_id in material_ids
            )
        ):
            return False
        expected_finding_ids.extend(material_ids)
        review_operation_ids.add(
            str(boundary.get("review_operation_id") or "")
        )
        review_callbacks.append(
            {
                "axis": axis,
                "round_operation_id": str(
                    boundary.get("round_operation_id") or ""
                ),
                "round_run_id": str(
                    boundary.get("round_run_id") or ""
                ),
                "callback_id": str(boundary.get("callback_id") or ""),
                "callback_sha256": str(
                    boundary.get("callback_sha256") or ""
                ),
            }
        )
    active_review_operation_id = str(
        gate_state.get("active_review_operation_id") or ""
    )
    try:
        review_identity_sha256 = review_transport_identity_sha256(
            active_review_operation_id, review_callbacks
        )
    except ResolutionError:
        return False
    if (
        len(reviewed_heads) != 1
        or "" in reviewed_heads
        or not expected_finding_ids
        or len(expected_finding_ids) != len(set(expected_finding_ids))
        or review_operation_ids != {active_review_operation_id}
    ):
        return False
    resolution_path = worktree / ".task-review-resolution.json"
    if not resolution_path.is_file() or resolution_path.is_symlink():
        return False
    try:
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    items = resolution.get("resolutions") if isinstance(resolution, dict) else None
    if (
        resolution.get("schema_version") != 1
        or resolution.get("operation_id") != operation_id
        or resolution.get("reviewed_head_sha") != next(iter(reviewed_heads))
        or resolution.get("resolved_head_sha") != current_head
        or resolution.get("review_identity_sha256")
        != review_identity_sha256
        or not isinstance(items, list)
        or not items
        or [
            item.get("finding_id")
            for item in items
            if isinstance(item, dict)
        ]
        != expected_finding_ids
    ):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("finding_id"), str)
        and bool(item["finding_id"])
        and item.get("disposition") in DISPOSITIONS
        and isinstance(item.get("rationale"), str)
        and bool(item["rationale"])
        and isinstance(item.get("follow_up"), str)
        and (
            item["disposition"] != "out-of-scope"
            or bool(item["follow_up"])
        )
        for item in items
    )


def _pipeline_verify_identity(
    parent: OperationSpec,
    *,
    definition_sha256: str,
    input_sha256: str,
    profile: str,
) -> tuple[OperationSpec, str, str]:
    """Derive one immutable verify operation from its exact pipeline input."""

    suffix = f"-verify-{input_sha256[:16]}"
    operation_id = f"{parent.operation_id[: 128 - len(suffix)]}{suffix}"
    idempotency_key = hashlib.sha256(
        (
            f"{parent.idempotency_key}:pipeline-verify:{operation_id}:"
            f"{definition_sha256}:{input_sha256}:{profile}"
        ).encode()
    ).hexdigest()
    child = OperationSpec(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        kind="pipeline-verify",
        owner_id=parent.owner_id,
        route=parent.route,
        context_manifest=parent.context_manifest,
        verification_profile=profile,
        keep_open=False,
        contract_sha256=definition_sha256,
    )
    lane_id = hashlib.sha256(
        f"{idempotency_key}:lane".encode()
    ).hexdigest()[:32]
    run_id = hashlib.sha256(
        f"{idempotency_key}:run".encode()
    ).hexdigest()[:32]
    return child, lane_id, run_id


def provider_exit_is_final(
    *,
    provider_exited: bool,
    callback_mode: str,
    callback_handled: bool,
    operation_state: str,
    operation_profile: str,
    callback_deadline_at: float,
) -> bool:
    """Keep callback transports alive until handled or durably stopped."""

    if not provider_exited:
        return False
    if callback_handled:
        return True
    if (
        callback_mode == "task-summary"
        or (
            operation_profile in REVIEWER_PROFILES
            and callback_deadline_at > 0
        )
    ):
        return operation_state in {
            "attention-required",
            "cancelling",
            "exiting",
            *TERMINAL,
        }
    return True


def enforce_callback_deadline(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    *,
    callback_handled: bool,
    now: float | None = None,
) -> bool:
    """Turn an expired live reviewer wait into durable typed attention."""

    record = store.read(owner_id, operation_id)
    if (
        callback_handled
        or record.spec.route.profile not in REVIEWER_PROFILES
        or record.state not in CALLBACK_WAIT_STATES
        or not record.deadline_at
    ):
        return False
    try:
        OperationSupervisor(
            store, owner_id, operation_id
        ).check_budget(
            now=now,
            timeout_reason=AttentionReason.CALLBACK_TIMEOUT,
        )
    except SupervisorError:
        current = store.read(owner_id, operation_id)
        return (
            current.state == "attention-required"
            and current.attention_reason
            == AttentionReason.CALLBACK_TIMEOUT
        )
    return False


def provider_argv(
    spec: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Select cmux's ephemeral wrapper only inside its exact owned surface."""

    argv = tuple(spec.get("argv") or ())
    runtime = str(spec.get("runtime") or "")
    surface_id = str(spec.get("surface_id") or "")
    values = os.environ if env is None else env
    runtime_interpreter = spec.get("runtime_interpreter")
    pinned_interpreter = (
        runtime_interpreter
        if isinstance(runtime_interpreter, Path)
        else None
    )
    if (
        not argv
        or runtime not in {"claude", "codex"}
        or not SURFACE_UUID.fullmatch(surface_id)
    ):
        return argv
    if spec.get("callback_mode") in {"research-fetch", "research-synth"}:
        return _pin_env_shebang(argv, values, pinned_interpreter)
    prefix = f"CMUX_{runtime.upper()}_WRAPPER_SHIM"
    raw_wrapper = str(values.get(prefix) or "").strip()
    raw_root = str(values.get(f"{prefix}_ROOT") or "").strip()
    if (
        not raw_wrapper
        or not raw_root
        or str(values.get("CMUX_SURFACE_ID") or "").casefold()
        != surface_id.casefold()
    ):
        return _pin_env_shebang(argv, values, pinned_interpreter)
    candidate = Path(raw_wrapper).expanduser()
    root = Path(raw_root).expanduser()
    try:
        if candidate.is_symlink() or root.is_symlink():
            return _pin_env_shebang(argv, values, pinned_interpreter)
        candidate = candidate.resolve()
        root = root.resolve()
        candidate_stat = candidate.stat()
        root_stat = root.stat()
    except OSError:
        return _pin_env_shebang(argv, values, pinned_interpreter)
    if (
        candidate.name != runtime
        or candidate.parent != root
        or root.name.casefold() != surface_id.casefold()
        or "cmux-cli-shims" not in root.parts
        or not candidate.is_file()
        or not root.is_dir()
        or not os.access(candidate, os.X_OK)
        or candidate_stat.st_uid != os.getuid()
        or root_stat.st_uid != os.getuid()
        or candidate_stat.st_mode & 0o022
        or root_stat.st_mode & 0o022
    ):
        return _pin_env_shebang(argv, values, pinned_interpreter)
    return _pin_env_shebang(
        (str(candidate), *argv[1:]), values, pinned_interpreter
    )


def _pin_env_shebang(
    argv: tuple[str, ...],
    env: Mapping[str, str],
    pinned_interpreter: Path | None = None,
) -> tuple[str, ...]:
    """Resolve one env shebang before protected runtimes sanitize PATH."""

    if not argv:
        return argv
    try:
        with Path(argv[0]).open("rb") as handle:
            first_line = handle.readline(256)
    except OSError:
        return argv
    match = re.fullmatch(
        rb"#![ \t]*/usr/bin/env[ \t]+([A-Za-z0-9._+-]+)[ \t]*\r?\n?",
        first_line,
    )
    if match is None:
        return argv
    interpreter_name = match.group(1).decode("ascii")
    if pinned_interpreter is not None:
        resolved = (
            pinned_interpreter
            if pinned_interpreter.name == interpreter_name
            else None
        )
    else:
        interpreter = shutil.which(interpreter_name, path=env.get("PATH"))
        resolved = (
            Path(interpreter).expanduser().resolve()
            if interpreter
            else None
        )
    if (
        resolved is None
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        return argv
    return (str(resolved), argv[0], *argv[1:])


def provider_resume_argv(
    argv: tuple[str, ...], runtime: str, checkpoint: str
) -> tuple[str, ...]:
    """Bind one provider restart to the exact previously captured session."""

    if not argv or runtime not in {"claude", "codex"}:
        raise RuntimeWorkerError("provider resume runtime is invalid")
    if not IDENTIFIER.fullmatch(checkpoint):
        raise RuntimeWorkerError("provider restart requires an exact checkpoint")
    if runtime == "claude":
        try:
            separator = len(argv) - 1 - tuple(reversed(argv)).index("--")
        except ValueError as exc:
            raise RuntimeWorkerError(
                "Claude provider command lacks the prompt separator"
            ) from exc
        return (
            *argv[:separator],
            "--resume",
            checkpoint,
            "--",
        )
    if len(argv) < 2:
        raise RuntimeWorkerError("Codex provider command lacks its prompt")
    return (*argv[:-1], "resume", checkpoint)


def publish_callback_wake(
    spec: dict[str, Any],
    state_root: Path,
    callback_id: str,
    cmux_adapter: object,
) -> bool:
    """Publish one idempotent coordinator wake after durable acceptance."""

    wake = str(spec.get("callback_wake") or "")
    if not wake:
        return True
    notify_path = state_root / "callback-wake.json"
    notified: dict[str, object] = {}
    if notify_path.is_file() and not notify_path.is_symlink():
        value = json.loads(notify_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeWorkerError("callback wake marker is invalid")
        notified = value
    if (
        notified.get("callback_id") == callback_id
        and notified.get("status") == "sent"
    ):
        return True
    _atomic_json(
        notify_path,
        {
            "schema_version": 1,
            "callback_id": callback_id,
            "status": "pending",
        },
    )
    try:
        cmux_adapter.send(spec["origin_surface"], wake)
        cmux_adapter.send_key(spec["origin_surface"], "Enter")
    except Exception:
        return False
    _atomic_json(
        notify_path,
        {
            "schema_version": 1,
            "callback_id": callback_id,
            "status": "sent",
        },
    )
    return True


def provider_environment(
    spec: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a fresh environment, isolating protected research from the caller."""

    values = os.environ if env is None else env
    if spec.get("callback_mode") not in {
        "research-fetch",
        "research-synth",
    }:
        return dict(values)
    runtime_home = spec.get("runtime_home")
    if not isinstance(runtime_home, Path):
        raise RuntimeWorkerError("research runtime home is unavailable")
    temporary = runtime_home / "tmp"
    temporary.mkdir(mode=0o700, exist_ok=True)
    temporary.chmod(0o700)
    shell = "/bin/zsh" if Path("/bin/zsh").is_file() else "/bin/sh"
    return {
        "CODEX_HOME": str(runtime_home),
        "HOME": str(runtime_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": RESEARCH_PATH,
        "SHELL": shell,
        "TERM": "xterm-256color",
        "TMPDIR": str(temporary),
        "TZ": "UTC",
    }


def automate_prompt(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    runtime: str,
    surface_id: str,
    screen: str,
    cmux_adapter: object,
    *,
    closure_armed: bool = False,
) -> PromptDecision:
    """Apply only an exact prompt decision; unknown choices become durable."""

    decision = classify(runtime, screen, closure_armed=closure_armed)
    record = store.read(owner_id, operation_id)
    if record.state in TERMINAL or record.state == "attention-required":
        return decision
    if decision.recognized:
        try:
            for key in decision.keys:
                cmux_adapter.send_key(surface_id, key)
        except Exception:
            current = store.read(owner_id, operation_id)
            if current.state not in TERMINAL and current.state != "attention-required":
                store.transition(
                    owner_id,
                    operation_id,
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
        return decision
    if decision.interactive:
        try:
            store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.PROMPT_UNKNOWN,
            )
        except Exception:
            pass
    return decision


def _atomic_json(path: Path, value: object) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _normalize_fetch_errors_at_provider_boundary(
    path: Path,
    raw: bytes,
) -> bytes:
    """Normalize only bounded provider error forms before strict validation."""

    value = json.loads(raw)
    if not isinstance(value, dict):
        return raw
    errors = value.get("fetch_errors")
    if not isinstance(errors, list):
        return raw
    normalized: list[object] = []
    changed = False
    for item in errors:
        if isinstance(item, str):
            if not item.strip():
                changed = True
                continue
            normalized.append(item)
            continue
        if isinstance(item, Mapping) and set(item) == {"url", "error"}:
            url = item["url"]
            error = item["error"]
            if (
                isinstance(url, str)
                and url.strip()
                and isinstance(error, str)
                and error.strip()
            ):
                canonical = f"{url}: {error}"
                if len(canonical) <= 2000:
                    normalized.append(canonical)
                    changed = True
                    continue
        normalized.append(item)
    if not changed:
        return raw
    value["fetch_errors"] = normalized
    _atomic_json(path, value)
    return path.read_bytes()


def _bounded_file_sha256(path: Path, *, limit: int = MAX_OUTBOX_BYTES) -> str:
    """Return only a bounded content digest; invalid pointers are no evidence."""

    try:
        if path.is_symlink() or not path.is_file():
            return ""
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw or len(raw) > limit:
        return ""
    return hashlib.sha256(raw).hexdigest()


def _current_callback_receipt_sha256(runtime_root: Path) -> str:
    """Return receipt evidence only for the currently bound callback target."""

    values: list[tuple[dict[str, Any], bytes]] = []
    for path in (
        runtime_root / "callback-target.json",
        runtime_root / "callback-receipt.json",
    ):
        try:
            if path.is_symlink() or not path.is_file():
                return ""
            raw = path.read_bytes()
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                return ""
            value = json.loads(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(value, dict):
            return ""
        values.append((value, raw))
    target, _target_raw = values[0]
    receipt, receipt_raw = values[1]
    generation = target.get("generation")
    operation_id = target.get("operation_id")
    if (
        target.get("schema_version") != 1
        or receipt.get("schema_version") != 1
        or type(generation) is not int
        or generation < 1
        or not isinstance(operation_id, str)
        or not operation_id
        or receipt.get("generation") != generation
        or receipt.get("operation_id") != operation_id
        or receipt.get("status") != "accepted"
    ):
        return ""
    return hashlib.sha256(receipt_raw).hexdigest()


def _submit_failure_requires_attention(
    result: subprocess.CompletedProcess[str], callback_path: Path
) -> bool:
    """Ignore the benign race where the model published the same callback."""

    return result.returncode != 0 and not callback_path.is_file()


def _write_once_json(path: Path, value: object) -> None:
    encoded = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise RuntimeWorkerError(
                "research input provenance is unreadable"
            ) from exc
        if existing != encoded:
            raise RuntimeWorkerError(
                "research input provenance changed"
            )
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _research_input_provenance(
    spec: dict[str, Any],
    spec_path: Path,
    *,
    create: bool,
) -> str:
    if spec["callback_mode"] != "research-synth":
        return ""
    artifact_path = spec["cwd"] / "artifact.json"
    artifact = load_artifact(str(artifact_path))
    try:
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeWorkerError(
            "research input artifact is unreadable"
        ) from exc
    value = {
        "schema_version": 1,
        "operation_id": spec["operation_id"],
        "run_id": spec["run_id"],
        "fetch_run_id": artifact["run_id"],
        "request_sha256": artifact["request_sha256"],
        "artifact_sha256": artifact_sha256,
    }
    marker = spec_path.parent / "research-input.json"
    if marker.is_symlink():
        raise RuntimeWorkerError(
            "research input provenance must not be a symlink"
        )
    if create:
        _write_once_json(marker, value)
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError(
            "research input provenance is unreadable"
        ) from exc
    if recorded != value:
        raise RuntimeWorkerError(
            "research input artifact changed after validation"
        )
    return artifact_sha256






def _callback_target(spec: dict[str, Any]) -> tuple[int, str, str, Path]:
    try:
        value = json.loads(
            spec["callback_registration"].read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError("callback target registration is unreadable") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or type(value.get("generation")) is not int
        or int(value["generation"]) < 1
    ):
        raise RuntimeWorkerError("callback target registration is invalid")
    operation_id = str(value.get("operation_id") or "")
    run_id = str(value.get("run_id") or "")
    if not IDENTIFIER.fullmatch(operation_id) or not IDENTIFIER.fullmatch(run_id):
        raise RuntimeWorkerError("callback target identity is invalid")
    raw_pointer = value.get("callback_pointer")
    if not isinstance(raw_pointer, str) or not raw_pointer:
        raise RuntimeWorkerError("callback target pointer is invalid")
    pointer = Path(raw_pointer).expanduser()
    if not pointer.is_absolute():
        pointer = spec["cwd"] / pointer
    pointer = pointer.resolve()
    try:
        pointer.relative_to(spec["cwd"])
    except ValueError as exc:
        raise RuntimeWorkerError("callback target pointer escapes cwd") from exc
    return int(value["generation"]), operation_id, run_id, pointer


def _envelope(value: object) -> CallbackEnvelope:
    if not isinstance(value, dict):
        raise RuntimeWorkerError("callback envelope must be an object")
    return CallbackEnvelope(
        callback_id=value.get("callback_id", ""),
        operation_id=value.get("operation_id", ""),
        run_id=value.get("run_id", ""),
        kind=value.get("kind", ""),
        payload=value.get("payload", {}),
        payload_sha256=value.get("payload_sha256", ""),
        schema_version=value.get("schema_version", 0),
    )


def _reap_child(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() <= deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited == pid:
            return True
        time.sleep(0.02)
    return False


def _contain_provider_start_failure(
    process: ProcessAdapter, handle: ProcessHandle
) -> None:
    try:
        process.signal_owned_child_group(
            handle.process_group,
            handle.process_identity,
            signal.SIGTERM,
        )
    except ProcessError:
        pass
    if _reap_child(handle.pid, 0.5):
        return
    try:
        process.signal_owned_child_group(
            handle.process_group,
            handle.process_identity,
            signal.SIGKILL,
        )
    except ProcessError:
        pass
    _reap_child(handle.pid, 0.5)


def run(spec_path: Path, *, poll_seconds: float=0.1, checkpoint_probe: Callable[[str, str], str] | None=None, cmux_adapter: object | None=None, review_launcher: Callable[[Path, Path], None] | None=None, verification_runner: Callable[..., subprocess.CompletedProcess[str]] | None=None) -> int:
    from .runtime_worker_execution import RuntimeWorkerExecution
    worker = RuntimeWorkerExecution()
    worker.contain_provider_start_failure = _contain_provider_start_failure
    return worker.execute(spec_path, poll_seconds=poll_seconds, checkpoint_probe=checkpoint_probe, cmux_adapter=cmux_adapter, review_launcher=review_launcher, verification_runner=verification_runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.spec)
    except RuntimeWorkerError:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
