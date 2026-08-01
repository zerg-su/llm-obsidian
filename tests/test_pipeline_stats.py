#!/usr/bin/env python3
"""Hermetic tests for evidence-bounded skill reporting in pipeline-stats.py.

Skill invocations are observable only in Claude history/transcripts. Codex runs
the same skills without leaving a trace there, so a zero must never be rendered
as a proven dead-weight verdict while uncovered runtime activity is observed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATS = ROOT / "scripts" / "pipeline-stats.py"
sys.path.insert(0, str(ROOT / "scripts"))


class Fail(SystemExit):
    pass


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise Fail(f"FAIL {label}{': ' + detail if detail else ''}")
    print(f"OK   {label}")


def load_stats():
    spec = importlib.util.spec_from_file_location("pipeline_stats_test", STATS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def build_vault(
    tmp: Path, *, router: list[dict], events: list[dict], typed: list[str] = ()
) -> Path:
    """A throwaway vault root: copied script, three skills, isolated Claude home."""
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(STATS, tmp / "scripts" / "pipeline-stats.py")
    shutil.copy2(
        ROOT / "scripts" / "review_contract.py",
        tmp / "scripts" / "review_contract.py",
    )
    for name in ("alpha", "beta", "gamma"):
        skill = tmp / "skills" / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    meta = tmp / ".vault-meta"
    meta.mkdir(parents=True, exist_ok=True)
    if router:
        (meta / "router-hits.jsonl").write_text(
            "".join(json.dumps(rec) + "\n" for rec in router), encoding="utf-8"
        )
    if events:
        (meta / "pipeline-events.jsonl").write_text(
            "".join(json.dumps(rec) + "\n" for rec in events), encoding="utf-8"
        )
    claude_home = tmp / "home" / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    if typed:
        # history.jsonl keys off the resolved vault root the script computes.
        project = str(tmp.resolve())
        now_ms = int(time.time() * 1000)
        (claude_home / "history.jsonl").write_text(
            "".join(
                json.dumps({"project": project, "timestamp": now_ms, "display": f"/{name}"}) + "\n"
                for name in typed
            ),
            encoding="utf-8",
        )
    return tmp


def run_stats(tmp: Path) -> str:
    env = dict(os.environ)
    env["HOME"] = str(tmp / "home")
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CODEX_THREAD_ID", None)
    proc = subprocess.run(
        [sys.executable, str(tmp / "scripts" / "pipeline-stats.py"), "--days", "30"],
        capture_output=True,
        text=True,
        env=env,
    )
    check(f"stats exits 0 ({tmp.name})", proc.returncode == 0, proc.stderr)
    return proc.stdout


def run_unit(stats) -> None:
    check(
        "only Claude skill invocations are observable",
        stats.SKILL_OBSERVABLE_RUNTIMES == frozenset({"claude"}),
    )
    check(
        "Codex counts as a skill-capable runtime",
        {"claude", "codex"} <= set(stats.SKILL_CAPABLE_RUNTIMES),
    )
    check(
        "an unrecognized runtime tag is clamped, not invented",
        stats.normalize_runtime("wat") == "unknown"
        and stats.normalize_runtime("codex") == "codex"
        and stats.normalize_runtime("") == "unknown",
    )
    check(
        "orchestration ops are recognized as model-driven",
        {
            "agent-run",
            "review-callback",
            "review-round-complete",
            "surface-lifecycle",
        }
        <= set(stats.AGENT_DRIVEN_OPS),
    )

    bounded = stats.classify_zero_usage(
        ["alpha", "beta", "gamma"],
        {"beta": {"codex"}, "gamma": {"claude"}},
        {"claude": 12, "codex": 3, "unknown": 40},
    )
    check(
        "observed Codex activity marks the verdict as unverified",
        bounded["uncovered_runtimes"] == ["codex"],
        repr(bounded),
    )
    check(
        "shell-only 'unknown' activity is not treated as a skill runtime",
        "unknown" not in bounded["uncovered_runtimes"],
    )
    check(
        "a Codex router hint rescues the skill from the dead list",
        bounded["hinted_elsewhere"] == ["beta"] and "beta" not in bounded["dead"],
        repr(bounded),
    )
    check(
        "a Claude-only hint does not rescue the skill",
        bounded["dead"] == ["alpha", "gamma"],
        repr(bounded),
    )

    complete = stats.classify_zero_usage(
        ["alpha"], {}, {"claude": 9, "unknown": 4}
    )
    check(
        "no uncovered runtime keeps the verdict complete",
        complete["uncovered_runtimes"] == [] and complete["dead"] == ["alpha"],
        repr(complete),
    )

    # 'unknown' means the runtime was never recorded, not that no model was
    # involved. An unattributed record carrying an orchestration op proves some
    # agent session ran, so its skill usage cannot be ruled out.
    unattributed = stats.classify_zero_usage(
        ["alpha"], {}, {"claude": 9, "unknown": 40}, unattributed_agent_activity=6
    )
    check(
        "unattributed agent activity blocks a complete verdict",
        unattributed["uncovered_runtimes"] == ["unattributed"],
        repr(unattributed),
    )
    unattributed_hint = stats.classify_zero_usage(
        ["alpha"],
        {"alpha": {"unknown"}},
        {"claude": 9, "unknown": 40},
        unattributed_agent_activity=6,
    )
    check(
        "an unattributed router hint is unverified evidence, not dead weight",
        unattributed_hint["hinted_elsewhere"] == ["alpha"]
        and unattributed_hint["dead"] == [],
        repr(unattributed_hint),
    )
    check(
        "unattributed activity without an agent op stays out of the verdict",
        stats.classify_zero_usage(
            ["alpha"], {}, {"claude": 9, "unknown": 40}, unattributed_agent_activity=0
        )["uncovered_runtimes"]
        == [],
    )


def run_report_with_codex(tmp: Path) -> None:
    now = int(time.time())
    vault = build_vault(
        tmp,
        router=[
            {
                "ts": now - 3600,
                "runtime": "codex",
                "session": "codex-thread",
                "skill_matches": [{"name": "beta", "hits": 2}],
                "agent_matches": [],
            }
        ],
        events=[
            {
                "schema": 1,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 1800)),
                "runtime": "codex",
                "session": "codex-thread",
                "actor": "task",
                "op": "vault-write",
                "status": "ok",
                "paths": [],
                "counts": {},
            }
        ],
        typed=["alpha"],
    )
    out = run_stats(vault)

    check(
        "report names the runtime coverage boundary",
        "## Skill telemetry coverage" in out,
        out,
    )
    check(
        "coverage table reports Codex as observed but unobservable",
        "| codex |" in out and "| claude |" in out,
        out,
    )
    check(
        "zero list is not presented as a proven dead-weight verdict",
        "## Dead-weight candidates" not in out,
        out,
    )
    check(
        "zero list is explicitly bounded to Claude evidence",
        "## Claude-zero skills" in out and "codex" in out.split("## Claude-zero skills")[1],
        out,
    )
    tail = out.split("## Claude-zero skills")[1]
    check(
        "a Codex-hinted skill is separated from removal candidates",
        "/beta" in tail.split("Dead-weight candidates")[0],
        tail,
    )
    check(
        "a router hint is not rendered as proof of invocation",
        "in use" not in tail and "unverified" in tail.split("Dead-weight candidates")[0],
        tail,
    )
    check(
        "the coverage counts are not presented as comparable across runtimes",
        "not comparable across runtimes" in out,
        out,
    )
    check(
        "unhinted skills stay listed as removal candidates",
        "/gamma" in tail.split("Dead-weight candidates")[1],
        tail,
    )


def run_report_claude_only(tmp: Path) -> None:
    now = int(time.time())
    vault = build_vault(
        tmp,
        router=[
            {
                "ts": now - 3600,
                "runtime": "claude",
                "session": "claude-session",
                "skill_matches": [{"name": "beta", "hits": 2}],
                "agent_matches": [],
            }
        ],
        events=[],
        typed=["alpha"],
    )
    out = run_stats(vault)
    check(
        "a Claude-only window with real usage reports a complete verdict",
        "## Dead-weight candidates" in out,
        out,
    )
    tail = out.split("## Dead-weight candidates")[1]
    check(
        "unused skills are candidates when nothing else was observed",
        "/beta" in tail and "/gamma" in tail,
        tail,
    )
    check("the skill that was actually used is not a candidate", "/alpha" not in tail, tail)
    check(
        "the report never claims a runtime tag means 'no model'",
        "cannot invoke skills" not in out,
        out,
    )


def run_report_unattributed_agent(tmp: Path) -> None:
    """Orchestration tagged 'unknown' is unattributed, not proof of no model."""
    now = int(time.time())
    vault = build_vault(
        tmp,
        router=[
            {
                "ts": now - 3600,
                # An unrecognized tag must be clamped, not shown as its own runtime.
                "runtime": "some-future-agent",
                "session": "s",
                "skill_matches": [{"name": "beta", "hits": 1}],
                "agent_matches": [],
            }
        ],
        events=[
            {
                "schema": 1,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 1800)),
                "runtime": "unknown",
                "session": "s",
                "actor": "task",
                "op": "agent-run",
                "status": "ok",
                "paths": [],
                "counts": {},
            }
        ],
        typed=["alpha"],
    )
    out = run_stats(vault)
    check(
        "unattributed orchestration blocks the complete dead-weight verdict",
        "## Dead-weight candidates" not in out and "## Claude-zero skills" in out,
        out,
    )
    check(
        "the unattributed boundary is named in the zero section",
        "unattributed" in out.split("## Claude-zero skills")[1],
        out,
    )
    zero_tail = out.split("## Claude-zero skills")[1]
    hinted, candidates = zero_tail.split("Dead-weight candidates", 1)
    check(
        "an unattributed router hint is reported as unverified, not removable",
        "unverified" in hinted and "/beta" in hinted and "/beta" not in candidates,
        zero_tail,
    )
    check(
        "an unrecognized router tag does not become its own coverage row",
        "some-future-agent" not in out,
        out,
    )
    check(
        "the unattributed row states missing attribution, not missing capability",
        "| unattributed |" in out and "runtime not recorded" in out,
        out,
    )


def run_report_healthy_but_empty(tmp: Path) -> None:
    """Claude records present but no skill call: the sources are fine, the window is small."""
    now = int(time.time())
    vault = build_vault(
        tmp,
        router=[
            {
                "ts": now - 600,
                "runtime": "claude",
                "session": "s",
                "skill_matches": [],
                "agent_matches": [],
            }
        ],
        events=[],
    )
    # A typed prompt that is not a skill call: evidence exists, usage does not.
    project = str(vault.resolve())
    (vault / "home" / ".claude" / "history.jsonl").write_text(
        json.dumps({"project": project, "timestamp": int(time.time() * 1000), "display": "hello"})
        + "\n",
        encoding="utf-8",
    )
    out = run_stats(vault)
    check(
        "a healthy but empty window still makes no dead-weight claim",
        "## Skill usage evidence unavailable" in out,
        out,
    )
    check(
        "healthy Claude sources are not blamed for the empty window",
        "Check that Claude history/transcripts cover" not in out,
        out,
    )


def run_report_no_evidence(tmp: Path) -> None:
    """An empty window proves nothing — not that every installed skill is dead."""
    vault = build_vault(tmp, router=[], events=[])
    out = run_stats(vault)
    check(
        "an evidence-free window makes no dead-weight claim",
        "## Dead-weight candidates" not in out,
        out,
    )
    check(
        "an evidence-free window says so explicitly",
        "## Skill usage evidence unavailable" in out,
        out,
    )
    tail = out.split("## Skill usage evidence unavailable")[1]
    check(
        "no installed skill is listed for removal without evidence",
        not any(f"/{n}" in tail.split("## Agents usage")[0] for n in ("alpha", "beta", "gamma")),
        tail,
    )


def main() -> int:
    run_unit(load_stats())
    with tempfile.TemporaryDirectory(prefix="pipeline-stats-codex.") as raw:
        run_report_with_codex(Path(raw))
    with tempfile.TemporaryDirectory(prefix="pipeline-stats-claude.") as raw:
        run_report_claude_only(Path(raw))
    with tempfile.TemporaryDirectory(prefix="pipeline-stats-unattributed.") as raw:
        run_report_unattributed_agent(Path(raw))
    with tempfile.TemporaryDirectory(prefix="pipeline-stats-empty.") as raw:
        run_report_no_evidence(Path(raw))
    with tempfile.TemporaryDirectory(prefix="pipeline-stats-small.") as raw:
        run_report_healthy_but_empty(Path(raw))
    print("\nAll pipeline stats tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
