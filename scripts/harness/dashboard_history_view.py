"""Compact root-terminal rendering for durable correction history."""

from __future__ import annotations

from dataclasses import dataclass

from .dashboard_policy import LifecyclePhaseView, ProgramView, TimingView


@dataclass(frozen=True)
class HistoryRow:
    text: str
    emphasis: str
    priority: int


def _timing(timing: TimingView) -> str:
    if timing.mode == "unknown" or timing.seconds is None:
        return ""
    seconds = timing.seconds
    if seconds < 60:
        value = f"{seconds}s"
    elif seconds < 3_600:
        value = f"{seconds // 60}m {seconds % 60:02d}s"
    else:
        value = f"{seconds // 3_600}h {(seconds % 3_600) // 60:02d}m"
    return f"{timing.mode} {value}"


def _compact_timing(status: str, timing: TimingView, *, current: bool) -> str:
    known = _timing(timing)
    if known:
        return known
    if current:
        return "time unavailable"
    return "—" if status in {"complete", "stopped", "attention"} else ""


def _human(value: str) -> str:
    words = " ".join(value.replace("_", "-").replace("-", " ").split())
    return words[:1].upper() + words[1:] if words else "Unknown"


def _identity(value: str) -> str:
    if len(value) <= 28:
        return value
    return f"{value[:8]}...{value[-17:]}"


def _fit(prefix: str, name: str, suffix: str, width: int) -> str:
    available = max(width - len(prefix) - len(suffix), 1)
    if len(name) > available:
        name = name[: max(available - 1, 1)] + ("…" if available > 1 else "")
    return f"{prefix}{name}{suffix}"


def _phase_label(phase: LifecyclePhaseView) -> str:
    if phase.kind == "fix" and phase.status == "running":
        return "Fixing review findings"
    if phase.kind == "reverify" and phase.status == "running":
        return "Re-verifying"
    return {
        "review": f"Review {phase.cycle}",
        "fix": f"Fix {phase.cycle}",
        "reverify": f"Re-verify {phase.cycle}",
    }[phase.kind]


def history_rows(program: ProgramView, *, width: int) -> tuple[HistoryRow, ...]:
    """Render compact completed phases and every operation in the active phase."""

    base_steps = tuple(
        step
        for step in program.steps
        if step.primitive.split("@", 1)[0] != "review"
    )
    items: tuple[tuple[str, object], ...] = (
        *(("step", step) for step in base_steps),
        *(("phase", phase) for phase in program.history),
    )
    current_phase = next(
        (
            phase
            for phase in program.history
            if phase.status in {"running", "attention", "unknown"}
        ),
        None,
    )
    current_step = None
    if current_phase is None:
        current_step = next(
            (
                step
                for status in ("running", "attention", "unknown", "pending")
                for step in program.steps
                if step.status == status
            ),
            None,
        )
    rows: list[HistoryRow] = []
    for index, (kind, item) in enumerate(items):
        branch = "└─" if index == len(items) - 1 else "├─"
        marker = {
            "complete": "✓",
            "running": "●",
            "pending": "○",
            "attention": "!",
            "stopped": "!",
            "unknown": "!",
        }.get(item.status, "!")
        current = item is (current_phase if kind == "phase" else current_step)
        timing = _compact_timing(item.status, item.timing, current=current)
        label = _phase_label(item) if kind == "phase" else _human(item.step_id)
        suffix = f"  {item.status}" + (f"  {timing}" if timing else "")
        if kind == "phase" and item.kind == "review":
            findings = (
                "unknown" if item.review.findings is None else item.review.findings
            )
            material = (
                "unknown"
                if item.review.material_findings is None
                else item.review.material_findings
            )
            suffix += f"  findings {findings} · material {material}"
        rows.append(
            HistoryRow(
                _fit(f"  {branch} {marker} ", label, suffix, width),
                "primary" if current else "dim" if item.status == "pending" else "none",
                0 if current else 3,
            )
        )
        if kind == "step":
            continue
        pad = "     " if branch == "└─" else "  │  "
        details: list[str] = []
        if item.kind == "fix":
            details.append(
                f"Executor {_identity(item.operation_id)}  "
                f"{item.route.runtime}/{item.route.model} · {item.route.effort}  "
                f"{item.status}  {_compact_timing(item.status, item.timing, current=current)}"
            )
        else:
            role = "Reviewer" if item.kind == "review" else "Verifier"
            for child in item.children:
                child_timing = _compact_timing(
                    child.status,
                    child.timing,
                    current=current and child.status == "running",
                )
                details.append(
                    f"{role} {_identity(child.operation_id)}  "
                    f"{child.route.runtime}/{child.route.model} · "
                    f"{child.route.effort}  {child.state}  {child_timing}"
                )
        if current and not details and item.kind == "review":
            details.append(
                f"Reviewer {item.route.runtime}/{item.route.model} · "
                f"{item.route.effort}  {item.status}  "
                f"{_compact_timing(item.status, item.timing, current=True)}"
            )
        rows.extend(
            HistoryRow(
                f"{pad}  {detail}",
                "none",
                2 if current else 7,
            )
            for detail in details
        )
    return tuple(rows)
