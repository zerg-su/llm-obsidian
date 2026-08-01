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
from review_resolution import DISPOSITIONS, MATERIAL_SEVERITIES
from task_contract import ContractError, validate_handoff
from wiki_summary_contract import WikiSummaryError, validate_summary_for_task


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)
MAX_OUTBOX_BYTES = 70_000
MAX_SCREEN_BYTES = 70_000
MAX_PIPELINE_VERIFY_RESUBMITS = 1
RESEARCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
CALLBACK_WAIT_STATES = frozenset(
    {"running", "awaiting-callback", "verifying"}
)


class RuntimeWorkerError(RuntimeError):
    pass


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
    if len(reviewed_heads) != 1 or "" in reviewed_heads:
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
        or not isinstance(items, list)
        or not items
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


def _absolute(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise RuntimeWorkerError(f"{label} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeWorkerError(f"{label} must be an absolute path")
    return path.resolve()


def load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError("runtime launch spec is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeWorkerError("runtime launch spec schema is invalid")
    for field in ("owner_id", "operation_id", "run_id"):
        if not IDENTIFIER.fullmatch(str(value.get(field) or "")):
            raise RuntimeWorkerError(f"runtime launch {field} is invalid")
    if not SURFACE_UUID.fullmatch(str(value.get("surface_id") or "")):
        raise RuntimeWorkerError("runtime launch surface identity is invalid")
    if value.get("runtime") not in {"claude", "codex"}:
        raise RuntimeWorkerError("runtime launch provider is invalid")
    callback_mode = str(value.get("callback_mode") or "envelope")
    if callback_mode not in {
        "envelope",
        "task-summary",
        "research-fetch",
        "research-synth",
    }:
        raise RuntimeWorkerError("runtime callback mode is invalid")
    argv = value.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(part, str) or "\0" in part for part in argv)
        or not Path(argv[0]).is_absolute()
    ):
        raise RuntimeWorkerError("runtime launch argv is invalid")
    cwd = _absolute(value.get("cwd"), "cwd")
    callback = _absolute(value.get("callback_pointer"), "callback_pointer")
    registration = _absolute(
        value.get("callback_registration"), "callback_registration"
    )
    task_summary: Path | None = None
    runtime_home: Path | None = None
    runtime_interpreter: Path | None = None
    research_request_sha256 = str(
        value.get("research_request_sha256") or ""
    )
    callback_wake = str(value.get("callback_wake") or "")
    origin_surface = str(value.get("origin_surface") or "")
    if callback_mode == "task-summary":
        task_summary = _absolute(
            value.get("task_summary_pointer"), "task_summary_pointer"
        )
        if (
            task_summary.name != ".task-summary.json"
            or not SURFACE_UUID.fullmatch(origin_surface)
        ):
            raise RuntimeWorkerError(
                "task-summary source or origin identity is invalid"
            )
    if callback_mode in {"research-fetch", "research-synth"}:
        raw_runtime_home = value.get("runtime_home")
        if (
            value.get("runtime") != "codex"
            or not isinstance(raw_runtime_home, str)
            or not raw_runtime_home
            or Path(raw_runtime_home).expanduser().is_symlink()
            or not SURFACE_UUID.fullmatch(origin_surface)
            or not callback_wake
            or callback_wake != callback_wake.strip()
            or "\0" in callback_wake
            or "\n" in callback_wake
            or "\r" in callback_wake
            or len(callback_wake.encode()) > 4096
        ):
            raise RuntimeWorkerError("research launch identity is invalid")
        runtime_home = _absolute(raw_runtime_home, "runtime_home")
        raw_runtime_interpreter = value.get("runtime_interpreter")
        if raw_runtime_interpreter:
            runtime_interpreter = _absolute(
                raw_runtime_interpreter, "runtime_interpreter"
            )
            try:
                interpreter_stat = runtime_interpreter.stat()
            except OSError as exc:
                raise RuntimeWorkerError(
                    "research runtime interpreter is unavailable"
                ) from exc
            if (
                not runtime_interpreter.is_file()
                or not os.access(runtime_interpreter, os.X_OK)
                or interpreter_stat.st_mode & 0o022
            ):
                raise RuntimeWorkerError(
                    "research runtime interpreter is untrusted"
                )
        try:
            runtime_stat = runtime_home.stat()
        except OSError as exc:
            raise RuntimeWorkerError(
                "research runtime home is unavailable"
            ) from exc
        if (
            not runtime_home.is_dir()
            or runtime_stat.st_uid != os.getuid()
            or runtime_stat.st_mode & 0o077
            or runtime_home == cwd
            or runtime_home in cwd.parents
            or cwd in runtime_home.parents
        ):
            raise RuntimeWorkerError(
                "research runtime home must be owner-only and disjoint"
            )
        expected_name = (
            "artifact.json"
            if callback_mode == "research-fetch"
            else "complete.json"
        )
        if callback.name != expected_name:
            raise RuntimeWorkerError(
                "research callback pointer is not canonical"
            )
        if callback_mode == "research-fetch":
            if not re.fullmatch(r"[0-9a-f]{64}", research_request_sha256):
                raise RuntimeWorkerError(
                    "research request digest is invalid"
                )
        elif research_request_sha256:
            raise RuntimeWorkerError(
                "research synth request digest must be derived"
            )
    elif callback_mode == "envelope" and callback_wake:
        if (
            not SURFACE_UUID.fullmatch(origin_surface)
            or callback_wake != callback_wake.strip()
            or "\0" in callback_wake
            or "\n" in callback_wake
            or "\r" in callback_wake
            or len(callback_wake.encode()) > 4096
        ):
            raise RuntimeWorkerError("review callback wake is invalid")
        if (
            value.get("runtime_home")
            or value.get("runtime_interpreter")
            or research_request_sha256
        ):
            raise RuntimeWorkerError(
                "research runtime fields require research callback mode"
            )
    elif (
        value.get("runtime_home")
        or value.get("runtime_interpreter")
        or research_request_sha256
        or callback_wake
    ):
        raise RuntimeWorkerError(
            "research launch fields require research callback mode"
        )
    store_root = _absolute(value.get("store_root"), "store_root")
    ready = _absolute(value.get("ready_path"), "ready_path")
    exit_path = _absolute(value.get("exit_path"), "exit_path")
    if (
        ready.parent != path.parent
        or exit_path.parent != path.parent
        or registration.parent != path.parent
    ):
        raise RuntimeWorkerError("runtime worker markers escape launch state")
    try:
        callback.relative_to(cwd)
    except ValueError as exc:
        raise RuntimeWorkerError("runtime callback pointer escapes cwd") from exc
    if task_summary is not None:
        try:
            task_summary.relative_to(cwd)
        except ValueError as exc:
            raise RuntimeWorkerError("task summary pointer escapes cwd") from exc
    if not cwd.is_dir() or not store_root.is_dir():
        raise RuntimeWorkerError("runtime launch roots are unavailable")
    value.update(
        {
            "cwd": cwd,
            "callback_pointer": callback,
            "callback_registration": registration,
            "callback_mode": callback_mode,
            "task_summary_pointer": task_summary,
            "runtime_home": runtime_home,
            "runtime_interpreter": runtime_interpreter,
            "research_request_sha256": research_request_sha256,
            "callback_wake": callback_wake,
            "origin_surface": origin_surface,
            "store_root": store_root,
            "ready_path": ready,
            "exit_path": exit_path,
        }
    )
    return value


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


def run(
    spec_path: Path,
    *,
    poll_seconds: float = 0.1,
    checkpoint_probe: Callable[[str, str], str] | None = None,
    cmux_adapter: object | None = None,
    review_launcher: Callable[[Path, Path], None] | None = None,
    verification_runner: Callable[
        ..., subprocess.CompletedProcess[str]
    ]
    | None = None,
) -> int:
    spec = load_spec(spec_path.resolve())
    ready = spec["ready_path"]
    exit_path = spec["exit_path"]
    store = OperationStore(spec["store_root"])
    trusted_store = spec["store_root"]
    trusted_vault = trusted_store.parent.parent
    if (
        spec["callback_mode"] == "task-summary"
        and trusted_store != trusted_vault / ".vault-meta" / "harness"
    ):
        _atomic_json(ready, {"schema_version": 1, "status": "failed"})
        _atomic_json(
            exit_path,
            {
                "schema_version": 1,
                "status": "store-root-invalid",
                "exit_code": 2,
            },
        )
        return 2
    process = ProcessAdapter()
    handle: ProcessHandle | None = None
    research_input_sha256 = ""
    if spec["callback_mode"] == "research-synth":
        try:
            research_input_sha256 = _research_input_provenance(
                spec,
                spec_path,
                create=True,
            )
        except (OSError, ResearchContractError, RuntimeWorkerError):
            try:
                store.transition(
                    spec["owner_id"],
                    spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.CALLBACK_INVALID,
                )
            except Exception:
                pass
            _atomic_json(
                ready,
                {"schema_version": 1, "status": "failed"},
            )
            _atomic_json(
                exit_path,
                {
                    "schema_version": 1,
                    "status": "research-input-invalid",
                    "exit_code": 2,
                },
            )
            return 2
    try:
        provider_command = provider_argv(spec)
        provider_env = provider_environment(spec)
        handle = process.start(
            provider_command,
            cwd=spec["cwd"],
            env=provider_env,
        )
        supervisor_identity = process.capture_identity(os.getpid())
        if not supervisor_identity:
            raise ProcessError("runtime worker identity is unavailable")
    except (OSError, ProcessError):
        if handle is not None:
            _contain_provider_start_failure(process, handle)
        _atomic_json(
            ready,
            {"schema_version": 1, "status": "failed"},
        )
        _atomic_json(
            exit_path,
            {"schema_version": 1, "status": "start-failed", "exit_code": 127},
        )
        return 127
    _atomic_json(
        ready,
        {
            "schema_version": 1,
            "status": "ready",
            "pid": handle.pid,
            "process_group": handle.process_group,
            "supervisor_pid": os.getpid(),
            "process_identity": handle.process_identity,
            "supervisor_identity": supervisor_identity,
        },
    )
    checkpoint_probe = checkpoint_probe or CmuxAdapter().resume_checkpoint
    checkpoint = ""
    next_checkpoint_probe = 0.0

    active_target: tuple[int, str, str, Path] | None = None
    last_digest = ""
    stable_reads = 0
    callback_handled = False
    registration_invalid = False
    summary_digest = ""
    summary_stable_reads = 0
    summary_attention_revision = -1
    cmux_adapter = cmux_adapter or CmuxAdapter()
    operation_contract = store.read(
        spec["owner_id"], spec["operation_id"]
    ).spec.contract_sha256
    try:
        _pipeline_name, pipeline = compiled_executable_for_contract(
            operation_contract
        )
        pipeline_extra_commands: tuple[str, ...] = ()
        custom_pipeline_spec = None
    except ValueError:
        try:
            (
                _pipeline_name,
                pipeline,
                pipeline_extra_commands,
                custom_pipeline_spec,
            ) = (
                resolve_custom_executable(
                store_root=spec_path.parent.parent,
                operation_id=spec["operation_id"],
                definition_sha256=operation_contract,
                registry=builtin_registry(),
                policy=CustomPipelinePolicy.default(),
                capabilities=("route:resolved",),
            )
            )
        except (ContractError, OSError, ValueError):
            _pipeline_name, pipeline, pipeline_extra_commands = "", None, ()
            custom_pipeline_spec = None
    is_custom_pipeline = custom_pipeline_spec is not None
    last_prompt_digest = ""
    latest_screen_digest = ""
    latest_prompt_state = "unknown"
    next_prompt_probe = 0.0
    liveness_policy = LivenessPolicy.default()
    liveness_controller = LivenessController(spec_path.parent / "liveness")
    next_liveness_probe = 0.0
    handled_control_id = ""
    invalid_control_digest = ""
    fix_callback_digest = ""
    fix_callback_stable_reads = 0
    fix_result_digest = ""
    fix_result_stable_reads = 0
    fix_submit_attempt_digest = ""
    fix_transport_complete = (
        _pipeline_name != "engineering/fix" or is_custom_pipeline
    )
    custom_transport_complete = not is_custom_pipeline
    custom_callback_digest = ""
    custom_callback_stable_reads = 0
    custom_result_digest = ""
    custom_result_stable_reads = 0
    custom_submit_attempt_digest = ""

    def inspect_control() -> None:
        nonlocal handled_control_id, invalid_control_digest
        control_path = spec_path.parent / "process-control.json"
        try:
            raw = control_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            raw = b""
        digest = hashlib.sha256(raw).hexdigest()
        if digest == invalid_control_digest:
            return
        try:
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                raise RuntimeWorkerError(
                    "process guardian command is invalid"
                )
            command = json.loads(raw)
            if not isinstance(command, dict):
                raise RuntimeWorkerError(
                    "process guardian command must be an object"
                )
            command_id = str(command.get("command_id") or "")
            unsigned = dict(command)
            unsigned.pop("command_id", None)
            encoded = json.dumps(
                unsigned, sort_keys=True, separators=(",", ":")
            ).encode()
            expected_id = hashlib.sha256(encoded).hexdigest()
            action = command.get("action")
            if (
                set(command)
                != {
                    "schema_version",
                    "action",
                    "operation_id",
                    "run_id",
                    "process_group",
                    "process_identity",
                    "supervisor_pid",
                    "supervisor_identity",
                    "command_id",
                }
                or command.get("schema_version") != 1
                or action not in {"request-exit", "terminate"}
                or command.get("operation_id") != spec["operation_id"]
                or command.get("run_id") != spec["run_id"]
                or command.get("process_group") != handle.process_group
                or command.get("process_identity")
                != handle.process_identity
                or command.get("supervisor_pid") != os.getpid()
                or command.get("supervisor_identity")
                != supervisor_identity
                or command_id != expected_id
            ):
                raise RuntimeWorkerError(
                    "process guardian command identity mismatches"
                )
            if command_id == handled_control_id:
                return
            process.signal_owned_child_group(
                handle.process_group,
                handle.process_identity,
                (
                    signal.SIGTERM
                    if action == "request-exit"
                    else signal.SIGKILL
                ),
            )
            handled_control_id = command_id
            _atomic_json(
                spec_path.parent / "process-control-receipt.json",
                {
                    "schema_version": 1,
                    "command_id": command_id,
                    "action": action,
                    "status": "accepted",
                },
            )
        except (
            json.JSONDecodeError,
            OSError,
            ProcessError,
            RuntimeWorkerError,
            TypeError,
            ValueError,
        ):
            invalid_control_digest = digest
            try:
                store.transition(
                    spec["owner_id"],
                    spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            except Exception:
                pass
            _atomic_json(
                spec_path.parent / "process-control-error.json",
                {"schema_version": 1, "status": "invalid"},
            )

    def inspect_prompt() -> None:
        nonlocal last_prompt_digest, latest_screen_digest, latest_prompt_state
        try:
            record = store.read(spec["owner_id"], spec["operation_id"])
        except Exception:
            return
        if record.resources.surface_id != spec["surface_id"]:
            return
        reader = getattr(cmux_adapter, "read", None)
        if reader is None:
            return
        try:
            screen = str(reader(spec["surface_id"]))
        except Exception:
            return
        encoded = screen.encode("utf-8", errors="replace")
        if not encoded or len(encoded) > MAX_SCREEN_BYTES:
            return
        digest = hashlib.sha256(encoded).hexdigest()
        decision = classify(
            spec["runtime"],
            screen,
            closure_armed=record.state == "exiting",
        )
        latest_screen_digest = digest
        latest_prompt_state = (
            "interactive" if decision.interactive else "non-interactive"
        )
        if digest == last_prompt_digest:
            return
        if not decision.interactive:
            return
        last_prompt_digest = digest
        automate_prompt(
            store,
            spec["owner_id"],
            spec["operation_id"],
            spec["runtime"],
            spec["surface_id"],
            screen,
            cmux_adapter,
            closure_armed=record.state == "exiting",
        )

    def inspect_callback() -> None:
        nonlocal active_target, last_digest, stable_reads, callback_handled
        nonlocal registration_invalid
        try:
            target = _callback_target(spec)
        except RuntimeWorkerError:
            if not registration_invalid:
                registration_invalid = True
                try:
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.CALLBACK_INVALID,
                    )
                except Exception:
                    pass
                _atomic_json(
                    spec_path.parent / "callback-error.json",
                    {"schema_version": 1, "status": "callback-target-invalid"},
                )
            return
        registration_invalid = False
        if target != active_target:
            if active_target is not None and target[0] <= active_target[0]:
                return
            active_target = target
            last_digest = ""
            stable_reads = 0
            callback_handled = False
        if callback_handled:
            return
        generation, operation_id, run_id, callback_path = target
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            raw = b""
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest != last_digest:
            last_digest = digest
            stable_reads = 1
            return
        stable_reads += 1
        if stable_reads < 2:
            return
        callback_handled = True
        try:
            envelope = _envelope(json.loads(raw))
            if (
                envelope.operation_id != operation_id
                or envelope.run_id != run_id
            ):
                raise RuntimeWorkerError("callback identity mismatches runtime launch")
            acceptance = CallbackBroker(
                store, spec["owner_id"]
            ).accept(
                envelope,
                deadline_operation_id=spec["operation_id"],
            )
            _atomic_json(
                spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "generation": generation,
                    "callback_id": envelope.callback_id,
                    "operation_id": operation_id,
                    "status": (
                        "duplicate" if acceptance.duplicate else "accepted"
                    ),
                },
            )
            if not publish_callback_wake(
                spec,
                spec_path.parent,
                envelope.callback_id,
                cmux_adapter,
            ):
                callback_handled = False
                return
        except CallbackTimeoutError:
            _atomic_json(
                spec_path.parent / "callback-timeout.json",
                {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "run_id": spec["run_id"],
                    "status": "attention-required",
                },
            )
        except (
            CallbackError,
            RuntimeWorkerError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            try:
                store.transition(
                    spec["owner_id"],
                    spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.CALLBACK_INVALID,
                )
            except Exception:
                pass
            _atomic_json(
                spec_path.parent / "callback-error.json",
                {"schema_version": 1, "status": "callback-invalid"},
            )

    def summary_attention(
        status: str,
        reason: AttentionReason = AttentionReason.CALLBACK_INVALID,
        *,
        write_error: bool = True,
    ) -> None:
        nonlocal callback_handled, summary_attention_revision
        callback_handled = True
        try:
            store.transition(
                spec["owner_id"],
                spec["operation_id"],
                "attention-required",
                reason=reason,
            )
        except Exception:
            pass
        try:
            current = store.read(
                spec["owner_id"], spec["operation_id"]
            )
            if current.state == "attention-required":
                summary_attention_revision = current.revision
        except Exception:
            pass
        if is_custom_pipeline and pipeline is not None:
            marker = spec_path.parent / "pipeline-custom" / "attention-telemetry.json"
            if not marker.exists():
                emit_compiled_pipeline_event(
                    spec["cwd"],
                    event="attention",
                    pipeline_id=pipeline.definition.pipeline_id,
                    pipeline_version=pipeline.definition.version,
                    profile=pipeline.definition.profile,
                    compiler_outcome="custom-resolved",
                    definition_sha=pipeline.definition_sha256,
                    primitive_count=len(pipeline.definition.steps),
                    attention_category="custom-attention",
                    status="degraded",
                )
                _atomic_json(
                    marker,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "status": "emitted",
                    },
                )
        if write_error:
            _atomic_json(
                spec_path.parent / "callback-error.json",
                {"schema_version": 1, "status": status},
            )

    def write_immutable_json(path: Path, value: dict[str, object]) -> None:
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise RuntimeWorkerError("immutable runtime receipt cannot be a symlink")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                current = path.read_bytes()
            except OSError as exc:
                raise RuntimeWorkerError(
                    "immutable runtime receipt is unreadable"
                ) from exc
            if current != encoded:
                raise RuntimeWorkerError(
                    "immutable runtime receipt changed"
                )
            return
        try:
            with os.fdopen(descriptor, "wb") as handle_file:
                handle_file.write(encoded)
                handle_file.flush()
                os.fsync(handle_file.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def git_head() -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        head = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", head):
            raise RuntimeWorkerError("engineering/fix HEAD is unavailable")
        return head

    def retarget_fix_callback(
        *,
        operation_id: str,
        run_id: str,
        callback_pointer: str,
    ) -> None:
        generation, current_operation, current_run, current_pointer = (
            _callback_target(spec)
        )
        expected_pointer = (spec["cwd"] / callback_pointer).resolve()
        if (
            current_operation == operation_id
            and current_run == run_id
            and current_pointer == expected_pointer
        ):
            return
        if current_operation != spec["operation_id"]:
            current_child = store.read(spec["owner_id"], current_operation)
            if current_child.state != "complete":
                raise RuntimeWorkerError(
                    "engineering/fix callback target changed before acceptance"
                )
        if expected_pointer.exists() or expected_pointer.is_symlink():
            if expected_pointer.is_symlink() or not expected_pointer.is_file():
                raise RuntimeWorkerError(
                    "engineering/fix callback outbox is not reusable"
                )
            expected_pointer.unlink()
        _atomic_json(
            spec["callback_registration"],
            {
                "schema_version": 1,
                "generation": generation + 1,
                "operation_id": operation_id,
                "run_id": run_id,
                "callback_pointer": callback_pointer,
            },
        )

    def notify_fix_phase(request: dict[str, object]) -> None:
        operation_id = str(request["operation_id"])
        step_id = str(request["step_id"])
        iteration = int(request["iteration"])
        prior_pointer = {
            "root-cause": ".task-pipeline/outputs/pass-0/reproduce.md",
            "regression-test": (
                f".task-pipeline/outputs/pass-{iteration}/root-cause.md"
            ),
            "minimal-fix": (
                f".task-pipeline/outputs/pass-{iteration}/regression-test.md"
            ),
        }.get(step_id, "")
        prior_context = (
            f"Read prior accepted evidence at {prior_pointer}. "
            "input_sha256 and prior_receipt_sha256 are opaque request "
            "bindings, not artifact content hashes. "
            if prior_pointer
            else ""
        )
        notify_path = (
            spec_path.parent
            / "pipeline-fix"
            / "notifications"
            / f"{operation_id}.json"
        )
        marker = {
            "schema_version": 1,
            "operation_id": operation_id,
            "step_id": step_id,
            "status": "sent",
        }
        if notify_path.is_file() and not notify_path.is_symlink():
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError(
                    "engineering/fix phase notification changed"
                )
            return
        message = (
            "Typed engineering/fix phase "
            f"{step_id} is ready in "
            ".task-pipeline-step-request.json. Complete only this phase. "
            f"{prior_context}"
            f"Write evidence to {request['output_pointer']} and write "
            f"{request['result_pointer']} as exact JSON with fields "
            '{"schema_version":1,"status":"complete",'
            '"output_sha256":"<sha256-of-evidence>",'
            '"head_sha":"<current-git-head>"}. For the reproduce phase only, '
            'status may instead be "cannot-reproduce". Then publish the '
            "request-bound callback with pipeline-step-submit.py. "
            "Remain in this same session for the next typed request."
        )
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError(
                "engineering/fix phase notification exceeds its bound"
            )
        cmux_adapter.send(spec["surface_id"], message)
        cmux_adapter.send_key(spec["surface_id"], "Enter")
        write_immutable_json(notify_path, marker)

    def notify_fix_finalization(iteration: int) -> bool:
        notify_path = (
            spec_path.parent
            / "pipeline-fix"
            / (
                "finalization-notify.json"
                if iteration == 0
                else f"pass-{iteration}/finalization-notify.json"
            )
        )
        marker = {
            "schema_version": 1,
            "operation_id": spec["operation_id"],
            "iteration": iteration,
            "status": "sent",
        }
        if notify_path.is_file() and not notify_path.is_symlink():
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError(
                    "engineering/fix finalization notification changed"
                )
            return False
        phase_count = "four" if iteration == 0 else "three retry"
        message = (
            f"All {phase_count} typed engineering/fix phase receipts are accepted. "
            "Finish the task in this same session: commit the minimal fix, "
            "run the approved scoped verification, and write the canonical "
            ".task-summary.json. Do not repeat an accepted phase."
        )
        cmux_adapter.send(spec["surface_id"], message)
        cmux_adapter.send_key(spec["surface_id"], "Enter")
        write_immutable_json(notify_path, marker)
        return True

    def notify_cannot_reproduce(receipt: FixStepReceipt) -> None:
        receipt_sha256 = receipt.receipt_sha256
        attention_path = spec["cwd"] / ".task-needs-attention.json"
        marker = {
            "version": 1,
            "id": f"pipeline-decision-{receipt_sha256[:24]}",
            "status": "pending",
            "task_name": "engineering/fix cannot reproduce",
            "category": "pipeline-decision",
            "reason": (
                "The approved fix pipeline cannot reproduce the reported defect"
            ),
            "question": "Choose stop or retry-with-fixture",
            "worktree": str(spec["cwd"]),
            "task_surface": spec["surface_id"],
            "raised_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "receipt_operation_id": receipt.operation_id,
            "receipt_sha256": receipt_sha256,
            "allowed_decisions": ["stop", "retry-with-fixture"],
        }
        if attention_path.exists():
            if attention_path.is_symlink() or not attention_path.is_file():
                raise RuntimeWorkerError(
                    "pipeline decision packet is invalid"
                )
            current = json.loads(attention_path.read_text(encoding="utf-8"))
            if (
                not isinstance(current, dict)
                or current.get("id") != marker["id"]
                or current.get("category") != "pipeline-decision"
                or current.get("receipt_operation_id")
                != receipt.operation_id
                or current.get("receipt_sha256") != receipt_sha256
            ):
                raise RuntimeWorkerError(
                    "pipeline decision packet changed"
                )
        else:
            _atomic_json(attention_path, marker)
        notify_path = (
            spec_path.parent
            / "pipeline-fix"
            / "cannot-reproduce-notify.json"
        )
        delivery = {
            "schema_version": 1,
            "operation_id": spec["operation_id"],
            "receipt_sha256": receipt_sha256,
            "status": "sent",
        }
        if notify_path.is_file() and not notify_path.is_symlink():
            if json.loads(notify_path.read_text(encoding="utf-8")) != delivery:
                raise RuntimeWorkerError(
                    "pipeline decision delivery changed"
                )
            return
        command = (
            "python3 "
            + shlex.quote(
                str(
                    spec["store_root"].parent.parent
                    / "scripts"
                    / "task_escalation.py"
                )
            )
            + " resolve --worktree "
            + shlex.quote(str(spec["cwd"]))
            + " --decision <decision>"
        )
        message = (
            "Typed task escalation callback received. Category: "
            "pipeline-decision. The approved engineering/fix pipeline "
            "cannot reproduce the defect. Inspect "
            f"{attention_path} and resolve from the originating coordinator "
            f"with: {command}. Allowed decisions: stop, retry-with-fixture."
        )
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError(
                "pipeline decision notification exceeds its bound"
            )
        cmux_adapter.send(spec["origin_surface"], message)
        cmux_adapter.send_key(spec["origin_surface"], "Enter")
        write_immutable_json(notify_path, delivery)

    def drive_fix_transport() -> None:
        nonlocal fix_callback_digest, fix_callback_stable_reads
        nonlocal fix_result_digest, fix_result_stable_reads
        nonlocal fix_submit_attempt_digest
        nonlocal fix_transport_complete
        if (
            _pipeline_name != "engineering/fix"
            or callback_handled
            or fix_transport_complete
        ):
            return
        try:
            meta_path = spec["cwd"] / ".task-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            policy = (
                meta.get("pipeline_policy")
                if isinstance(meta, dict)
                else None
            )
            if (
                not isinstance(policy, dict)
                or policy.get("name") != "engineering/fix"
                or pipeline is None
                or policy.get("definition_sha256")
                != pipeline.definition_sha256
            ):
                raise RuntimeWorkerError(
                    "engineering/fix metadata mismatches its compiled contract"
                )
            completion_policy = str(
                policy.get("completion_policy") or ""
            )
            total_pass_limit = policy.get("total_pass_limit")
            if (
                completion_policy not in {"attention", "autonomous"}
                or type(total_pass_limit) is not int
                or total_pass_limit
                != {
                    "attention": 2,
                    "autonomous": 3,
                }[completion_policy]
            ):
                raise RuntimeWorkerError(
                    "engineering/fix completion policy is invalid"
                )
            approved_plan_sha256 = str(
                meta.get("approved_plan_sha256") or ""
            )
            controller_path = (
                spec_path.parent / "pipeline-fix" / "controller.json"
            )
            if controller_path.is_symlink():
                raise RuntimeWorkerError(
                    "engineering/fix controller must not be a symlink"
                )
            if controller_path.is_file():
                controller = json.loads(
                    controller_path.read_text(encoding="utf-8")
                )
                if (
                    not isinstance(controller, dict)
                    or set(controller)
                    != {
                        "schema_version",
                        "operation_id",
                        "definition_sha256",
                        "approved_plan_sha256",
                        "initial_head_sha",
                        "iteration",
                    }
                    or controller.get("schema_version") != 1
                    or controller.get("operation_id")
                    != spec["operation_id"]
                    or controller.get("definition_sha256")
                    != pipeline.definition_sha256
                    or controller.get("approved_plan_sha256")
                    != approved_plan_sha256
                    or controller.get("iteration") != 0
                ):
                    raise RuntimeWorkerError(
                        "engineering/fix controller receipt changed"
                    )
            else:
                try:
                    initial_request = json.loads(
                        (spec["cwd"] / ".task-pipeline-step-request.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    initial_head_sha = str(
                        initial_request.get("input_head_sha")
                        if isinstance(initial_request, dict)
                        else ""
                    )
                except (OSError, json.JSONDecodeError):
                    initial_head_sha = git_head()
                if not re.fullmatch(r"[0-9a-f]{40,64}", initial_head_sha):
                    raise RuntimeWorkerError("pipeline initial HEAD is unavailable")
                controller = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "definition_sha256": pipeline.definition_sha256,
                    "approved_plan_sha256": approved_plan_sha256,
                    "initial_head_sha": initial_head_sha,
                    "iteration": 0,
                }
                write_immutable_json(controller_path, controller)
            initial_head_sha = str(controller["initial_head_sha"])
            parent = store.read(spec["owner_id"], spec["operation_id"])
            initial_receipt_root = (
                spec_path.parent / "pipeline-fix" / "pass-0"
            )
            initial_receipts: list[FixStepReceipt] = []
            for step_id in (
                "reproduce",
                "root-cause",
                "regression-test",
                "minimal-fix",
            ):
                receipt_path = (
                    initial_receipt_root / step_id / "receipt.json"
                )
                if not receipt_path.is_file():
                    break
                initial_receipts.append(load_receipt(receipt_path))
            initial_progress = reconcile_fix(
                parent,
                definition_sha256=pipeline.definition_sha256,
                approved_plan_sha256=approved_plan_sha256,
                initial_head_sha=initial_head_sha,
                receipts=tuple(initial_receipts),
                iteration=0,
            )
            if initial_progress.action == "attention":
                cannot_receipt = initial_progress.prior_receipt
                if cannot_receipt is None:
                    raise RuntimeWorkerError(
                        "cannot-reproduce receipt is unavailable"
                    )
                emit_compiled_pipeline_event(
                    spec["cwd"],
                    event="fix-phase-attention",
                    pipeline_id=pipeline.definition.pipeline_id,
                    pipeline_version=pipeline.definition.version,
                    profile=pipeline.definition.profile,
                    compiler_outcome="resolved",
                    definition_sha=pipeline.definition_sha256,
                    primitive_count=len(pipeline.definition.steps),
                    loop_iteration=0,
                    attention_category="cannot-reproduce",
                )
                notify_cannot_reproduce(cannot_receipt)
                summary_attention(
                    "pipeline-fix-cannot-reproduce",
                    AttentionReason.ATTENTION_REQUIRED,
                )
                return
            iteration = 0
            receipt_root = initial_receipt_root
            receipts = initial_receipts
            progress = initial_progress
            retry_intent: dict[str, object] | None = None
            retry_intent_paths = sorted(
                (spec_path.parent / "pipeline-fix").glob(
                    "pass-*/retry-intent.json"
                )
            )
            if retry_intent_paths and progress.action != "complete":
                raise RuntimeWorkerError(
                    "fix retry started before the initial pass completed"
                )
            expected_retry_iterations = list(
                range(1, len(retry_intent_paths) + 1)
            )
            observed_retry_iterations: list[int] = []
            for path in retry_intent_paths:
                match = re.fullmatch(
                    r"pass-([1-9][0-9]*)", path.parent.name
                )
                if match is None:
                    raise RuntimeWorkerError(
                        "fix retry intent path is invalid"
                    )
                observed_retry_iterations.append(int(match.group(1)))
            if (
                observed_retry_iterations != expected_retry_iterations
                or len(retry_intent_paths) >= int(total_pass_limit)
            ):
                raise RuntimeWorkerError(
                    "fix retry intents are not a bounded prefix"
                )
            if retry_intent_paths:
                retry_intent_path = retry_intent_paths[-1]
                if retry_intent_path.is_symlink():
                    raise RuntimeWorkerError(
                        "fix retry intent cannot be a symlink"
                    )
                retry_intent = json.loads(
                    retry_intent_path.read_text(encoding="utf-8")
                )
                iteration = observed_retry_iterations[-1]
                expected_intent_fields = {
                    "schema_version",
                    "operation_id",
                    "definition_sha256",
                    "iteration",
                    "completion_policy",
                    "total_pass_limit",
                    "reproduction_receipt_sha256",
                    "verification_operation_id",
                    "verification_sha256",
                    "failed_head_sha",
                    "current_head_sha",
                    "status",
                }
                if (
                    not isinstance(retry_intent, dict)
                    or set(retry_intent) != expected_intent_fields
                    or retry_intent.get("schema_version") != 1
                    or retry_intent.get("operation_id")
                    != spec["operation_id"]
                    or retry_intent.get("definition_sha256")
                    != pipeline.definition_sha256
                    or retry_intent.get("iteration") != iteration
                    or retry_intent.get("completion_policy")
                    != completion_policy
                    or retry_intent.get("total_pass_limit")
                    != total_pass_limit
                    or retry_intent.get("status") != "pending"
                    or not initial_receipts
                    or retry_intent.get(
                        "reproduction_receipt_sha256"
                    )
                    != initial_receipts[0].receipt_sha256
                ):
                    raise RuntimeWorkerError(
                        "fix retry intent identity changed"
                    )
                verification_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / str(retry_intent["verification_operation_id"])
                    / "receipt.json"
                )
                if (
                    verification_path.is_symlink()
                    or not verification_path.is_file()
                ):
                    raise RuntimeWorkerError(
                        "fix retry verification receipt is unavailable"
                    )
                verification_value = json.loads(
                    verification_path.read_text(encoding="utf-8")
                )
                verification_sha256 = hashlib.sha256(
                    json.dumps(
                        verification_value,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if (
                    not isinstance(verification_value, dict)
                    or verification_value.get("status") != "failed"
                    or verification_value.get("parent_operation_id")
                    != spec["operation_id"]
                    or verification_value.get("head_sha")
                    != retry_intent["failed_head_sha"]
                    or verification_sha256
                    != retry_intent["verification_sha256"]
                ):
                    raise RuntimeWorkerError(
                        "fix retry verification binding changed"
                    )
                receipt_root = (
                    spec_path.parent
                    / "pipeline-fix"
                    / f"pass-{iteration}"
                )
                receipts = []
                for step_id in (
                    "root-cause",
                    "regression-test",
                    "minimal-fix",
                ):
                    receipt_path = (
                        receipt_root / step_id / "receipt.json"
                    )
                    if not receipt_path.is_file():
                        break
                    receipts.append(load_receipt(receipt_path))
                progress = reconcile_retry_fix(
                    parent,
                    definition_sha256=pipeline.definition_sha256,
                    reproduction_receipt=initial_receipts[0],
                    verification_sha256=str(
                        retry_intent["verification_sha256"]
                    ),
                    failed_head_sha=str(
                        retry_intent["failed_head_sha"]
                    ),
                    current_head_sha=str(
                        retry_intent["current_head_sha"]
                    ),
                    receipts=tuple(receipts),
                    iteration=iteration,
                )
            if progress.action == "complete":
                retarget_fix_callback(
                    operation_id=spec["operation_id"],
                    run_id=spec["run_id"],
                    callback_pointer=".task-summary.json",
                )
                if notify_fix_finalization(iteration):
                    emit_compiled_pipeline_event(
                        spec["cwd"],
                        event="fix-final-retarget",
                        pipeline_id=pipeline.definition.pipeline_id,
                        pipeline_version=pipeline.definition.version,
                        profile=pipeline.definition.profile,
                        compiler_outcome="resolved",
                        definition_sha=pipeline.definition_sha256,
                        primitive_count=len(pipeline.definition.steps),
                        loop_iteration=iteration,
                        terminal_category="phases-complete",
                    )
                if (
                    retry_intent is not None
                    and git_head()
                    == retry_intent["current_head_sha"]
                ):
                    return
                fix_transport_complete = True
                return
            if spec["task_summary_pointer"].is_file():
                _atomic_json(
                    spec_path.parent
                    / "pipeline-fix"
                    / "early-summary.json",
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "status": "ignored-until-phases-complete",
                    },
                )
            if retry_intent is None:
                round_ = prepare_next_phase(
                    store,
                    parent,
                    definition_sha256=pipeline.definition_sha256,
                    approved_plan_sha256=approved_plan_sha256,
                    initial_head_sha=initial_head_sha,
                    receipts=tuple(receipts),
                    iteration=0,
                )
            else:
                round_ = prepare_retry_phase(
                    store,
                    parent,
                    definition_sha256=pipeline.definition_sha256,
                    reproduction_receipt=initial_receipts[0],
                    verification_sha256=str(
                        retry_intent["verification_sha256"]
                    ),
                    failed_head_sha=str(
                        retry_intent["failed_head_sha"]
                    ),
                    current_head_sha=str(
                        retry_intent["current_head_sha"]
                    ),
                    receipts=tuple(receipts),
                    iteration=iteration,
                )
            result_pointer = (
                f".task-pipeline/results/pass-{iteration}/"
                f"{round_.step_id}.json"
            )
            output_pointer = (
                f".task-pipeline/outputs/pass-{iteration}/"
                f"{round_.step_id}.md"
            )
            request = {
                "schema_version": 1,
                "operation_id": round_.spec.operation_id,
                "run_id": round_.run_id,
                "parent_operation_id": round_.parent_operation_id,
                "lane_id": round_.lane_id,
                "definition_sha256": round_.spec.contract_sha256,
                "step_id": round_.step_id,
                "iteration": round_.iteration,
                "input_schema": round_.input_schema,
                "input_sha256": round_.input_sha256,
                "input_head_sha": round_.input_head_sha,
                "prior_receipt_sha256": round_.prior_receipt_sha256,
                "verification_sha256": round_.verification_sha256,
                "output_schema": round_.output_schema,
                "result_pointer": result_pointer,
                "output_pointer": output_pointer,
            }
            _atomic_json(
                spec["cwd"] / ".task-pipeline-step-request.json",
                request,
            )
            retarget_fix_callback(
                operation_id=round_.spec.operation_id,
                run_id=round_.run_id,
                callback_pointer=".task-pipeline-step-callback.json",
            )
            notify_fix_phase(request)
            _generation, operation_id, run_id, callback_path = (
                _callback_target(spec)
            )
            if (
                operation_id != round_.spec.operation_id
                or run_id != round_.run_id
            ):
                raise RuntimeWorkerError(
                    "engineering/fix active callback target changed"
                )
            try:
                raw = callback_path.read_bytes()
            except FileNotFoundError:
                result_path = spec["cwd"] / result_pointer
                result_digest = _bounded_file_sha256(result_path)
                if result_digest:
                    if result_digest != fix_result_digest:
                        fix_result_digest = result_digest
                        fix_result_stable_reads = 1
                    else:
                        fix_result_stable_reads += 1
                    if (
                        fix_result_stable_reads >= 2
                        and fix_submit_attempt_digest != result_digest
                    ):
                        fix_submit_attempt_digest = result_digest
                        submitted = subprocess.run(
                            [
                                sys.executable,
                                str(trusted_vault / "scripts" / "pipeline-step-submit.py"),
                                "--worktree",
                                str(spec["cwd"]),
                            ],
                            cwd=spec["cwd"],
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if _submit_failure_requires_attention(
                            submitted, callback_path
                        ):
                            _atomic_json(
                                spec_path.parent
                                / "pipeline-fix"
                                / "submit-failed.json",
                                {
                                    "schema_version": 1,
                                    "operation_id": round_.spec.operation_id,
                                    "returncode": submitted.returncode,
                                    "status": "attention-required",
                                },
                            )
                            summary_attention(
                                "pipeline-fix-submit-failed",
                                AttentionReason.CALLBACK_INVALID,
                            )
                return
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                raise RuntimeWorkerError(
                    "engineering/fix phase callback is invalid"
                )
            digest = hashlib.sha256(raw).hexdigest()
            if digest != fix_callback_digest:
                fix_callback_digest = digest
                fix_callback_stable_reads = 1
                return
            fix_callback_stable_reads += 1
            if fix_callback_stable_reads < 2:
                return
            envelope = _envelope(json.loads(raw))
            receipt_path = (
                receipt_root
                / round_.step_id
                / "receipt.json"
            )
            accepted_receipt = accept_phase(
                store,
                round_,
                envelope,
                current_head_sha=git_head(),
                receipt_path=receipt_path,
            )
            callback_path.unlink()
            emit_compiled_pipeline_event(
                spec["cwd"],
                event="fix-phase-accepted",
                pipeline_id=pipeline.definition.pipeline_id,
                pipeline_version=pipeline.definition.version,
                profile=pipeline.definition.profile,
                compiler_outcome="resolved",
                definition_sha=pipeline.definition_sha256,
                primitive_count=len(pipeline.definition.steps),
                loop_iteration=accepted_receipt.iteration,
                terminal_category=accepted_receipt.step_id,
            )
            fix_callback_digest = ""
            fix_callback_stable_reads = 0
            fix_result_digest = ""
            fix_result_stable_reads = 0
            fix_submit_attempt_digest = ""
        except (
            FixWorkflowError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            summary_attention("pipeline-fix-callback-invalid")

    def notify_custom_step(request: dict[str, object]) -> None:
        operation_id = str(request["operation_id"])
        notify_path = (
            spec_path.parent
            / "pipeline-custom"
            / "notifications"
            / f"{operation_id}.json"
        )
        marker = {
            "schema_version": 1,
            "operation_id": operation_id,
            "step_id": str(request["step_id"]),
            "visit": int(request["visit"]),
            "status": "sent",
        }
        if notify_path.is_file() and not notify_path.is_symlink():
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError("custom step notification changed")
            return
        allowed = request["allowed_outcomes"]
        if not isinstance(allowed, list):
            raise RuntimeWorkerError("custom step outcomes are unavailable")
        message = (
            f"Typed custom step {request['step_id']} visit {request['visit']} "
            "is ready in .task-pipeline-step-request.json. Complete only this "
            "registered step, write its exact evidence/result, choose one of "
            f"these outcomes: {', '.join(str(item) for item in allowed)}; then "
            "publish with pipeline-step-submit.py. Remain in this same session "
            "for the next harness-owned transition."
        )
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("custom step notification exceeds its bound")
        cmux_adapter.send(spec["surface_id"], message)
        cmux_adapter.send_key(spec["surface_id"], "Enter")
        write_immutable_json(notify_path, marker)

    def notify_custom_finalization(receipt_count: int) -> None:
        notify_path = spec_path.parent / "pipeline-custom" / "finalization-notify.json"
        marker = {
            "schema_version": 1,
            "operation_id": spec["operation_id"],
            "receipt_count": receipt_count,
            "status": "sent",
        }
        if notify_path.is_file() and not notify_path.is_symlink():
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError("custom finalization notification changed")
            return
        message = (
            f"All {receipt_count} custom model-step receipts are accepted. "
            "Finish the task in this same session, commit the approved result, "
            "run only task-specific checks not already owned by the harness, "
            "and write the canonical .task-summary.json. The harness now owns "
            "configured verification and review."
        )
        cmux_adapter.send(spec["surface_id"], message)
        cmux_adapter.send_key(spec["surface_id"], "Enter")
        write_immutable_json(notify_path, marker)

    def notify_custom_attention(
        outcome: str,
        receipt: CustomStepReceipt | None,
    ) -> None:
        receipt_sha256 = receipt.receipt_sha256 if receipt is not None else ""
        path = spec["cwd"] / ".task-needs-attention.json"
        packet = {
            "version": 1,
            "id": f"custom-decision-{(receipt_sha256 or pipeline.definition_sha256)[:24]}",
            "status": "pending",
            "task_name": "custom pipeline decision",
            "category": "pipeline-decision",
            "reason": "The approved custom pipeline reached a declared terminal outcome",
            "question": f"Resolve declared outcome: {outcome}",
            "worktree": str(spec["cwd"]),
            "task_surface": spec["surface_id"],
            "raised_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "receipt_operation_id": receipt.operation_id if receipt is not None else "",
            "receipt_sha256": receipt_sha256,
            "allowed_decisions": ["stop", "reapprove-pipeline"],
        }
        if path.is_file() and not path.is_symlink():
            if json.loads(path.read_text(encoding="utf-8")) != packet:
                raise RuntimeWorkerError("custom decision packet changed")
        else:
            _atomic_json(path, packet)
        notify_path = spec_path.parent / "pipeline-custom" / "attention-notify.json"
        if notify_path.is_file() and not notify_path.is_symlink():
            return
        command = (
            "python3 "
            + shlex.quote(str(trusted_vault / "scripts" / "task_escalation.py"))
            + " resolve --worktree "
            + shlex.quote(str(spec["cwd"]))
            + " --decision <decision>"
        )
        cmux_adapter.send(
            spec["origin_surface"],
            "Typed custom pipeline escalation received. Inspect "
            f"{path} and resolve from the originating coordinator with: "
            f"{command}. Allowed decisions: stop, reapprove-pipeline.",
        )
        cmux_adapter.send_key(spec["origin_surface"], "Enter")
        write_immutable_json(
            notify_path,
            {
                "schema_version": 1,
                "operation_id": spec["operation_id"],
                "receipt_sha256": receipt_sha256,
                "status": "sent",
            },
        )

    def drive_custom_transport() -> None:
        nonlocal custom_callback_digest, custom_callback_stable_reads
        nonlocal custom_result_digest, custom_result_stable_reads
        nonlocal custom_submit_attempt_digest, custom_transport_complete
        if (
            not is_custom_pipeline
            or custom_pipeline_spec is None
            or pipeline is None
            or callback_handled
            or custom_transport_complete
        ):
            return
        try:
            meta = json.loads(
                (spec["cwd"] / ".task-meta.json").read_text(encoding="utf-8")
            )
            policy = meta.get("pipeline_policy") if isinstance(meta, dict) else None
            if (
                not isinstance(policy, dict)
                or policy.get("definition_sha256") != pipeline.definition_sha256
            ):
                raise RuntimeWorkerError("custom metadata mismatches its compiled contract")
            approved_plan_sha256 = str(meta.get("approved_plan_sha256") or "")
            controller_path = spec_path.parent / "pipeline-custom" / "controller.json"
            if controller_path.is_symlink():
                raise RuntimeWorkerError("custom controller must not be a symlink")
            if controller_path.is_file():
                controller = json.loads(controller_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(controller, dict)
                    or set(controller)
                    != {
                        "schema_version",
                        "operation_id",
                        "definition_sha256",
                        "approved_plan_sha256",
                        "initial_head_sha",
                    }
                    or controller.get("schema_version") != 1
                    or controller.get("operation_id") != spec["operation_id"]
                    or controller.get("definition_sha256") != pipeline.definition_sha256
                    or controller.get("approved_plan_sha256") != approved_plan_sha256
                ):
                    raise RuntimeWorkerError("custom controller receipt changed")
            else:
                initial_request = json.loads(
                    (spec["cwd"] / ".task-pipeline-step-request.json").read_text(
                        encoding="utf-8"
                    )
                )
                initial_head_sha = str(
                    initial_request.get("input_head_sha")
                    if isinstance(initial_request, dict)
                    else ""
                )
                if not re.fullmatch(r"[0-9a-f]{40,64}", initial_head_sha):
                    raise RuntimeWorkerError("custom initial HEAD is unavailable")
                controller = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "definition_sha256": pipeline.definition_sha256,
                    "approved_plan_sha256": approved_plan_sha256,
                    "initial_head_sha": initial_head_sha,
                }
                write_immutable_json(controller_path, controller)
            receipt_root = spec_path.parent / "pipeline-custom" / "receipts"
            receipts: list[CustomStepReceipt] = []
            if receipt_root.is_dir():
                paths = sorted(receipt_root.glob("*.json"))
                expected_names = [f"{index:03d}.json" for index in range(len(paths))]
                if [path.name for path in paths] != expected_names:
                    raise RuntimeWorkerError("custom receipts are not a contiguous prefix")
                receipts = [load_custom_receipt(path) for path in paths]
            parent = store.read(spec["owner_id"], spec["operation_id"])
            progress = reconcile_custom_sequence(
                parent,
                custom_pipeline_spec,
                definition_sha256=pipeline.definition_sha256,
                approved_plan_sha256=approved_plan_sha256,
                initial_head_sha=str(controller["initial_head_sha"]),
                receipts=tuple(receipts),
            )
            if progress.action == "attention":
                notify_custom_attention(progress.terminal_outcome, progress.prior_receipt)
                summary_attention(
                    f"pipeline-custom-{progress.terminal_outcome}",
                    AttentionReason.ATTENTION_REQUIRED,
                )
                return
            if progress.action == "complete":
                custom_transport_complete = True
                notify_custom_finalization(len(receipts))
                emit_compiled_pipeline_event(
                    spec["cwd"],
                    event="custom-model-steps-complete",
                    pipeline_id=pipeline.definition.pipeline_id,
                    pipeline_version=pipeline.definition.version,
                    profile=pipeline.definition.profile,
                    compiler_outcome="custom-resolved",
                    definition_sha=pipeline.definition_sha256,
                    primitive_count=len(pipeline.definition.steps),
                    loop_iteration=max(0, len(receipts) - 1),
                    terminal_category="model-steps-complete",
                )
                return
            if spec["task_summary_pointer"].is_file():
                _atomic_json(
                    spec_path.parent / "pipeline-custom" / "early-summary.json",
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "status": "ignored-until-model-steps-complete",
                    },
                )
            round_ = prepare_custom_step(
                store,
                parent,
                custom_pipeline_spec,
                definition_sha256=pipeline.definition_sha256,
                approved_plan_sha256=approved_plan_sha256,
                initial_head_sha=str(controller["initial_head_sha"]),
                receipts=tuple(receipts),
            )
            request = custom_step_request(round_)
            _atomic_json(spec["cwd"] / ".task-pipeline-step-request.json", request)
            retarget_fix_callback(
                operation_id=round_.spec.operation_id,
                run_id=round_.run_id,
                callback_pointer=".task-pipeline-step-callback.json",
            )
            notify_custom_step(request)
            _generation, operation_id, run_id, callback_path = _callback_target(spec)
            if operation_id != round_.spec.operation_id or run_id != round_.run_id:
                raise RuntimeWorkerError("custom callback target changed")

            if not callback_path.exists():
                result_path = spec["cwd"] / str(request["result_pointer"])
                result_digest = _bounded_file_sha256(result_path)
                if result_digest:
                    if result_digest != custom_result_digest:
                        custom_result_digest = result_digest
                        custom_result_stable_reads = 1
                    else:
                        custom_result_stable_reads += 1
                    if (
                        custom_result_stable_reads >= 2
                        and custom_submit_attempt_digest != result_digest
                    ):
                        custom_submit_attempt_digest = result_digest
                        submitted = subprocess.run(
                            [
                                sys.executable,
                                str(trusted_vault / "scripts" / "pipeline-step-submit.py"),
                                "--worktree",
                                str(spec["cwd"]),
                            ],
                            cwd=spec["cwd"],
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if _submit_failure_requires_attention(
                            submitted, callback_path
                        ):
                            _atomic_json(
                                spec_path.parent
                                / "pipeline-custom"
                                / "submit-failed.json",
                                {
                                    "schema_version": 1,
                                    "operation_id": round_.spec.operation_id,
                                    "returncode": submitted.returncode,
                                    "status": "attention-required",
                                },
                            )
                            summary_attention(
                                "pipeline-custom-submit-failed",
                                AttentionReason.CALLBACK_INVALID,
                            )
                return
            raw = callback_path.read_bytes()
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                raise RuntimeWorkerError("custom callback is invalid")
            digest = hashlib.sha256(raw).hexdigest()
            if digest != custom_callback_digest:
                custom_callback_digest = digest
                custom_callback_stable_reads = 1
                return
            custom_callback_stable_reads += 1
            if custom_callback_stable_reads < 2:
                return
            envelope = _envelope(json.loads(raw))
            accepted = accept_custom_step(
                store,
                round_,
                envelope,
                current_head_sha=git_head(),
                receipt_path=receipt_root / f"{round_.visit:03d}.json",
            )
            callback_path.unlink()
            emit_compiled_pipeline_event(
                spec["cwd"],
                event="custom-step-accepted",
                pipeline_id=pipeline.definition.pipeline_id,
                pipeline_version=pipeline.definition.version,
                profile=pipeline.definition.profile,
                compiler_outcome="custom-resolved",
                definition_sha=pipeline.definition_sha256,
                primitive_count=len(pipeline.definition.steps),
                loop_iteration=accepted.visit,
                terminal_category=accepted.step_id,
            )
            custom_callback_digest = ""
            custom_callback_stable_reads = 0
            custom_result_digest = ""
            custom_result_stable_reads = 0
            custom_submit_attempt_digest = ""
        except (
            CustomSequenceError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            summary_attention("pipeline-custom-callback-invalid")

    def recover_task_summary_attention() -> None:
        nonlocal callback_handled, summary_digest, summary_stable_reads
        nonlocal summary_attention_revision
        if (
            spec["callback_mode"] != "task-summary"
            or not callback_handled
            or summary_attention_revision < 0
        ):
            return
        try:
            current = store.read(
                spec["owner_id"], spec["operation_id"]
            )
        except Exception:
            return
        if (
            current.state not in CALLBACK_WAIT_STATES
            or current.revision <= summary_attention_revision
        ):
            return
        _atomic_json(
            spec_path.parent / "callback-recovery.json",
            {
                "schema_version": 1,
                "operation_id": spec["operation_id"],
                "attention_revision": summary_attention_revision,
                "resumed_revision": current.revision,
                "status": "resumed",
            },
        )
        callback_handled = False
        summary_digest = ""
        summary_stable_reads = 0
        summary_attention_revision = -1

    def inspect_task_summary() -> None:
        nonlocal callback_handled, summary_digest, summary_stable_reads
        if callback_handled:
            return
        if (
            _pipeline_name == "engineering/fix"
            and not fix_transport_complete
            or is_custom_pipeline
            and not custom_transport_complete
        ):
            return
        summary_path: Path = spec["task_summary_pointer"]
        try:
            raw = summary_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            summary_attention("wiki-summary-unreadable")
            return
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            summary_attention("wiki-summary-invalid")
            return
        finish_task_summary(raw)

    def inspect_research() -> None:
        nonlocal active_target, last_digest, stable_reads, callback_handled
        if callback_handled:
            return
        try:
            target = _callback_target(spec)
        except RuntimeWorkerError:
            summary_attention("research-callback-invalid")
            return
        if target != active_target:
            if active_target is not None and target[0] <= active_target[0]:
                return
            active_target = target
            last_digest = ""
            stable_reads = 0
        generation, operation_id, run_id, callback_path = target
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            summary_attention("research-callback-unreadable")
            return
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            summary_attention("research-callback-invalid")
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest != last_digest:
            last_digest = digest
            stable_reads = 1
            return
        stable_reads += 1
        if stable_reads < 2:
            return
        try:
            if spec["callback_mode"] == "research-fetch":
                normalized_raw = _normalize_fetch_errors_at_provider_boundary(
                    callback_path,
                    raw,
                )
                if normalized_raw != raw:
                    last_digest = hashlib.sha256(normalized_raw).hexdigest()
                    stable_reads = 1
                    return
                artifact = load_artifact(
                    str(callback_path),
                    expected_run_id=run_id,
                    expected_request_sha256=spec[
                        "research_request_sha256"
                    ],
                )
                payload = {
                    "stage": "fetch",
                    "artifact_path": "artifact.json",
                    "artifact_sha256": digest,
                    "source_count": len(artifact["sources"]),
                }
            else:
                if (
                    not research_input_sha256
                    or _research_input_provenance(
                        spec,
                        spec_path,
                        create=False,
                    )
                    != research_input_sha256
                ):
                    raise RuntimeWorkerError(
                        "research input artifact changed after launch"
                    )
                artifact = load_artifact(str(spec["cwd"] / "artifact.json"))
                complete = json.loads(raw)
                result = validate_result_artifact(
                    complete,
                    root=spec["cwd"],
                    expected_run_id=run_id,
                    source_urls={
                        str(source["url"])
                        for source in artifact["sources"]
                    },
                )
                payload = {
                    "stage": "synth",
                    "artifact_path": result["artifact"]["path"],
                    "artifact_sha256": result["artifact"]["sha256"],
                    "citation_count": len(
                        result["artifact"]["citations"]
                    ),
                }
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode()
            payload_sha256 = hashlib.sha256(encoded).hexdigest()
            envelope = CallbackEnvelope(
                callback_id=(
                    f"research-{payload['stage']}-"
                    f"{payload_sha256[:24]}"
                ),
                operation_id=operation_id,
                run_id=run_id,
                kind="research",
                payload=payload,
                payload_sha256=payload_sha256,
            )
            acceptance = CallbackBroker(
                store, spec["owner_id"]
            ).accept(envelope)
            callback_handled = True
            _atomic_json(
                spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "generation": generation,
                    "callback_id": envelope.callback_id,
                    "operation_id": operation_id,
                    "status": (
                        "duplicate" if acceptance.duplicate else "accepted"
                    ),
                },
            )
            notify_path = spec_path.parent / "research-notify.json"
            if notify_path.exists():
                marker = json.loads(notify_path.read_text(encoding="utf-8"))
                if (
                    marker.get("schema_version") != 1
                    or marker.get("callback_id") != envelope.callback_id
                ):
                    raise RuntimeWorkerError(
                        "research notification marker is invalid"
                    )
                if marker.get("status") == "sent":
                    return
                if marker.get("status") == "pending":
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                    return
                raise RuntimeWorkerError(
                    "research notification marker state is invalid"
                )
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "pending",
                },
            )
            cmux_adapter.send(
                spec["origin_surface"], spec["callback_wake"]
            )
            cmux_adapter.send_key(spec["origin_surface"], "Enter")
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "sent",
                },
            )
        except (
            CallbackError,
            ResearchContractError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            summary_attention("research-callback-invalid")
            return

    def finish_task_summary(raw: bytes) -> None:
        nonlocal callback_handled, summary_digest, summary_stable_reads
        nonlocal fix_transport_complete
        digest = hashlib.sha256(raw).hexdigest()
        if digest != summary_digest:
            summary_digest = digest
            summary_stable_reads = 1
            return
        summary_stable_reads += 1
        if summary_stable_reads < 2:
            return
        try:
            raw_summary = json.loads(raw)
            meta_path = spec["cwd"] / ".task-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict) or meta.get("version") not in {3, 4}:
                raise RuntimeWorkerError("task summary requires v3 or v4 metadata")
            summary = validate_summary_for_task(
                raw_summary,
                meta,
                allow_missing_session=True,
                require_schema=True,
            )
            if (
                meta.get("task_id") != spec["operation_id"]
                or Path(str(meta.get("worktree") or "")).resolve()
                != spec["cwd"]
                or meta.get("task_surface") != spec["surface_id"]
            ):
                raise RuntimeWorkerError(
                    "task summary metadata mismatches the runtime owner"
                )
            current_session = str(meta.get("origin_session") or "")
            validate_handoff(meta, summary, current_session)
            review = task_review_status(
                meta,
                spec["cwd"],
                expected_vault=trusted_vault,
                expected_operation_id=spec["operation_id"],
            )
            operation = store.read(
                spec["owner_id"], spec["operation_id"]
            )
            if (
                pipeline is None
                or operation.spec.contract_sha256
                != pipeline.definition_sha256
            ):
                summary_attention(
                    "pipeline-contract-drift",
                    AttentionReason.CONTRACT_DRIFT,
                )
                return
            marker_path = spec_path.parent / "pipeline-review-start.json"
            marker = None
            if marker_path.is_file() and not marker_path.is_symlink():
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if (
                    marker.get("schema_version") != 1
                    or marker.get("operation_id") != spec["operation_id"]
                    or marker.get("definition_sha256")
                    != pipeline.definition_sha256
                    or marker.get("status") not in {"pending", "started"}
                ):
                    raise RuntimeWorkerError(
                        "pipeline review launch receipt is invalid"
                    )

            def review_drive_sha256() -> str:
                digest = hashlib.sha256()
                gate_state = review.gate_root / "review-gate.json"
                if gate_state.is_file():
                    if gate_state.is_symlink():
                        raise RuntimeWorkerError(
                            "review gate state cannot be a symlink"
                        )
                    digest.update(gate_state.read_bytes())
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=spec["cwd"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if head.returncode:
                    raise RuntimeWorkerError(
                        "automatic review cannot resolve product HEAD"
                    )
                digest.update(head.stdout.strip().encode())
                callback_root = (
                    trusted_vault
                    / ".vault-meta"
                    / "harness"
                    / "review-runtime"
                    / spec["operation_id"]
                    / "callbacks"
                )
                if callback_root.is_dir():
                    for callback in sorted(
                        callback_root.rglob(".review-callback.json")
                    ):
                        if callback.is_symlink():
                            raise RuntimeWorkerError(
                                "review callback cannot be a symlink"
                            )
                        digest.update(
                            callback.relative_to(callback_root)
                            .as_posix()
                            .encode()
                        )
                        digest.update(callback.read_bytes())
                return digest.hexdigest()

            def drive_review() -> bool:
                input_sha256 = review_drive_sha256()
                _atomic_json(
                    marker_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "definition_sha256": pipeline.definition_sha256,
                        "status": "pending",
                        "drive_sha256": input_sha256,
                    },
                )
                try:
                    if review_launcher is not None:
                        review_launcher(trusted_vault, spec["cwd"])
                    else:
                        runner = (
                            trusted_vault
                            / "scripts"
                            / "task-review-runner.py"
                        )
                        if not runner.is_file() or runner.is_symlink():
                            raise RuntimeWorkerError(
                                "trusted task review runner is unavailable"
                            )
                        launched = subprocess.run(
                            [
                                sys.executable,
                                str(runner),
                                "run",
                                "--worktree",
                                str(spec["cwd"]),
                            ],
                            cwd=trusted_vault,
                            text=True,
                            capture_output=True,
                            check=False,
                            timeout=10,
                        )
                        if launched.returncode != 0:
                            raise RuntimeWorkerError(
                                "automatic task review drive failed"
                            )
                except (
                    OSError,
                    RuntimeWorkerError,
                    subprocess.TimeoutExpired,
                ):
                    summary_attention(
                        "review-drive-failed",
                        AttentionReason.ATTENTION_REQUIRED,
                    )
                    return False
                _atomic_json(
                    marker_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "definition_sha256": pipeline.definition_sha256,
                        "status": "started",
                        "drive_sha256": input_sha256,
                    },
                )
                return True

            def review_gate_state() -> dict[str, object]:
                gate_path = review.gate_root / "review-gate.json"
                if not gate_path.is_file() or gate_path.is_symlink():
                    return {}
                state = json.loads(gate_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(state, dict)
                    or state.get("schema_version") != 1
                    or state.get("dispatch_operation_id")
                    != spec["operation_id"]
                ):
                    raise RuntimeWorkerError(
                        "review gate state is invalid"
                    )
                return state

            def notify_review_resolution(
                gate_state: dict[str, object],
            ) -> None:
                awaiting = gate_state.get("awaiting_resolution")
                if not isinstance(awaiting, dict) or not awaiting:
                    raise RuntimeWorkerError(
                        "review resolution evidence is unavailable"
                    )
                findings: list[dict[str, object]] = []
                reviewed_heads: set[str] = set()
                for axis in sorted(awaiting):
                    evidence = awaiting[axis]
                    if not isinstance(evidence, dict):
                        raise RuntimeWorkerError(
                            "review resolution evidence is invalid"
                        )
                    pointer = Path(str(evidence.get("pointer") or ""))
                    result_path = (review.gate_root / pointer).resolve()
                    if (
                        pointer.is_absolute()
                        or review.gate_root not in result_path.parents
                        or not result_path.is_file()
                        or result_path.is_symlink()
                    ):
                        raise RuntimeWorkerError(
                            "review result pointer is invalid"
                        )
                    result = json.loads(
                        result_path.read_text(encoding="utf-8")
                    )
                    rows = (
                        result.get("findings")
                        if isinstance(result, dict)
                        else None
                    )
                    if (
                        not isinstance(result, dict)
                        or result.get("axis") != axis
                        or not isinstance(rows, list)
                    ):
                        raise RuntimeWorkerError(
                            "review result evidence is invalid"
                        )
                    for finding in rows:
                        if not isinstance(finding, dict):
                            raise RuntimeWorkerError(
                                "review finding evidence is invalid"
                            )
                        findings.append(dict(finding))
                    reviewed_heads.add(
                        str(evidence.get("reviewed_head_sha") or "")
                    )
                if (
                    not findings
                    or len(findings) > 50
                    or len(reviewed_heads) != 1
                    or "" in reviewed_heads
                ):
                    raise RuntimeWorkerError(
                        "review decision packet is invalid"
                    )
                material_findings = [
                    finding
                    for finding in findings
                    if finding.get("severity") in MATERIAL_SEVERITIES
                ]
                if not material_findings:
                    raise RuntimeWorkerError(
                        "review decision packet has no material findings"
                    )
                material_ids = [
                    str(finding.get("finding_id") or "")
                    for finding in material_findings
                ]
                if (
                    "" in material_ids
                    or len(material_ids) != len(set(material_ids))
                ):
                    raise RuntimeWorkerError(
                        "review decision packet finding identities are invalid"
                    )
                reviewed_head = next(iter(reviewed_heads))
                packet = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "reviewed_head_sha": reviewed_head,
                    "allowed_dispositions": sorted(DISPOSITIONS),
                    "resolution_path": ".task-review-resolution.json",
                    "material_finding_ids": material_ids,
                    "findings": findings,
                }
                encoded = json.dumps(
                    packet, sort_keys=True, separators=(",", ":")
                ).encode()
                if len(encoded) > MAX_OUTBOX_BYTES:
                    raise RuntimeWorkerError(
                        "review decision packet exceeds size cap"
                    )
                packet_sha256 = hashlib.sha256(encoded).hexdigest()
                packet_path = spec["cwd"] / ".task-review.json"
                if packet_path.is_symlink():
                    raise RuntimeWorkerError(
                        "review decision packet cannot be a symlink"
                    )
                if packet_path.exists():
                    current = json.loads(
                        packet_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(current, dict)
                        or current.get("schema_version") != 1
                        or current.get("operation_id")
                        != spec["operation_id"]
                    ):
                        raise RuntimeWorkerError(
                            "review decision packet identity changed"
                        )
                _atomic_json(packet_path, packet)
                resolution_path = (
                    spec["cwd"] / ".task-review-resolution.json"
                )
                if resolution_path.is_symlink():
                    raise RuntimeWorkerError(
                        "review resolution response cannot be a symlink"
                    )
                write_resolution_template = True
                if resolution_path.exists():
                    current_resolution = json.loads(
                        resolution_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(current_resolution, dict)
                        or current_resolution.get("schema_version") != 1
                        or current_resolution.get("operation_id")
                        != spec["operation_id"]
                    ):
                        raise RuntimeWorkerError(
                            "review resolution response identity changed"
                        )
                    write_resolution_template = (
                        current_resolution.get("reviewed_head_sha")
                        != reviewed_head
                    )
                if write_resolution_template:
                    _atomic_json(
                        resolution_path,
                        {
                            "schema_version": 1,
                            "operation_id": spec["operation_id"],
                            "reviewed_head_sha": reviewed_head,
                            "resolved_head_sha": "",
                            "resolutions": [
                                {
                                    "finding_id": str(
                                        finding.get("finding_id") or ""
                                    ),
                                    "disposition": "",
                                    "rationale": "",
                                    "follow_up": "",
                                }
                                for finding in material_findings
                            ],
                        },
                    )
                notify_path = (
                    spec_path.parent
                    / "pipeline-review-resolution-notify.json"
                )
                notified = None
                if notify_path.is_file() and not notify_path.is_symlink():
                    notified = json.loads(
                        notify_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(notified, dict)
                        or notified.get("schema_version") != 1
                        or notified.get("operation_id")
                        != spec["operation_id"]
                    ):
                        raise RuntimeWorkerError(
                            "review resolution notification is invalid"
                        )
                    if (
                        notified.get("packet_sha256") == packet_sha256
                        and notified.get("status") == "sent"
                    ):
                        return
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "reviewed_head_sha": packet[
                            "reviewed_head_sha"
                        ],
                        "summary_sha256": digest,
                        "status": "pending",
                    },
                )
                message = (
                    "Typed review findings are ready in "
                    f"{packet_path.name}. Resolve every material finding in "
                    f"{resolution_path.name} as applied, rejected, or "
                    "out-of-scope; include bounded rationale, and a durable "
                    "follow-up pointer for out-of-scope. Commit a new HEAD "
                    "and set resolved_head_sha; for a material fork use the "
                    "task_escalation.py raise contract. Do not launch review. "
                    "Refresh .task-summary.json after the commit so it covers "
                    "the final HEAD. Remain available for same-session "
                    "verification."
                )
                if len(message.encode()) > 4096:
                    raise RuntimeWorkerError(
                        "review resolution notification is too large"
                    )
                cmux_adapter.send(spec["surface_id"], message)
                cmux_adapter.send_key(spec["surface_id"], "Enter")
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "reviewed_head_sha": packet[
                            "reviewed_head_sha"
                        ],
                        "summary_sha256": digest,
                        "status": "sent",
                    },
                )

            def wait_for_summary_refresh_after_resolution(
                gate_state: dict[str, object],
            ) -> bool:
                resolution_path = (
                    spec_path.parent
                    / "pipeline-review-resolution-notify.json"
                )
                if not resolution_path.is_file():
                    return False
                if resolution_path.is_symlink():
                    raise RuntimeWorkerError(
                        "review resolution notification cannot be a symlink"
                    )
                resolution = json.loads(
                    resolution_path.read_text(encoding="utf-8")
                )
                reviewed_head = str(
                    resolution.get("reviewed_head_sha") or ""
                )
                initial_summary = str(
                    resolution.get("summary_sha256") or ""
                )
                context = gate_state.get("context")
                approved_head = (
                    str(context.get("head_sha") or "")
                    if isinstance(context, dict)
                    else ""
                )
                if (
                    resolution.get("schema_version") != 1
                    or resolution.get("operation_id")
                    != spec["operation_id"]
                    or resolution.get("status") != "sent"
                    or not re.fullmatch(r"[0-9a-f]{40,64}", reviewed_head)
                    or not re.fullmatch(r"[0-9a-f]{64}", initial_summary)
                    or not re.fullmatch(r"[0-9a-f]{40,64}", approved_head)
                ):
                    raise RuntimeWorkerError(
                        "review resolution summary binding is invalid"
                    )
                if approved_head == reviewed_head or digest != initial_summary:
                    return False
                notify_path = (
                    spec_path.parent
                    / "pipeline-summary-refresh-notify.json"
                )
                marker = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "approved_head_sha": approved_head,
                    "summary_sha256": digest,
                }
                if notify_path.is_file():
                    if notify_path.is_symlink():
                        raise RuntimeWorkerError(
                            "summary refresh notification cannot be a symlink"
                        )
                    existing = json.loads(
                        notify_path.read_text(encoding="utf-8")
                    )
                    if all(
                        existing.get(field) == value
                        for field, value in marker.items()
                    ) and existing.get("status") == "sent":
                        return True
                _atomic_json(notify_path, {**marker, "status": "pending"})
                message = (
                    "Refresh .task-summary.json before finalization: its body "
                    "still describes the pre-resolution HEAD. Preserve the "
                    "exact schema/type/title/session, cover every applied or "
                    "rejected finding, and summarize final HEAD "
                    f"{approved_head}."
                )
                cmux_adapter.send(spec["surface_id"], message)
                cmux_adapter.send_key(spec["surface_id"], "Enter")
                _atomic_json(notify_path, {**marker, "status": "sent"})
                return True

            steps = pipeline.definition.steps
            primitive_shape = tuple(
                step.primitive_id for step in steps
            )
            if not is_custom_pipeline and primitive_shape not in {
                ("model_step", "review"),
                ("model_step", "verify", "review"),
                (
                    "model_step",
                    "model_step",
                    "model_step",
                    "model_step",
                    "verify",
                    "review",
                ),
            }:
                raise RuntimeWorkerError(
                    "compiled production pipeline shape is unsupported"
                )
            verify_step = next(
                (
                    step
                    for step in steps
                    if step.primitive_id == "verify"
                ),
                None,
            )
            verification_controller_receipt_path = (
                spec_path.parent / "pipeline-step-verify.json"
            )
            review_policy = meta.get("review_policy")
            if not isinstance(review_policy, dict):
                raise RuntimeWorkerError(
                    "task verification policy is unavailable"
                )
            profiles = load_profiles(
                trusted_vault / "config" / "verification-profiles.toml"
            )
            profile_name = str(
                review_policy.get("verification_profile") or ""
            )
            profile = profiles.get(profile_name)
            if (
                profile is None
                or profile.sha256
                != review_policy.get("verification_profile_sha256")
            ):
                raise RuntimeWorkerError(
                    "task verification profile binding is stale"
                )
            verification_head = ""
            if verify_step is not None:
                head_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=spec["cwd"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                verification_head = head_result.stdout.strip()
                if (
                    head_result.returncode
                    or not re.fullmatch(
                        r"[0-9a-f]{40,64}", verification_head
                    )
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification HEAD is unavailable"
                    )
            verification_input_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "definition_sha256": (
                            pipeline.definition_sha256
                        ),
                        "head_sha": verification_head,
                        "profile_sha256": profile.sha256,
                        "schema_version": (
                            verify_step.schema_version
                            if verify_step is not None
                            else 1
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            verification_effect_id = (
                "pipeline-verify-"
                + verification_input_sha256[:32]
            )
            (
                verification_spec,
                verification_lane_id,
                verification_run_id,
            ) = _pipeline_verify_identity(
                operation.spec,
                definition_sha256=pipeline.definition_sha256,
                input_sha256=verification_input_sha256,
                profile=profile.name,
            )
            verification_root = (
                spec_path.parent
                / "pipeline-verification"
                / verification_spec.operation_id
            )
            verification_receipt_path = (
                verification_root / "receipt.json"
            )

            def load_verification_receipt(
                receipt_path: Path,
            ) -> dict[str, object] | None:
                if not receipt_path.exists():
                    return None
                if (
                    not receipt_path.is_file()
                    or receipt_path.is_symlink()
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification receipt is invalid"
                    )
                receipt = json.loads(
                    receipt_path.read_text(encoding="utf-8")
                )
                evidence = (
                    receipt.get("evidence")
                    if isinstance(receipt, dict)
                    else None
                )
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("schema_version") != 1
                    or receipt.get("parent_operation_id")
                    != spec["operation_id"]
                    or receipt.get("definition_sha256")
                    != pipeline.definition_sha256
                    or receipt.get("step_id") != "verify"
                    or receipt.get("profile") != profile.name
                    or receipt.get("profile_sha256")
                    != profile.sha256
                    or not re.fullmatch(
                        r"[0-9a-f]{40,64}",
                        str(receipt.get("head_sha") or ""),
                    )
                    or receipt.get("status")
                    not in {"complete", "failed"}
                    or not IDENTIFIER.fullmatch(
                        str(receipt.get("operation_id") or "")
                    )
                    or not IDENTIFIER.fullmatch(
                        str(receipt.get("lane_id") or "")
                    )
                    or not IDENTIFIER.fullmatch(
                        str(receipt.get("run_id") or "")
                    )
                    or not isinstance(evidence, list)
                    or not evidence
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification receipt is invalid"
                    )
                receipt_head = str(receipt["head_sha"])
                receipt_input_sha256 = str(
                    receipt.get("input_sha256") or ""
                )
                if not re.fullmatch(
                    r"[0-9a-f]{64}", receipt_input_sha256
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification input identity is invalid"
                    )
                (
                    expected_spec,
                    expected_lane_id,
                    expected_run_id,
                ) = _pipeline_verify_identity(
                    operation.spec,
                    definition_sha256=pipeline.definition_sha256,
                    input_sha256=receipt_input_sha256,
                    profile=profile.name,
                )
                if (
                    receipt.get("input_sha256")
                    != receipt_input_sha256
                    or receipt.get("operation_id")
                    != expected_spec.operation_id
                    or receipt.get("lane_id") != expected_lane_id
                    or receipt.get("run_id") != expected_run_id
                    or receipt.get("effect_id")
                    != "pipeline-verify-"
                    + receipt_input_sha256[:32]
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification replay identity is invalid"
                    )
                exit_codes: list[int] = []
                heads: set[str] = set()
                command_ids: list[str] = []
                for row in evidence:
                    if (
                        not isinstance(row, dict)
                        or row.get("profile") != profile.name
                        or row.get("profile_sha256")
                        != profile.sha256
                        or type(row.get("exit_code")) is not int
                        or not re.fullmatch(
                            r"[0-9a-f]{40,64}",
                            str(row.get("head_sha") or ""),
                        )
                    ):
                        raise RuntimeWorkerError(
                            "pipeline verification evidence is invalid"
                        )
                    pointer = Path(
                        str(row.get("output_pointer") or "")
                    )
                    output = (spec_path.parent / pointer).resolve()
                    evidence_root = (
                        spec_path.parent / "pipeline-verification"
                    ).resolve()
                    if (
                        pointer.is_absolute()
                        or evidence_root not in output.parents
                        or not output.is_file()
                        or output.is_symlink()
                    ):
                        raise RuntimeWorkerError(
                            "pipeline verification output is invalid"
                        )
                    exit_codes.append(int(row["exit_code"]))
                    heads.add(str(row["head_sha"]))
                    command_ids.append(str(row.get("command_id") or ""))
                succeeded = all(code == 0 for code in exit_codes)
                expected_command_ids = [
                    f"{profile.name}-{index + 1}"
                    for index in range(
                        len(compose_commands(profile, pipeline_extra_commands))
                    )
                ]
                if (
                    len(heads) != 1
                    or heads != {receipt_head}
                    or command_ids
                    != expected_command_ids[: len(command_ids)]
                    or (
                        succeeded
                        and len(command_ids)
                        != len(expected_command_ids)
                    )
                    or (
                        not succeeded
                        and exit_codes[-1] == 0
                    )
                    or (
                        receipt["status"] == "complete"
                    )
                    != succeeded
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification outcome is invalid"
                    )
                stored = store.read(
                    spec["owner_id"], expected_spec.operation_id
                )
                if (
                    stored.spec != expected_spec
                    or stored.lane_id != expected_lane_id
                    or stored.run_id != expected_run_id
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification operation identity is invalid"
                    )
                expected_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / expected_spec.operation_id
                    / "receipt.json"
                )
                if receipt_path.resolve() != expected_path.resolve():
                    raise RuntimeWorkerError(
                        "pipeline verification receipt pointer is invalid"
                    )
                return receipt

            def controller_verification_receipt(
            ) -> dict[str, object] | None:
                linked: dict[str, object] | None = None
                if verification_controller_receipt_path.exists():
                    if (
                        not verification_controller_receipt_path.is_file()
                        or verification_controller_receipt_path.is_symlink()
                    ):
                        raise RuntimeWorkerError(
                            "pipeline verification controller receipt is invalid"
                        )
                    raw_linked = json.loads(
                        verification_controller_receipt_path.read_text(
                            encoding="utf-8"
                        )
                    )
                    if not isinstance(raw_linked, dict):
                        raise RuntimeWorkerError(
                            "pipeline verification controller receipt is invalid"
                        )
                    linked_operation_id = str(
                        raw_linked.get("operation_id") or ""
                    )
                    if not IDENTIFIER.fullmatch(linked_operation_id):
                        raise RuntimeWorkerError(
                            "pipeline verification controller receipt is invalid"
                        )
                    child_path = (
                        spec_path.parent
                        / "pipeline-verification"
                        / linked_operation_id
                        / "receipt.json"
                    )
                    linked = load_verification_receipt(child_path)
                    if linked != raw_linked:
                        raise RuntimeWorkerError(
                            "pipeline verification controller linkage is invalid"
                        )

                receipts_root = (
                    spec_path.parent / "pipeline-verification"
                )
                receipts = (
                    [
                        receipt
                        for path in receipts_root.glob("*/receipt.json")
                        if (
                            receipt := load_verification_receipt(path)
                        )
                        is not None
                    ]
                    if receipts_root.is_dir()
                    else []
                )
                unresolved_failures = [
                    receipt
                    for receipt in receipts
                    if receipt["status"] == "failed"
                    and not verification_response_accepted(receipt)
                ]
                if len(unresolved_failures) > 1:
                    raise RuntimeWorkerError(
                        "multiple failed verification children need reconciliation"
                    )
                if unresolved_failures:
                    recovered = unresolved_failures[0]
                    if recovered != linked:
                        link_verification_receipt(recovered)
                    return recovered
                current_receipts = [
                    receipt
                    for receipt in receipts
                    if receipt["operation_id"]
                    == verification_spec.operation_id
                ]
                if len(current_receipts) > 1:
                    raise RuntimeWorkerError(
                        "duplicate verification child receipts are invalid"
                    )
                if current_receipts:
                    recovered = current_receipts[0]
                    if recovered != linked:
                        link_verification_receipt(recovered)
                    return recovered
                return linked

            def verification_receipt() -> dict[str, object] | None:
                receipt = load_verification_receipt(
                    verification_receipt_path
                )
                if receipt is None:
                    return None
                if (
                    receipt["head_sha"] != verification_head
                    or receipt["operation_id"]
                    != verification_spec.operation_id
                ):
                    return None
                return receipt

            def verification_response_accepted(
                receipt: dict[str, object],
            ) -> bool:
                response_receipt_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / str(receipt["operation_id"])
                    / "response-receipt.json"
                )
                if not response_receipt_path.exists():
                    return False
                if (
                    not response_receipt_path.is_file()
                    or response_receipt_path.is_symlink()
                ):
                    raise RuntimeWorkerError(
                        "verification response receipt is invalid"
                    )
                accepted = json.loads(
                    response_receipt_path.read_text(encoding="utf-8")
                )
                if (
                    not isinstance(accepted, dict)
                    or accepted.get("schema_version") != 1
                    or accepted.get("operation_id")
                    != spec["operation_id"]
                    or accepted.get("verification_operation_id")
                    != receipt["operation_id"]
                    or accepted.get("failed_head_sha")
                    != receipt["head_sha"]
                    or accepted.get("status") != "accepted"
                    or not re.fullmatch(
                        r"[0-9a-f]{40,64}",
                        str(accepted.get("resubmitted_head_sha") or ""),
                    )
                    or accepted.get("resubmitted_head_sha")
                    == receipt["head_sha"]
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(accepted.get("response_sha256") or ""),
                    )
                ):
                    raise RuntimeWorkerError(
                        "verification response receipt is invalid"
                    )
                return True

            def link_verification_receipt(
                receipt: dict[str, object],
            ) -> None:
                if verification_controller_receipt_path.is_symlink():
                    raise RuntimeWorkerError(
                        "pipeline verification controller receipt is invalid"
                    )
                _atomic_json(
                    verification_controller_receipt_path, receipt
                )

            def failed_verification_count() -> int:
                count = 0
                receipts_root = (
                    spec_path.parent / "pipeline-verification"
                )
                if not receipts_root.is_dir():
                    return 0
                for path in receipts_root.glob("*/receipt.json"):
                    receipt = load_verification_receipt(path)
                    if receipt is not None and receipt["status"] == "failed":
                        count += 1
                return count

            def fix_retry_policy() -> tuple[str, int]:
                raw_policy = meta.get("pipeline_policy")
                if not isinstance(raw_policy, dict):
                    raise RuntimeWorkerError(
                        "engineering/fix completion policy is unavailable"
                    )
                completion = str(
                    raw_policy.get("completion_policy") or ""
                )
                limit = raw_policy.get("total_pass_limit")
                if (
                    completion not in {"attention", "autonomous"}
                    or type(limit) is not int
                    or limit
                    != {
                        "attention": 2,
                        "autonomous": 3,
                    }[completion]
                ):
                    raise RuntimeWorkerError(
                        "engineering/fix completion policy is invalid"
                    )
                return completion, limit

            def schedule_fix_retry(
                failed: dict[str, object],
            ) -> None:
                nonlocal fix_transport_complete
                completion, total_limit = fix_retry_policy()
                completed_passes = failed_verification_count()
                if completed_passes < 1:
                    raise RuntimeWorkerError(
                        "engineering/fix failed-pass count is invalid"
                    )
                verification_sha256 = hashlib.sha256(
                    json.dumps(
                        failed,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if completed_passes >= total_limit:
                    if completion == "attention":
                        summary_attention(
                            "pipeline-verification-retry-exhausted",
                            AttentionReason.RETRY_EXHAUSTED,
                            write_error=False,
                        )
                        return
                    terminal_path = (
                        spec_path.parent
                        / "pipeline-fix"
                        / "terminal-exhausted.json"
                    )
                    write_immutable_json(
                        terminal_path,
                        {
                            "schema_version": 1,
                            "operation_id": spec["operation_id"],
                            "completion_policy": completion,
                            "total_pass_limit": total_limit,
                            "completed_passes": completed_passes,
                            "verification_operation_id": failed[
                                "operation_id"
                            ],
                            "verification_sha256": verification_sha256,
                            "failed_head_sha": failed["head_sha"],
                            "status": "retry-exhausted",
                        },
                    )
                    current_parent = store.read(
                        spec["owner_id"], spec["operation_id"]
                    )
                    if current_parent.state not in TERMINAL:
                        store.transition(
                            spec["owner_id"],
                            spec["operation_id"],
                            "failed",
                        )
                    return
                reproduction_path = (
                    spec_path.parent
                    / "pipeline-fix"
                    / "pass-0"
                    / "reproduce"
                    / "receipt.json"
                )
                reproduction = load_receipt(reproduction_path)
                iteration = completed_passes
                intent_path = (
                    spec_path.parent
                    / "pipeline-fix"
                    / f"pass-{iteration}"
                    / "retry-intent.json"
                )
                write_immutable_json(
                    intent_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "definition_sha256": pipeline.definition_sha256,
                        "iteration": iteration,
                        "completion_policy": completion,
                        "total_pass_limit": total_limit,
                        "reproduction_receipt_sha256": (
                            reproduction.receipt_sha256
                        ),
                        "verification_operation_id": failed[
                            "operation_id"
                        ],
                        "verification_sha256": verification_sha256,
                        "failed_head_sha": failed["head_sha"],
                        "current_head_sha": git_head(),
                        "status": "pending",
                    },
                )
                fix_transport_complete = False
                emit_compiled_pipeline_event(
                    spec["cwd"],
                    event="fix-retry-scheduled",
                    pipeline_id=pipeline.definition.pipeline_id,
                    pipeline_version=pipeline.definition.version,
                    profile=pipeline.definition.profile,
                    compiler_outcome="resolved",
                    definition_sha=pipeline.definition_sha256,
                    primitive_count=len(pipeline.definition.steps),
                    loop_iteration=iteration,
                    terminal_category="verification-failed",
                )

            def accept_fix_retry_resubmission(
                failed: dict[str, object],
            ) -> bool:
                matching_intents: list[dict[str, object]] = []
                for intent_path in sorted(
                    (spec_path.parent / "pipeline-fix").glob(
                        "pass-*/retry-intent.json"
                    )
                ):
                    if intent_path.is_symlink():
                        raise RuntimeWorkerError(
                            "fix retry intent cannot be a symlink"
                        )
                    intent = json.loads(
                        intent_path.read_text(encoding="utf-8")
                    )
                    if (
                        isinstance(intent, dict)
                        and intent.get("verification_operation_id")
                        == failed["operation_id"]
                    ):
                        matching_intents.append(intent)
                if len(matching_intents) != 1:
                    raise RuntimeWorkerError(
                        "failed verification has no exact fix retry"
                    )
                intent = matching_intents[0]
                iteration = intent.get("iteration")
                if type(iteration) is not int:
                    raise RuntimeWorkerError(
                        "fix retry iteration is invalid"
                    )
                receipt_root = (
                    spec_path.parent
                    / "pipeline-fix"
                    / f"pass-{iteration}"
                )
                if not all(
                    (receipt_root / step / "receipt.json").is_file()
                    for step in (
                        "root-cause",
                        "regression-test",
                        "minimal-fix",
                    )
                ):
                    return False
                response_receipt_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / str(failed["operation_id"])
                    / "response-receipt.json"
                )
                response_sha256 = hashlib.sha256(
                    json.dumps(
                        intent, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                response_receipt = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "verification_operation_id": failed["operation_id"],
                    "failed_head_sha": failed["head_sha"],
                    "resubmitted_head_sha": verification_head,
                    "response_sha256": response_sha256,
                    "status": "accepted",
                }
                write_immutable_json(
                    response_receipt_path, response_receipt
                )
                failed_record = store.read(
                    spec["owner_id"], str(failed["operation_id"])
                )
                if failed_record.state == "attention-required":
                    store.transition(
                        spec["owner_id"],
                        failed_record.spec.operation_id,
                        "failed",
                    )
                elif failed_record.state != "failed":
                    raise RuntimeWorkerError(
                        "failed verification operation cannot resume"
                    )
                return True

            def verification_attention_packet(
                receipt: dict[str, object],
                *,
                allow_resubmit: bool,
            ) -> tuple[dict[str, object], str]:
                raw_evidence = receipt.get("evidence")
                if not isinstance(raw_evidence, list):
                    raise RuntimeWorkerError(
                        "verification attention evidence is invalid"
                    )
                packet_evidence = [
                    {
                        "command_id": str(row["command_id"]),
                        "exit_code": int(row["exit_code"]),
                        "output_pointer": str(
                            (
                                spec_path.parent
                                / str(row["output_pointer"])
                            ).resolve()
                        ),
                    }
                    for row in raw_evidence
                    if isinstance(row, dict)
                ]
                if len(packet_evidence) != len(raw_evidence):
                    raise RuntimeWorkerError(
                        "verification attention evidence is invalid"
                    )
                allowed = (
                    ["fix-and-resubmit", "escalate"]
                    if allow_resubmit
                    else ["escalate"]
                )
                packet = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "verification_operation_id": str(
                        receipt["operation_id"]
                    ),
                    "verification_lane_id": str(receipt["lane_id"]),
                    "verification_run_id": str(receipt["run_id"]),
                    "definition_sha256": pipeline.definition_sha256,
                    "step_id": "verify",
                    "head_sha": str(receipt["head_sha"]),
                    "status": "attention-required",
                    "reason": "verification-failed",
                    "safe_boundary": "tdd-slices-complete",
                    "allowed_responses": allowed,
                    "response_pointer": (
                        ".task-verification-response.json"
                    ),
                    "receipt_pointer": str(
                        (
                            spec_path.parent
                            / "pipeline-verification"
                            / str(receipt["operation_id"])
                            / "receipt.json"
                        ).resolve()
                    ),
                    "evidence": packet_evidence,
                }
                encoded = json.dumps(
                    packet, sort_keys=True, separators=(",", ":")
                ).encode()
                if len(encoded) > MAX_OUTBOX_BYTES:
                    raise RuntimeWorkerError(
                        "verification attention packet is too large"
                    )
                return packet, hashlib.sha256(encoded).hexdigest()

            def notify_verification_attention(
                receipt: dict[str, object],
                *,
                allow_resubmit: bool,
            ) -> str:
                packet, packet_sha256 = verification_attention_packet(
                    receipt, allow_resubmit=allow_resubmit
                )
                packet_path = spec["cwd"] / ".task-verification.json"
                if packet_path.is_symlink():
                    raise RuntimeWorkerError(
                        "verification attention packet cannot be a symlink"
                    )
                _atomic_json(packet_path, packet)
                notify_path = (
                    spec_path.parent
                    / "pipeline-verification-attention-notify.json"
                )
                if notify_path.is_file():
                    if notify_path.is_symlink():
                        raise RuntimeWorkerError(
                            "verification attention notification is invalid"
                        )
                    notified = json.loads(
                        notify_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(notified, dict)
                        or notified.get("schema_version") != 1
                        or notified.get("operation_id")
                        != spec["operation_id"]
                    ):
                        raise RuntimeWorkerError(
                            "verification attention notification is invalid"
                        )
                    if (
                        notified.get("packet_sha256")
                        == packet_sha256
                        and notified.get("status") == "sent"
                    ):
                        return packet_sha256
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "status": "pending",
                    },
                )
                cmux_adapter.send(
                    spec["surface_id"],
                    "Typed pipeline verification attention is ready in "
                    ".task-verification.json. For fix-and-resubmit, "
                    "commit the fix and run `python3 "
                    f"{trusted_vault}/scripts/pipeline-verification-resubmit.py "
                    f"--worktree {spec['cwd']}`; otherwise use "
                    "task_escalation.py. Do not launch review or reap.",
                )
                cmux_adapter.send_key(spec["surface_id"], "Enter")
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "status": "sent",
                    },
                )
                return packet_sha256

            def accept_verification_resubmission(
                failed: dict[str, object],
            ) -> bool:
                if verification_head == failed["head_sha"]:
                    return False
                _, packet_sha256 = verification_attention_packet(
                    failed, allow_resubmit=True
                )
                response_path = (
                    spec["cwd"] / ".task-verification-response.json"
                )
                try:
                    raw = response_path.read_bytes()
                except FileNotFoundError:
                    return False
                if (
                    response_path.is_symlink()
                    or not raw
                    or len(raw) > MAX_OUTBOX_BYTES
                ):
                    raise RuntimeWorkerError(
                        "verification resubmission response is invalid"
                    )
                response = json.loads(raw)
                expected_keys = {
                    "schema_version",
                    "operation_id",
                    "verification_operation_id",
                    "failed_head_sha",
                    "packet_sha256",
                    "response",
                    "resubmitted_head_sha",
                }
                if (
                    not isinstance(response, dict)
                    or set(response) != expected_keys
                    or response.get("schema_version") != 1
                    or response.get("operation_id")
                    != spec["operation_id"]
                    or response.get("verification_operation_id")
                    != failed["operation_id"]
                    or response.get("failed_head_sha")
                    != failed["head_sha"]
                    or response.get("packet_sha256")
                    != packet_sha256
                    or response.get("response")
                    != "fix-and-resubmit"
                    or response.get("resubmitted_head_sha")
                    != verification_head
                ):
                    raise RuntimeWorkerError(
                        "verification resubmission response is invalid"
                    )
                response_sha256 = hashlib.sha256(
                    json.dumps(
                        response, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                response_receipt_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / str(failed["operation_id"])
                    / "response-receipt.json"
                )
                response_receipt = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "verification_operation_id": failed["operation_id"],
                    "failed_head_sha": failed["head_sha"],
                    "resubmitted_head_sha": verification_head,
                    "response_sha256": response_sha256,
                    "status": "accepted",
                }
                if response_receipt_path.is_file():
                    if response_receipt_path.is_symlink():
                        raise RuntimeWorkerError(
                            "verification response receipt is invalid"
                        )
                    existing = json.loads(
                        response_receipt_path.read_text(encoding="utf-8")
                    )
                    if existing != response_receipt:
                        raise RuntimeWorkerError(
                            "verification response receipt is invalid"
                        )
                else:
                    _atomic_json(
                        response_receipt_path, response_receipt
                    )
                failed_record = store.read(
                    spec["owner_id"], str(failed["operation_id"])
                )
                if failed_record.state == "attention-required":
                    store.transition(
                        spec["owner_id"],
                        failed_record.spec.operation_id,
                        "failed",
                    )
                elif failed_record.state != "failed":
                    raise RuntimeWorkerError(
                        "failed verification operation cannot resume"
                    )
                return True

            def reconcile_failed_verification_child(
                failed: dict[str, object],
            ) -> None:
                failed_operation_id = str(failed["operation_id"])
                failed_record = store.read(
                    spec["owner_id"], failed_operation_id
                )
                if failed_record.pending_effect:
                    if (
                        failed_record.pending_effect
                        != failed["effect_id"]
                    ):
                        raise RuntimeWorkerError(
                            "failed verification effect is uncertain"
                        )
                    store.resolve_effect(
                        spec["owner_id"],
                        failed_operation_id,
                        EffectOutcome.SUCCEEDED,
                    )
                    failed_record = store.read(
                        spec["owner_id"], failed_operation_id
                    )
                if failed_record.state == "verifying":
                    store.transition(
                        spec["owner_id"],
                        failed_operation_id,
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                elif failed_record.state not in {
                    "attention-required",
                    "failed",
                }:
                    raise RuntimeWorkerError(
                        "failed verification operation state is invalid"
                    )

            def run_verification() -> None:
                existing = verification_receipt()
                current = store.create(
                    verification_spec,
                    lane_id=verification_lane_id,
                    run_id=verification_run_id,
                )
                supervisor = OperationSupervisor(
                    store,
                    spec["owner_id"],
                    verification_spec.operation_id,
                )
                supervisor.configure_budget(
                    attempt_limit=1,
                    model_restart_limit=0,
                    time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
                    token_limit=DEFAULT_TOKEN_LIMIT,
                )
                current = supervisor.read()
                if current.state == "created":
                    supervisor.transition("preflight")
                    supervisor.transition("starting")
                    supervisor.transition("running")
                    supervisor.transition("verifying")
                    supervisor.consume_attempt()
                    current = supervisor.read()
                if current.pending_effect:
                    if (
                        current.pending_effect
                        == verification_effect_id
                        and existing is not None
                    ):
                        store.resolve_effect(
                            spec["owner_id"],
                            verification_spec.operation_id,
                            EffectOutcome.SUCCEEDED,
                        )
                    else:
                        summary_attention(
                            "pipeline-verification-effect-uncertain"
                        )
                        return
                if existing is None:
                    current = supervisor.read()
                    if current.state != "verifying":
                        raise RuntimeWorkerError(
                            "pipeline verification state is invalid"
                        )

                    def execute_verification(
                        _record: object,
                    ) -> list[object]:
                        evidence = list(
                            run_profile(
                                profile,
                                root=spec["cwd"],
                                evidence_dir=(
                                    verification_root / "evidence"
                                ),
                                runner=(
                                    verification_runner
                                    or subprocess.run
                                ),
                                extra_commands=pipeline_extra_commands,
                                pointer_root=spec_path.parent,
                            )
                        )
                        verified_heads = {
                            str(item.head_sha) for item in evidence
                        }
                        current_head = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=spec["cwd"],
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if (
                            current_head.returncode
                            or current_head.stdout.strip()
                            != verification_head
                            or verified_heads != {verification_head}
                        ):
                            raise VerificationError(
                                "verification HEAD changed during execution"
                            )
                        return evidence

                    def persist_verification(
                        _record: object,
                        evidence: list[object],
                    ) -> None:
                        rows = [to_dict(item) for item in evidence]
                        _atomic_json(
                            verification_receipt_path,
                            {
                                "schema_version": 1,
                                "operation_id": (
                                    verification_spec.operation_id
                                ),
                                "parent_operation_id": (
                                    spec["operation_id"]
                                ),
                                "lane_id": verification_lane_id,
                                "run_id": verification_run_id,
                                "definition_sha256": (
                                    pipeline.definition_sha256
                                ),
                                "step_id": "verify",
                                "head_sha": verification_head,
                                "input_sha256": (
                                    verification_input_sha256
                                ),
                                "profile": profile.name,
                                "profile_sha256": profile.sha256,
                                "effect_id": verification_effect_id,
                                "status": (
                                    "complete"
                                    if all(
                                        row["exit_code"] == 0
                                        for row in rows
                                    )
                                    else "failed"
                                ),
                                "evidence": rows,
                            }
                        )
                        persisted = json.loads(
                            verification_receipt_path.read_text(
                                encoding="utf-8"
                            )
                        )
                        link_verification_receipt(persisted)

                    supervisor.effect(
                        verification_effect_id,
                        execute_verification,
                        persist_result=persist_verification,
                    )
                    existing = verification_receipt()
                if existing is None:
                    raise RuntimeWorkerError(
                        "pipeline verification produced no receipt"
                    )
                if existing["status"] == "failed":
                    current = supervisor.read()
                    if current.state == "verifying":
                        store.transition(
                            spec["owner_id"],
                            verification_spec.operation_id,
                            "attention-required",
                            reason=AttentionReason.ATTENTION_REQUIRED,
                        )
                    return
                current = supervisor.read()
                if current.state == "verifying":
                    supervisor.transition("finalizing")
                    supervisor.transition("exiting")
                    supervisor.transition("complete")

            previous_verification = (
                controller_verification_receipt()
                if verify_step is not None
                else None
            )
            if (
                previous_verification is not None
                and previous_verification["status"] == "failed"
                and previous_verification["head_sha"]
                != verification_head
            ):
                reconcile_failed_verification_child(
                    previous_verification
                )
                if _pipeline_name == "engineering/fix":
                    if not accept_fix_retry_resubmission(
                        previous_verification
                    ):
                        return
                else:
                    allow_resubmit = (
                        failed_verification_count()
                        <= MAX_PIPELINE_VERIFY_RESUBMITS
                    )
                    notify_verification_attention(
                        previous_verification,
                        allow_resubmit=allow_resubmit,
                    )
                    if not allow_resubmit:
                        summary_attention(
                            "pipeline-verification-retry-exhausted",
                            AttentionReason.RETRY_EXHAUSTED,
                        )
                        return
                    if not accept_verification_resubmission(
                        previous_verification
                    ):
                        return
            existing_verification = (
                verification_receipt()
                if verify_step is not None
                else None
            )
            if existing_verification is not None:
                run_verification()
                existing_verification = verification_receipt()
                if (
                    existing_verification is not None
                    and existing_verification["status"] == "failed"
                ):
                    if _pipeline_name == "engineering/fix":
                        schedule_fix_retry(existing_verification)
                    else:
                        allow_resubmit = (
                            failed_verification_count()
                            <= MAX_PIPELINE_VERIFY_RESUBMITS
                        )
                        notify_verification_attention(
                            existing_verification,
                            allow_resubmit=allow_resubmit,
                        )
                        if not allow_resubmit:
                            summary_attention(
                                "pipeline-verification-retry-exhausted",
                                AttentionReason.RETRY_EXHAUSTED,
                            )
                    return
            if (
                existing_verification is not None
                and existing_verification["status"] == "complete"
            ):
                evidence = existing_verification["evidence"]
                if (
                    not isinstance(evidence, list)
                    or verification_head
                    != evidence[0]["head_sha"]
                ):
                    summary_attention(
                        "pipeline-verification-head-drift",
                        AttentionReason.CONTRACT_DRIFT,
                    )
                    return

            verification_complete = (
                verify_step is None
                or existing_verification is not None
                and existing_verification["status"] == "complete"
            )
            if verification_complete:
                gate_state = review_gate_state()
                if gate_state.get("status") == "awaiting-resolution":
                    notify_review_resolution(gate_state)
                    if review.status == "stale":
                        if not _review_resolution_handoff_ready(
                            worktree=spec["cwd"],
                            operation_id=spec["operation_id"],
                            gate_state=gate_state,
                            current_head=verification_head,
                        ):
                            return
                        drive_review()
                        return
                    _atomic_json(
                        marker_path,
                        {
                            "schema_version": 1,
                            "operation_id": spec["operation_id"],
                            "definition_sha256": (
                                pipeline.definition_sha256
                            ),
                            "status": "started",
                            "drive_sha256": review_drive_sha256(),
                        },
                    )
                    return

                if (
                    marker is not None
                    and review.status in {"reviewing", "stale"}
                ):
                    current_drive_sha256 = review_drive_sha256()
                    if (
                        marker["status"] == "pending"
                        or marker.get("drive_sha256")
                        != current_drive_sha256
                    ):
                        drive_review()
                    return
            review_observation = {
                "missing": "pending",
                "reviewing": "running",
                "approved": "complete",
                "skipped": "complete",
                "attention": "attention",
                "stale": "attention",
            }[review.status]
            observations: dict[str, str] = {}
            for step in steps:
                if step.primitive_id == "model_step":
                    observations[step.step_id] = "complete"
                elif step.primitive_id == "verify":
                    observations[step.step_id] = (
                        "pending"
                        if existing_verification is None
                        else (
                            "complete"
                            if existing_verification["status"]
                            == "complete"
                            else "attention"
                        )
                    )
                else:
                    observations[step.step_id] = (
                        review_observation
                        if (
                            verify_step is None
                            or existing_verification is not None
                            and existing_verification["status"]
                            == "complete"
                        )
                        else "pending"
                    )
            progress = reconcile_pipeline(
                pipeline,
                observations,
            )
            if progress.action == "start":
                step = next(
                    row
                    for row in steps
                    if row.step_id == progress.step_id
                )
                if step.primitive_id == "verify":
                    run_verification()
                    return
                if marker is not None:
                    if marker["status"] == "started":
                        return
                drive_review()
                return
            if progress.action == "wait":
                return
            if progress.action == "attention":
                if progress.step_id == (
                    verify_step.step_id if verify_step else ""
                ):
                    summary_attention(
                        "pipeline-verification-failed",
                        AttentionReason.ATTENTION_REQUIRED,
                    )
                else:
                    summary_attention(
                        f"review-finalization-{review.status}"
                    )
                return
            if progress.action != "reap-ready":
                raise RuntimeWorkerError(
                    "compiled pipeline returned an invalid finalization action"
                )
            if wait_for_summary_refresh_after_resolution(
                review_gate_state()
            ):
                return
            callback_handled = True
            encoded = json.dumps(
                summary, sort_keys=True, separators=(",", ":")
            ).encode()
            payload_sha256 = hashlib.sha256(encoded).hexdigest()
            envelope = CallbackEnvelope(
                callback_id=f"wiki-summary-{payload_sha256[:24]}",
                operation_id=spec["operation_id"],
                run_id=spec["run_id"],
                kind="wiki-summary",
                payload=summary,
                payload_sha256=payload_sha256,
            )
            acceptance = CallbackBroker(
                store, spec["owner_id"]
            ).accept(envelope)
            emit_compiled_pipeline_event(
                spec["cwd"],
                event="terminal",
                pipeline_id=pipeline.definition.pipeline_id,
                pipeline_version=pipeline.definition.version,
                profile=pipeline.definition.profile,
                compiler_outcome=(
                    "custom-resolved"
                    if pipeline.definition.pipeline_id == "custom"
                    else "resolved"
                ),
                definition_sha=pipeline.definition_sha256,
                primitive_count=len(pipeline.definition.steps),
                loop_iteration=0,
                terminal_category="complete",
            )
            _atomic_json(
                spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "operation_id": envelope.operation_id,
                    "status": (
                        "duplicate" if acceptance.duplicate else "accepted"
                    ),
                },
            )
            notify_path = spec_path.parent / "task-summary-notify.json"
            if notify_path.exists():
                marker = json.loads(notify_path.read_text(encoding="utf-8"))
                if (
                    marker.get("schema_version") != 1
                    or marker.get("callback_id") != envelope.callback_id
                ):
                    raise RuntimeWorkerError(
                        "task summary notification marker is invalid"
                    )
                if marker.get("status") == "sent":
                    return
                if marker.get("status") == "pending":
                    try:
                        store.transition(
                            spec["owner_id"],
                            spec["operation_id"],
                            "attention-required",
                            reason=AttentionReason.ATTENTION_REQUIRED,
                        )
                    except Exception:
                        pass
                    return
                raise RuntimeWorkerError(
                    "task summary notification marker state is invalid"
                )
            vault_root = Path(str(meta.get("vault_root") or "")).resolve()
            reap_runner = vault_root / "scripts" / "reap-runner.py"
            if (
                not reap_runner.is_file()
                or reap_runner.is_symlink()
                or not (vault_root / "wiki").is_dir()
            ):
                raise RuntimeWorkerError("trusted reap runner is unavailable")
            command = shlex.join(
                [
                    "python3",
                    str(reap_runner),
                    "--vault-root",
                    str(vault_root),
                    "--worktree",
                    str(spec["cwd"]),
                ]
            )
            wake = (
                "Typed final task summary callback was accepted. "
                f"Run this exact command now: {command}"
            )
            if len(wake.encode()) > 4096:
                raise RuntimeWorkerError("task summary wake message is too large")
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "pending",
                },
            )
            cmux_adapter.send(spec["origin_surface"], wake)
            cmux_adapter.send_key(spec["origin_surface"], "Enter")
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "sent",
                },
            )
        except (
            CallbackError,
            ContractError,
            RuntimeWorkerError,
            VerificationError,
            WikiSummaryError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            summary_attention("wiki-summary-invalid")

    exit_code = 0
    provider_exited = False
    exit_containment_failed = False

    def restart_for_liveness(action_id: str) -> None:
        nonlocal handle, provider_exited, exit_code, exit_containment_failed

        supervisor = OperationSupervisor(
            store, spec["owner_id"], spec["operation_id"]
        )
        try:
            budgeted = supervisor.consume_model_restart(
                explicitly_permitted=True
            )
            old_handle = handle
            if not provider_exited:
                process.signal_owned_child_group(
                    old_handle.process_group,
                    old_handle.process_identity,
                    signal.SIGTERM,
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    waited, _status = os.waitpid(old_handle.pid, os.WNOHANG)
                    if waited == old_handle.pid:
                        break
                    time.sleep(0.05)
                else:
                    process.signal_owned_child_group(
                        old_handle.process_group,
                        old_handle.process_identity,
                        signal.SIGKILL,
                    )
                    os.waitpid(old_handle.pid, 0)
            resume_command = provider_resume_argv(
                provider_command,
                str(spec["runtime"]),
                checkpoint,
            )
            restarted = process.start(
                resume_command,
                cwd=spec["cwd"],
                env=provider_env,
            )
            resources = budgeted.resources
            supervisor.bind_resources(
                OwnedResources(
                    surface_id=resources.surface_id or spec["surface_id"],
                    process_group=restarted.process_group,
                    supervisor_pid=resources.supervisor_pid or os.getpid(),
                    process_identity=restarted.process_identity,
                    supervisor_identity=(
                        resources.supervisor_identity or supervisor_identity
                    ),
                )
            )
            handle = restarted
            provider_exited = False
            exit_code = 0
            exit_containment_failed = False
            write_immutable_json(
                spec_path.parent
                / "liveness"
                / f"provider-restart-{budgeted.model_restarts}.json",
                {
                    "schema_version": 1,
                    "action_id": action_id,
                    "operation_id": spec["operation_id"],
                    "run_id": spec["run_id"],
                    "model_restarts": budgeted.model_restarts,
                    "checkpoint": checkpoint,
                    "provider_argv_sha256": hashlib.sha256(
                        json.dumps(
                            resume_command,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "old_process_identity": old_handle.process_identity,
                    "new_process_identity": restarted.process_identity,
                    "status": "restarted",
                },
            )
        except (
            HarnessContractError,
            OSError,
            ProcessError,
            RuntimeWorkerError,
            StoreError,
            SupervisorError,
        ):
            try:
                current = store.read(spec["owner_id"], spec["operation_id"])
                if current.state not in TERMINAL and current.state != "attention-required":
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
            except Exception:
                pass

    def inspect_liveness() -> None:
        try:
            record = store.read(spec["owner_id"], spec["operation_id"])
            process_status = (
                "dead"
                if provider_exited
                else process.process_status(
                    handle.process_group,
                    handle.process_identity,
                )
            )
            typed_result_path = spec["cwd"] / spec["task_summary_pointer"]
            if (
                spec["callback_mode"] == "task-summary"
                and (
                    _pipeline_name == "engineering/fix"
                    and not fix_transport_complete
                    or is_custom_pipeline
                    and not custom_transport_complete
                )
            ):
                try:
                    step_request = json.loads(
                        (spec["cwd"] / ".task-pipeline-step-request.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    typed_result_path = spec["cwd"] / str(
                        step_request.get("result_pointer") or ""
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            typed_result_sha256 = (
                _bounded_file_sha256(typed_result_path)
                if spec["callback_mode"] == "task-summary"
                else ""
            )
            callback_sha256 = ""
            if (
                spec["callback_mode"] != "task-summary"
                or _pipeline_name == "engineering/fix"
                and not fix_transport_complete
                or is_custom_pipeline
                and not custom_transport_complete
            ):
                try:
                    callback_sha256 = _bounded_file_sha256(
                        _callback_target(spec)[3]
                    )
                except RuntimeWorkerError:
                    callback_sha256 = ""
            decision = liveness_controller.observe(
                LivenessEvidence(
                    observed_at=time.time(),
                    process_status=process_status,
                    operation_revision=record.revision,
                    operation_state=record.state,
                    screen_sha256=latest_screen_digest,
                    prompt_state=latest_prompt_state,
                    typed_result_sha256=typed_result_sha256,
                    callback_sha256=callback_sha256,
                    receipt_sha256=_current_callback_receipt_sha256(
                        spec_path.parent
                    ),
                ),
                liveness_policy,
            )
            if decision.action != "observe":
                telemetry_marker = (
                    spec_path.parent
                    / "liveness"
                    / "telemetry"
                    / f"{decision.action_id}.json"
                )
                if not telemetry_marker.exists():
                    emit_lifecycle_event(
                        spec["cwd"],
                        "pipeline-liveness",
                        actor=decision.action,
                        counts={"model_call": int(decision.model_call)},
                        identifiers={
                            "stage": decision.action,
                            "action_id": decision.action_id,
                        },
                        status=(
                            "degraded"
                            if decision.action
                            in {"suspected-idle", "attention-required"}
                            else "ok"
                        ),
                    )
                    _atomic_json(
                        telemetry_marker,
                        {
                            "schema_version": 1,
                            "action_id": decision.action_id,
                            "status": "emitted",
                        },
                    )
            if decision.action == "reconcile-result":
                if spec["callback_mode"] == "task-summary":
                    recover_task_summary_attention()
                    drive_fix_transport()
                    drive_custom_transport()
                    inspect_task_summary()
                elif spec["callback_mode"] in {
                    "research-fetch",
                    "research-synth",
                }:
                    inspect_research()
                else:
                    inspect_callback()
            elif decision.action == "nudge":
                cmux_adapter.send(
                    spec["surface_id"],
                    "Harness liveness check: continue the current task, or if "
                    "it is complete, write the exact required typed callback now.",
                )
                cmux_adapter.send_key(spec["surface_id"], "Enter")
            elif decision.action == "restart":
                restart_for_liveness(decision.action_id)
            elif decision.action == "attention-required":
                current = store.read(spec["owner_id"], spec["operation_id"])
                if current.state not in TERMINAL and current.state != "attention-required":
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.RETRY_EXHAUSTED,
                    )
        except (
            HarnessContractError,
            OSError,
            ProcessError,
            StoreError,
            TypeError,
            ValueError,
        ):
            return

    while True:
        inspect_control()
        if spec["callback_mode"] == "task-summary":
            recover_task_summary_attention()
            drive_fix_transport()
            drive_custom_transport()
            inspect_task_summary()
        elif spec["callback_mode"] in {
            "research-fetch",
            "research-synth",
        }:
            inspect_research()
        else:
            inspect_callback()
        if enforce_callback_deadline(
            store,
            spec["owner_id"],
            spec["operation_id"],
            callback_handled=callback_handled,
        ):
            _atomic_json(
                spec_path.parent / "callback-timeout.json",
                {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "run_id": spec["run_id"],
                    "status": "attention-required",
                },
            )
        now = time.monotonic()
        if now >= next_liveness_probe:
            next_liveness_probe = now + liveness_policy.probe_seconds
            inspect_liveness()
        if now >= next_prompt_probe:
            next_prompt_probe = now + 0.2
            inspect_prompt()
        if not checkpoint and now >= next_checkpoint_probe:
            next_checkpoint_probe = now + 0.5
            try:
                checkpoint = checkpoint_probe(
                    str(spec["surface_id"]), str(spec["runtime"])
                )
            except Exception:
                checkpoint = ""
            if checkpoint:
                _atomic_json(
                    spec_path.parent / "checkpoint.json",
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "run_id": spec["run_id"],
                        "runtime": spec["runtime"],
                        "checkpoint": checkpoint,
                    },
                )
        if not provider_exited:
            try:
                exit_pending = os.waitid(
                    os.P_PID,
                    handle.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError:
                exit_pending = None
                provider_exited = True
                exit_code = 0
                try:
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                except Exception:
                    pass
            except OSError:
                exit_pending = None
            if exit_pending is not None:
                try:
                    process.signal_owned_child_group(
                        handle.process_group,
                        handle.process_identity,
                        signal.SIGKILL,
                    )
                except ProcessError:
                    if not exit_containment_failed:
                        exit_containment_failed = True
                        try:
                            store.transition(
                                spec["owner_id"],
                                spec["operation_id"],
                                "attention-required",
                                reason=AttentionReason.ATTENTION_REQUIRED,
                            )
                        except Exception:
                            pass
                    time.sleep(max(0.02, poll_seconds))
                    continue
                waited, status = os.waitpid(handle.pid, os.WNOHANG)
                if waited != handle.pid:
                    time.sleep(max(0.02, poll_seconds))
                    continue
                exit_code = os.waitstatus_to_exitcode(status)
                provider_exited = True
        if (
            provider_exited
            and exit_code != 0
            and not callback_handled
            and spec["callback_mode"] in {"research-fetch", "research-synth"}
        ):
            try:
                current = store.read(spec["owner_id"], spec["operation_id"])
                if (
                    current.state not in TERMINAL
                    and current.state != "attention-required"
                ):
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=(
                            AttentionReason.RUNTIME_UNAVAILABLE
                            if exit_code == 127
                            else AttentionReason.ATTENTION_REQUIRED
                        ),
                    )
            except Exception:
                pass
        if (
            provider_exited
            and (
                _pipeline_name == "engineering/fix"
                and not fix_transport_complete
                or is_custom_pipeline
                and not custom_transport_complete
            )
            and not callback_handled
        ):
            recovery_kind = "custom" if is_custom_pipeline else "fix"
            recovery_root = spec_path.parent / f"pipeline-{recovery_kind}"
            parent_record = store.read(
                spec["owner_id"], spec["operation_id"]
            )
            if (
                parent_record.state not in TERMINAL
                and parent_record.state != "attention-required"
            ):
                restart_supervisor = OperationSupervisor(
                    store, spec["owner_id"], spec["operation_id"]
                )
                old_handle = handle
                try:
                    budgeted = restart_supervisor.consume_model_restart(
                        explicitly_permitted=True
                    )
                except SupervisorError:
                    write_immutable_json(
                        recovery_root / "provider-restart-exhausted.json",
                        {
                            "schema_version": 1,
                            "operation_id": spec["operation_id"],
                            "model_restarts": parent_record.model_restarts,
                            "model_restart_limit": (
                                parent_record.model_restart_limit
                            ),
                            "status": "retry-exhausted",
                        },
                    )
                    summary_attention(
                        f"pipeline-{recovery_kind}-provider-restart-exhausted",
                        AttentionReason.RETRY_EXHAUSTED,
                    )
                else:
                    restarted_handle: ProcessHandle | None = None
                    try:
                        resume_command = provider_resume_argv(
                            provider_command,
                            str(spec["runtime"]),
                            checkpoint,
                        )
                        restarted_handle = process.start(
                            resume_command,
                            cwd=spec["cwd"],
                            env=provider_env,
                        )
                        previous_resources = budgeted.resources
                        restart_supervisor.bind_resources(
                            OwnedResources(
                                surface_id=(
                                    previous_resources.surface_id
                                    or spec["surface_id"]
                                ),
                                process_group=(
                                    restarted_handle.process_group
                                ),
                                supervisor_pid=(
                                    previous_resources.supervisor_pid
                                    or os.getpid()
                                ),
                                process_identity=(
                                    restarted_handle.process_identity
                                ),
                                supervisor_identity=(
                                    previous_resources.supervisor_identity
                                    or supervisor_identity
                                ),
                            )
                        )
                        handle = restarted_handle
                        provider_exited = False
                        exit_code = 0
                        exit_containment_failed = False
                        _atomic_json(
                            ready,
                            {
                                "schema_version": 1,
                                "status": "ready",
                                "pid": handle.pid,
                                "process_group": handle.process_group,
                                "supervisor_pid": os.getpid(),
                                "process_identity": (
                                    handle.process_identity
                                ),
                                "supervisor_identity": (
                                    supervisor_identity
                                ),
                            },
                        )
                        command_sha256 = hashlib.sha256(
                            json.dumps(
                                resume_command,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        environment_sha256 = hashlib.sha256(
                            json.dumps(
                                sorted(provider_env.items()),
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        write_immutable_json(
                            recovery_root
                            / (
                                "provider-restart-"
                                f"{budgeted.model_restarts}.json"
                            ),
                            {
                                "schema_version": 1,
                                "operation_id": spec["operation_id"],
                                "model_restarts": (
                                    budgeted.model_restarts
                                ),
                                "checkpoint": checkpoint,
                                "old_process_group": (
                                    old_handle.process_group
                                ),
                                "old_process_identity": (
                                    old_handle.process_identity
                                ),
                                "new_process_group": (
                                    handle.process_group
                                ),
                                "new_process_identity": (
                                    handle.process_identity
                                ),
                                "provider_argv_sha256": (
                                    command_sha256
                                ),
                                "provider_environment_sha256": (
                                    environment_sha256
                                ),
                                "status": "restarted",
                            },
                        )
                    except (
                        ContractError,
                        HarnessContractError,
                        OSError,
                        ProcessError,
                        RuntimeWorkerError,
                        StoreError,
                        SupervisorError,
                    ):
                        if restarted_handle is not None:
                            _contain_provider_start_failure(
                                process, restarted_handle
                            )
                        summary_attention(
                            f"pipeline-{recovery_kind}-provider-restart-failed",
                            AttentionReason.ATTENTION_REQUIRED,
                        )
        try:
            operation_record = store.read(
                spec["owner_id"], spec["operation_id"]
            )
            operation_state = operation_record.state
            operation_profile = operation_record.spec.route.profile
            callback_deadline_at = operation_record.deadline_at
        except Exception:
            operation_state = ""
            operation_profile = ""
            callback_deadline_at = 0.0
        if provider_exit_is_final(
            provider_exited=provider_exited,
            callback_mode=spec["callback_mode"],
            callback_handled=callback_handled,
            operation_state=operation_state,
            operation_profile=operation_profile,
            callback_deadline_at=callback_deadline_at,
        ):
            break
        time.sleep(max(0.02, poll_seconds))
    for _ in range(3):
        if spec["callback_mode"] == "task-summary":
            recover_task_summary_attention()
            drive_fix_transport()
            drive_custom_transport()
            inspect_task_summary()
        elif spec["callback_mode"] in {
            "research-fetch",
            "research-synth",
        }:
            inspect_research()
        else:
            inspect_callback()
        if callback_handled:
            break
        time.sleep(max(0.02, poll_seconds))
    _atomic_json(
        exit_path,
        {
            "schema_version": 1,
            "status": "exited",
            "exit_code": exit_code,
        },
    )
    return exit_code


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
