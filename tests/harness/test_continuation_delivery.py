#!/usr/bin/env python3
"""Ordering matrix for semantic retained-session continuation delivery."""

from __future__ import annotations

import sys
import tempfile
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.runtime_session_continuation import (  # noqa: E402
    _editor_digest,
    _screen_digest,
    await_initial_start_acknowledged,
    classify_continuation_screen,
    deliver_continuation,
)
from harness.retained_notification import (  # noqa: E402
    RetainedNotificationError,
    deliver_worker_notification,
    recover_visible_notification,
    send_visible_notification,
)


SURFACE = "11111111-1111-1111-1111-111111111111"
PROMPT = "# Harness-owned review verification\nInspect the exact HEAD."

codex_158_activity = (
    "Working (13s • esc to interrupt)\n\n› Ask Codex to do anything\n"
    "GPT-6-Astra high · Context 4% used · 258K window · never\n"
    "? for shortcuts\n"
)
assert classify_continuation_screen("codex", codex_158_activity, "") == "active"
for near_match in (
    "Working (13s • esc to interrupt)",
    codex_158_activity.replace("Working (", "Quoted Working ("),
    codex_158_activity.replace("? for shortcuts", "quoted footer"),
):
    assert classify_continuation_screen("codex", near_match, "") != "active"
print("OK   Codex 0.158 activity requires the exact native status and composer footer")


class FakePort:
    def __init__(self, screens: list[str]) -> None:
        self.screens = list(screens)
        self.sent: list[str] = []
        self.keys: list[str] = []

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        if not self.screens:
            return ""
        return self.screens.pop(0)

    def send(self, surface_id: str, text: str) -> None:
        assert surface_id == SURFACE
        self.sent.append(text)

    def send_key(self, surface_id: str, key: str) -> None:
        assert surface_id == SURFACE and key == "Enter"
        self.keys.append(key)


assert await_initial_start_acknowledged(
    FakePort([codex_158_activity]),
    surface_id=SURFACE,
    runtime="codex",
    anchor=PROMPT.splitlines()[0],
    paste_screen_sha256=_screen_digest("› " + PROMPT),
    observation_limit=1,
) == "started"
print("OK   Codex 0.158 activity acknowledges initial submission without a resend")


class SemanticPort(FakePort):
    def agent_status(self, workspace_id: str, runtime: str) -> str:
        assert workspace_id == "22222222-2222-2222-2222-222222222222"
        assert runtime == "claude"
        return "idle"


class FakeWorker:
    def __init__(self, port: FakePort) -> None:
        self.cmux_adapter = port
        self.spec = {"surface_id": SURFACE, "runtime": "codex"}

    def _workspace_id(self) -> str:
        return "22222222-2222-2222-2222-222222222222"

    @staticmethod
    def write_immutable_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


def run_case(
    screens: list[str],
    *,
    pre_screen: str = "›",
    artifacts: list[bool] | None = None,
    retry: bool = True,
    send_prompt: bool = True,
    submit_already_accepted: bool = False,
    accepted_submit_count: int = 0,
    pre_send_screen_sha256: str = "",
    pre_send_editor_sha256: str = "",
    paste_screen_sha256: str = "",
    runtime: str = "codex",
    ownership: list[bool] | None = None,
):
    port = FakePort(([pre_screen] if send_prompt else []) + screens)
    artifact_values = list(artifacts or [])
    retries: list[bool] = []
    stages: list[tuple[str, int]] = []
    ownership_values = list(ownership or [])

    def artifact_ready() -> bool:
        return artifact_values.pop(0) if artifact_values else False

    def reserve_retry() -> bool:
        retries.append(retry)
        return retry

    def ownership_ready() -> bool:
        return ownership_values.pop(0) if ownership_values else True

    result = deliver_continuation(
        port,
        surface_id=SURFACE,
        prompt=PROMPT,
        runtime=runtime,
        artifact_ready=artifact_ready,
        ownership_ready=ownership_ready,
        reserve_retry=reserve_retry,
        observe_stage=lambda stage, count, *_digests: stages.append((stage, count)),
        send_prompt=send_prompt,
        submit_already_accepted=submit_already_accepted,
        accepted_submit_count=accepted_submit_count,
        pre_send_screen_sha256=pre_send_screen_sha256,
        pre_send_editor_sha256=pre_send_editor_sha256,
        paste_screen_sha256=paste_screen_sha256,
        observation_limit=2,
        wait=lambda _seconds: None,
    )
    return result, port, retries, stages


