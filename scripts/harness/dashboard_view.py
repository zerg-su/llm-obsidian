"""Bounded English terminal rendering of the read-only harness dashboard.

Rendering is deliberately separate from projection: the view may shorten and
drop, but it may never decide what is true.  Every line it prints is already a
projected fact, so a narrower terminal can lose detail without ever turning an
unknown state into a confident one.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .dashboard_projection import (
    ACTIVE,
    ATTENTION,
    WAITING,
    ChildView,
    DashboardProjection,
    ProgramView,
    RouteView,
)
from .dashboard_policy import ReviewSummaryView, TimingView
from .dashboard_history_view import history_rows
from .state_machine import TERMINAL
MAX_LINE = 120
ROOT_COLUMNS = 100
DIAGNOSTIC_COLUMNS = 96
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
    "primary": "\x1b[38;2;244;244;247m",
    "secondary": "\x1b[38;2;119;122;140m",
    "rule": "\x1b[38;2;68;71;88m",
    "complete": "\x1b[38;2;85;230;139m",
    "running": "\x1b[38;2;91;217;238m",
    "waiting": "\x1b[38;2;240;196;84m",
    "retry": "\x1b[38;2;255;156;74m",
    "attention": "\x1b[38;2;255;101;122m",
    "model": "\x1b[38;2;216;120;238m",
    "identity": "\x1b[38;2;112;168;255m",
}
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
# The typed semantic role one composed root row carries, so neither emphasis
# nor viewport priority has to be guessed back out of the rendered text.
PRIMARY_EMPHASIS = "primary"
DIM_EMPHASIS = "dim"
NO_EMPHASIS = "none"
ROOT_PRIORITY_IDENTITY = 0
ROOT_PRIORITY_HEADING = 1
ROOT_PRIORITY_ROUTE = 2
ROOT_PRIORITY_STEP = 3
ROOT_PRIORITY_ISSUE = 4
ROOT_PRIORITY_FRAME = 5
ROOT_PRIORITY_DETAIL = 6
ROOT_PRIORITY_HISTORY = 7
MODEL_TOKEN = re.compile(
    r"(?<=/)[A-Za-z0-9][A-Za-z0-9._-]*(?=(?:/| ·))"
)
RUNTIME_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(?:claude|codex)(?![A-Za-z0-9_-])")
IDENTITY_TOKEN = re.compile(
    r"(?:(?<=dispatch )|(?<=↳ ))[A-Za-z0-9][A-Za-z0-9._:-]*"
)
REVIEW_LABEL = re.compile(r"\bReviewer\b")
SEMANTIC_TOKENS = {
    "verification-receipt-failed": "retry",
    "fix-receipt-failed": "retry",
    "attention-required": "attention",
    "awaiting-transition": "waiting",
    "awaiting-callback": "waiting",
    "in-progress": "running",
    "reviewing": "waiting",
    "waiting": "waiting",
    "pending": "secondary",
    "running": "running",
    "complete": "complete",
    "elapsed": "running",
    "duration": "complete",
    "ACTIVE": "running",
    "COMPLETE": "complete",
    "WAITING": "waiting",
    "ATTENTION": "attention",
    "✓": "complete",
    "●": "running",
    "○": "secondary",
    "!": "attention",
    "↻": "retry",
    "healthy": "complete",
    "[!]": "attention",
    "[>]": "running",
    "[x]": "complete",
    "[-]": "attention",
    "[?]": "attention",
    "[ ]": "secondary",
}
SEMANTIC_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    + "|".join(
        re.escape(token)
        for token in sorted(SEMANTIC_TOKENS, key=len, reverse=True)
    )
    + r")(?![A-Za-z0-9_-])"
)


def _short(value: str) -> str:
    return value[:SHORT_ID] if len(value) > SHORT_ID else value


def _identity(value: str) -> str:
    """Shorten one operation id while keeping the part that identifies it."""

    if len(value) <= CHILD_ID:
        return value
    tail = CHILD_ID - CHILD_ID_HEAD - 1
    return f"{value[:CHILD_ID_HEAD]}...{value[-tail:]}"


def _clip(value: str, width: int = DIAGNOSTIC_COLUMNS) -> str:
    if len(value) <= width:
        return value
    separator = "  dispatch "
    if separator in value:
        head, tail = value.rsplit(separator, 1)
        available = width - len(separator) - len(tail) - 3
        if available >= 8:
            return f"{head[:available]}...{separator}{tail}"
    return value[: width - 3] + "..."


def _fit_named_suffix(
    prefix: str,
    name: str,
    suffix: str,
    width: int,
) -> str:
    """Fit a semantic row by shortening its lower-priority name first."""

    line = f"{prefix}{name}{suffix}"
    if len(line) <= width:
        return line
    available = width - len(prefix) - len(suffix)
    if available < 4:
        return line
    return f"{prefix}{name[: available - 3]}...{suffix}"


def _route(route: RouteView) -> str:
    return (
        f"{route.runtime}/{route.model}/{route.effort}  preset {route.preset}"
    )


def _timing(timing: TimingView) -> str:
    if timing.mode == "unknown" or timing.seconds is None:
        return "time unknown"
    seconds = timing.seconds
    if seconds < 60:
        value = f"{seconds}s"
    elif seconds < 3600:
        value = f"{seconds // 60}m {seconds % 60:02d}s"
    else:
        value = f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{timing.mode} {value}"


def _scalar(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _review(summary: ReviewSummaryView) -> str:
    return (
        f"review cycle {_scalar(summary.cycle)}/{_scalar(summary.limit)}  "
        f"findings {_scalar(summary.findings)}  "
        f"material {_scalar(summary.material_findings)}"
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
            f"{child.status}  {_timing(child.timing)}"
        )
        lines.append(f"{pad}    state {child.state}  route {_route(child.route)}")
        lines.extend(_child_lines(child.children, indent + 4))
    return lines


def _compact_step(step: object, *, summarize_child: bool = False) -> str:
    marker = STEP_MARKERS.get(step.status, STEP_MARKERS["unknown"])
    route = ""
    if step.route != RouteView():
        route = (
            f" {step.route.runtime}/{step.route.model}/{step.route.effort}"
        )
    child = ""
    if summarize_child and step.children:
        item = step.children[0]
        child = (
            f" {STEP_MARKERS.get(item.status, '[?]')} "
            f"{_short(item.operation_id)} {item.state}"
        )
    primitive = step.primitive.split("@", 1)[0]
    return (
        f"    {marker} {step.step_id} {primitive} {step.status}"
        f"{child}{route} {_timing(step.timing)}"
    )


def _current_step(program: ProgramView) -> object | None:
    for status in ("running", "attention", "unknown"):
        selected = next((step for step in program.steps if step.status == status), None)
        if selected is not None:
            return selected
    if program.state in {"failed", "cancelled"}:
        selected = next(
            (step for step in program.steps if step.status == "stopped"), None
        )
        if selected is not None:
            return selected
    return next((step for step in program.steps if step.status == "pending"), None)


def _step_lines(program: ProgramView) -> list[str]:
    if not program.steps:
        return ["  steps    none projected"]
    lines = ["  steps"]
    current = _current_step(program)
    for step in program.steps:
        lines.append(_compact_step(step, summarize_child=step is not current))
        if step is not current:
            continue
        lines.append(
            f"        route {_route(step.route)}  session {step.session_mode}  "
            f"visits {step.visits}"
        )
        lines.extend(_child_lines(step.children, 8))
        if step.review != ReviewSummaryView():
            lines.append(f"        {_review(step.review)}")
        if step.evidence_issue:
            lines.append(f"        evidence {step.evidence_issue}")
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
        f"  classification {program.classification}  pipeline {program.pipeline}",
        f"  progress {sum(step.status == 'complete' for step in program.steps)}/{len(program.steps)}  root {_timing(program.timing)}",
        f"  next {NEXT_ACTION_TEXT.get(program.next_action, program.next_action)}",
        f"  surface {program.surface}  definition {_short(program.definition_sha256) or 'none'}",
        f"  executor {_route(program.executor)}  {program.executor_status}",
        f"  controls {controls}  loop {loop}",
    ]
    if program.self_healed_count or program.task_result.status == "complete":
        lines.append(
            f"  stage {program.current_stage}  "
            f"self-healed {program.self_healed_count}"
        )
    if program.task_result.status == "complete":
        result = program.task_result
        lines.append(
            f"  result {result.disposition}  evidence {result.evidence_count}  "
            f"gaps {result.gap_count}  plan {result.plan_close_status}"
        )
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


def _emphasis_base(line: str, emphasis: str) -> str:
    """Style one row from its typed role, inferring only for the dump view.

    The root product view knows which root and which step are current before a
    single character is formatted, so it states that role explicitly.  Only the
    owner-wide diagnostic dump, whose `[x]`/`[>]`/`[ ]` markers are
    unambiguous, still infers emphasis from its own text.
    """

    if emphasis == PRIMARY_EMPHASIS:
        return BOLD + SEMANTIC_COLORS["primary"]
    if emphasis == DIM_EMPHASIS:
        return DIM + SEMANTIC_COLORS["secondary"]
    if emphasis:
        return ""
    stripped = line.lstrip()
    if stripped.startswith(
        ("[>]", "[!]", "● ACTIVE", "├─ ●", "└─ ●", "├─ !", "└─ !")
    ):
        return BOLD + SEMANTIC_COLORS["primary"]
    if stripped.startswith(("[ ]", "├─ ○", "└─ ○")):
        return DIM + SEMANTIC_COLORS["secondary"]
    return ""


def _colorize(line: str, *, color: bool, emphasis: str = "") -> str:
    if not color:
        return line
    if line.startswith("HARNESS PIPELINES"):
        return f"{SEMANTIC_COLORS['primary']}{line}{RESET}"
    if line and set(line) == {"─"}:
        return f"{SEMANTIC_COLORS['rule']}{line}{RESET}"
    base = _emphasis_base(line, emphasis)

    def paint(role: str, value: str) -> str:
        return f"{SEMANTIC_COLORS[role]}{value}{RESET}{base}"

    rendered = MODEL_TOKEN.sub(
        lambda match: paint("model", match.group()),
        line,
    )
    rendered = RUNTIME_TOKEN.sub(
        lambda match: paint("identity", match.group()), rendered
    )
    rendered = IDENTITY_TOKEN.sub(
        lambda match: paint("identity", match.group()), rendered
    )
    rendered = REVIEW_LABEL.sub(
        lambda match: paint("model", match.group()), rendered
    )
    rendered = SEMANTIC_TOKEN.sub(
        lambda match: paint(SEMANTIC_TOKENS[match.group()], match.group()),
        rendered,
    )
    return f"{base}{rendered}{RESET}" if base else rendered


def _history_line(program: ProgramView) -> str:
    return (
        f"  {program.operation_id}  {program.kind}  {program.state}  "
        f"pipeline {program.pipeline}  {_timing(program.timing)}"
    )


def _compact_program_line(program: ProgramView) -> str:
    return (
        f"  {program.operation_id}  {program.kind}  {program.state}  "
        f"pipeline {program.pipeline}  {program.classification}  "
        f"{_timing(program.timing)}"
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
                next(
                    line
                    for line in _step_lines(program)
                    if line.lstrip().startswith(tuple(STEP_MARKERS.values()))
                    and _current_step(program) is not None
                    and _current_step(program).step_id in line
                ),
                f"  Viewport truncated +{max(len(_program_lines(program)) - 2, 0)} lines",
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
            f"  Viewport truncated +{omitted} lines"
        ]
        remaining = 0
    return [line for group in groups for line in group]


def _footer_lines(
    terminal: tuple[ProgramView, ...],
    projection: DashboardProjection,
) -> list[str]:
    lines = [f"Terminal history: {len(terminal)}"]
    lines.extend(_history_line(program) for program in terminal)
    lines.append("")
    lines.extend(_issue_lines(projection))
    return lines


def _bounded_footer_lines(
    terminal: tuple[ProgramView, ...],
    projection: DashboardProjection,
    budget: int,
) -> list[str]:
    """Shrink history before issues while publishing every omitted count."""

    full = _footer_lines(terminal, projection)
    if len(full) <= budget:
        return full
    dropped_issues = int(projection.truncated.get("issues", 0))
    total_issues = len(projection.issues) + dropped_issues
    if budget <= 0:
        return []
    if budget == 1:
        return [
            f"Footer compacted: history {len(terminal)}, issues {total_issues}"
        ]

    # Both section headers are retained. Detail slots go to issues first, so
    # completed history absorbs the first shortfall and live failures remain
    # diagnosable. Any further issue loss is included in the header count.
    detail_budget = budget - 2
    shown_issues = min(len(projection.issues), detail_budget)
    detail_budget -= shown_issues
    shown_history = min(len(terminal), detail_budget)
    hidden_history = len(terminal) - shown_history
    hidden_issues = total_issues - shown_issues
    history_suffix = f" (+{hidden_history} hidden)" if hidden_history else ""
    issue_suffix = f" (+{hidden_issues} more)" if hidden_issues else ""
    lines = [f"Terminal history: {shown_history}{history_suffix}"]
    lines.extend(_history_line(program) for program in terminal[:shown_history])
    lines.append(f"Recent issues: {shown_issues}{issue_suffix}")
    for issue in projection.issues[:shown_issues]:
        detail = issue.detail if issue.detail.isascii() else ""
        suffix = f"  {detail}" if detail else ""
        lines.append(
            f"  - {issue.code}  {_short(issue.operation_id)}  "
            f"{issue.classification}{suffix}"
        )
    return lines[:budget]


def _program_floor(programs: tuple[ProgramView, ...]) -> int:
    """Reserve the newest highest-priority identity and its omission count."""

    if not programs:
        return 0
    first = min(programs, key=_presentation_priority)
    identity_rows = 3 if _presentation_priority(first) < 2 else 1
    return identity_rows + (1 if len(programs) > 1 else 0)


def _human(value: str) -> str:
    words = " ".join(value.replace("_", "-").replace("-", " ").split())
    return words[:1].upper() + words[1:] if words else "Unknown"


def _task_name(program: ProgramView) -> str:
    value = program.task_name
    return value if value and value != "unknown" else _human(program.kind)


def _known_timing(timing: TimingView) -> str:
    return "" if timing.mode == "unknown" else _timing(timing)


def _compact_timing(status: str, timing: TimingView, *, current: bool) -> str:
    known = _known_timing(timing)
    if known:
        return known
    if current:
        return "time unavailable"
    if status in {"complete", "stopped", "attention"}:
        return "—"
    return ""


@dataclass(frozen=True)
class RootRow:
    """One composed root row with its typed emphasis and viewport priority."""

    text: str
    emphasis: str = NO_EMPHASIS
    priority: int = ROOT_PRIORITY_DETAIL


def _root_identity(program: ProgramView) -> tuple[str, str]:
    if program.state == "complete":
        return "✓", "COMPLETE"
    if program.state in {"failed", "cancelled", "attention-required"}:
        return "!", "ATTENTION"
    if program.classification == ATTENTION:
        return "!", "ATTENTION"
    if program.classification == WAITING:
        return "○", "WAITING"
    return "●", "ACTIVE"


def _root_header(projection: DashboardProjection) -> list[RootRow]:
    observed = projection.observed_at
    updated = "unknown"
    if (
        isinstance(observed, (int, float))
        and not isinstance(observed, bool)
        and math.isfinite(observed)
        and observed >= 0
    ):
        updated = datetime.fromtimestamp(observed, timezone.utc).strftime("%H:%M:%S")
    return [
        RootRow(
            "HARNESS PIPELINES  store: .vault-meta/harness  "
            f"updated {updated}",
            NO_EMPHASIS,
            ROOT_PRIORITY_HEADING,
        ),
        RootRow("─" * 64, NO_EMPHASIS, ROOT_PRIORITY_FRAME),
    ]


def _root_summary(program: ProgramView) -> list[RootRow]:
    marker, label = _root_identity(program)
    timing = _compact_timing(
        "complete" if program.state == "complete" else "running",
        program.timing,
        current=program.state not in TERMINAL,
    )
    suffix = f"  {timing}" if timing else ""
    route = program.executor
    executor_timing = _known_timing(program.timing)
    rows = [
        RootRow(
            f"{marker} {label} {_task_name(program)}  "
            f"dispatch {_short(program.operation_id)}{suffix}",
            # A root the observer is still watching is the current identity
            # whatever marker its classification renders, WAITING included.
            NO_EMPHASIS if program.state in TERMINAL else PRIMARY_EMPHASIS,
            ROOT_PRIORITY_IDENTITY,
        ),
        RootRow(
            f"  Executor {route.runtime}/{route.model} · {route.effort}  "
            f"{program.executor_status}"
            + (f"  {executor_timing}" if executor_timing else ""),
            NO_EMPHASIS,
            ROOT_PRIORITY_ROUTE,
        ),
    ]
    if program.self_healed_count or program.task_result.status == "complete":
        rows.append(
            RootRow(
                f"  stage {program.current_stage} · "
                f"self-healed {program.self_healed_count}",
                NO_EMPHASIS,
                ROOT_PRIORITY_DETAIL,
            )
        )
    if program.task_result.status == "complete":
        result = program.task_result
        rows.append(
            RootRow(
                f"  result {result.disposition} · evidence "
                f"{result.evidence_count} · gaps {result.gap_count} · "
                f"plan {result.plan_close_status}",
                NO_EMPHASIS,
                ROOT_PRIORITY_DETAIL,
            )
        )
    return rows


def _root_step_lines(
    program: ProgramView,
    *,
    width: int = ROOT_COLUMNS,
) -> list[RootRow]:
    if program.history:
        return [RootRow(row.text, row.emphasis, row.priority) for row in history_rows(program, width=width)]
    current = _current_step(program)
    lines: list[RootRow] = []
    for index, step in enumerate(program.steps):
        branch = "└─" if index == len(program.steps) - 1 else "├─"
        marker = {
            "complete": "✓",
            "running": "●",
            "pending": "○",
            "attention": "!",
            "stopped": "!",
            "unknown": "!",
        }.get(step.status, "!")
        timing = _compact_timing(
            step.status, step.timing, current=step is current
        )
        prefix = f"  {branch} {marker} "
        suffix = f"  {step.status}" + (f"  {timing}" if timing else "")
        lines.append(
            RootRow(
                _fit_named_suffix(
                    prefix,
                    _human(step.step_id),
                    suffix,
                    width,
                ),
                # The selected step leads the work even before it starts, so a
                # pending current row is emphasised while later rows stay dim.
                PRIMARY_EMPHASIS
                if step is current
                else DIM_EMPHASIS
                if step.status == "pending"
                else NO_EMPHASIS,
                ROOT_PRIORITY_IDENTITY
                if step is current
                else ROOT_PRIORITY_STEP,
            )
        )
        if step is not current:
            continue
        pad = "     " if branch == "└─" else "  │  "
        details: list[str] = []
        if step.primitive.split("@", 1)[0] == "review":
            route = step.route
            detail = (
                f"Reviewer {route.runtime}/{route.model} · {route.effort}"
            )
            if step.review != ReviewSummaryView():
                detail += (
                    f"  cycle {_scalar(step.review.cycle)}/{_scalar(step.review.limit)}"
                    f" · findings {_scalar(step.review.findings)}"
                    f" · material {_scalar(step.review.material_findings)}"
                )
            details.append(detail)
        for child in step.children[:2]:
            child_timing = _known_timing(child.timing)
            details.append(
                f"↳ {_identity(child.operation_id)}  {_human(child.kind).lower()}  "
                f"{child.state}"
                + (f"  {child_timing}" if child_timing else "")
            )
        if step.evidence_issue:
            details.append(f"! {step.evidence_issue}")
        lines.extend(
            RootRow(
                f"{pad}  {detail}",
                NO_EMPHASIS,
                ROOT_PRIORITY_ROUTE
                if detail.startswith("Reviewer ")
                else ROOT_PRIORITY_ISSUE
                if detail.startswith("! ")
                else ROOT_PRIORITY_DETAIL,
            )
            for detail in details[:3]
        )
    return lines


def _root_recent(
    main: ProgramView,
    programs: tuple[ProgramView, ...],
    recent: int,
) -> list[RootRow]:
    terminal = tuple(
        program
        for program in programs
        if program is not main and program.state in TERMINAL
    )[:recent]
    lines = [RootRow("RECENT", NO_EMPHASIS, ROOT_PRIORITY_HISTORY)]
    if not terminal:
        return lines + [RootRow("  none")]
    for program in terminal:
        marker = "✓" if program.state == "complete" else "!"
        timing = _compact_timing("complete", program.timing, current=False)
        lines.append(
            RootRow(
                f"  {marker} {_task_name(program)}  "
                f"dispatch {_short(program.operation_id)}  {timing}",
                NO_EMPHASIS,
                ROOT_PRIORITY_HISTORY
                if marker == "✓"
                else ROOT_PRIORITY_ISSUE,
            )
        )
    return lines


def _root_issues(projection: DashboardProjection) -> list[RootRow]:
    dropped = int(projection.truncated.get("issues", 0))
    total = len(projection.issues) + dropped
    lines = [RootRow(f"ISSUES ({total})", NO_EMPHASIS, ROOT_PRIORITY_HEADING)]
    if not projection.issues:
        return lines + [RootRow("  none")]
    for issue in projection.issues:
        lines.append(
            RootRow(
                f"  ! {issue.code}  {_short(issue.operation_id)}  "
                f"{issue.classification}",
                NO_EMPHASIS,
                ROOT_PRIORITY_ISSUE,
            )
        )
    if dropped:
        lines.append(
            RootRow(f"  +{dropped} more", NO_EMPHASIS, ROOT_PRIORITY_HISTORY)
        )
    return lines


def _root_lines(
    projection: DashboardProjection,
    *,
    recent: int,
    width: int = ROOT_COLUMNS,
) -> list[RootRow]:
    lines = _root_header(projection)
    if not projection.programs:
        lines.extend(
            [
                RootRow(
                    f"○ WAITING Dispatch not started  "
                    f"dispatch {_short(projection.owner_id)}",
                    PRIMARY_EMPHASIS,
                    ROOT_PRIORITY_IDENTITY,
                ),
                RootRow("  Observer waiting for dispatch start."),
            ]
        )
        main = None
    else:
        main = next(
            (
                program
                for program in projection.programs
                if program.state not in TERMINAL
            ),
            projection.programs[0],
        )
        lines.extend(_root_summary(main))
        lines.extend(_root_step_lines(main, width=width))
    lines.append(RootRow("", NO_EMPHASIS, ROOT_PRIORITY_HISTORY))
    lines.extend(
        [RootRow("RECENT", NO_EMPHASIS, ROOT_PRIORITY_HISTORY), RootRow("  none")]
        if main is None
        else _root_recent(main, projection.programs, recent)
    )
    lines.append(RootRow("", NO_EMPHASIS, ROOT_PRIORITY_HISTORY))
    lines.extend(_root_issues(projection))
    lines.extend(
        [
            RootRow("─" * 64, NO_EMPHASIS, ROOT_PRIORITY_FRAME),
            RootRow(
                "✓ complete  ● running  ○ pending  ! attention  ↻ retry",
                NO_EMPHASIS,
                ROOT_PRIORITY_FRAME,
            ),
        ]
    )
    return lines


def _bounded_root_lines(lines: list[RootRow], rows: int | None) -> list[RootRow]:
    if rows is None or len(lines) <= rows:
        return lines
    if rows <= 0:
        return []
    if rows == 1:
        return [
            RootRow(
                f"Viewport truncated +{len(lines)} lines",
                NO_EMPHASIS,
                ROOT_PRIORITY_IDENTITY,
            )
        ]
    selected = sorted(
        sorted(
            range(len(lines)),
            key=lambda index: (lines[index].priority, index),
        )[: rows - 1]
    )
    omitted = len(lines) - len(selected)
    return [lines[index] for index in selected] + [
        RootRow(
            f"Viewport truncated +{omitted} lines",
            NO_EMPHASIS,
            ROOT_PRIORITY_IDENTITY,
        )
    ]


def _diagnostic_lines(
    projection: DashboardProjection,
    *,
    recent: int,
    rows: int | None,
    scope: str,
) -> list[str]:
    dropped = int(projection.truncated.get("programs", 0))
    suffix = f" (+{dropped} more)" if dropped else ""
    active = tuple(
        program for program in projection.programs if program.state not in TERMINAL
    )
    terminal = tuple(
        program for program in projection.programs if program.state in TERMINAL
    )[:recent]
    header = [
        "HARNESS PIPELINES",
        f"Harness dashboard - {scope} {projection.owner_id}",
        f"Classification: {projection.classification}  "
        f"Programs: {len(projection.programs)}{suffix}  "
        f"Active pipelines: {len(active)}",
        f"cmux surface probe: {projection.surface_probe}",
        "",
    ]
    footer = _footer_lines(terminal, projection)
    lines = list(header)
    if not projection.programs:
        lines.append(
            "No root program is projected yet; "
            "the observer is waiting for dispatch start."
            if scope == "root"
            else "No program is bound to this owner."
        )
        lines.append("")
    elif rows is None:
        for program in active:
            lines.extend(_program_lines(program))
            lines.append("")
    else:
        available = max(rows - len(header), 0)
        footer = _bounded_footer_lines(
            terminal,
            projection,
            max(available - _program_floor(active), 0),
        )
        lines.extend(
            _bounded_program_lines(active, max(available - len(footer), 0))
        )
    lines.extend(footer)
    if rows is not None and len(lines) > rows:
        omitted = len(lines) - rows + 1
        lines = lines[: max(rows - 1, 0)]
        if rows:
            lines.append(f"Viewport truncated +{omitted} lines")
    if rows is not None and rows <= 0:
        return []
    return lines


def render(
    projection: DashboardProjection,
    *,
    recent: int = 3,
    color: bool = False,
    rows: int | None = None,
    scope: str = "owner",
    columns: int | None = None,
) -> str:
    """Render one bounded read-only English dashboard for a terminal."""

    default_width = ROOT_COLUMNS if scope == "root" else DIAGNOSTIC_COLUMNS
    width = min(max(columns or default_width, 20), MAX_LINE)
    rendered = (
        _bounded_root_lines(
            _root_lines(projection, recent=recent, width=width), rows
        )
        if scope == "root"
        else [
            RootRow(line, "")
            for line in _diagnostic_lines(
                projection, recent=recent, rows=rows, scope=scope
            )
        ]
    )
    return "\n".join(
        _colorize(_clip(row.text, width), color=color, emphasis=row.emphasis)
        for row in rendered
    ) + ("\n" if rendered else "")
