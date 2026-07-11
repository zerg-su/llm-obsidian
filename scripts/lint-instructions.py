#!/usr/bin/env python3
"""Lint skill/docs instructions against the repository's hard contracts."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_WEB_SKILLS = ("autoresearch", "wiki-ingest", "wiki-query")
WRITER_REQUIRED_SKILLS = ("daily", "journal")


def frontmatter(text: str) -> str:
    parts = text.split("---", 2)
    return parts[1] if len(parts) == 3 else ""


def protected_tool_issues(name: str, text: str) -> list[str]:
    fm = frontmatter(text)
    return [f"skills/{name}: protected coordinator exposes web tools"] if re.search(r"allowed-tools:.*\bWeb(?:Search|Fetch)\b", fm) else []


def writer_path_issues(name: str, text: str) -> list[str]:
    issues: list[str] = []
    fm = frontmatter(text)
    if re.search(r"allowed-tools:.*\b(?:Write|Edit)\b", fm):
        issues.append(f"skills/{name}: page mutator exposes direct Write/Edit tools")
    if "vault-write.py" not in text:
        issues.append(f"skills/{name}: page mutations must route through vault-write.py")
    direct_patterns = (
        r"mkdir -p \"\$DIR\"",
        r"write (?:the |its )?(?:lines|content).*(?:into|under) (?:today's |the )?`?##",
        r"append (?:the )?item.*under `?##",
    )
    if any(re.search(pattern, text, flags=re.I) for pattern in direct_patterns):
        issues.append(f"skills/{name}: contains stale direct wiki mutation instructions")
    return issues


def daily_runtime_issues(text: str) -> list[str]:
    issues: list[str] = []
    if re.search(r"allowed-tools:.*\bAgent\b", frontmatter(text)) is None:
        issues.append("skills/daily: Claude subagent routing requires the Agent tool")
    required = (
        "scripts/detect-runtime.sh --three-way",
        "scripts/claude-subscription-check.py",
        "llm-obsidian:daily-summarizer",
        "Never fall back to the parent Claude model",
        "pipeline-stats.py --days 7",
    )
    for value in required:
        if value not in text:
            issues.append(f"skills/daily: missing runtime invariant {value!r}")
    stale = "On Claude or when that custom agent is unavailable, produce the same JSON in the parent"
    if stale in text:
        issues.append("skills/daily: Claude synthesis regressed to parent fallback")
    return issues


def daily_runtime_repo_issues(root: Path) -> list[str]:
    path = root / "skills" / "daily" / "SKILL.md"
    if not path.is_file():
        return []  # WRITER_REQUIRED_SKILLS owns the canonical missing-file issue.
    issues = daily_runtime_issues(path.read_text(encoding="utf-8"))
    if not (root / "agents" / "daily-summarizer.md").is_file():
        issues.append("missing agents/daily-summarizer.md")
    return issues


def check_repo(root: Path) -> list[str]:
    issues: list[str] = []
    for name in PROTECTED_WEB_SKILLS:
        path = root / "skills" / name / "SKILL.md"
        if not path.is_file():
            issues.append(f"missing {path.relative_to(root)}")
            continue
        issues.extend(protected_tool_issues(name, path.read_text(encoding="utf-8")))

    for name in WRITER_REQUIRED_SKILLS:
        path = root / "skills" / name / "SKILL.md"
        if not path.is_file():
            issues.append(f"missing {path.relative_to(root)}")
            continue
        issues.extend(writer_path_issues(name, path.read_text(encoding="utf-8")))

    issues.extend(daily_runtime_repo_issues(root))

    ingest = (root / "skills" / "wiki-ingest" / "SKILL.md").read_text(encoding="utf-8")
    for forbidden in ("Use PATCH", "Save to `.raw", "Write the updated manifest back"):
        if forbidden in ingest:
            issues.append(f"wiki-ingest contains stale instruction: {forbidden}")
    if "expected_sha256" not in ingest or "manifest_update" not in ingest:
        issues.append("wiki-ingest must describe optimistic full-content/manifest writes")

    for path in sorted((root / "skills").glob("*/SKILL.md")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if '"hot_bullet"' in line and "c-NNNNNN" not in line:
                issues.append(f"{path.relative_to(root)}:{line_no}: hot_bullet example lacks c-NNNNNN")

    runtime_doc = (root / "docs" / "runtime-capabilities.md").read_text(encoding="utf-8")
    if re.search(r"\| `SessionStart`[^\n]*\| Not provided by this plugin \|", runtime_doc):
        issues.append("runtime capabilities still claim Codex hooks are unavailable")
    # Personal vaults may keep an additional provider comparison, but the
    # public template intentionally does not ship private learning notes.
    comparison_path = root / "wiki" / "learning" / "Anthropic vs OpenAI.md"
    if comparison_path.is_file():
        comparison = comparison_path.read_text(encoding="utf-8")
        if "hooks-аналога нет" in comparison:
            issues.append("learning comparison still says Codex has no hooks")

    research = (root / "scripts" / "research-isolation.py").read_text(encoding="utf-8")
    for required in ('stage == "fetch" else "disabled"', "UNTRUSTED DATA", "codex-home-"):
        if required not in research:
            issues.append(f"research isolation missing invariant {required!r}")
    review = (root / "skills" / "review-dispatch" / "scripts" / "spawn_review.py").read_text(encoding="utf-8")
    supervisor = (root / "scripts" / "cmux_agent_supervisor.py").read_text(encoding="utf-8")
    review_contract = review + "\n" + supervisor
    if "--permission-mode auto" in review:
        issues.append("review launcher regressed to Claude auto permissions")
    for required in (
        '"workspace-write"', "review_runtime_dir", "review-outbox-relay",
        '"--disable",\n            "hooks"',
        "Codex reviewer command must not request additional writable roots",
    ):
        if required not in review_contract:
            issues.append(f"Codex reviewer missing isolated relay invariant {required!r}")
    for required in (
        '"--permission-mode", "dontAsk"', 'CLAUDE_REVIEW_TOOL_SURFACE = "Read,Glob,Grep,Write,Bash"',
        "Edit(./.review-outbox.json)", "send_review.py submit", "cmux_agent_supervisor.py",
    ):
        if required not in review_contract:
            issues.append(f"Claude reviewer missing unattended read-only invariant {required!r}")
    for required in (
        "Bash(python3 tests/test_task_lifecycle.py)",
        "Bash(bash tests/test_review_dispatch.sh)",
        "Bash(python3 tests/test_contract_schemas.py)",
        "Bash(python3 scripts/lint-instructions.py)",
    ):
        if required not in supervisor:
            issues.append(f"Claude reviewer missing bounded diagnostic {required!r}")
    if "claude-subscription-check.py" not in review:
        issues.append("Claude reviewer missing subscription-only preflight")
    if ".task-summary.json" not in (root / "skills" / "reap-send" / "SKILL.md").read_text(encoding="utf-8"):
        issues.append("reap-send must use canonical .task-summary.json")
    dispatch_text = (root / "skills" / "dispatch" / "SKILL.md").read_text(encoding="utf-8")
    reap_send_text = (root / "skills" / "reap-send" / "SKILL.md").read_text(encoding="utf-8")
    reap_text = (root / "skills" / "reap" / "SKILL.md").read_text(encoding="utf-8")
    for required in ("interaction_policy", "approved_plan_sha256", "forbidden_actions", "watchdog_policy", "cmux_agent_supervisor.py"):
        if required not in dispatch_text:
            issues.append(f"dispatch missing unattended contract invariant {required!r}")
    for required in ("-a never", "workspace-write", "cmux_agent_supervisor.py"):
        if required not in dispatch_text:
            issues.append(f"Codex dispatch missing unattended approval invariant {required!r}")
    for required in ("cmux_task_watchdog.py", "cmux_surface_lifecycle.py", "subprocess.run(argv"):
        if required not in supervisor:
            issues.append(f"cmux supervisor missing lifecycle invariant {required!r}")
    if "shell=True" in supervisor:
        issues.append("cmux supervisor must not execute agent commands through a shell")
    task_prompt = (root / "skills" / "dispatch" / "references" / "task-prompt-template.md").read_text(encoding="utf-8")
    for required in ("task_escalation.py", "Treat `.task-meta.json` as read-only", "Never push, deploy, publish", "it never sends you input"):
        if required not in task_prompt:
            issues.append(f"dispatch task prompt missing safety invariant {required!r}")
    for required in ("interaction_policy=unattended", "task_contract.py", "final"):
        if required not in reap_send_text:
            issues.append(f"reap-send missing unattended handoff invariant {required!r}")
    for required in (
        "check-handoff", "prepare-reap", "expected_sha256",
        "validate-vault.py --summary", "request-exit",
    ):
        if required not in reap_text:
            issues.append(f"reap missing unattended finalization invariant {required!r}")
    source_ref = root / "skills" / "wiki-ingest" / "references" / "frontmatter.md"
    source_text = source_ref.read_text(encoding="utf-8") if source_ref.is_file() else ""
    if not all(value in source_text for value in ("source_class", "verified_at", "content_sha256")):
        issues.append("source provenance reference is missing required fields")
    return issues


def main() -> int:
    issues = check_repo(ROOT)
    if issues:
        for issue in issues:
            print(f"INSTRUCTION_DRIFT: {issue}", file=sys.stderr)
        return 1
    print("instruction lint: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