delayed = FakePort(["❯", f"❯ {PROMPT.splitlines()[0]}"])
send_visible_notification(
    delayed,
    surface_id=SURFACE,
    runtime="claude",
    message=PROMPT,
    observation_limit=2,
    wait=lambda _seconds: None,
)
assert delayed.sent == [PROMPT] and delayed.keys == ["Enter"]
print("OK   retained notification waits for editor visibility before Enter")

with tempfile.TemporaryDirectory(prefix="notification-recovery.") as raw:
    recovery = Path(raw) / "submit-recovery.json"
    pending = SemanticPort([f"❯ {PROMPT.splitlines()[0]}"])
    assert recover_visible_notification(
        pending,
        surface_id=SURFACE,
        workspace_id="22222222-2222-2222-2222-222222222222",
        runtime="claude",
        message=PROMPT,
        receipt_path=recovery,
        identity={"operation_id": "notification-op"},
    )
    assert pending.keys == ["Enter"]
    assert recover_visible_notification(
        pending,
        surface_id=SURFACE,
        workspace_id="22222222-2222-2222-2222-222222222222",
        runtime="claude",
        message=PROMPT,
        receipt_path=recovery,
        identity={"operation_id": "notification-op"},
    )
    assert pending.keys == ["Enter"]
print("OK   visible sent notification recovery submits exactly once")

with tempfile.TemporaryDirectory(prefix="notification-historical-claude.") as raw:
    historical = Path(raw) / "submit-recovery.json"
    completed = SemanticPort(
        [
            f"❯ {PROMPT.splitlines()[0]}\n"
            "Assistant: completed.\n"
            "❯ unrelated current draft"
        ]
    )
    assert not recover_visible_notification(
        completed,
        surface_id=SURFACE,
        workspace_id="22222222-2222-2222-2222-222222222222",
        runtime="claude",
        message=PROMPT,
        receipt_path=historical,
        identity={"operation_id": "historical-claude"},
    )
    assert completed.keys == [] and not historical.exists()
print("OK   Claude transcript anchor cannot submit an unrelated current draft")


class CodexSemanticPort(FakePort):
    def agent_status(self, workspace_id: str, runtime: str) -> str:
        assert workspace_id == "22222222-2222-2222-2222-222222222222"
        assert runtime == "codex"
        return "idle"


class ClaudeNeedsInputPort(SemanticPort):
    def agent_status(self, workspace_id: str, runtime: str) -> str:
        super().agent_status(workspace_id, runtime)
        return "needs-input"


class CodexNeedsInputPort(CodexSemanticPort):
    def agent_status(self, workspace_id: str, runtime: str) -> str:
        super().agent_status(workspace_id, runtime)
        return "needs-input"


with tempfile.TemporaryDirectory(prefix="notification-coalesced-editor.") as raw:
    recovery = Path(raw) / "submit-recovery.json"
    coalesced = ClaudeNeedsInputPort(
        [
            f"{'─' * 120}❯ {PROMPT.splitlines()[0]}"
            "────────────────────────────────────────\n"
            "  ╭─ task branch · Sonnet · effort medium\n"
            "  ├─ CTX  8%"
        ]
    )
    assert recover_visible_notification(
        coalesced,
        surface_id=SURFACE,
        workspace_id="22222222-2222-2222-2222-222222222222",
        runtime="claude",
        message=PROMPT,
        receipt_path=recovery,
        identity={"operation_id": "coalesced-editor"},
    )
    assert coalesced.keys == ["Enter"]
print("OK   coalesced Claude separator retains current editor recovery")


with tempfile.TemporaryDirectory(prefix="notification-coalesced-codex.") as raw:
    recovery = Path(raw) / "submit-recovery.json"
    coalesced = CodexNeedsInputPort(
        [f"{'─' * 120}› {PROMPT.splitlines()[0]}"]
    )
    assert not recover_visible_notification(
        coalesced,
        surface_id=SURFACE,
        workspace_id="22222222-2222-2222-2222-222222222222",
        runtime="codex",
        message=PROMPT,
        receipt_path=recovery,
        identity={"operation_id": "coalesced-codex"},
    )
    assert coalesced.keys == [] and not recovery.exists()
print("OK   Claude separator recovery grants no Codex submission authority")


