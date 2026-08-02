"""Model-routing configuration parsing, invariants, and fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TRACKED_CONFIG = Path("config/model-routing.toml")
LOCAL_CONFIG = Path("config/model-routing.local.toml")
RUNTIMES = {"codex", "claude"}
CODEX_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
ROLES = {
    "dispatch",
    "daily",
    "review",
    "protected-research",
    "unsafe-research",
    "deep",
    "diagnostic-fast",
}


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
        return {
            "runtime": runtime,
            "model": value["model"],
            "effort": value["effort"],
        }

    def resolve_alias(self, name: str, runtime: str = "") -> dict[str, str]:
        aliases = self.data["model_aliases"]
        if name in aliases:
            value = aliases[name]
            if runtime and value["runtime"] != runtime:
                raise RoutingError(
                    f"model alias {name!r} belongs to {value['runtime']}, not {runtime}"
                )
            return {"runtime": value["runtime"], "model": value["target"]}
        registered = self.data["model_registry"].get(name)
        if registered:
            if runtime and registered != runtime:
                raise RoutingError(
                    f"model {name!r} is registered for {registered}, not {runtime}"
                )
            return {"runtime": registered, "model": name}
        if not runtime:
            raise RoutingError("an unregistered explicit model requires --runtime")
        return {"runtime": runtime, "model": name}

    def reviewer_default(
        self, runtime: str, profile: str = "simple"
    ) -> dict[str, str]:
        value = self.data["review_profiles"][profile][runtime]
        target = self.resolve_alias(value["model"], runtime)
        return {
            "runtime": runtime,
            "model": target["model"],
            "effort": value["effort"],
        }

    def legacy_reviewer_default(self, runtime: str) -> dict[str, str]:
        value = self.data["legacy_review_defaults"][runtime]
        return {
            "runtime": runtime,
            "model": value["model"],
            "effort": value["effort"],
        }

    def default_models(self) -> set[str]:
        diagnostic = self.resolve_alias(
            self.data["roles"]["diagnostic-fast"]["model"]
        )
        return {
            *(self.runtime_default(runtime)["model"] for runtime in RUNTIMES),
            *(
                self.reviewer_default(runtime, profile)["model"]
                for runtime in RUNTIMES
                for profile in ("simple", "deep")
            ),
            diagnostic["model"],
        }


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


def _merge(
    base: dict[str, Any],
    overlay: dict[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key == "schema_version":
            if value != base.get(key):
                raise RoutingError(
                    "local routing override cannot change schema_version"
                )
            continue
        if key not in base:
            if prefix in {"model_registry.", "model_aliases."} and isinstance(
                value, (str, dict)
            ):
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
    if (
        runtime not in RUNTIMES
        or not isinstance(effort, str)
        or effort not in allowed
    ):
        raise RoutingError(f"{runtime} effort must be one of {sorted(allowed)}")
    return effort


def _versioned_claude_generation(target: str) -> int | None:
    match = re.fullmatch(r"claude-[a-z0-9]+-(\d+)(?:-\d+)*", target)
    return int(match.group(1)) if match else None


def _validate_schema_and_registry(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("schema_version") != 1:
        raise RoutingError("model routing schema_version must be 1")
    if set(data.get("runtimes", {})) != RUNTIMES:
        raise RoutingError(
            "routing config must define exactly codex and claude runtimes"
        )
    registry = data.get("model_registry")
    if not isinstance(registry, dict) or not registry:
        raise RoutingError("model_registry must be a non-empty table")
    return registry


def _validate_runtime_defaults(
    data: dict[str, Any], registry: dict[str, Any]
) -> None:
    for runtime in sorted(RUNTIMES):
        item = data["runtimes"].get(runtime)
        if not isinstance(item, dict) or set(item) != {"model", "effort"}:
            raise RoutingError(
                f"runtimes.{runtime} must contain model and effort"
            )
        model, effort = item["model"], item["effort"]
        if not isinstance(model, str) or not model.strip():
            raise RoutingError(f"runtimes.{runtime}.model must be non-empty")
        validate_effort(runtime, effort)
        if registry.get(model) != runtime:
            raise RoutingError(
                f"default model {model!r} is not registered for {runtime}"
            )


def _validate_role_efforts(roles: dict[str, Any]) -> None:
    for role in ("daily", "deep"):
        item = roles[role]
        if (
            not isinstance(item, dict)
            or set(item) != {"effort"}
            or not isinstance(item["effort"], str)
        ):
            raise RoutingError(f"roles.{role} must contain one effort")
        for runtime in RUNTIMES:
            validate_effort(runtime, item["effort"])


def _validate_review_role(
    roles: dict[str, Any], registry: dict[str, Any]
) -> None:
    review = roles["review"]
    if not isinstance(review, dict) or set(review) != RUNTIMES:
        raise RoutingError("roles.review must define exactly codex and claude")
    for runtime in sorted(RUNTIMES):
        item = review[runtime]
        if not isinstance(item, dict) or set(item) != {"model", "effort"}:
            raise RoutingError(
                f"roles.review.{runtime} must contain model and effort"
            )
        model, effort = item["model"], item["effort"]
        if not isinstance(model, str) or not model.strip():
            raise RoutingError(
                f"roles.review.{runtime}.model must be non-empty"
            )
        validate_effort(runtime, effort)
        if registry.get(model) != runtime:
            raise RoutingError(
                f"review model {model!r} is not registered for {runtime}"
            )


def _validate_roles(
    data: dict[str, Any], registry: dict[str, Any]
) -> dict[str, Any]:
    roles = data.get("roles")
    if not isinstance(roles, dict) or set(roles) != {
        "daily",
        "deep",
        "diagnostic-fast",
        "review",
    }:
        raise RoutingError(
            "roles must define exactly daily, deep, diagnostic-fast, and review"
        )
    _validate_role_efforts(roles)
    _validate_review_role(roles, registry)
    return roles


def _validate_registry_entries(registry: dict[str, Any]) -> None:
    for model, runtime in registry.items():
        if (
            not isinstance(model, str)
            or not model.strip()
            or runtime not in RUNTIMES
        ):
            raise RoutingError(
                "model_registry entries must map non-empty model names to codex or claude"
            )


def _validate_aliases(
    data: dict[str, Any], registry: dict[str, Any]
) -> dict[str, Any]:
    aliases = data.get("model_aliases")
    if not isinstance(aliases, dict) or len(aliases) != 4:
        raise RoutingError(
            "model_aliases must define exactly four release aliases"
        )
    for alias, item in aliases.items():
        if not isinstance(item, dict):
            raise RoutingError(f"model_aliases.{alias} must be a table")
        required = {"runtime", "target"}
        if not required.issubset(item) or set(item) - required - {
            "expected_generation"
        }:
            raise RoutingError(f"model_aliases.{alias} has an invalid shape")
        runtime, target = item["runtime"], item["target"]
        if runtime not in RUNTIMES or registry.get(target) != runtime:
            raise RoutingError(
                f"model_aliases.{alias} target/runtime mismatch"
            )
        generation = item.get("expected_generation")
        if runtime == "claude" and generation != 5:
            raise RoutingError(
                f"model_aliases.{alias} expected generation drift"
            )
        target_generation = (
            _versioned_claude_generation(target)
            if runtime == "claude"
            else None
        )
        if target_generation is not None and target_generation != generation:
            raise RoutingError(
                f"model_aliases.{alias} expected generation drift"
            )
        if runtime == "codex" and generation is not None:
            raise RoutingError(
                f"model_aliases.{alias} must not declare a Claude generation"
            )
    counts = {
        runtime: sum(
            item["runtime"] == runtime for item in aliases.values()
        )
        for runtime in RUNTIMES
    }
    if counts != {runtime: 2 for runtime in RUNTIMES}:
        raise RoutingError("model_aliases must define two targets per runtime")
    return aliases


def _validate_diagnostic_role(
    roles: dict[str, Any],
    registry: dict[str, Any],
    aliases: dict[str, Any],
) -> None:
    diagnostic = roles["diagnostic-fast"]
    if (
        not isinstance(diagnostic, dict)
        or set(diagnostic) != {"model", "effort"}
        or not isinstance(diagnostic["model"], str)
        or not isinstance(diagnostic["effort"], str)
    ):
        raise RoutingError(
            "roles.diagnostic-fast must contain model and effort"
        )
    diagnostic_model = diagnostic["model"]
    diagnostic_runtime = (
        aliases[diagnostic_model]["runtime"]
        if diagnostic_model in aliases
        else registry.get(diagnostic_model)
    )
    if diagnostic_runtime is None:
        raise RoutingError(
            "roles.diagnostic-fast.model must be a registered model or alias"
        )
    validate_effort(diagnostic_runtime, diagnostic["effort"])


def _validate_review_profiles(
    data: dict[str, Any],
    registry: dict[str, Any],
    aliases: dict[str, Any],
) -> None:
    profiles = data.get("review_profiles")
    if not isinstance(profiles, dict) or set(profiles) != {"simple", "deep"}:
        raise RoutingError("review_profiles must define simple and deep")
    for profile, by_runtime in profiles.items():
        if not isinstance(by_runtime, dict) or set(by_runtime) != RUNTIMES:
            raise RoutingError(
                f"review_profiles.{profile} must define both runtimes"
            )
        for runtime, item in by_runtime.items():
            if not isinstance(item, dict) or set(item) != {"model", "effort"}:
                raise RoutingError(
                    f"review_profiles.{profile}.{runtime} has an invalid shape"
                )
            model = item["model"]
            registered_runtime = (
                aliases[model]["runtime"]
                if model in aliases
                else registry.get(model)
            )
            if registered_runtime != runtime:
                raise RoutingError(
                    f"review profile alias/runtime mismatch: {profile}.{runtime}"
                )
            validate_effort(runtime, item["effort"])


def _validate_legacy_defaults(
    data: dict[str, Any], registry: dict[str, Any]
) -> None:
    legacy_defaults = data.get("legacy_review_defaults")
    if not isinstance(legacy_defaults, dict) or set(legacy_defaults) != RUNTIMES:
        raise RoutingError("legacy_review_defaults must define both runtimes")
    for runtime, item in legacy_defaults.items():
        if not isinstance(item, dict) or set(item) != {"model", "effort"}:
            raise RoutingError(
                f"legacy_review_defaults.{runtime} has an invalid shape"
            )
        model = item["model"]
        if registry.get(model) != runtime:
            raise RoutingError(
                f"legacy review model {model!r} is not registered for {runtime}"
            )
        validate_effort(runtime, item["effort"])


def _validate(data: dict[str, Any]) -> None:
    registry = _validate_schema_and_registry(data)
    _validate_runtime_defaults(data, registry)
    roles = _validate_roles(data, registry)
    _validate_registry_entries(registry)
    aliases = _validate_aliases(data, registry)
    _validate_diagnostic_role(roles, registry, aliases)
    _validate_review_profiles(data, registry, aliases)
    _validate_legacy_defaults(data, registry)


def _config(
    root: Path,
    data: dict[str, Any],
    *,
    local_override: bool,
) -> RoutingConfig:
    _validate(data)
    canonical = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RoutingConfig(
        root,
        data,
        hashlib.sha256(canonical.encode()).hexdigest(),
        local_override,
    )


def load_config(root: Path | str = ROOT) -> RoutingConfig:
    root = Path(root).expanduser().resolve()
    tracked = _read_toml(root / TRACKED_CONFIG)
    local_path = root / LOCAL_CONFIG
    local = local_path.is_file()
    data = _merge(tracked, _read_toml(local_path)) if local else tracked
    return _config(root, data, local_override=local)


def load_tracked_config(root: Path | str = ROOT) -> RoutingConfig:
    """Load only the release-owned defaults, ignoring any local override."""

    root = Path(root).expanduser().resolve()
    data = _read_toml(root / TRACKED_CONFIG)
    return _config(root, data, local_override=False)


def validate_local_config(root: Path | str, text: str) -> RoutingConfig:
    """Validate prospective local TOML without installing it."""

    root = Path(root).expanduser().resolve()
    tracked = _read_toml(root / TRACKED_CONFIG)
    try:
        overlay = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RoutingError(f"invalid prospective {LOCAL_CONFIG}: {exc}") from exc
    data = _merge(tracked, overlay)
    return _config(root, data, local_override=True)
