"""Bounded English terminal rendering of the read-only harness dashboard.

Rendering is deliberately separate from projection: the view may shorten and
drop, but it may never decide what is true.  Every line it prints is already a
projected fact, so a narrower terminal can lose detail without ever turning an
unknown state into a confident one.
"""

from __future__ import annotations

import re

from .dashboard_projection import (
    ACTIVE,
    ATTENTION,
    WAITING,
    ChildView,
    DashboardProjection,
    ProgramView,
    RouteView,
)
from .state_machine import TERMINAL

MAX_LINE = 96
SHORT_ID = 8
# A nested operation is identified by its own id, so it keeps more of it than a
# lane label.  Real ids share a long leading UUID and differ only in a derived
# suffix, so a leading slice renders a review parent and its round identically:
# what distinguishes them has to survive, which means eliding the middle.
CHILD_ID = 28
CHILD_ID_HEAD = 8
STEP_MARKERS = {
    "complete": "[x]",
    "running": "[>]",
    "pending": "[ ]",
    "attention": "[!]",
    "stopped": "[-]",
    "unknown": "[?]",
}
NEXT_ACTION_TEXT = {
    "start": "start the next step",
    "wait": "wait for the running step",
    "attention": "resolve attention on the current step",
    "reap-ready": "ready to reap",
    "unknown": "cannot be derived from durable evidence",
    "none": "no compiled pipeline is bound to this operation",
}
RESET = "\x1b[0m"
SEMANTIC_COLORS = {
    "complete": "\x1b[32m",
    "running": "\x1b[36m",
    "waiting": "\x1b[33m",
    "retry": "\x1b[38;5;208m",
    "attention": "\x1b[31m",
    "model": "\x1b[35m",
}
MODEL_TOKEN = re.compile(r"(?<=/)[A-Za-z0-9][A-Za-z0-9._-]*(?=/)")


def _short(value: str) -> str:
    return value[:SHORT_ID] if len(value) > SHORT_ID else value


def _identity(value: str) -> str:
    """Shorten one operation id while keeping the part that identifies it."""

    if len(value) <= CHILD_ID:
        return value
    tail = CHILD_ID - CHILD_ID_HEAD - 1
    return f"{value[:CHILD_ID_HEAD]}...{value[-tail:]}"


def _clip(value: str) -> str:
    return value if len(value) <= MAX_LINE else value[: MAX_LINE - 3] + "..."


def _route(route: RouteView) -> str:
    return (
        f"{route.runtime}/{route.model}/{route.effort}  preset {route.preset}"
    )


def _child_lines(children: tuple[ChildView, ...], indent: int) -> list[str]:
    """Render one nested operation subtree under the step that runs it."""

    pad = " " * indent
    lines: list[str] = []
    for child in children:
        marker = STEP_MARKERS.get(child.status, STEP_MARKERS["unknown"])
        identity = _identity(child.operation_id)
        lines.append(
            f"{pad}{marker} {identity:<{CHILD_ID}} {child.kind:<22} "
            f"{child.state}"
        )
        lines.append(f"{pad}    route {_route(child.route)}")
        lines.extend(_child_lines(child.children, indent + 4))
    return lines


def _step_lines(program: ProgramView) -> list[str]:
    if not program.steps:
        return ["  steps    none projected"]
    lines = ["  steps"]
    for step in program.steps:
        marker = STEP_MARKERS.get(step.status, STEP_MARKERS["unknown"])
        lines.append(
            f"    {marker} {step.step_id:<16} {step.primitive:<18} "
            f"{step.session_mode:<13} visits {step.visits}"
        )
        lines.append(f"        route {_route(step.route)}")
        lines.extend(_child_lines(step.children, 8))
    return lines


def _lane_lines(program: ProgramView) -> list[str]:
    if not program.lanes:
        return ["  lanes    none projected"]
    lines = ["  lanes"]
    for lane in program.lanes:
        if lane.scope == "operation":
            label, members = _short(lane.lane_id), f"{len(lane.members)} operation(s)"
        else:
            label, members = lane.lane_id, "review axis"
        lines.append(
            f"    {lane.scope:<11} {label:<18} {lane.status:<9} {members}"
        )
    if len(program.lanes) > 1:
        active = sum(1 for lane in program.lanes if lane.status == "active")
        lines.append(
            f"    parallel lanes: {len(program.lanes)} ({active} active)"
        )
    return lines


def _program_lines(program: ProgramView) -> list[str]:
    header = (
        f"Program {program.operation_id}  {program.kind}  "
        f"{program.state}  rev {program.revision}"
    )
    controls = ", ".join(program.controls) or "none"
    loop = (
        f"pass {program.loop_passes} of {program.loop_limit}"
        if program.loop_limit
        else "not applicable"
    )
    lines = [
        header,
        f"  pipeline {program.pipeline}  definition {_short(program.definition_sha256) or 'none'}",
        f"  controls {controls}",
        f"  loop     {loop}",
        f"  next     {NEXT_ACTION_TEXT.get(program.next_action, program.next_action)}",
        f"  surface  {program.surface}",
        f"  executor {_route(program.executor)}  {program.executor_status}",
    ]
    lines.extend(_step_lines(program))
    if program.children:
        lines.append("  children with no exact step lineage")
        lines.extend(_child_lines(program.children, 4))
    lines.extend(_lane_lines(program))
    if program.dropped_children or program.dropped_lanes:
        lines.append(
            "  truncated "
            f"children +{program.dropped_children}, lanes +{program.dropped_lanes}"
        )
    lines.append(f"  status   {program.classification}")
    return lines