dialog_cases = (
    (
        "claude-recognized",
        "claude",
        ClaudeNeedsInputPort,
        f"❯ {PROMPT.splitlines()[0]}\n"
        "Accessing workspace: /tmp/review\n"
        "Quick safety check: Is this a project you created or one you trust?\n"
        "1. Yes, I trust this\n"
        "2. No, exit\n"
        "Enter to confirm",
    ),
    (
        "claude-unknown",
        "claude",
        ClaudeNeedsInputPort,
        f"❯ {PROMPT.splitlines()[0]}\n1. Allow\n2. Deny\nEnter",
    ),
    (
        "codex-recognized",
        "codex",
        CodexNeedsInputPort,
        f"› {PROMPT.splitlines()[0]}\n"
        "Do you trust the contents of this directory?\n"
        "1. Yes, continue\n"
        "2. No, quit\n"
        "Press enter to continue",
    ),
    (
        "codex-unknown",
        "codex",
        CodexNeedsInputPort,
        f"› {PROMPT.splitlines()[0]}\n1. Allow\n2. Deny\nPress enter",
    ),
)
for case_name, runtime, port_type, screen in dialog_cases:
    with tempfile.TemporaryDirectory(prefix=f"notification-{case_name}.") as raw:
        recovery = Path(raw) / "submit-recovery.json"
        dialog = port_type([screen])
        assert not recover_visible_notification(
            dialog,
            surface_id=SURFACE,
            workspace_id="22222222-2222-2222-2222-222222222222",
            runtime=runtime,
            message=PROMPT,
            receipt_path=recovery,
            identity={"operation_id": case_name},
        )
        assert dialog.keys == [] and not recovery.exists()
print("OK   provider dialogs cannot inherit recovery authority from transcript")


with tempfile.TemporaryDirectory(prefix="notification-historical-codex.") as raw:
    historical = Path(raw) / "submit-recovery.json"
    completed = CodexSemanticPort(
        [
            f"› {PROMPT.splitlines()[0]}\n"
            "• Completed the requested work.\n"
            "›"
        ]
    )
    assert not recover_visible_notification(
        completed,
        surface_id=SURFACE,
        workspace_id="22222222-2222-2222-2222-222222222222",
        runtime="codex",
        message=PROMPT,
        receipt_path=historical,
        identity={"operation_id": "historical-codex"},
    )
    assert completed.keys == [] and not historical.exists()
print("OK   Codex transcript anchor cannot submit an empty current composer")


stale_direct = CodexSemanticPort(
    ["› [Pasted Content 7 chars]", "› [Pasted Content 7 chars]"]
)
try:
    send_visible_notification(
        stale_direct,
        surface_id=SURFACE,
        runtime="codex",
        message="This is a different exact notification",
        observation_limit=1,
        wait=lambda _seconds: None,
    )
except RetainedNotificationError:
    pass
else:
    raise AssertionError("direct stale Codex placeholder authorized Enter")
assert stale_direct.keys == []
print("OK   direct delivery rejects a stale Codex placeholder")


with tempfile.TemporaryDirectory(prefix="notification-stale-placeholder.") as raw:
    notify = Path(raw) / "notify.json"
    stale = CodexSemanticPort(
        ["› [Pasted Content 7 chars]", "› [Pasted Content 7 chars]"]
    )
    try:
        deliver_worker_notification(
            FakeWorker(stale),
            notify_path=notify,
            marker={"schema_version": 1, "operation_id": "stale"},
            message="This is a different exact notification",
        )
    except RetainedNotificationError:
        pass
    else:
        raise AssertionError("stale Codex placeholder authorized Enter")
    assert stale.sent == ["This is a different exact notification"]
    assert stale.keys == [] and not notify.exists()
print("OK   stale Codex placeholder cannot prove the current notification")


with tempfile.TemporaryDirectory(prefix="notification-delayed-paste.") as raw:
    notify = Path(raw) / "notify.json"
    port = CodexSemanticPort(["› old", "› old"])
    worker = FakeWorker(port)
    marker = {"schema_version": 1, "operation_id": "delayed"}
    try:
        deliver_worker_notification(
            worker,
            notify_path=notify,
            marker=marker,
            message=PROMPT,
        )
    except RetainedNotificationError:
        pass
    else:
        raise AssertionError("late paste unexpectedly completed immediately")
    assert port.sent == [PROMPT] and port.keys == []
    port.screens = ["› " + PROMPT]
    deliver_worker_notification(
        worker,
        notify_path=notify,
        marker=marker,
        message=PROMPT,
    )
    assert port.sent == [PROMPT] and port.keys == ["Enter"] and notify.is_file()
