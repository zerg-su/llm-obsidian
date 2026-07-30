"""Exact-identity cmux adapter. No focus/title/index ownership guesses."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z")
CHECKPOINT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
REF_RE = re.compile(r"(surface|workspace|window):[1-9][0-9]*\Z")


class CmuxError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _untargeted_environment(
    source: object | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for key in (
        "CMUX_PANE_ID",
        "CMUX_SURFACE_ID",
        "CMUX_WINDOW_ID",
        "CMUX_WORKSPACE_ID",
    ):
        environment.pop(key, None)
    return environment


@dataclass(frozen=True)
class Surface:
    surface_id: str
    surface_ref: str = ""
    workspace_id: str = ""
    workspace_ref: str = ""
    window_id: str = ""
    window_ref: str = ""


def run_cmux(
    args: Sequence[str],
    *,
    runner: Runner | None = None,
    binary: str = "cmux",
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Compatibility perimeter for exact cmux verbs not yet given a typed method."""
    if not args or any(not isinstance(value, str) or "\0" in value for value in args):
        raise CmuxError("cmux adapter arguments are invalid")
    creates_by_title = args[0] in {"new-split", "new-workspace"} or tuple(
        args[:2]
    ) == ("workspace", "create")
    if args[0] in {"focus", "select"} or (
        "--title" in args and creates_by_title
    ):
        raise CmuxError("focus/title operations cannot establish ownership")
    invoke = runner or subprocess.run
    defaults: dict[str, object] = {"text": True, "capture_output": True, "check": False}
    defaults.update(kwargs)
    defaults["env"] = _untargeted_environment(defaults.get("env"))
    return invoke([binary, *args], **defaults)


