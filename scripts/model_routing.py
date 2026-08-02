#!/usr/bin/env python3
"""Strict, session-aware model routing for task, review, daily, and research."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from model_routing_config import (
    CLAUDE_EFFORTS,
    CODEX_EFFORTS,
    LOCAL_CONFIG,
    ROLES,
    ROOT,
    RUNTIMES,
    TRACKED_CONFIG,
    RoutingConfig,
    RoutingError,
    _merge,
    _read_toml,
    _validate,
    _versioned_claude_generation,
    load_config,
    load_tracked_config,
    validate_effort,
    validate_local_config,
)


SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def session_from_meta(meta: dict[str, Any]) -> dict[str, str] | None:
    routing = meta.get("routing")
    candidate: Any = routing.get("session") if isinstance(routing, dict) else None
    if not isinstance(candidate, dict):
        fields = {
            "runtime": meta.get("session_runtime"),
            "model": meta.get("session_model"),
            "effort": meta.get("session_effort"),
        }
        candidate = fields if all(fields.values()) else None
    if not isinstance(candidate, dict):
        return None
    runtime = str(candidate.get("runtime") or "")
    model = str(candidate.get("model") or "")
    effort = str(candidate.get("effort") or "")
    if runtime not in RUNTIMES or not model or not effort:
        raise RoutingError("session routing requires runtime, model, and effort")
    validate_effort(runtime, effort)
    result = {"runtime": runtime, "model": model, "effort": effort}
    source = str(candidate.get("source") or "")
    if source:
        result["source"] = source
    return result


def resolve(
    config: RoutingConfig,
    role: str,
    *,
    session: dict[str, str] | None = None,
    explicit_runtime: str = "",
    explicit_model: str = "",
    explicit_effort: str = "",
    same_model: bool = False,
    review_profile: str = "simple",
) -> dict[str, Any]:
    if role not in ROLES:
        raise RoutingError(f"unknown routing role: {role}")
    if session:
        runtime = session.get("runtime", "")
        if runtime not in RUNTIMES or not session.get("model") or not session.get("effort"):
            raise RoutingError("session route is incomplete")
        validate_effort(runtime, session["effort"])
    if explicit_runtime and explicit_runtime not in RUNTIMES:
        raise RoutingError("explicit runtime must be codex or claude")
    if review_profile not in {"simple", "deep"}:
        raise RoutingError("review profile must be simple or deep")
    if role != "review" and review_profile != "simple":
        raise RoutingError("review profile is valid only for the review role")
    if (
        role == "review"
        and explicit_model
        and explicit_model not in config.data["model_aliases"]
    ):
        raise RoutingError("review model override must be a registered alias")

    source: list[str] = []
    session_source = str(session.get("source") or "") if session else ""

    def inherit_session() -> dict[str, str]:
        if session is None:  # pragma: no cover - guarded by callers below
            raise RoutingError(f"{role} routing requires a captured current session")
        if session_source == "tracked-default":
            raise RoutingError(
                f"{role} routing requires a host-confirmed current session route; "
                "the SessionStart snapshot contains only the tracked default"
            )
        source.append(f"session:{session_source}" if session_source else "session")
        return dict(session)

    if role == "review" and (not same_model or review_profile == "deep"):
        base_runtime = (
            session["runtime"]
            if same_model and session
            else "claude"
            if session and session["runtime"] == "codex"
            else "codex"
        )
        base = config.reviewer_default(base_runtime, review_profile)
        source.append(
            f"{'same-runtime' if same_model else 'opposite-runtime'}-{review_profile}-profile"
        )
    elif role == "diagnostic-fast":
        diagnostic = config.data["roles"]["diagnostic-fast"]
        target = config.resolve_alias(diagnostic["model"])
        base = {
            "runtime": target["runtime"],
            "model": target["model"],
            "effort": diagnostic["effort"],
        }
        source.append("diagnostic-fast-profile")
    elif role == "protected-research":
        if session and session["runtime"] == "codex":
            base = inherit_session()
        else:
            base = config.runtime_default("codex")
            source.append("tracked-default")
    elif role in {"dispatch", "daily", "unsafe-research", "deep"} or (role == "review" and same_model):
        if session is None:
            raise RoutingError(f"{role} routing requires a captured current session")
        base = inherit_session()
    else:  # pragma: no cover
        raise RoutingError(f"unhandled routing role: {role}")

    runtime = explicit_runtime or base["runtime"]
    if explicit_runtime:
        source.append("explicit-runtime")
        if explicit_runtime != base["runtime"] and not explicit_model:
            base = (
                config.reviewer_default(explicit_runtime, review_profile)
                if role == "review" and (not same_model or review_profile == "deep")
                else config.runtime_default(explicit_runtime)
            )
            source.append("runtime-default")
    model = explicit_model or base["model"]
    if explicit_model:
        source.append("explicit-model")
        resolved = config.resolve_alias(explicit_model, explicit_runtime)
        runtime, model = resolved["runtime"], resolved["model"]

    effort = explicit_effort or base["effort"]
    if role == "daily" and not explicit_effort:
        effort = config.data["roles"]["daily"]["effort"]
        source.append("role-effort")
    elif role == "deep" and not explicit_effort:
        effort = config.data["roles"]["deep"]["effort"]
        source.append("role-effort")
    if explicit_effort:
        source.append("explicit-effort")
    validate_effort(runtime, effort)
    return {
        "schema_version": 1,
        "role": role,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "source": source,
        "config_sha256": config.fingerprint,
        "local_override": config.local_override,
    }


def session_path(root: Path, session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise RoutingError("session id contains unsupported characters")
    return root / ".vault-meta" / "session-routing" / f"{session_id}.json"


def capture_session(config: RoutingConfig, session_id: str, runtime: str, model: str, effort: str, *, source: str) -> dict[str, Any]:
    if runtime not in RUNTIMES or not model:
        raise RoutingError("captured session requires runtime and model")
    validate_effort(runtime, effort)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "source": source,
        "config_sha256": config.fingerprint,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path = session_path(config.root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return payload


def load_session(config: RoutingConfig, session_id: str) -> dict[str, Any]:
    path = session_path(config.root, session_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"cannot read captured session route: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RoutingError("captured session route has an unsupported schema")
    runtime = value.get("runtime")
    if runtime not in RUNTIMES or not isinstance(value.get("model"), str):
        raise RoutingError("captured session route is incomplete")
    validate_effort(runtime, value.get("effort"))
    return value


def native_targets(config: RoutingConfig) -> dict[Path, dict[str, str | None]]:
    default = config.runtime_default("codex")
    reviewer = config.reviewer_default("codex", "simple")
    return {
        config.root / ".codex/config.toml": {"model": default["model"], "model_reasoning_effort": default["effort"]},
        config.root / ".codex/profiles/default.toml": {"model": default["model"], "model_reasoning_effort": default["effort"]},
        config.root / ".codex/profiles/wiki-write.toml": {"model": default["model"], "model_reasoning_effort": default["effort"]},
        config.root / ".codex/profiles/reviewer-readonly.toml": {"model": reviewer["model"], "model_reasoning_effort": reviewer["effort"]},
        config.root / ".codex/profiles/deep.toml": {"model": default["model"], "model_reasoning_effort": config.data["roles"]["deep"]["effort"]},
        config.root / ".codex/agents/daily-summarizer.toml": {"model": None, "model_reasoning_effort": config.data["roles"]["daily"]["effort"]},
    }


def sync_native(config: RoutingConfig, *, apply: bool) -> list[str]:
    changed: list[str] = []
    for path, expected in native_targets(config).items():
        try:
            text = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RoutingError(f"cannot inspect native config {path}: {exc}") from exc
        if all(parsed.get(key) == value for key, value in expected.items()):
            continue
        changed.append(str(path.relative_to(config.root)))
        if apply:
            for key, value in expected.items():
                pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=\s*[^\n]+$\n?")
                replacement = "" if value is None else f'{key} = "{value}"\n'
                text, count = pattern.subn(replacement, text, count=1)
                if count == 0 and value is not None:
                    text = replacement + text
            path.write_text(text, encoding="utf-8")
    return changed


def codex_session_route(thread_id: str) -> dict[str, str] | None:
    """Read only model fields from the exact local Codex session transcript."""
    if not SESSION_ID_RE.fullmatch(thread_id):
        return None
    sessions = Path.home() / ".codex" / "sessions"
    candidates = sorted(sessions.rglob(f"*{thread_id}*.jsonl")) if sessions.is_dir() else []
    if len(candidates) != 1:
        return None
    route: dict[str, str] | None = None
    try:
        with candidates[0].open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "turn_context":
                    continue
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    continue
                model = payload.get("model")
                effort = payload.get("effort")
                if not isinstance(effort, str):
                    collaboration = payload.get("collaboration_mode")
                    settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
                    effort = settings.get("reasoning_effort") if isinstance(settings, dict) else None
                if isinstance(model, str) and model and isinstance(effort, str):
                    validate_effort("codex", effort)
                    route = {"runtime": "codex", "model": model, "effort": effort}
    except (OSError, RoutingError):
        return None
    return route


def routing_from_environment(config: RoutingConfig, runtime: str = "", model: str = "", effort: str = "") -> tuple[dict[str, str], str]:
    runtime = runtime or os.environ.get("LLM_OBSIDIAN_SESSION_RUNTIME", "")
    model = model or os.environ.get("LLM_OBSIDIAN_SESSION_MODEL", "")
    effort = effort or os.environ.get("LLM_OBSIDIAN_SESSION_EFFORT", "")
    if runtime or model or effort:
        if not all((runtime, model, effort)):
            raise RoutingError("session routing environment must set runtime, model, and effort together")
        validate_effort(runtime, effort)
        return {"runtime": runtime, "model": model, "effort": effort}, "runtime-environment"
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if thread_id:
        detected = codex_session_route(thread_id)
        if detected is not None:
            return detected, "codex-session-log"
    runtime = "codex" if thread_id else "claude"
    return config.runtime_default(runtime), "tracked-default"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sync = sub.add_parser("sync-native")
    sync.add_argument("--apply", action="store_true")
    cap = sub.add_parser("capture-session")
    cap.add_argument("--session-id", required=True)
    cap.add_argument("--runtime", choices=sorted(RUNTIMES), default="")
    cap.add_argument("--model", default="")
    cap.add_argument("--effort", default="")
    res = sub.add_parser("resolve")
    res.add_argument("--role", choices=sorted(ROLES), required=True)
    res.add_argument("--session-id", default="")
    res.add_argument("--runtime", choices=sorted(RUNTIMES), default="")
    res.add_argument("--model", default="")
    res.add_argument("--effort", default="")
    res.add_argument("--same-model", action="store_true")
    res.add_argument("--review-profile", choices=("simple", "deep"), default="simple")
    args = parser.parse_args()
    try:
        config = load_config(args.root)
        if args.command == "check":
            changed = sync_native(config, apply=False)
            if changed:
                raise RoutingError("native routing config drift: " + ", ".join(changed))
            print(json.dumps({"status": "ok", "config_sha256": config.fingerprint, "local_override": config.local_override}, sort_keys=True))
        elif args.command == "sync-native":
            changed = sync_native(config, apply=args.apply)
            if changed and not args.apply:
                raise RoutingError("native routing config drift: " + ", ".join(changed))
            print(json.dumps({"changed": changed, "applied": args.apply}, sort_keys=True))
        elif args.command == "capture-session":
            route, source = routing_from_environment(config, args.runtime, args.model, args.effort)
            print(json.dumps(capture_session(config, args.session_id, **route, source=source), sort_keys=True))
        else:
            session = load_session(config, args.session_id) if args.session_id else None
            print(json.dumps(resolve(config, args.role, session=session, explicit_runtime=args.runtime, explicit_model=args.model, explicit_effort=args.effort, same_model=args.same_model, review_profile=args.review_profile), sort_keys=True))
        return 0
    except RoutingError as exc:
        die(str(exc), 3)


if __name__ == "__main__":
    raise SystemExit(main())