print("OK   delayed paste resumes without a second paste")


class CrashAfterPastePort(CodexSemanticPort):
    def send(self, surface_id: str, text: str) -> None:
        super().send(surface_id, text)
        raise RuntimeError("crash after paste")


with tempfile.TemporaryDirectory(prefix="notification-paste-crash.") as raw:
    notify = Path(raw) / "notify.json"
    port = CrashAfterPastePort(["› old"])
    worker = FakeWorker(port)
    marker = {"schema_version": 1, "operation_id": "paste-crash"}
    try:
        deliver_worker_notification(
            worker, notify_path=notify, marker=marker, message=PROMPT
        )
    except RuntimeError as exc:
        assert str(exc) == "crash after paste"
    else:
        raise AssertionError("paste crash did not interrupt delivery")
    try:
        deliver_worker_notification(
            worker, notify_path=notify, marker=marker, message=PROMPT
        )
    except RetainedNotificationError:
        pass
    else:
        raise AssertionError("uncertain paste was replayed")
    assert port.sent == [PROMPT] and port.keys == [] and not notify.exists()
print("OK   paste crash stays fail-closed without a second paste")


class CrashAfterNotificationEnterPort(CodexSemanticPort):
    def send_key(self, surface_id: str, key: str) -> None:
        super().send_key(surface_id, key)
        raise RuntimeError("crash after notification Enter")


with tempfile.TemporaryDirectory(prefix="notification-enter-crash.") as raw:
    notify = Path(raw) / "notify.json"
    port = CrashAfterNotificationEnterPort(
        ["› old", "› " + PROMPT]
    )
    worker = FakeWorker(port)
    marker = {"schema_version": 1, "operation_id": "enter-crash"}
    try:
        deliver_worker_notification(
            worker, notify_path=notify, marker=marker, message=PROMPT
        )
    except RuntimeError as exc:
        assert str(exc) == "crash after notification Enter"
    else:
        raise AssertionError("Enter crash did not interrupt delivery")
    try:
        deliver_worker_notification(
            worker, notify_path=notify, marker=marker, message=PROMPT
        )
    except RetainedNotificationError:
        pass
    else:
        raise AssertionError("uncertain Enter was replayed")
    assert port.sent == [PROMPT] and port.keys == ["Enter"] and not notify.exists()
print("OK   Enter crash stays fail-closed without a second key")


with tempfile.TemporaryDirectory(prefix="notification-concurrent-recovery.") as raw:
    recovery = Path(raw) / "submit-recovery.json"
    port = SemanticPort([f"❯ {PROMPT.splitlines()[0]}"] * 2)
    start = threading.Barrier(2)

    def concurrent_recovery() -> bool:
        start.wait()
        return recover_visible_notification(
            port,
            surface_id=SURFACE,
            workspace_id="22222222-2222-2222-2222-222222222222",
            runtime="claude",
            message=PROMPT,
            receipt_path=recovery,
            identity={"operation_id": "concurrent-op"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: concurrent_recovery(), range(2)))
    assert results == [True, True]
    assert port.keys == ["Enter"]
    assert json.loads(recovery.read_text(encoding="utf-8"))["status"] == "accepted"
print("OK   concurrent recovery linearizes one Enter")


