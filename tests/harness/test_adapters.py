#!/usr/bin/env python3
"""Shared fake conformance tests for runtime and cmux adapters."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.adapters.claude import ClaudeDriver, ClaudeDriverError
from harness.adapters.cmux import CmuxAdapter, CmuxError, run_cmux
from harness.adapters.codex import CodexDriver, CodexDriverError
from harness.adapters.process import ProcessAdapter, ProcessError
from harness.contracts import RuntimeRoute


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


surface = "11111111-1111-1111-1111-111111111111"
workspace = "22222222-2222-2222-2222-222222222222"
window = "33333333-3333-3333-3333-333333333333"
calls: list[list[str]] = []
environments: list[dict[str, str]] = []


def fake(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    calls.append(command)
    environments.append(dict(_kwargs.get("env") or {}))
    if "new-split" in command:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "surface_id": surface,
                    "surface_ref": "surface:7",
                    "workspace_id": workspace,
                    "workspace_ref": "workspace:6",
                    "window_id": window,
                    "window_ref": "window:5",
                }
            ),
            "",
        )
    if "identify" in command:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "caller": {
                        "surface_id": surface,
                        "surface_ref": "surface:7",
                        "workspace_id": workspace,
                        "workspace_ref": "workspace:6",
                        "window_id": window,
                        "window_ref": "window:5",
                    }
                }
            ),
            "",
        )
    if "workspace" in command and "create" in command:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "surface_id": surface,
                    "surface_ref": "surface:8",
                    "workspace_id": workspace,
                    "workspace_ref": "workspace:9",
                    "window_id": window,
                    "window_ref": "window:5",
                }
            ),
            "",
        )
    return subprocess.CompletedProcess(command, 0, "ok", "")


cmux = CmuxAdapter(fake)
opened = cmux.open_split(surface)
check(
    "split returns exact UUID and container identity",
    opened.surface_id == surface
    and opened.surface_ref == "surface:7"
    and opened.workspace_id == workspace
    and opened.workspace_ref == "workspace:6"
    and opened.window_id == window
    and opened.window_ref == "window:5",
)
check("status matches exact UUID", cmux.status(surface) == "alive")
workspace_opened = cmux.open_workspace(surface, cwd=ROOT)
check(
    "workspace creation derives exact origin window without surface guessing",
    workspace_opened.workspace_id == workspace
    and any(
        (
            "new-workspace" in call
            or ("workspace" in call and "create" in call)
        )
        and "--window" in call
        and window in call
        and "--surface" not in call
        and "--focus" in call
        and "false" in call
        for call in calls
    ),
)
cmux.send(surface, "bounded prompt pointer")
cmux.send_key(surface, "Enter")
cmux.close_exact(surface)
cmux.close_workspace_exact(workspace, window)
with patch.dict(
    os.environ,
    {
        "CMUX_PANE_ID": "pane:99",
        "CMUX_SURFACE_ID": "surface:99",
        "CMUX_WINDOW_ID": "window:99",
        "CMUX_WORKSPACE_ID": "workspace:99",
        "HARNESS_ENV_KEEP": "present",
    },
):
    environment_start = len(environments)
    cmux.status(surface)
    run_cmux(("identify", "--surface", surface), runner=fake)
seeded_environments = environments[environment_start:]
check(
    "exact cmux calls ignore ambient caller targeting",
    bool(seeded_environments)
    and all(
        not {
            "CMUX_PANE_ID",
            "CMUX_SURFACE_ID",
            "CMUX_WINDOW_ID",
            "CMUX_WORKSPACE_ID",
        }
        & set(environment)
        and environment.get("HARNESS_ENV_KEEP") == "present"
        for environment in seeded_environments
    )
)
check(
    "workspace cleanup binds exact workspace and window",
    any(
        "workspace" in call
        and "close" in call
        and workspace in call
        and window in call
        for call in calls
    ),
)
check(
    "adapter never uses focus/title/index ownership guesses",
    all(
        "focus-pane" not in call
        and "focus-window" not in call
        and "--title" not in call
        for call in calls
    ),
)
check(
    "split uses explicit direction and no-focus semantics",
    any(
        "new-split" in call
        and "right" in call
        and "--focus" in call
        and "false" in call
        for call in calls
    ),
)
for label, call in (
    ("invalid surface rejected", lambda: cmux.close_exact("surface:7")),
    ("generic key rejected", lambda: cmux.send_key(surface, "a")),
):
    try:
        call()
    except CmuxError:
        check(label, True)
    else:
        check(label, False)
try:
    run_cmux(
        ("workspace", "create", "--title", "ambiguous"),
        runner=fake,
    )
except CmuxError:
    check("canonical workspace title ownership guess is rejected", True)
else:
    check("canonical workspace title ownership guess is rejected", False)


def offscreen(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    if "identify" in command:
        payload = {"caller": {"surface_id": ""}}
    elif "tree" in command:
        payload = {
            "windows": [
                {
                    "workspaces": [
                        {
                            "panes": [
                                {"surfaces": [{"id": surface}]}
                            ]
                        }
                    ]
                }
            ]
        }
    else:
        payload = {}
    return subprocess.CompletedProcess(
        command, 0, json.dumps(payload), ""
    )


check(
    "offscreen exact surface remains observable through the all-workspace tree",
    CmuxAdapter(offscreen).status(surface) == "alive",
)


def workspace_tree(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        0,
        json.dumps(
            {
                "windows": [
                    {
                        "id": window,
                        "workspaces": [{"id": workspace, "panes": []}],
                    }
                ]
            }
        ),
        "",
    )


check(
    "exact workspace remains observable independently of its surface",
    CmuxAdapter(workspace_tree).workspace_status(workspace, window)
    == "alive",
)


def moved_workspace_tree(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        0,
        json.dumps(
            {
                "windows": [
                    {
                        "id": "44444444-4444-4444-8444-444444444444",
                        "workspaces": [{"id": workspace, "panes": []}],
                    }
                ]
            }
        ),
        "",
    )


try:
    CmuxAdapter(moved_workspace_tree).workspace_status(
        workspace, window
    )
except CmuxError:
    check("workspace window identity drift is never reported missing", True)
else:
    check("workspace window identity drift is never reported missing", False)


def invalid_tree(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    if "identify" in command:
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"caller": {"surface_id": ""}}), ""
        )
    return subprocess.CompletedProcess(command, 0, "not-json", "")


try:
    CmuxAdapter(invalid_tree).status(surface)
except CmuxError:
    check("invalid all-workspace hierarchy raises typed adapter error", True)
else:
    check("invalid all-workspace hierarchy raises typed adapter error", False)

owned_identity = "a" * 64
recycled_identity = "b" * 64
darwin_sysctls: list[bytes] = []


def fake_darwin_sysctl(
    name: bytes,
    value: object,
    size: object,
    _new_value: object,
    _new_size: int,
) -> int:
    darwin_sysctls.append(name)
    del value, size
    ctypes.set_errno(errno.EPERM)
    return -1


with patch.object(
    ProcessAdapter,
    "_darwin_session_file_id",
    side_effect=PermissionError(errno.EPERM, "sandbox denied file"),
):
    check(
        "Darwin boot identity survives managed-sandbox EPERM",
        ProcessAdapter._darwin_boot_id(
            sysctlbyname=fake_darwin_sysctl
        )
        == "unavailable"
        and darwin_sysctls == [b"kern.boottime"],
    )

with (
    patch("harness.adapters.process.sys.platform", "darwin"),
    patch.object(
        ProcessAdapter,
        "_darwin_process_fields",
        return_value=(123, "1700000000:123456"),
    ),
    patch.object(
        ProcessAdapter,
        "_darwin_session_file_id",
        side_effect=PermissionError(errno.EPERM, "sandbox denied file"),
    ),
    patch.object(
        ProcessAdapter,
        "_darwin_sysctl_boot_id",
        side_effect=PermissionError(errno.EPERM, "sandbox denied sysctl"),
    ),
):
    sandbox_identity = ProcessAdapter.capture_identity(
        123, process_group=123
    )
check(
    "Darwin capture retains PID PGID and start generation without boot API",
    len(sandbox_identity) == 64,
)

with patch("harness.adapters.process.os.killpg") as killpg:
    check(
        "legacy process group without birth identity is unknown",
        ProcessAdapter.process_status(123, "") == "unknown",
    )
    try:
        ProcessAdapter.terminate_exact(123, "")
    except ProcessError:
        pass
    else:
        raise AssertionError("legacy process group must never be signalled")
    check(
        "legacy process group receives no probe or signal",
        not killpg.called,
    )

with (
    patch.object(
        ProcessAdapter,
        "_current_identity",
        return_value=(123, recycled_identity),
    ),
    patch("harness.adapters.process.os.killpg") as killpg,
):
    check(
        "recycled process group is never accepted as owned",
        ProcessAdapter.process_status(123, owned_identity) == "unknown",
    )
    try:
        ProcessAdapter.terminate_exact(123, owned_identity)
    except ProcessError:
        pass
    else:
        raise AssertionError("recycled process group must not be signalled")
    check(
        "recycled process group receives no mutating signal",
        all(call.args[1] == 0 for call in killpg.call_args_list),
    )

with (
    patch.object(
        ProcessAdapter,
        "_current_identity",
        side_effect=[
            (123, owned_identity),
            (123, recycled_identity),
        ],
    ),
    patch("harness.adapters.process.os.killpg") as killpg,
):
    check(
        "initial owned process observation succeeds",
        ProcessAdapter.process_status(123, owned_identity) == "alive",
    )
    try:
        ProcessAdapter.request_exit(123, owned_identity)
    except ProcessError:
        pass
    else:
        raise AssertionError("signal must freshly recheck process identity")
    check(
        "identity changed after observation receives no TERM",
        all(call.args[1] == 0 for call in killpg.call_args_list),
    )

with (
    patch.object(
        ProcessAdapter,
        "_current_identity",
        return_value=(123, owned_identity),
    ),
    patch("harness.adapters.process.os.killpg") as killpg,
):
    try:
        ProcessAdapter.request_exit(123, owned_identity)
    except ProcessError:
        pass
    else:
        raise AssertionError(
            "a non-parent identity check cannot authorize direct killpg"
        )
    check(
        "ownership transfer at mutation boundary cannot hit a recycled PGID",
        not killpg.called,
    )

with (
    patch("harness.adapters.process.os.waitid", return_value=None) as waitid,
    patch.object(
        ProcessAdapter,
        "_current_identity",
        return_value=(123, owned_identity),
    ),
    patch("harness.adapters.process.os.killpg") as killpg,
):
    ProcessAdapter.signal_owned_child_group(
        123, owned_identity, signal.SIGTERM
    )
    check(
        "sole parent pins the unreaped leader before group mutation",
        waitid.call_args.args[0:2] == (os.P_PID, 123)
        and waitid.call_args.args[2] & os.WNOWAIT
        and killpg.call_args_list == [((123, signal.SIGTERM), {})],
    )

with (
    patch("harness.adapters.process.os.waitid", return_value=None),
    patch.object(
        ProcessAdapter,
        "_current_identity",
        return_value=(123, recycled_identity),
    ),
    patch("harness.adapters.process.os.killpg") as killpg,
):
    try:
        ProcessAdapter.signal_owned_child_group(
            123, owned_identity, signal.SIGTERM
        )
    except ProcessError:
        pass
    else:
        raise AssertionError(
            "guardian must reject identity transfer before mutation"
        )
    check(
        "identity transfer at the guarded mutation boundary sends no signal",
        not killpg.called,
    )

with (
    patch("harness.adapters.process.os.waitid", return_value=None),
    patch.object(
        ProcessAdapter,
        "_current_identity",
        return_value=(123, owned_identity),
    ),
    patch(
        "harness.adapters.process.os.killpg",
        side_effect=OSError(errno.EINVAL, "invalid signal target"),
    ),
):
    try:
        ProcessAdapter.signal_owned_child_group(
            123, owned_identity, signal.SIGTERM
        )
    except ProcessError:
        check("non-lookup signal errors are typed", True)
    else:
        check("non-lookup signal errors are typed", False)

linux_fields = ["S", "1", "123", *(["0"] * 16), "987654"]
with patch(
    "harness.adapters.process.Path.read_text",
    side_effect=[
        f"123 (provider ) with paren) {' '.join(linux_fields)}",
        "boot-identity\n",
    ],
):
    check(
        "Linux identity parser tolerates a closing paren in process name",
        ProcessAdapter._linux_process_record(123)
        == (123, "987654", "boot-identity"),
    )


class UnidentifiedChild:
    pid = 321

    def __init__(self) -> None:
        self.waited = False

    def wait(self, *, timeout: float) -> int:
        self.waited = timeout == 1.0
        return 0


unidentified = UnidentifiedChild()
with (
    patch(
        "harness.adapters.process.subprocess.Popen",
        return_value=unidentified,
    ),
    patch("harness.adapters.process.os.getpgid", return_value=321),
    patch("harness.adapters.process.os.killpg") as containment_signal,
    patch.object(ProcessAdapter, "capture_identity", return_value=""),
):
    try:
        ProcessAdapter().start(
            (str(Path(sys.executable).resolve()), "-c", "pass"),
            cwd=ROOT,
            env=dict(os.environ),
        )
    except ProcessError:
        pass
    else:
        raise AssertionError("unidentified provider must fail closed")
check(
    "failed identity capture contains and reaps the new provider",
    unidentified.waited
    and containment_signal.call_args_list
    == [((321, signal.SIGTERM), {})],
)

live_handle = ProcessAdapter().start(
    (
        str(Path(sys.executable).resolve()),
        "-c",
        "import time; time.sleep(30)",
    ),
    cwd=ROOT,
    env=dict(os.environ),
)
try:
    check(
        "host identity backend recognizes one exact live child",
        live_handle.pid == live_handle.process_group
        and len(live_handle.process_identity) == 64
        and ProcessAdapter.process_status(
            live_handle.process_group, live_handle.process_identity
        )
        == "alive",
    )
    ProcessAdapter.signal_owned_child_group(
        live_handle.process_group,
        live_handle.process_identity,
        signal.SIGKILL,
    )
    os.waitpid(live_handle.pid, 0)
    check(
        "dead exact child remains idempotent after reap",
        ProcessAdapter.process_status(
            live_handle.process_group, live_handle.process_identity
        )
        == "dead",
    )
finally:
    try:
        ProcessAdapter.signal_owned_child_group(
            live_handle.process_group,
            live_handle.process_identity,
            signal.SIGKILL,
        )
    except ProcessError:
        pass
    try:
        os.waitpid(live_handle.pid, 0)
    except ChildProcessError:
        pass

with tempfile.TemporaryDirectory(prefix="exited-leader-group.") as raw:
    group_root = Path(raw)
    ready_path = group_root / "descendant-ready"
    signaled_path = group_root / "descendant-signaled"
    descendant_pid_path = group_root / "descendant-pid"
    provider_path = group_root / "provider.py"
    provider_path.write_text(
        "import pathlib,subprocess,sys,time\n"
        "code = '''import pathlib,signal,sys,time\n"
        "ready=pathlib.Path(sys.argv[1])\n"
        "signaled=pathlib.Path(sys.argv[2])\n"
        "def stop(_sig, _frame):\n"
        "    signaled.write_text(\"signal\", encoding=\"utf-8\")\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "ready.write_text(\"ready\", encoding=\"utf-8\")\n"
        "while True: time.sleep(1)\n"
        "'''\n"
        "child = subprocess.Popen([sys.executable, '-c', code, sys.argv[1], sys.argv[2]])\n"
        "while not pathlib.Path(sys.argv[1]).is_file(): time.sleep(0.01)\n"
        "pathlib.Path(sys.argv[3]).write_text(str(child.pid), encoding='utf-8')\n",
        encoding="utf-8",
    )
    group_handle = ProcessAdapter().start(
        (
            str(Path(sys.executable).resolve()),
            str(provider_path),
            str(ready_path),
            str(signaled_path),
            str(descendant_pid_path),
        ),
        cwd=group_root,
        env=dict(os.environ),
    )
    deadline = time.monotonic() + 3
    while (
        not descendant_pid_path.is_file()
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    exited = None
    deadline = time.monotonic() + 3
    while exited is None and time.monotonic() < deadline:
        exited = os.waitid(
            os.P_PID,
            group_handle.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        if exited is None:
            time.sleep(0.02)
    try:
        descendant_visible = ProcessAdapter._group_has_other_members(
            group_handle.process_group, group_handle.pid
        )
        ProcessAdapter.signal_owned_child_group(
            group_handle.process_group,
            group_handle.process_identity,
            signal.SIGTERM,
        )
        deadline = time.monotonic() + 3
        while not signaled_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        check(
            "exited pinned leader still signals live same-PGID descendant",
            exited is not None
            and descendant_visible
            and signaled_path.is_file(),
        )
    finally:
        try:
            os.killpg(group_handle.process_group, signal.SIGKILL)
        except OSError:
            pass
        try:
            descendant_pid = int(
                descendant_pid_path.read_text(encoding="utf-8")
            )
            os.kill(descendant_pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
        try:
            os.waitpid(group_handle.pid, 0)
        except ChildProcessError:
            pass

real_run = subprocess.run
late_calls: list[list[str]] = []
def late_fake(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    late_calls.append(command)
    return subprocess.CompletedProcess(command, 0, "ok", "")
subprocess.run = late_fake
try:
    CmuxAdapter().send(surface, "late-bound")
finally:
    subprocess.run = real_run
check("default runner is resolved at construction time", bool(late_calls))

digest = "a" * 64
claude_route = RuntimeRoute("claude", "fable", "xhigh", "reviewer-readonly", digest)
codex_route = RuntimeRoute("codex", "gpt-5.6-sol", "xhigh", "reviewer-readonly", digest)
claude = ClaudeDriver(Path("/usr/bin/claude")).command(claude_route)
codex = CodexDriver(Path("/usr/bin/codex")).command(codex_route)
check("Claude reviewer is interactive dontAsk", "--permission-mode" in claude and "dontAsk" in claude and "--print" not in claude)
check("Codex reviewer is read-only approval-never", "read-only" in codex and "never" in codex)
with tempfile.TemporaryDirectory(prefix="codex-executor-policy.") as raw:
    policy_root = Path(raw)
    source = policy_root / "source"
    source.mkdir()
    product = policy_root / "product"
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "policy@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Policy Test"],
        cwd=source,
        check=True,
    )
    (source / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "task/policy", str(product)],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    )
    inherited_cmux_socket = os.environ.get("CMUX_SOCKET_PATH", "").strip()
    inherited_cmux_path = (
        Path(inherited_cmux_socket).expanduser()
        if inherited_cmux_socket
        else None
    )
    # Exact-socket executor sandboxes cannot bind a second AF_UNIX path. Reuse
    # their already validated socket; hermetic host runs still own a fixture.
    if inherited_cmux_path is not None and inherited_cmux_path.is_socket():
        cmux_socket = inherited_cmux_path.resolve()
        server = None
    else:
        cmux_socket = policy_root / "cmux.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(cmux_socket))
    previous_socket = os.environ.get("CMUX_SOCKET_PATH")
    os.environ["CMUX_SOCKET_PATH"] = str(cmux_socket)
    try:
        executor = CodexDriver(Path("/usr/bin/codex")).command(
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "high",
                "executor",
                digest,
            ),
            product_root=product,
            session_root=product,
        )
    finally:
        if previous_socket is None:
            os.environ.pop("CMUX_SOCKET_PATH", None)
        else:
            os.environ["CMUX_SOCKET_PATH"] = previous_socket
        if server is not None:
            server.close()
    git_common = (
        subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=product,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    git_common_path = (product / git_common).resolve()
    check(
        "Codex executor receives exact Git and cmux capabilities",
        "--cd" in executor
        and executor[executor.index("--cd") + 1] == str(product.resolve())
        and "--add-dir" in executor
        and executor[executor.index("--add-dir") + 1]
        == str(git_common_path)
        and git_common_path == (source / ".git").resolve()
        and "workspace-write" in executor
        and "never" in executor
        and any(
            str(cmux_socket.resolve()) in item
            and "features.network_proxy.unix_sockets" in item
            for item in executor
        ),
    )
for label, call in (
    ("Claude rejects wrong runtime", lambda: ClaudeDriver(Path("/usr/bin/claude")).command(codex_route)),
    ("Codex rejects wrong runtime", lambda: CodexDriver(Path("/usr/bin/codex")).command(claude_route)),
):
    try:
        call()
    except (ClaudeDriverError, CodexDriverError):
        check(label, True)
    else:
        check(label, False)
