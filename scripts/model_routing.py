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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
TRACKED_CONFIG = Path("config/model-routing.toml")
LOCAL_CONFIG = Path("config/model-routing.local.toml")
RUNTIMES = {"codex", "claude"}
CODEX_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
ROLES = {"dispatch", "daily", "review", "protected-research", "unsafe-research", "deep"}
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingConfig:
    root: Path
    data: dict[str, Any]
    fingerprint: str
    local_override: bool

    def runtime_default(self, runtime: str) -> dict[str, str]:
        value = self.data["runtimes"][runtime]
        return {"runtime": runtime, "model": value["model"], "effort": value["effort"]}


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RoutingError(f"missing routing config: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RoutingError(f"invalid routing config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RoutingError(f"routing config must be an object: {path}")
    return value


def _merge(base: dict[str, Any], overlay: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key == "schema_version":
            if value != base.get(key):
                raise RoutingError("local routing override cannot change schema_version")
            continue
        if key not in base:
            if prefix == "model_registry." and isinstance(value, str):
                result[key] = value
                continue
            raise RoutingError(f"unknown local routing key: {prefix}{key}")
        if isinstance(value, dict) and isinstance(base[key], dict):
            result[key] = _merge(base[key], value, f"{prefix}{key}.")
        elif isinstance(value, str) and isinstance(base[key], str):
            result[key] = value
        else:
            raise RoutingError(f"invalid local routing value: {prefix}{key}")
    return result


def validate_effort(runtime: str, effort: Any) -> str:
    allowed = CODEX_EFFORTS if runtime == "codex" else CLAUDE_EFFORTS
    if runtime not in RUNTIMES or not isinstance(effort, str) or effort not in allowed:
        raise RoutingError(f"{runtime} effort must be one of {sorted(allowed)}")
    return effort


def _validate(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise RoutingError("model routing schema_version must be 1")
    if set(data.get("runtimes", {})) != RUNTIMES:
        raise RoutingError("routing config must define exactly codex and claude runtimes")
    registry = data.get("model_registry")
    if not isinstance(registry, dict) or not registry:
        raise RoutingError("model_registry must be a non-empty table")
    for runtime in sorted(RUNTIMES):
        item = data["runtimes"].get(runtime)
        if not isinstance(item, dict) or set(item) != {"model", "effort"}:
            raise RoutingError(f"runtimes.{runtime} must contain model and effort")
        model, effort = item["model"], item["effort"]
        if not isinstance(model, str) or not model.strip():
            raise RoutingError(f"runtimes.{runtime}.model must be non-empty")
        validate_effort(runtime, effort)
        if registry.get(model) != runtime:
            raise RoutingError(f"default model {model!r} is not registered for {runtime}")
    roles = data.get("roles")
    if not isinstance(roles, dict) or set(roles) != {"daily", "deep"}:
        raise RoutingError("roles must define exactly daily and deep")
    for role in ("daily", "deep"):
        item = roles[role]
        if not isinstance(item, dict) or set(item) != {"effort"} or not isinstance(item["effort"], str):
            raise RoutingError(f"roles.{role} must contain one effort")
        for runtime in RUNTIMES:
            validate_effort(runtime, item["effort"])
    for model, runtime in registry.items():
        if not isinstance(model, str) or not model.strip() or runtime not in RUNTIMES:
            raise RoutingError("model_registry entries must map non-empty model names to codex or claude")


def load_config(root: Path | str = ROOT) -> RoutingConfig:
    root = Path(root).expanduser().resolve()
    tracked = _read_toml(root / TRACKED_CONFIG)
    local_path = root / LOCAL_CONFIG
    local = local_path.is_file()
    data = _merge(tracked, _read_toml(local_path)) if local else tracked
    _validate(data)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RoutingConfig(root, data, hashlib.sha256(canonical.encode()).hexdigest(), local)


def load_tracked_config(root: Path | str = ROOT) -> RoutingConfig:
    """Load only the release-owned defaults, ignoring any local override."""
    root = Path(root).expanduser().resolve()
    data = _read_toml(root / TRACKED_CONFIG)
    _validate(data)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RoutingConfig(root, data, hashlib.sha256(canonical.encode()).hexdigest(), False)


def validate_local_config(root: Path | str, text: str) -> RoutingConfig:
    """Validate prospective local TOML without installing it."""
    root = Path(root).expanduser().resolve()
    tracked = _read_toml(root / TRACKED_CONFIG)
    try:
        overlay = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RoutingError(f"invalid prospective {LOCAL_CONFIG}: {exc}") from exc
    data = _merge(tracked, overlay)
    _validate(data)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RoutingConfig(root, data, hashlib.sha256(canonical.encode()).hexdigest(), True)


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

    if role == "review" and not same_model:
        base_runtime = "claude" if session and session["runtime"] == "codex" else "codex"
        base = config.runtime_default(base_runtime)
        source.append("opposite-runtime-default")
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
            base = config.runtime_default(explicit_runtime)
            source.append("runtime-default")
    model = explicit_model or base["model"]
    if explicit_model:
        source.append("explicit-model")
        registered = config.data["model_registry"].get(explicit_model)
        if not explicit_runtime:
            if registered is None:
                raise RoutingError("an unregistered explicit model requires --runtime")
            runtime = registered
        elif registered and registered != runtime:
            raise RoutingError(f"model {explicit_model!r} is registered for {registered}, not {runtime}")

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
    return {
        config.root / ".codex/config.toml": {"model": default["model"], "model_reasoning_effort": default["effort"]},
        config.root / ".codex/profiles/default.toml": {"model": default["model"], "model_reasoning_effort": default["effort"]},
        config.root / ".codex/profiles/wiki-write.toml": {"model": default["model"], "model_reasoning_effort": default["effort"]},
        config.root / ".codex/profiles/reviewer-readonly.toml": {"model": default["model"], "model_reasoning_effort": default["effort"]},
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
            print(json.dumps(resolve(config, args.role, session=session, explicit_runtime=args.runtime, explicit_model=args.model, explicit_effort=args.effort, same_model=args.same_model), sort_keys=True))
        return 0
    except RoutingError as exc:
        die(str(exc), 3)


if __name__ == "__main__":
    raise SystemExit(main())