with tempfile.TemporaryDirectory(prefix="notification-process-recovery.") as raw:
    recovery = Path(raw) / "submit-recovery.json"
    key_log = Path(raw) / "keys.log"
    context = multiprocessing.get_context("fork")
    start = context.Barrier(2)

    def process_recovery() -> None:
        class ProcessPort:
            def agent_status(self, workspace_id: str, runtime: str) -> str:
                return "idle"

            def read(self, surface_id: str) -> str:
                return f"❯ {PROMPT.splitlines()[0]}"

            def send_key(self, surface_id: str, key: str) -> None:
                descriptor = os.open(key_log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                try:
                    os.write(descriptor, b"Enter\n")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

        start.wait()
        assert recover_visible_notification(
            ProcessPort(),
            surface_id=SURFACE,
            workspace_id="22222222-2222-2222-2222-222222222222",
            runtime="claude",
            message=PROMPT,
            receipt_path=recovery,
            identity={"operation_id": "process-op"},
        )

    processes = [context.Process(target=process_recovery) for _index in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert key_log.read_text(encoding="utf-8").splitlines() == ["Enter"]
print("OK   cross-process recovery linearizes one Enter")


result, port, retries, stages = run_case(
    [
        "› " + PROMPT,
        "• Working (1s)",
    ]
)
assert result.acknowledged and result.evidence == "provider-activity"
assert port.sent == [PROMPT] and port.keys == ["Enter"] and not retries
assert stages == [
    ("paste-reserved", 0),
    ("transport-accepted", 0),
    ("submit-reserved", 1),
    ("submit-accepted", 1),
]
print("OK   paste visibility precedes first Enter and activity acknowledges")

# Distinct pointers/hashes may share the entire historical 96-character anchor.
from harness.runtime_provider_input import interactive_provider_input
expected_pointer = interactive_provider_input(
    "codex", Path("/tmp/" + "shared-directory/" * 8 + "expected.md"), "Expected contract",
)
wrong_path = interactive_provider_input(
    "codex", Path("/tmp/" + "shared-directory/" * 8 + "unrelated.md"), "Unrelated contract",
)
wrong_hash = interactive_provider_input(
    "codex", Path("/tmp/" + "shared-directory/" * 8 + "expected.md"), "Other contract bytes",
)
for runtime, marker, active in (
    ("codex", "›", "• Working (1s)"),
    ("claude", "❯", "✻ Working…(1s · ↓10 tokens)"),
):
    for draft in (
        f"{marker} {wrong_path}", f"{marker} {wrong_hash}",
        f"{marker} {expected_pointer.replace('expected.md', 'expected .md')}",
        f"{marker} {expected_pointer}\nAssistant: historical response\n{marker} unrelated current draft",
        f"{marker} [Pasted Content 3675 chars]",
    ):
        port = FakePort([f"{marker} previous editor\nold footer",
                         f"{marker} previous editor\nnew footer", draft, active])
        result = deliver_continuation(
            port, surface_id=SURFACE, prompt=expected_pointer, runtime=runtime,
            artifact_ready=lambda: False, ownership_ready=lambda: True,
            reserve_retry=lambda: False, observe_stage=lambda *_args: None,
            observation_limit=3, wait=lambda _seconds: None,
        )
        assert not result.acknowledged and result.submit_count == 0
        assert port.sent == [expected_pointer] and not port.keys
    # A soft wrap inside the distinguishing path/hash must not erase identity.
    wrapped = f"{marker} {expected_pointer[:120]}\n  {expected_pointer[120:]}\n\n? for shortcuts"
    port = FakePort([f"{marker} previous editor", f"{marker} previous editor", wrapped, active])
    result = deliver_continuation(
        port, surface_id=SURFACE, prompt=expected_pointer, runtime=runtime,
        artifact_ready=lambda: False, ownership_ready=lambda: True,
        reserve_retry=lambda: False, observe_stage=lambda *_args: None,
        observation_limit=3, wait=lambda _seconds: None,
    )
    assert result.acknowledged and result.submit_count == 1
    assert port.sent == [expected_pointer] and port.keys == ["Enter"]
print("OK   complete current composer binds path and hash across lag and wrapping; prefix, history and placeholder cannot submit")

# cmux send can return before Codex repaints the unchanged idle editor.
for runtime, marker in (("codex", "›"), ("claude", "❯")):
    baseline = f"{marker} previous editor\nold footer"
    result, port, retries, _stages = run_case(
        [f"{marker} previous editor\nnew footer", f"{marker} {PROMPT}",
         "• Working (1s)" if runtime == "codex" else "✻ Working…(1s · ↓10 tokens)"],
        pre_screen=baseline, runtime=runtime,
    )
    assert result.acknowledged and result.evidence == "provider-activity"
    assert result.submit_count == 1 and port.sent == [PROMPT]
    assert port.keys == ["Enter"] and not retries
print("OK   delayed paste repaint waits for the exact changed editor without resending")

result, port, retries, _stages = run_case(["›", "›"])
assert not result.acknowledged and result.evidence == "paste-unconfirmed"
assert result.submit_count == 0 and port.sent == [PROMPT] and not port.keys and not retries
print("OK   unchanged idle exhaustion never submits or acknowledges the continuation")

for blocker in ("› unrelated changed draft", "1. Allow\n2. Deny\nEnter"):
    result, port, retries, _stages = run_case([blocker, "› " + PROMPT])
    assert not result.acknowledged and result.evidence in {"idle", "unknown"}
    assert result.submit_count == 0 and not port.keys and not retries
print("OK   changed unrelated editor and interactive blockers are not paste lag")

result, port, retries, _stages = run_case(
    ["›", "› " + PROMPT], ownership=[True, True, False],
)
assert not result.acknowledged and result.evidence == "ownership-lost"
assert result.submit_count == 0 and not port.keys and not retries
print("OK   ownership loss during delayed paste remains fail-closed")

pre_key_port = FakePort([
    "› previous editor",
    "› " + PROMPT,
])
pre_key_stage: dict[str, object] = {}


def crash_after_submit_reservation(
    stage: str,
    count: int,
    pre_screen_sha256: str,
    pre_editor_sha256: str,
    paste_sha256: str,
) -> None:
    if stage == "submit-reserved":
        pre_key_stage.update(
            stage=stage,
            count=count,
            pre_screen=pre_screen_sha256,
            pre_editor=pre_editor_sha256,
            paste=paste_sha256,
        )
        raise RuntimeError("kill point before Enter")


try:
    deliver_continuation(
        pre_key_port,
        surface_id=SURFACE,
        prompt=PROMPT,
        runtime="codex",
        artifact_ready=lambda: False,
        ownership_ready=lambda: True,
        reserve_retry=lambda: False,
        observe_stage=crash_after_submit_reservation,
        wait=lambda _seconds: None,
    )
except RuntimeError as exc:
    assert str(exc) == "kill point before Enter"
else:
    raise AssertionError("pre-Enter kill point did not interrupt continuation")
assert pre_key_stage["count"] == 1 and pre_key_port.keys == []
pre_key_port.screens = ["› " + PROMPT]
pre_key_replay = deliver_continuation(
    pre_key_port,
    surface_id=SURFACE,
    prompt=PROMPT,
    runtime="codex",
    artifact_ready=lambda: False,
    ownership_ready=lambda: True,
    reserve_retry=lambda: False,
    observe_stage=lambda *_args: None,
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=int(pre_key_stage["count"]),
    paste_screen_sha256=str(pre_key_stage["paste"]),
    wait=lambda _seconds: None,
)
assert not pre_key_replay.acknowledged
assert pre_key_replay.evidence == "submit-effect-uncertain"
assert pre_key_port.keys == []
print("OK   pre-Enter reservation replay fails closed without an unbudgeted key")


class CrashAfterEnterPort(FakePort):
    def send_key(self, surface_id: str, key: str) -> None:
        super().send_key(surface_id, key)
        raise RuntimeError("kill point after Enter before receipt")


post_key_port = CrashAfterEnterPort([
    "› previous editor",
    "› " + PROMPT,
])
post_key_stage: dict[str, object] = {}


def remember_post_key_reservation(
    stage: str,
    count: int,
    pre_screen_sha256: str,
    pre_editor_sha256: str,
    paste_sha256: str,
) -> None:
    if stage == "submit-reserved":
        post_key_stage.update(
            count=count,
            pre_screen=pre_screen_sha256,
            pre_editor=pre_editor_sha256,
            paste=paste_sha256,
        )


try:
    deliver_continuation(
        post_key_port,
        surface_id=SURFACE,
        prompt=PROMPT,
        runtime="codex",
        artifact_ready=lambda: False,
        ownership_ready=lambda: True,
        reserve_retry=lambda: False,
        observe_stage=remember_post_key_reservation,
        wait=lambda _seconds: None,
    )
except RuntimeError as exc:
    assert str(exc) == "kill point after Enter before receipt"
else:
    raise AssertionError("post-Enter kill point did not interrupt continuation")
assert post_key_port.keys == ["Enter"] and post_key_stage["count"] == 1
post_key_port.screens = ["• Working (recovered exact continuation)"]
post_key_replay = deliver_continuation(
    post_key_port,
    surface_id=SURFACE,
    prompt=PROMPT,
    runtime="codex",
    artifact_ready=lambda: False,
    ownership_ready=lambda: True,
    reserve_retry=lambda: False,
    observe_stage=lambda *_args: None,
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=int(post_key_stage["count"]),
    paste_screen_sha256=str(post_key_stage["paste"]),
    wait=lambda _seconds: None,
)
assert post_key_replay.acknowledged
assert post_key_replay.evidence == "provider-activity"
assert post_key_port.keys == ["Enter"]
print("OK   post-Enter crash replay observes activity without a second key")

crash_port = FakePort(["› previous editor"])
reserved: dict[str, str] = {}


def crash_after_paste(
    stage: str,
    _count: int,
    pre_send_screen_sha256: str,
    pre_send_editor_sha256: str,
    _paste_screen_sha256: str,
) -> None:
    reserved["stage"] = stage
    reserved["screen"] = pre_send_screen_sha256
    reserved["editor"] = pre_send_editor_sha256
    if stage == "transport-accepted":
        raise RuntimeError("kill point after prompt transport")


try:
    deliver_continuation(
        crash_port,
        surface_id=SURFACE,
        prompt=PROMPT,
        runtime="codex",
        artifact_ready=lambda: False,
        ownership_ready=lambda: True,
        reserve_retry=lambda: False,
        observe_stage=crash_after_paste,
        wait=lambda _seconds: None,
    )
except RuntimeError as exc:
    assert str(exc) == "kill point after prompt transport"
else:
    raise AssertionError("kill point did not interrupt continuation")
assert crash_port.sent == [PROMPT] and reserved["stage"] == "transport-accepted"
crash_port.screens = [
    "› " + PROMPT,
    "• Working (recovered turn)",
]
replayed = deliver_continuation(
    crash_port,
    surface_id=SURFACE,
    prompt=PROMPT,
    runtime="codex",
    artifact_ready=lambda: False,
    ownership_ready=lambda: True,
    reserve_retry=lambda: False,
    observe_stage=lambda *_args: None,
    send_prompt=False,
    pre_send_screen_sha256=reserved["screen"],
    pre_send_editor_sha256=reserved["editor"],
    observation_limit=2,
    wait=lambda _seconds: None,
)
assert replayed.acknowledged and replayed.evidence == "provider-activity"
assert crash_port.sent == [PROMPT] and crash_port.keys == ["Enter"]
print("OK   crash after prompt transport replays without a second paste")

result, port, retries, _stages = run_case(
    [
        "› [Pasted Content 3675 chars]",
        "• Working (1s)",
    ]
)
assert not result.acknowledged and result.evidence == "prompt-mismatch"
assert result.submit_count == 0 and port.sent == [PROMPT] and not port.keys and not retries
print("OK   generic pasted-content placeholder cannot identify the intended continuation")

result, port, retries, _stages = run_case(
    [
        "› " + PROMPT,
        "› " + PROMPT,
        "› " + PROMPT,
        "• Working (2s)",
    ]
)
assert result.acknowledged and result.submit_count == 2
assert port.sent == [PROMPT] and port.keys == ["Enter", "Enter"]
assert retries == [True]
print("OK   one identity-bound Enter retry never repeats the prompt")

result, port, retries, _stages = run_case(
    [
        "› " + PROMPT,
        "› " + PROMPT,
        "› " + PROMPT,
    ],
    retry=False,
)
assert not result.acknowledged
assert result.evidence == "submit-retry-budget-unavailable"
assert port.sent == [PROMPT] and port.keys == ["Enter"] and retries == [False]
print("OK   exhausted shared nudge budget fails closed without duplicate input")

result, port, retries, _stages = run_case(
    ["› " + PROMPT],
    artifacts=[False, False, True],
)
assert result.acknowledged and result.evidence == "artifact"
assert port.sent == [PROMPT] and port.keys == ["Enter"] and not retries
print("OK   callback artifact wins the delivery race")

transport_baseline = "› previous editor"
result, port, retries, _stages = run_case(
    ["› " + PROMPT, "• Working"],
    send_prompt=False,
    pre_send_screen_sha256=_screen_digest(transport_baseline),
    pre_send_editor_sha256=_editor_digest("codex", transport_baseline),
)
assert result.acknowledged and port.sent == [] and port.keys == ["Enter"]
print("OK   transport replay submits only after a baseline-bound editor change")

stale_editor = "› " + PROMPT
result, port, retries, _stages = run_case(
    [stale_editor, "• Working (stale previous turn)"],
    send_prompt=False,
    pre_send_screen_sha256=_screen_digest(stale_editor),
    pre_send_editor_sha256=_editor_digest("codex", stale_editor),
)
assert not result.acknowledged and result.evidence == "paste-unconfirmed"
assert port.sent == [] and port.keys == [] and not retries
print("OK   transport replay cannot submit a stale same-heading editor")

result, port, retries, stages = run_case(
    [
        "• Working (stale previous turn)",
        "› " + PROMPT,
        "• Working (current turn)",
    ]
)
assert result.acknowledged and result.submit_count == 1
assert port.sent == [PROMPT] and port.keys == ["Enter"] and not retries
assert stages == [
    ("paste-reserved", 0),
    ("transport-accepted", 0),
    ("submit-reserved", 1),
    ("submit-accepted", 1),
]
print("OK   stale pre-Enter activity cannot acknowledge the new continuation")

result, port, retries, _stages = run_case(
    ["• Working (stale previous turn)", "• Working (still stale)"],
)
assert not result.acknowledged and result.evidence == "paste-unconfirmed"
assert port.sent == [PROMPT] and port.keys == [] and not retries
print("OK   stale activity without current input visibility fails closed")

result, port, retries, _stages = run_case(
    [
        "› " + PROMPT,
        "• Working (stale previous turn)",
    ],
    pre_screen="› " + PROMPT,
)
assert not result.acknowledged and result.evidence == "paste-unconfirmed"
assert port.sent == [PROMPT] and port.keys == [] and not retries
print("OK   stale same-heading editor cannot identify the current paste")

result, port, retries, _stages = run_case(
    ["• Working (submitted continuation)"],
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=1,
    paste_screen_sha256=_screen_digest("› " + PROMPT),
)
assert result.acknowledged and result.submit_count == 1
assert port.sent == [] and port.keys == [] and not retries
print("OK   durable prior submit may acknowledge activity without another Enter")

result, port, retries, _stages = run_case(
    ["› " + PROMPT],
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=1,
    paste_screen_sha256=_screen_digest("› " + PROMPT),
)
assert not result.acknowledged and result.evidence == "submit-effect-uncertain"
assert port.sent == [] and port.keys == [] and not retries
print("OK   accepted submit with visible editor fails closed without replay")

stale_activity = "• Working (stale previous turn)"
result, port, retries, _stages = run_case(
    [stale_activity],
    send_prompt=False,
    submit_already_accepted=True,
    accepted_submit_count=1,
    paste_screen_sha256=_screen_digest(stale_activity),
)
assert not result.acknowledged and result.evidence == "submit-effect-uncertain"
assert port.sent == [] and port.keys == [] and not retries
print("OK   submit replay rejects activity identical to its durable baseline")

result, port, retries, _stages = run_case(
    [
        "› " + PROMPT,
        "› " + PROMPT + "\n• Working (2s)",
    ]
)
assert result.acknowledged and result.evidence == "provider-activity"
assert port.keys == ["Enter"] and not retries
print("OK   visible transcript anchor does not hide exact provider activity")

result, port, retries, _stages = run_case(
    ["› " + PROMPT, "›"]
)
assert not result.acknowledged and result.evidence == "idle"
assert port.keys == ["Enter"] and not retries
print("OK   idle repaint cannot acknowledge a continuation")

result, port, retries, _stages = run_case(
    ["› " + PROMPT, "", ""]
)
assert not result.acknowledged and result.evidence == "submit-unconfirmed"
assert result.submit_count == 1
assert port.keys == ["Enter"] and not retries
print("OK   missing screen after Enter fails closed without retry")

result, port, retries, _stages = run_case(
    ["› " + PROMPT, "› " + PROMPT, ""]
)
assert not result.acknowledged and result.evidence == "submit-unconfirmed"
assert result.submit_count == 1
assert port.keys == ["Enter"] and not retries
print("OK   later missing screen also blocks the Enter retry")

result, port, retries, _stages = run_case(
    ["› " + PROMPT, "1. Allow\n2. Deny\nEnter"]
)
assert not result.acknowledged and result.evidence == "unknown"
assert port.keys == ["Enter"] and not retries
print("OK   unknown interactive screen fails closed")

result, port, retries, _stages = run_case(
    ["› " + PROMPT],
    ownership=[True, True, False],
)
assert not result.acknowledged and result.evidence == "ownership-lost"
assert port.keys == [] and not retries
print("OK   ownership is rechecked before Enter")

result, port, retries, _stages = run_case(
    [
        "❯ " + PROMPT,
        "❯ " + PROMPT + "\n✻ Working…(1s · ↓10 tokens)",
    ],
    runtime="claude",
)
assert result.acknowledged and result.evidence == "provider-activity"
assert port.keys == ["Enter"] and not retries
print("OK   Claude activity is classified without dropping the prompt anchor")

print("Continuation delivery matrix passed.")
