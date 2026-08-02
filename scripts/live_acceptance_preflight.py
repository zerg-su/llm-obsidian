"""Provider/profile host preflight for live acceptance."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Callable

from harness.contracts import RuntimeRoute
from live_acceptance_contracts import (
    CELL_IDS,
    SHA,
    LiveDriverError,
    _operations_for,
    _route,
    validate_preflight_evidence,
)
from live_acceptance_review import _review_scratch


def preflight_release(
    root: Path,
    release: dict[str, Any],
    *,
    timeout: int,
    origin_surface: str = "",
    route_preflight: Callable[
        [tuple[tuple[RuntimeRoute, Path, str], ...]], object
    ]
    | None = None,
) -> dict[str, Any]:
    """Check every provider/profile and the exact origin before any model starts."""
    root = root.expanduser().resolve()
    commit_sha = release.get("commit_sha")
    rows = release.get("cells")
    if (
        not SHA.fullmatch(str(commit_sha or ""))
        or not isinstance(rows, list)
        or len(rows) != len(CELL_IDS)
        or {row.get("cell_id") for row in rows if isinstance(row, dict)}
        != set(CELL_IDS)
        or type(timeout) is not int
        or timeout < 1
    ):
        raise LiveDriverError("release preflight request is invalid")
    origin = origin_surface or str(os.environ.get("CMUX_SURFACE_ID") or "")
    if not re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}",
        origin,
    ):
        raise LiveDriverError("release preflight requires the exact origin surface")

    checked: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    requests: list[tuple[RuntimeRoute, Path, str]] = []
    for cell_id in CELL_IDS:
        for operation in _operations_for(cell_id):
            route = _route(root, operation)
            key = (
                route.runtime,
                route.model,
                route.effort,
                route.profile,
                route.routing_sha256,
            )
            if key in seen:
                continue
            seen.add(key)
            if route.profile == "reviewer-callback":
                callback_dir = (
                    _review_scratch(root, str(commit_sha), cell_id)
                    / "preflight"
                    / operation.kind
                )
            else:
                callback_dir = (
                    root
                    / ".vault-meta/acceptance/live-runtime/preflight"
                    / operation.kind
                )
            callback_dir.mkdir(parents=True, exist_ok=True)
            callback_dir.chmod(0o700)
            requests.append((route, callback_dir, origin))
    if route_preflight is None:
        from harness.runtime_sessions import RuntimeSessionManager

        route_preflight = RuntimeSessionManager.for_root(
            root,
            start_timeout_seconds=float(timeout),
        ).preflight_routes
    reports = tuple(route_preflight(tuple(requests)))
    if len(reports) != len(requests):
        raise LiveDriverError("release route preflight returned an incomplete report")
    for (route, _callback_dir, _origin), report in zip(requests, reports):
        if getattr(report, "route", None) != route:
            raise LiveDriverError("release route preflight changed the requested route")
        if getattr(report, "compatible", False) is not True:
            reason = getattr(getattr(report, "reason", None), "value", "")
            raise LiveDriverError(
                "release route preflight failed"
                + (f": {reason}" if reason else "")
            )
        checked.append(
            {
                "runtime": route.runtime,
                "model": route.model,
                "effort": route.effort,
                "profile": route.profile,
                "capabilities": list(
                    getattr(report, "capabilities", ())
                ),
            }
        )
    artifact = {
        "schema_version": 1,
        "commit_sha": commit_sha,
        "origin_surface": origin,
        "routes": checked,
        "status": "compatible",
    }
    return validate_preflight_evidence(
        artifact, commit_sha=str(commit_sha)
    )
