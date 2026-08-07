#!/usr/bin/env python3
"""Lint skill/docs instructions against the repository's hard contracts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_WEB_SKILLS = ("research", "wiki-ingest", "wiki-query")
WRITER_REQUIRED_SKILLS = ("agenda", "daily", "journal")
LEGACY_PUBLIC_SKILLS = ("reap-send",)
ENGINEERING_DISCIPLINE_PATTERN = re.compile(
    r"^(?:- )?Engineering discipline: think before coding; simplicity first; "
    r"surgical changes;(?: |\n)goal/evidence discipline—local green is not task "
    r"completion\.$",
    re.MULTILINE,
)
MARKDOWN_LIST_ITEM = re.compile(r"^\s*(?:[-*+] |\d+[.)] )")
ENGINEERING_WEAKENING_TERMS = r"optional|ignore|suggestions?\s+only|advisory"
ENGINEERING_WEAKENING = re.compile(
    rf"\b(?:{ENGINEERING_WEAKENING_TERMS}|unless)\b",
    flags=re.I,
)
STANDALONE_ENGINEERING_WEAKENING = re.compile(
    rf"\b(?:{ENGINEERING_WEAKENING_TERMS})\b", flags=re.I
)
ENGINEERING_LEAD_IN_SCOPE = re.compile(
    r"\b(?:this (?:whole )?section|everything below|"
    r"(?:the|this|these|following|next) (?:rules?|contracts?)(?: below)?|"
    r"(?:rules?|contracts?) below|treat[^\n]*(?:rules?|contracts?))\b",
    flags=re.I,
)


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


def legacy_skill_issues(root: Path) -> list[str]:
    issues: list[str] = []
    for name in LEGACY_PUBLIC_SKILLS:
        skill_root = root / "skills" / name
        if not skill_root.is_dir():
            continue
        for path in sorted(skill_root.rglob("*")):
            relative = path.relative_to(skill_root)
            if (
                not path.is_file()
                or path.suffix == ".pyc"
                or "__pycache__" in relative.parts
            ):
                continue
            issues.append(
                f"legacy public skill file remains: {path.relative_to(root)}"
            )
    return issues


def failure_repair_issues(
    claude: str,
    agents: str,
    task_prompt: str,
    escalation: str,
    reference: str,
) -> list[str]:
    """Keep coordinator auto-repair and background pause boundaries aligned."""
    issues: list[str] = []
    required_reference = (
        "Contain before classification",
        "Coordinator auto-repair boundary",
        "Execute the repair",
        "mechanism-failure",
        "pipeline-events.jsonl",
    )
    for value in required_reference:
        if value not in reference:
            issues.append(f"failure repair reference missing invariant {value!r}")
    central_required = {
        "CLAUDE.md": ("Failure-to-repair", "без дополнительного вопроса", "один раз спрашивает", "regression test"),
        "AGENTS.md": ("Failure-to-repair", "auto-repair", "ask the user once", "regression test"),
    }
    for name, text in (("CLAUDE.md", claude), ("AGENTS.md", agents)):
        for value in central_required[name]:
            if value not in text:
                issues.append(f"{name} missing failure-repair invariant {value!r}")
    for value in (
        "mechanism-failure",
        "read-only diagnosis",
        "request coordinator classification",
        "Remain paused",
        "may authorize",
        "must ask",
    ):
        if value not in task_prompt:
            issues.append(f"dispatch task prompt missing failure-repair invariant {value!r}")
    if '"mechanism-failure"' not in escalation:
        issues.append("task escalation missing mechanism-failure category")
    for value in ("MECHANISM_REPAIR_POLICY", "Auto-repair only", "otherwise ask the user once"):
        if value not in escalation:
            issues.append(f"task escalation missing coordinator repair invariant {value!r}")
    return issues


def _explicit_weakening_lead_in(candidate: str) -> bool:
    for raw_line in candidate.splitlines():
        line = MARKDOWN_LIST_ITEM.sub("", raw_line, count=1).strip()
        bare = line.rstrip(".: ").casefold()
        if bare in {
            "optional",
            "ignore",
            "ignore this",
            "suggestion only",
            "suggestions only",
        }:
            return True
        if (
            ENGINEERING_LEAD_IN_SCOPE.search(line)
            and ENGINEERING_WEAKENING.search(line)
        ):
            return True
    return False


def _engineering_discipline_is_weakened(text: str, offset: int) -> bool:
    """Decide weakening from one bounded contract item and lead-in."""

    lines = text.splitlines()
    line_index = text.count("\n", 0, offset)
    start = line_index
    current_is_item = bool(MARKDOWN_LIST_ITEM.match(lines[start]))
    if not current_is_item:
        while (
            start > 0
            and lines[start - 1].strip()
            and not MARKDOWN_LIST_ITEM.match(lines[start - 1])
        ):
            start -= 1
    end = line_index + 1
    while end < len(lines) and lines[end].strip():
        if MARKDOWN_LIST_ITEM.match(lines[end]):
            break
        end += 1
    item = "\n".join(lines[start:end])
    if ENGINEERING_WEAKENING.search(item):
        return True

    # A contract may be either the next sibling or the lazy continuation of an
    # immediately preceding item.  Include that item only when it explicitly
    # scopes the following rule; generic advisory prose in a sibling is not a
    # weakening of this contract.
    prior_item_end = start
    prior_item_start = prior_item_end - 1
    while prior_item_start >= 0 and lines[prior_item_start].strip():
        if MARKDOWN_LIST_ITEM.match(lines[prior_item_start]):
            candidate = "\n".join(lines[prior_item_start:prior_item_end])
            if _explicit_weakening_lead_in(candidate):
                return True
            break
        prior_item_start -= 1

    # A standalone lead-in can scope a complete list rather than only its
    # first item, so inspect the prose block immediately before that list.
    lead_anchor = start
    if current_is_item:
        while lead_anchor > 0 and lines[lead_anchor - 1].strip():
            previous = lines[lead_anchor - 1]
            if MARKDOWN_LIST_ITEM.match(previous) or previous[:1].isspace():
                lead_anchor -= 1
                continue
            break
    prior = lead_anchor - 1
    while prior >= 0 and not lines[prior].strip():
        prior -= 1
    if prior < 0:
        return False
    prior_start = prior
    while prior_start > 0 and lines[prior_start - 1].strip():
        prior_start -= 1
    candidate = "\n".join(lines[prior_start : prior + 1])
    if any(MARKDOWN_LIST_ITEM.match(line) for line in candidate.splitlines()):
        return _explicit_weakening_lead_in(candidate)
    return bool(STANDALONE_ENGINEERING_WEAKENING.search(candidate))


def engineering_discipline_issues(agents: str, claude: str) -> list[str]:
    """Keep the concise engineering-discipline contract equivalent."""
    issues: list[str] = []
    for source, text in (("AGENTS.md", agents), ("CLAUDE.md", claude)):
        matches = list(ENGINEERING_DISCIPLINE_PATTERN.finditer(text))
        weakened_context = False
        if len(matches) == 1:
            weakened_context = _engineering_discipline_is_weakened(
                text, matches[0].start()
            )
        if len(matches) != 1:
            issues.append(
                f"{source} must contain one exact positive engineering discipline contract"
            )
        elif weakened_context:
            issues.append(
                f"{source} engineering discipline contract is weakened by its item or lead-in"
            )
    return issues


def finalization_parity_issues(
    implementation_plan: str,
    dispatch: str,
    review: str,
    agents: str,
    claude: str,
    runtime: str,
    memory: str,
) -> list[str]:
    """Keep bounded-finalization and ephemeral execution guidance aligned."""
    issues: list[str] = []
    required_by_source = {
        "skills/implementation-plan": (
            "FinalizationLedger",
            "cycles 1–5",
            "sixth reservation",
            "standalone `review --deep`",
            "finalization_policy",
        ),
        "skills/dispatch": (
            "code-owned ephemeral adapter",
            "subscription preflight",
            "fixed argv",
            "schema validation",
            "durable receipts",
            "continuable",
            "arbitrary direct print-mode",
        ),
        "skills/review": (
            "Standalone Deep",
            "finalization-primary",
            "finalization-independent",
            "cycles 1–3",
            "cycles 4–5",
            "explicit single-model",
            "fresh exact-HEAD attempt",
            "sixth cycle",
        ),
        "AGENTS.md": (
            "D-265-EPH-01",
            "code-owned ephemeral adapter",
            "subscription preflight",
            "arbitrary direct print-mode",
        ),
        "CLAUDE.md": (
            "D-265-EPH-01",
            "code-owned ephemeral adapter",
            "subscription preflight",
            "arbitrary direct print-mode",
            "но не отправляет input",
        ),
        "docs/runtime-capabilities.md": (
            "D-265-EPH-01",
            "code-owned ephemeral adapter",
            "fixed provider-specific argv",
            "schema-validated result",
            "durable receipts",
        ),
        ".claude-memory/feedback_no_claude_p_headless.md": (
            "D-265-EPH-01",
            "supersedes",
            "code-owned ephemeral adapter",
            "arbitrary direct print-mode",
        ),
    }
    texts = {
        "skills/implementation-plan": implementation_plan,
        "skills/dispatch": dispatch,
        "skills/review": review,
        "AGENTS.md": agents,
        "CLAUDE.md": claude,
        "docs/runtime-capabilities.md": runtime,
        ".claude-memory/feedback_no_claude_p_headless.md": memory,
    }
    for source, required in required_by_source.items():
        normalized = " ".join(texts[source].split()).lower()
        for value in required:
            if value.lower() not in normalized:
                issues.append(f"{source} missing finalization parity invariant {value!r}")
    return issues


def check_repo(root: Path) -> list[str]:
    issues: list[str] = []
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    issues.extend(engineering_discipline_issues(agents, claude))
    repair_reference_path = root / "docs" / "skill-references" / "failure-repair-contract.md"
    repair_reference = repair_reference_path.read_text(encoding="utf-8") if repair_reference_path.is_file() else ""
    if not repair_reference:
        issues.append("missing docs/skill-references/failure-repair-contract.md")
    issues.extend(
        failure_repair_issues(
            claude,
            agents,
            (root / "skills" / "dispatch" / "references" / "task-prompt-template.md").read_text(encoding="utf-8"),
            (root / "scripts" / "task_escalation.py").read_text(encoding="utf-8"),
            repair_reference,
        )
    )
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
    issues.extend(legacy_skill_issues(root))
    memory_path = root / ".claude-memory" / "feedback_no_claude_p_headless.md"
    issues.extend(
        finalization_parity_issues(
            (root / "skills" / "implementation-plan" / "SKILL.md").read_text(encoding="utf-8"),
            (root / "skills" / "dispatch" / "SKILL.md").read_text(encoding="utf-8"),
            (root / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8"),
            agents,
            claude,
            (root / "docs" / "runtime-capabilities.md").read_text(encoding="utf-8"),
            memory_path.read_text(encoding="utf-8") if memory_path.is_file() else "",
        )
    )

    ingest = (root / "skills" / "wiki-ingest" / "SKILL.md").read_text(encoding="utf-8")
    for forbidden in ("Use PATCH", "Save to `.raw", "Write the updated manifest back"):
        if forbidden in ingest:
            issues.append(f"wiki-ingest contains stale instruction: {forbidden}")
    if "expected_sha256" not in ingest or "manifest_update" not in ingest:
        issues.append("wiki-ingest must describe optimistic full-content/manifest writes")
    normalization_ref_path = root / "skills" / "wiki-ingest" / "references" / "document-normalization.md"
    if normalization_ref_path.is_file():
        normalization_ref = normalization_ref_path.read_text(encoding="utf-8")
    else:
        normalization_ref = ""
        issues.append("wiki-ingest is missing references/document-normalization.md")
    for required in (
        "scripts/document-normalize.py normalize",
        "needs_user_action",
        "needs_semantic_cleanup",
        "explicit user confirmation",
        "enable_remote_services=False",
    ):
        if required not in ingest and required not in normalization_ref:
            issues.append(f"wiki-ingest document normalization missing invariant {required!r}")

    normalizer = (root / "scripts" / "document-normalize.py").read_text(encoding="utf-8")
    adapter = (root / "scripts" / "docling-adapter.py").read_text(encoding="utf-8")
    document_contract = normalizer + "\n" + adapter
    for required in (
        "enable_remote_services=False",
        "allow_external_plugins=False",
        '"HF_HUB_OFFLINE": "1"',
        "download_enabled=False",
        "page_range=(start, end)",
    ):
        if required not in document_contract:
            issues.append(f"document normalizer missing isolation invariant {required!r}")
    document_tools = json.loads((root / "config" / "document-tools.json").read_text(encoding="utf-8"))
    if document_tools.get("docling", {}).get("ocr_languages") != ["ru", "en"]:
        issues.append("document normalizer must pin ru/en OCR languages")

    for path in sorted((root / "skills").glob("*/SKILL.md")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if '"hot_bullet"' in line and "c-NNNNNN" not in line:
                issues.append(f"{path.relative_to(root)}:{line_no}: hot_bullet example lacks c-NNNNNN")

    runtime_doc = (root / "docs" / "runtime-capabilities.md").read_text(encoding="utf-8")
    if re.search(r"\| `SessionStart`[^\n]*\| Not provided by this plugin \|", runtime_doc):
        issues.append("runtime capabilities still claim Codex hooks are unavailable")
    # Personal vaults may keep an additional provider comparison, but the
    # public template intentionally does not require private learning notes.
    comparison_path = root / "wiki" / "learning" / "Anthropic vs OpenAI.md"
    if comparison_path.is_file():
        comparison = comparison_path.read_text(encoding="utf-8")
        if "hooks-аналога нет" in comparison:
            issues.append("learning comparison still says Codex has no hooks")

    research = (
        root / "scripts" / "harness" / "workflows" / "research.py"
    ).read_text(encoding="utf-8")
    for required in ("stage == 'fetch' else 'disabled'", "UNTRUSTED DATA", "codex-home-"):
        if required not in research:
            issues.append(f"research isolation missing invariant {required!r}")
    review = (root / "scripts" / "review-runner.py").read_text(encoding="utf-8")
    review += "\n" + (root / "scripts" / "harness" / "adapters" / "claude.py").read_text(encoding="utf-8")
    review += "\n" + (root / "scripts" / "harness" / "adapters" / "codex.py").read_text(encoding="utf-8")
    if "--permission-mode auto" in review:
        issues.append("review launcher regressed to Claude auto permissions")
    for required in (
        '"reviewer-callback": ("workspace-write", "never")',
        "REVIEWER_CONFIG",
        '"sandbox_workspace_write.network_access=false"',
        '"sandbox_workspace_write.writable_roots=[]"',
        "validate_reviewer_sandbox_command",
        '"--ask-for-approval"',
        "review runtime scratch must be disjoint from the product worktree",
    ):
        if required not in review:
            issues.append(f"Codex reviewer missing isolated session invariant {required!r}")
    for required in (
        '"reviewer-callback": "dontAsk"',
        "reviewer_sandbox_settings",
        "review sandbox tool surface drifted",
        '"--strict-mcp-config"',
        '"--disable-slash-commands"',
        "hand-write the generated callback",
    ):
        if required not in review:
            issues.append(f"Claude reviewer missing unattended callback invariant {required!r}")
    for forbidden in (
        "Bash(python3 */", "Bash(bash */", "Bash(git diff *)", "Bash(git -C *",
        "Bash(python3 *send_review.py",
    ):
        if forbidden in review:
            issues.append(f"Claude reviewer has a broad shell wildcard {forbidden!r}")
    dispatch_text = (root / "skills" / "dispatch" / "SKILL.md").read_text(encoding="utf-8")
    reap_text = (root / "skills" / "reap" / "SKILL.md").read_text(encoding="utf-8")
    for required in (
        "interaction_policy",
        "approved_plan_sha256",
        "forbidden_actions",
        "watchdog_policy",
        "generic provider runtime",
    ):
        if required not in dispatch_text:
            issues.append(f"dispatch missing unattended contract invariant {required!r}")
    for required in (
        "-a never", "workspace-write", "code-owned provider runtime",
        "localhost", "cmux-socket policy", "classic compatibility",
        "not this generic harness path",
    ):
        if required not in dispatch_text:
            issues.append(f"Codex dispatch missing unattended approval invariant {required!r}")
    active_runtime = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "scripts/harness/adapters/cmux.py",
            "scripts/harness/adapters/codex.py",
            "scripts/harness/adapters/process.py",
            "scripts/harness/runtime_session_cleanup.py",
            "scripts/harness/supervisor.py",
        )
    )
    for required in (
        "task_codex_config_values",
        "subprocess.run",
        "request_exit",
        "begin_effect",
        "resolve_effect",
    ):
        if required not in active_runtime:
            issues.append(f"active harness missing lifecycle invariant {required!r}")
    if "shell=True" in active_runtime:
        issues.append("active harness must not execute agent commands through a shell")
    task_prompt = (root / "skills" / "dispatch" / "references" / "task-prompt-template.md").read_text(encoding="utf-8")
    for required in ("task_escalation.py", "Treat `.task-meta.json` as read-only", "Never push, deploy, publish", "it never sends you input"):
        if required not in task_prompt:
            issues.append(f"dispatch task prompt missing safety invariant {required!r}")
    for required in (
        ".task-summary.json", "internal callback broker", "coordinator owns",
    ):
        if required not in task_prompt:
            issues.append(f"dispatch task prompt missing internal handoff invariant {required!r}")
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