def _issue_lines(projection: DashboardProjection) -> list[str]:
    dropped = int(projection.truncated.get("issues", 0))
    if not projection.issues:
        return ["Recent issues: none"]
    suffix = f" (+{dropped} more)" if dropped else ""
    lines = [f"Recent issues: {len(projection.issues)}{suffix}"]
    for issue in projection.issues:
        detail = issue.detail if issue.detail.isascii() else ""
        suffix = f"  {detail}" if detail else ""
        lines.append(
            f"  - {issue.code}  {_short(issue.operation_id)}  "
            f"{issue.classification}{suffix}"
        )
    return lines


def _colorize(line: str, *, color: bool) -> str:
    if not color:
        return line
    rendered = MODEL_TOKEN.sub(
        lambda match: f"{SEMANTIC_COLORS['model']}{match.group()}{RESET}",
        line,
    )
    tokens = (
        ("verification-receipt-failed", "retry"),
        ("fix-receipt-failed", "retry"),
        ("attention-required", "attention"),
        ("in-progress", "running"),
        ("awaiting-transition", "waiting"),
        ("reviewing", "waiting"),
        ("waiting", "waiting"),
        ("healthy", "complete"),
        ("[!]", "attention"),
        ("[>]", "running"),
        ("[x]", "complete"),
    )
    for token, role in tokens:
        rendered = rendered.replace(
            token, f"{SEMANTIC_COLORS[role]}{token}{RESET}"
        )
    return rendered


def _history_line(program: ProgramView) -> str:
    return (
        f"  {program.operation_id}  {program.kind}  {program.state}  "
        f"pipeline {program.pipeline}  rev {program.revision}"
    )


def _compact_program_line(program: ProgramView) -> str:
    return (
        f"  {program.operation_id}  {program.kind}  {program.state}  "
        f"pipeline {program.pipeline}  {program.classification}"
    )


def _presentation_priority(program: ProgramView) -> int:
    if program.classification == ACTIVE:
        return 0
    if program.classification == WAITING:
        return 1
    if program.classification == ATTENTION:
        return 2
    return 3


def _bounded_program_lines(
    programs: tuple[ProgramView, ...],
    budget: int,
) -> list[str]:
    """Fit live detail and compact attention summaries into one row budget."""

    if budget <= 0 or not programs:
        return []
    ordered = tuple(
        program
        for _index, program in sorted(
            enumerate(programs),
            key=lambda item: (_presentation_priority(item[1]), item[0]),
        )
    )
    minimum = [
        (
            [
                _program_lines(program)[0],
                f"  status   {program.classification}  details compacted",
            ]
            if _presentation_priority(program) < 2
            else [_compact_program_line(program)]
        )
        for program in ordered
    ]
    required = sum(len(group) for group in minimum)
    if required > budget:
        lines: list[str] = []
        hidden = 0
        for index, group in enumerate(minimum):
            remaining = len(minimum) - index - 1
            reserve = 1 if remaining else 0
            if len(lines) + len(group) + reserve > budget:
                hidden = len(minimum) - index
                break
            lines.extend(group)
        if hidden and len(lines) < budget:
            lines.append(f"Programs hidden by viewport: {hidden}")
        return lines[:budget]

    groups = [list(group) for group in minimum]
    remaining = budget - required
    for index, program in enumerate(ordered):
        if _presentation_priority(program) >= 2 or remaining <= 0:
            continue
        full = _program_lines(program) + [""]
        extra_needed = len(full) - len(groups[index])
        if extra_needed <= remaining:
            groups[index] = full
            remaining -= extra_needed
            continue
        visible = max(len(groups[index]), len(groups[index]) + remaining - 1)
        omitted = len(full) - visible
        groups[index] = full[:visible] + [
            f"  viewport details truncated +{omitted} lines"
        ]
        remaining = 0
    return [line for group in groups for line in group]


def render(
    projection: DashboardProjection,
    *,
    recent: int = 3,
    color: bool = False,
    rows: int | None = None,
) -> str:
    """Render one bounded read-only English dashboard for a terminal."""

    dropped = int(projection.truncated.get("programs", 0))
    suffix = f" (+{dropped} more)" if dropped else ""
    active = tuple(
        program for program in projection.programs if program.state not in TERMINAL
    )
    terminal = tuple(
        program for program in projection.programs if program.state in TERMINAL
    )[:recent]
    header = [
        f"Harness dashboard - owner {projection.owner_id}",
        f"Classification: {projection.classification}",
        f"cmux surface probe: {projection.surface_probe}",
        f"Programs: {len(projection.programs)}{suffix}",
        f"Active pipelines: {len(active)}",
        "",
    ]
    footer = [f"Terminal history: {len(terminal)}"]
    footer.extend(_history_line(program) for program in terminal)
    footer.append("")
    footer.extend(_issue_lines(projection))
    lines = list(header)
    if not projection.programs:
        lines.append("No program is bound to this owner.")
        lines.append("")
    elif rows is None:
        for program in active:
            lines.extend(_program_lines(program))
            lines.append("")
    else:
        lines.extend(
            _bounded_program_lines(active, max(rows - len(header) - len(footer), 0))
        )
    lines.extend(footer)
    if rows is not None and len(lines) > rows:
        omitted = len(lines) - rows + 1
        lines = lines[: max(rows - 1, 0)]
        if rows:
            lines.append(f"Viewport truncated +{omitted} lines")
    return "\n".join(_colorize(_clip(line), color=color) for line in lines) + "\n"