class CmuxAdapter:
    def __init__(self, runner: Runner | None = None, binary: str = "cmux"):
        self.runner = runner or subprocess.run
        self.binary = binary

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            [self.binary, *args],
            text=True,
            capture_output=True,
            check=False,
            env=_untargeted_environment(),
        )
        if result.returncode:
            diagnostic = (result.stderr or result.stdout).strip()[:1000]
            raise CmuxError(diagnostic or f"cmux command failed: {args[0]}")
        return result

    @staticmethod
    def _surface(value: dict[str, object]) -> Surface:
        nested_surface = value.get("surface")
        nested_workspace = value.get("workspace")
        nested_window = value.get("window")
        surface = nested_surface if isinstance(nested_surface, dict) else {}
        workspace = nested_workspace if isinstance(nested_workspace, dict) else {}
        window = nested_window if isinstance(nested_window, dict) else {}
        surface_id = str(
            value.get("surface_id")
            or surface.get("id")
            or value.get("id")
            or ""
        )
        surface_ref = str(
            value.get("surface_ref")
            or surface.get("ref")
            or value.get("ref")
            or ""
        )
        workspace_id = str(value.get("workspace_id") or workspace.get("id") or "")
        workspace_ref = str(
            value.get("workspace_ref") or workspace.get("ref") or ""
        )
        window_id = str(value.get("window_id") or window.get("id") or "")
        window_ref = str(value.get("window_ref") or window.get("ref") or "")
        if not UUID_RE.fullmatch(surface_id):
            raise CmuxError("cmux response did not contain an exact surface UUID")
        for identity, label in (
            (workspace_id, "workspace"),
            (window_id, "window"),
        ):
            if identity and not UUID_RE.fullmatch(identity):
                raise CmuxError(f"cmux response contained an invalid {label} UUID")
        for ref, label in (
            (surface_ref, "surface"),
            (workspace_ref, "workspace"),
            (window_ref, "window"),
        ):
            if ref and (
                not REF_RE.fullmatch(ref) or not ref.startswith(f"{label}:")
            ):
                raise CmuxError(f"cmux response contained an invalid {label} ref")
        return Surface(
            surface_id,
            surface_ref,
            workspace_id,
            workspace_ref,
            window_id,
            window_ref,
        )

    def _tree(self) -> dict[str, object]:
        try:
            value = json.loads(
                self._run(
                    [
                        "--id-format",
                        "both",
                        "tree",
                        "--all",
                        "--json",
                    ]
                ).stdout
            )
        except json.JSONDecodeError as exc:
            raise CmuxError("cmux tree returned invalid JSON") from exc
        if not isinstance(value, dict) or not isinstance(
            value.get("windows"), list
        ):
            raise CmuxError("cmux tree returned an invalid hierarchy")
        return value

    def open_split(self, origin_surface: str) -> Surface:
        if not UUID_RE.fullmatch(origin_surface):
            raise CmuxError("origin surface must be an exact UUID")
        value = json.loads(
            self._run(
                [
                    "--id-format",
                    "both",
                    "new-split",
                    "right",
                    "--surface",
                    origin_surface,
                    "--focus",
                    "false",
                    "--json",
                ]
            ).stdout
        )
        return self._surface(value)

    def open_workspace(
        self, origin_surface: str, *, cwd: Path | None = None
    ) -> Surface:
        if not UUID_RE.fullmatch(origin_surface):
            raise CmuxError("origin surface must be an exact UUID")
        identified = json.loads(
            self._run(
                [
                    "--id-format",
                    "both",
                    "identify",
                    "--surface",
                    origin_surface,
                    "--json",
                ]
            ).stdout
        )
        caller = (
            identified.get("caller") if isinstance(identified, dict) else None
        )
        window_id = str(caller.get("window_id") or "") if isinstance(caller, dict) else ""
        if not UUID_RE.fullmatch(window_id):
            raise CmuxError("origin surface has no exact window identity")
        args = [
            "--id-format",
            "both",
            "workspace",
            "create",
            "--window",
            window_id,
            "--focus",
            "false",
        ]
        if cwd is not None:
            resolved_cwd = cwd.expanduser().resolve()
            if not resolved_cwd.is_dir():
                raise CmuxError("workspace cwd must be an existing directory")
            args.extend(["--cwd", str(resolved_cwd)])
        args.append("--json")
        value = json.loads(self._run(args).stdout)
        return self._surface(value)

    def send(self, surface_id: str, text: str) -> None:
        self._require_surface(surface_id)
        self._run(["send", "--surface", surface_id, text])

    def send_key(self, surface_id: str, key: str) -> None:
        self._require_surface(surface_id)
        if key not in {"Enter", "Tab", "Escape", "Backspace", "ctrl+u"}:
            raise CmuxError("key is not allowlisted")
        self._run(["send-key", "--surface", surface_id, key])

    def read(self, surface_id: str) -> str:
        self._require_surface(surface_id)
        return self._run(["read-screen", "--surface", surface_id]).stdout

    def status(self, surface_id: str) -> str:
        self._require_surface(surface_id)
        result = self._run(
            [
                "--id-format",
                "both",
                "identify",
                "--surface",
                surface_id,
                "--json",
            ]
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CmuxError("cmux identify returned invalid JSON") from exc
        caller = value.get("caller") if isinstance(value, dict) else None
        if not isinstance(caller, dict):
            raise CmuxError("cmux identify returned no caller identity")
        actual = str(caller.get("surface_id") or "")
        if not actual:
            tree = self._tree()
            matches = [
                str(surface.get("id") or surface.get("surface_id") or "")
                for window in tree.get("windows", [])
                if isinstance(window, dict)
                for workspace in window.get("workspaces", [])
                if isinstance(workspace, dict)
                for pane in workspace.get("panes", [])
                if isinstance(pane, dict)
                for surface in pane.get("surfaces", [])
                if isinstance(surface, dict)
                and str(
                    surface.get("id") or surface.get("surface_id") or ""
                ).casefold()
                == surface_id.casefold()
            ]
            if len(matches) > 1:
                raise CmuxError("cmux tree returned duplicate surface identity")
            return "alive" if matches else "missing"
        if not UUID_RE.fullmatch(actual):
            raise CmuxError("cmux identify returned an invalid surface identity")
        return "alive" if actual.casefold() == surface_id.casefold() else "missing"

    def workspace_status(self, workspace_id: str, window_id: str) -> str:
        if not UUID_RE.fullmatch(workspace_id):
            raise CmuxError("workspace must be an exact UUID")
        if not UUID_RE.fullmatch(window_id):
            raise CmuxError("window must be an exact UUID")
        matches = [
            (
                str(window.get("id") or window.get("window_id") or ""),
                str(
                    workspace.get("id")
                    or workspace.get("workspace_id")
                    or ""
                ),
            )
            for window in self._tree().get("windows", [])
            if isinstance(window, dict)
            for workspace in window.get("workspaces", [])
            if isinstance(workspace, dict)
            and str(
                workspace.get("id") or workspace.get("workspace_id") or ""
            ).casefold()
            == workspace_id.casefold()
        ]
        if len(matches) > 1:
            raise CmuxError("cmux tree returned duplicate workspace identity")
        if not matches:
            return "missing"
        if matches[0][0].casefold() != window_id.casefold():
            raise CmuxError("cmux workspace moved outside its exact window")
        return "alive"

    def resume_checkpoint(self, surface_id: str, runtime: str) -> str:
        """Return the exact provider checkpoint bound by cmux to this surface."""

        self._require_surface(surface_id)
        if runtime not in {"claude", "codex"}:
            raise CmuxError("resume runtime must be claude or codex")
        result = self._run(
            ["surface", "resume", "get", "--json", "--surface", surface_id]
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CmuxError("cmux resume binding is invalid JSON") from exc
        binding = value.get("resume_binding") if isinstance(value, dict) else None
        if not isinstance(binding, dict):
            raise CmuxError("cmux resume binding is unavailable")
        kind = str(binding.get("kind") or "")
        checkpoint = str(
            binding.get("checkpoint_id") or binding.get("checkpoint") or ""
        )
        if kind != runtime or not CHECKPOINT_RE.fullmatch(checkpoint):
            raise CmuxError("cmux resume binding does not match the runtime")
        return checkpoint

    def close_exact(self, surface_id: str) -> None:
        self._require_surface(surface_id)
        self._run(["close-surface", "--surface", surface_id])

    def close_workspace_exact(
        self, workspace_id: str, window_id: str
    ) -> None:
        if not UUID_RE.fullmatch(workspace_id):
            raise CmuxError("workspace must be an exact UUID")
        if not UUID_RE.fullmatch(window_id):
            raise CmuxError("window must be an exact UUID")
        self._run(
            [
                "workspace",
                "close",
                workspace_id,
                "--window",
                window_id,
            ]
        )

    @staticmethod
    def _require_surface(surface_id: str) -> None:
        if not UUID_RE.fullmatch(surface_id):
            raise CmuxError("surface must be an exact UUID")
