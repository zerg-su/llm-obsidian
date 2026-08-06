#!/usr/bin/env python3
"""Darwin EPERM liveness fallback requires one exact running ownership pair."""

from __future__ import annotations

import errno
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.process import ProcessAdapter  # noqa: E402
from harness.adapters.process_identity import DarwinProcessSnapshot  # noqa: E402


PROVIDER_PID = 4101
SUPERVISOR_PID = 4102
PROVIDER_IDENTITY = "1" * 64
SUPERVISOR_IDENTITY = "2" * 64


def check(label: str, condition: bool, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"OK   {label}")


def snapshot(
    pid: int,
    *,
    status: int = 2,
    parent_pid: int | None = None,
    process_group: int | None = None,
) -> DarwinProcessSnapshot:
    return DarwinProcessSnapshot(
        pid=pid,
        parent_pid=(
            SUPERVISOR_PID
            if parent_pid is None and pid == PROVIDER_PID
            else (1 if parent_pid is None else parent_pid)
        ),
        process_group=pid if process_group is None else process_group,
        status=status,
        started_at=f"1700000000:{pid}",
    )


def identities(pid: int) -> tuple[int, str]:
    if pid == PROVIDER_PID:
        return PROVIDER_PID, PROVIDER_IDENTITY
    if pid == SUPERVISOR_PID:
        return SUPERVISOR_PID, SUPERVISOR_IDENTITY
    raise ProcessLookupError(pid)


def snapshots(pid: int) -> DarwinProcessSnapshot:
    return snapshot(pid)


def exact_statuses() -> tuple[str, str]:
    return ProcessAdapter.exact_statuses(
        PROVIDER_PID,
        PROVIDER_IDENTITY,
        SUPERVISOR_PID,
        SUPERVISOR_IDENTITY,
    )


def main() -> int:
    denied = PermissionError(errno.EPERM, "sandbox denied zero-signal probe")
    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch("harness.adapters.process_signals.os.killpg", side_effect=denied) as group,
        patch("harness.adapters.process_signals.os.kill", side_effect=denied) as pid,
        patch.object(ProcessAdapter, "_current_identity", side_effect=identities),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
            side_effect=snapshots,
        ),
    ):
        result = exact_statuses()
        check(
            "Darwin EPERM plus exact running ownership reports both live",
            result == ("alive", "alive"),
            result,
        )
        check(
            "Darwin fallback sends no signal",
            group.call_args_list == [((PROVIDER_PID, 0), {})]
            and pid.call_args_list == [((SUPERVISOR_PID, 0), {})],
            (group.call_args_list, pid.call_args_list),
        )

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch.object(
            ProcessAdapter,
            "_current_identity",
            side_effect=lambda pid: (
                (PROVIDER_PID, "9" * 64)
                if pid == PROVIDER_PID
                else (SUPERVISOR_PID, SUPERVISOR_IDENTITY)
            ),
        ),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
            side_effect=snapshots,
        ),
    ):
        check(
            "EPERM fallback rejects provider PID reuse",
            exact_statuses() == ("unknown", "unknown"),
        )

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch.object(ProcessAdapter, "_current_identity", side_effect=identities),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
            side_effect=lambda pid: (
                snapshot(pid, process_group=9999)
                if pid == PROVIDER_PID
                else snapshot(pid)
            ),
        ),
    ):
        check(
            "EPERM fallback rejects provider PGID reuse",
            exact_statuses() == ("unknown", "unknown"),
        )

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch.object(
            ProcessAdapter,
            "_current_identity",
            side_effect=lambda pid: (
                (PROVIDER_PID, PROVIDER_IDENTITY)
                if pid == PROVIDER_PID
                else (SUPERVISOR_PID, "8" * 64)
            ),
        ),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
            side_effect=snapshots,
        ),
    ):
        check(
            "EPERM fallback rejects supervisor PID reuse",
            exact_statuses() == ("unknown", "unknown"),
        )

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch.object(ProcessAdapter, "_current_identity", side_effect=identities),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
            side_effect=lambda pid: (
                snapshot(pid, parent_pid=9999)
                if pid == PROVIDER_PID
                else snapshot(pid)
            ),
        ),
    ):
        check(
            "EPERM fallback rejects a provider with the wrong parent",
            exact_statuses() == ("unknown", "unknown"),
        )

    for bad_status, label in ((4, "stopped"), (5, "zombie"), (0, "exited")):
        with (
            patch("harness.adapters.process.sys.platform", "darwin"),
            patch(
                "harness.adapters.process_signals.os.killpg",
                side_effect=PermissionError(errno.EPERM, "denied"),
            ),
            patch(
                "harness.adapters.process_signals.os.kill",
                side_effect=PermissionError(errno.EPERM, "denied"),
            ),
            patch.object(ProcessAdapter, "_current_identity", side_effect=identities),
            patch.object(
                ProcessAdapter,
                "_darwin_process_snapshot",
                side_effect=lambda pid, value=bad_status: snapshot(
                    pid, status=value if pid == PROVIDER_PID else 2
                ),
            ),
        ):
            check(
                f"EPERM fallback rejects an exact {label} process",
                exact_statuses() == ("unknown", "unknown"),
            )

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch.object(ProcessAdapter, "_current_identity", side_effect=identities),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
            side_effect=lambda pid: (
                snapshot(pid, status=5)
                if pid == SUPERVISOR_PID
                else snapshot(pid)
            ),
        ),
    ):
        check(
            "EPERM fallback rejects a zombie supervisor",
            exact_statuses() == ("unknown", "unknown"),
        )

    proof_calls: list[int] = []

    def unavailable_proof(pid: int) -> DarwinProcessSnapshot:
        proof_calls.append(pid)
        raise PermissionError(errno.EPERM, "libproc denied")

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch.object(ProcessAdapter, "_current_identity", side_effect=identities),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
            side_effect=unavailable_proof,
        ),
    ):
        check(
            "permission without libproc proof remains unknown",
            exact_statuses() == ("unknown", "unknown")
            and proof_calls == [PROVIDER_PID],
            proof_calls,
        )

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=ProcessLookupError(PROVIDER_PID),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=ProcessLookupError(SUPERVISOR_PID),
        ),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
        ) as proof,
    ):
        check(
            "ordinary absent processes remain dead without fallback",
            exact_statuses() == ("dead", "dead") and not proof.called,
        )

    with (
        patch("harness.adapters.process.sys.platform", "darwin"),
        patch("harness.adapters.process_signals.os.killpg", return_value=None),
        patch("harness.adapters.process_signals.os.kill", return_value=None),
        patch.object(ProcessAdapter, "_current_identity", side_effect=identities),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
        ) as proof,
    ):
        check(
            "ordinary successful zero-signal probes preserve existing behavior",
            exact_statuses() == ("alive", "alive") and not proof.called,
        )

    with (
        patch("harness.adapters.process.sys.platform", "linux"),
        patch(
            "harness.adapters.process_signals.os.killpg",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch(
            "harness.adapters.process_signals.os.kill",
            side_effect=PermissionError(errno.EPERM, "denied"),
        ),
        patch.object(
            ProcessAdapter,
            "_darwin_process_snapshot",
        ) as proof,
    ):
        check(
            "non-Darwin EPERM behavior remains unknown",
            exact_statuses() == ("unknown", "unknown") and not proof.called,
        )

    print("\nDarwin exact process status tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
