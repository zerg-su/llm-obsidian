#!/usr/bin/env python3
"""Safe, self-healing turn-end pipeline shared by Claude and Codex hooks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[1]).resolve()
META = ROOT / ".vault-meta"
LOCK = META / ".stop-hook.lock"
LATENCY = META / "stop-hook-latency.jsonl"
DENSE_RETRY = META / "dense-refresh.pending.json"
RETRIEVAL_QUALITY_PENDING = META / "retrieval-quality.pending.json"
OPT_OUT = META / "auto-commit.disabled"
SCOPED_PATHS = ("wiki", ".raw", ".vault-meta")
SLOW_SECONDS = 30.0
DEFAULT_REQUIRED_TIMEOUT = 60.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 600.0
REQUIRED_TIMEOUT_ENV = "LLM_OBSIDIAN_STOP_REQUIRED_TIMEOUT_SEC"


def emit(message: str) -> None:
    print(message, flush=True)


def mark_retrieval_quality_pending() -> None:
    """Record content-free corpus drift for a later explicit quality gate."""
    index_path = META / "retrieval" / "index.json"
    goldset = META / "retrieval-goldset.jsonl"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        payload = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "corpus_sha256": str(index.get("source_fingerprint") or ""),
            "goldset_sha256": hashlib.sha256(goldset.read_bytes()).hexdigest() if goldset.is_file() else None,
        }
        tmp = RETRIEVAL_QUALITY_PENDING.with_name(
            f"{RETRIEVAL_QUALITY_PENDING.name}.tmp.{os.getpid()}"
        )
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, RETRIEVAL_QUALITY_PENDING)
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def timeout_setting(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not MIN_TIMEOUT <= value <= MAX_TIMEOUT:
        emit(
            f"STOP_CONFIG_WARN: {name} must be a finite number in "
            f"[{MIN_TIMEOUT:g}, {MAX_TIMEOUT:g}]; using {default:g}s."
        )
        return default
    return value


def timeout_label(value: float) -> str:
    return f"{value:g}s"


def run(
    args: list[str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return result


def run_bounded(
    phase: str,
    args: list[str],
    *,
    timeout: float,
    retry_hint: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return run(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{phase} timed out after {timeout_label(timeout)}; retry: {retry_hint}; "
            f"tune {REQUIRED_TIMEOUT_ENV} if this phase is expected to run longer"
        ) from exc


def run_required(
    phase: str,
    args: list[str],
    *,
    timeout: float,
    retry_hint: str,
) -> subprocess.CompletedProcess[str]:
    result = run_bounded(phase, args, timeout=timeout, retry_hint=retry_hint)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail[:600]}" if detail else ""
        raise RuntimeError(
            f"{phase} failed with exit {result.returncode}{suffix}; retry: {retry_hint}"
        )
    return result


def acquire(lock_file: object, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def git_repo() -> bool:
    return (ROOT / ".git").exists() and run(["git", "rev-parse", "--git-dir"]).returncode == 0


def wiki_dirty() -> bool:
    if not git_repo():
        return False
    return bool(run(["git", "status", "--porcelain", "--untracked-files=all", "--", "wiki"]).stdout.strip())


def read_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def dispatched_task_worktree() -> bool:
    """Task splits leave coordinator-owned vault maintenance to the coordinator."""
    meta = read_json_object(ROOT / ".task-meta.json")
    if meta.get("version") not in {2, 3}:
        return False
    raw_vault = str(meta.get("vault_root") or "").strip()
    if not raw_vault:
        return False
    vault = Path(raw_vault).expanduser()
    if not vault.is_absolute():
        return False
    try:
        return vault.resolve() != ROOT
    except OSError:
        return False


def sparse_fingerprint() -> str:
    index = read_json_object(META / "retrieval" / "index.json")
    return str(index.get("source_fingerprint") or "")


def dense_cache_current() -> tuple[bool, str]:
    """Return whether dense exactly covers the current sparse snapshot."""
    sparse = read_json_object(META / "retrieval" / "index.json")
    fingerprint = str(sparse.get("source_fingerprint") or "")
    docs = sparse.get("docs")
    if not fingerprint or not isinstance(docs, dict):
        return False, fingerprint
    dense = read_json_object(META / "retrieval" / "dense.json")
    embeddings = dense.get("embeddings")
    current = (
        dense.get("schema_version") == sparse.get("schema_version")
        and dense.get("chunk_config") == sparse.get("chunk_config")
        and dense.get("source_fingerprint") == fingerprint
        and dense.get("model") == os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")
        and dense.get("complete") is True
        and isinstance(embeddings, dict)
        and set(embeddings) == set(docs)
    )
    return current, fingerprint


def dense_refresh_due(*, now: float | None = None) -> bool:
    """Schedule stale dense once per fingerprint while respecting retry backoff."""
    current, fingerprint = dense_cache_current()
    if current or not fingerprint:
        return False
    marker = read_json_object(DENSE_RETRY)
    if marker.get("schema_version") != 2:
        return True
    if str(marker.get("source_fingerprint") or "") != fingerprint:
        return True
    try:
        return float(marker.get("next_retry_at", 0)) <= (time.time() if now is None else now)
    except (TypeError, ValueError):
        return True


def mark_dense_pending() -> None:
    index_path = META / "retrieval" / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        atomic_json(
            DENSE_RETRY,
            {
                "schema_version": 2,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "source_fingerprint": str(index.get("source_fingerprint") or ""),
                "next_retry_at": 0,
            },
        )
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def schedule_dense_refresh(needed: bool) -> tuple[bool, str]:
    worker = ROOT / "scripts" / "dense-refresh-worker.py"
    if not needed or not worker.is_file():
        return True, "skipped"
    try:
        subprocess.Popen(
            [sys.executable, str(worker)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        )
        return True, "scheduled"
    except OSError:
        return False, "worker launch failed"


def rotate(path: Path, cap: int = 1_048_576) -> None:
    try:
        if path.is_file() and path.stat().st_size > cap:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def scoped_git_paths(include_memory: bool = False) -> list[str]:
    paths: list[str] = []
    candidates = (*SCOPED_PATHS, ".claude-memory") if include_memory else SCOPED_PATHS
    for rel in candidates:
        if (ROOT / rel).exists() or run(["git", "ls-files", "--", rel]).stdout.strip():
            paths.append(rel)
    return paths


def commit_scoped(include_memory: bool = False) -> tuple[bool, str]:
    if not git_repo():
        return True, "no-git"
    paths = scoped_git_paths(include_memory)
    if not paths:
        return True, "nothing"
    add = run(["git", "add", "-A", "--", *paths])
    if add.returncode:
        return False, f"git add failed: {(add.stderr or add.stdout).strip()}"
    changed_result = run(["git", "diff", "--cached", "--name-only", "--", *paths])
    changed = [line for line in changed_result.stdout.splitlines() if line]
    if not changed:
        return True, "nothing"
    visible = [path for path in changed if not path.startswith(".vault-meta/")]
    labels = [Path(path).with_suffix("").name for path in visible[:3]]
    summary = ", ".join(labels) if labels else "indexes"
    if len(visible) > 3:
        summary += f" +{len(visible) - 3}"
    message = f"wiki: {summary} ({time.strftime('%Y-%m-%d %H:%M')})"
    result = run(["git", "commit", "--only", "-m", message, "--", *paths], timeout=60.0)
    if result.returncode:
        return False, f"git commit failed: {(result.stderr or result.stdout).strip()}"
    return True, f"committed {len(changed)} path(s)"


def fold_hint() -> None:
    helper = ROOT / "scripts" / "fold-log.py"
    if not helper.is_file():
        return
    result = run([sys.executable, str(helper), "status", "--json"])
    if result.returncode:
        return
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return
    if status.get("ready"):
        emit(
            "WIKI_FOLD_SUGGEST: "
            f"{status['unprocessed_entries']} hashed log entries are unprocessed; "
            f"next deterministic batch is {status['batch_size']}. Run /wiki-fold."
        )


def append_latency(timings: dict[str, float], *, locked: bool, dirty: bool, blocked: bool) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "total_s": round(sum(timings.values()), 3),
        **{f"{name}_s": round(value, 3) for name, value in timings.items()},
        "lock": int(locked),
        "wiki_dirty": int(dirty),
        "commit_blocked": int(blocked),
    }
    try:
        with LATENCY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass
    if record["total_s"] >= SLOW_SECONDS:
        emit(
            "STOP_HOOK_SLOW: turn-wrap took "
            f"{record['total_s']:.1f}s; phases={{{', '.join(f'{k}:{v:.1f}' for k, v in timings.items())}}}."
        )


def timed(timings: dict[str, float], name: str, fn):
    started = time.monotonic()
    try:
        return fn()
    finally:
        timings[name] = time.monotonic() - started


def main() -> int:
    if dispatched_task_worktree():
        emit(
            "TASK_SPLIT_STOP_SKIPPED: coordinator-owned vault maintenance is "
            "disabled in the dispatched worktree."
        )
        return 0
    if OPT_OUT.is_file():
        emit(
            "AUTO_COMMIT_DISABLED: .vault-meta/auto-commit.disabled present — "
            "turn-end mutation/reindex/commit skipped. Remove it to re-enable."
        )
        return 0
    required_timeout = timeout_setting(REQUIRED_TIMEOUT_ENV, DEFAULT_REQUIRED_TIMEOUT)
    META.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    with LOCK.open("a+", encoding="utf-8") as lock_file:
        if not acquire(lock_file):
            emit(
                "STOP_LOCK_BUSY: another session owns the turn-end pipeline; "
                "this Stop skipped and the next one will self-heal."
            )
            return 0

        blocked = False
        try:
            writer = ROOT / "scripts" / "vault-write.py"
            if writer.is_file():
                recovered = timed(
                    timings,
                    "recover",
                    lambda: run_required(
                        "transaction recovery",
                        [sys.executable, str(writer), "--recover"],
                        timeout=required_timeout,
                        retry_hint="python3 scripts/vault-write.py --recover",
                    ),
                )
                if "RECOVERED" in recovered.stderr:
                    emit(recovered.stderr.strip())

            dirty = wiki_dirty()
            sparse_before = sparse_fingerprint()
            reindex = ROOT / "scripts" / "reindex.py"
            if reindex.is_file():
                timed(
                    timings,
                    "reindex",
                    lambda: run_required(
                        "reindex",
                        [sys.executable, str(reindex), "--quiet", "--folder-indexes"],
                        timeout=required_timeout,
                        retry_hint="python3 scripts/reindex.py --quiet --folder-indexes",
                    ),
                )

            bm25 = ROOT / "scripts" / "bm25-index.py"
            if bm25.is_file():
                timed(
                    timings,
                    "bm25",
                    lambda: run_required(
                        "BM25 ensure",
                        [sys.executable, str(bm25), "ensure", "--quiet"],
                        timeout=required_timeout,
                        retry_hint="python3 scripts/bm25-index.py ensure --quiet",
                    ),
                )

            retriever = ROOT / "scripts" / "retrieve.py"
            if retriever.is_file():
                timed(
                    timings,
                    "sparse",
                    lambda: run_required(
                        "sparse ensure",
                        [sys.executable, str(retriever), "ensure", "--quiet"],
                        timeout=required_timeout,
                        retry_hint="python3 scripts/retrieve.py ensure --quiet",
                    ),
                )
            sparse_after = sparse_fingerprint()
            corpus_changed = dirty or bool(
                sparse_before and sparse_after and sparse_before != sparse_after
            )
            if corpus_changed:
                mark_retrieval_quality_pending()
            dense_needed = dense_refresh_due()

            memory = ROOT / "scripts" / "memory-backup.py"
            memory_enabled = False
            if memory.is_file():
                memory_status = run_bounded(
                    "memory backup status",
                    [sys.executable, str(memory), "--status"],
                    timeout=required_timeout,
                    retry_hint="python3 scripts/memory-backup.py --status",
                )
                if memory_status.returncode == 0:
                    try:
                        memory_enabled = bool(json.loads(memory_status.stdout).get("enabled"))
                    except (AttributeError, json.JSONDecodeError):
                        memory_enabled = False
                memory_result = timed(
                    timings,
                    "backup",
                    lambda: run_bounded(
                        "memory backup",
                        [sys.executable, str(memory)],
                        timeout=required_timeout,
                        retry_hint="python3 scripts/memory-backup.py",
                    ),
                )
                if memory_result.returncode:
                    blocked = True
                    detail = (memory_result.stderr or memory_result.stdout).strip()
                    emit(
                        "MEMORY_BACKUP_BLOCKED: enabled backup failed closed"
                        + (f" ({detail[:600]})" if detail else ".")
                    )
            else:
                timings["backup"] = 0.0

            validator = ROOT / "scripts" / "validate-vault.py"
            if validator.is_file():
                validation = timed(
                    timings,
                    "validate",
                    lambda: run_bounded(
                        "vault validation",
                        [sys.executable, str(validator), "--summary"],
                        timeout=required_timeout,
                        retry_hint="python3 scripts/validate-vault.py --summary",
                    ),
                )
                if validation.returncode:
                    blocked = True
                    detail = (validation.stdout or validation.stderr).strip()
                    if detail:
                        emit(detail)
                    emit(
                        "COMMIT_BLOCKED: strict vault validation failed; changes remain "
                        "unstaged/dirty for repair."
                    )

            if not blocked:
                commit_ok, commit_status = timed(
                    timings,
                    "commit",
                    lambda: commit_scoped(memory_enabled),
                )
                if not commit_ok:
                    blocked = True
                    emit(f"COMMIT_FAILED: {commit_status}")
            else:
                timings["commit"] = 0.0

            if not blocked:
                if dense_needed:
                    mark_dense_pending()
                dense_ok, dense_status = timed(
                    timings,
                    "dense",
                    lambda: schedule_dense_refresh(dense_needed),
                )
                if not dense_ok:
                    emit(
                        "DENSE_DEFER_FAILED: optional worker launch failed; "
                        "sparse retrieval remains available and the pending marker was preserved."
                    )
                elif dense_status == "scheduled":
                    emit("DENSE_DEFERRED: refresh scheduled; Stop does not wait for embeddings.")
            else:
                timings["dense"] = 0.0

            if dirty and not blocked:
                emit(
                    "WIKI_CHANGED: validated and handled through the scoped turn-end pipeline; "
                    "log/hot remain writer-owned."
                )
            fold_hint()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            blocked = True
            timings.setdefault("commit", 0.0)
            emit(f"COMMIT_BLOCKED: required turn-end phase failed: {exc}")

        rotate(ROOT / ".vault-meta/router-hits.jsonl")
        rotate(ROOT / ".vault-meta/command-log.jsonl")
        rotate(LATENCY)
        append_latency(timings, locked=True, dirty=locals().get("dirty", False), blocked=blocked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
