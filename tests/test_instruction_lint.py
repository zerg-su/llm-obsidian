#!/usr/bin/env python3
"""Tests for canonical pipeline instruction drift linting."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lint-instructions.py"
spec = importlib.util.spec_from_file_location("instruction_lint_test", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

assert module.check_repo(ROOT) == []
print("OK   repository instructions align")
bad = "---\nname: research\nallowed-tools: Read WebSearch WebFetch\n---\n"
assert module.protected_tool_issues("research", bad)
print("OK   protected web-tool regression detected")

bad_writer = "---\nname: daily\nallowed-tools: Read Write Edit Bash\n---\nmkdir -p \"$DIR\"\nwrite lines under `## Сделано`\n"
issues = module.writer_path_issues("daily", bad_writer)
assert any("Write/Edit" in issue for issue in issues)
assert any("vault-write.py" in issue for issue in issues)
assert any("direct wiki" in issue for issue in issues)
print("OK   direct wiki mutation regression detected")

bad_daily = "On Claude or when that custom agent is unavailable, produce the same JSON in the parent"
issues = module.daily_runtime_issues(bad_daily)
assert any("Agent tool" in issue for issue in issues)
assert any("runtime invariant" in issue for issue in issues)
assert any("parent fallback" in issue for issue in issues)
assert any("detect-runtime.sh --three-way" in issue for issue in issues)
print("OK   Claude subscription fallback regression detected")

with tempfile.TemporaryDirectory(prefix="instruction-lint-test.") as raw:
    assert module.daily_runtime_repo_issues(Path(raw)) == []
print("OK   missing daily skill handled without traceback")

with tempfile.TemporaryDirectory(prefix="instruction-lint-test.") as raw:
    root = Path(raw)
    cache = root / "skills" / "reap-send" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "send_reap.cpython-313.pyc").write_bytes(b"legacy bytecode")
    assert module.legacy_skill_issues(root) == []
    (root / "skills" / "reap-send" / "send_reap.py").write_text(
        "raise SystemExit('legacy source')\n",
        encoding="utf-8",
    )
    assert module.legacy_skill_issues(root)
print("OK   legacy skill lint ignores bytecode cache but rejects source")

issues = module.failure_repair_issues("", "", "", "", "")
assert any("CLAUDE.md" in issue for issue in issues)
assert any("AGENTS.md" in issue for issue in issues)
assert any("dispatch task prompt" in issue for issue in issues)
assert any("mechanism-failure category" in issue for issue in issues)
assert any("reference missing" in issue for issue in issues)
print("OK   failure-repair auto-repair boundary drift detected")

issues = module.finalization_parity_issues("", "", "", "", "", "", "")
assert any("implementation-plan" in issue for issue in issues)
assert any("dispatch" in issue for issue in issues)
assert any("review" in issue for issue in issues)
assert any("AGENTS.md" in issue for issue in issues)
assert any("CLAUDE.md" in issue for issue in issues)
assert any("runtime-capabilities.md" in issue for issue in issues)
assert any("feedback_no_claude_p_headless.md" in issue for issue in issues)
print("OK   finalization and ephemeral policy drift detected")

discipline = (
    "Engineering discipline: think before coding; simplicity first; surgical "
    "changes; goal/evidence discipline—local green is not task completion."
)
assert module.engineering_discipline_issues(discipline, discipline) == []
assert module._enclosing_markdown_item(discipline, 0) == discipline
bullet_contract = f"- {discipline}\n  Continuation without weakening."
assert module._enclosing_markdown_item(bullet_contract, 2) == bullet_contract
lazy_contract = f"- Treat the next rule as advisory.\n{discipline}"
assert module._enclosing_markdown_item(
    lazy_contract, lazy_contract.index(discipline)
) == lazy_contract
standalone_lead_in = f"The rule below is optional.\n\n{discipline}"
assert module._enclosing_markdown_item(
    standalone_lead_in, standalone_lead_in.index(discipline)
) == standalone_lead_in
sibling_contract = f"- Unrelated behavior is advisory.\n- {discipline}"
assert module._enclosing_markdown_item(
    sibling_contract, sibling_contract.index(discipline)
) == f"- {discipline}"
for surface in ("AGENTS.md", "CLAUDE.md"):
    for principle in (
        "think before coding",
        "simplicity first",
        "surgical changes",
        "goal/evidence discipline",
        "local green is not task completion",
    ):
        weakened = discipline.replace(principle, "weakened")
        issues = module.engineering_discipline_issues(
            weakened if surface == "AGENTS.md" else discipline,
            weakened if surface == "CLAUDE.md" else discipline,
        )
        assert any(surface in issue for issue in issues), (
            surface,
            principle,
            issues,
        )
for drift in (
    "Do not think before coding; simplicity first is optional.",
    discipline + "\n" + discipline,
    discipline.replace("Engineering discipline:", "Engineering suggestions:"),
):
    assert module.engineering_discipline_issues(drift, discipline)
for surface in ("AGENTS.md", "CLAUDE.md"):
    for drift in (
        f"Optional {discipline}",
        f"Ignore this: {discipline}",
        f"{discipline} Unless inconvenient.",
    ):
        issues = module.engineering_discipline_issues(
            drift if surface == "AGENTS.md" else discipline,
            drift if surface == "CLAUDE.md" else discipline,
        )
        assert any(surface in issue for issue in issues), (surface, drift, issues)
    for drift in (
        f"Optional:\n{discipline}",
        f"Ignore this:\n{discipline}",
        f"Suggestions only:\n\n{discipline}",
        f"Optional policy context.\nStill the same paragraph.\nThird line.\n{discipline}",
        f"{discipline}\nThis contract is optional.",
    ):
        issues = module.engineering_discipline_issues(
            drift if surface == "AGENTS.md" else discipline,
            drift if surface == "CLAUDE.md" else discipline,
        )
        assert any(surface in issue for issue in issues), (surface, drift, issues)
    sibling = f"- Unrelated behavior is advisory unless requested.\n- {discipline}"
    issues = module.engineering_discipline_issues(
        sibling if surface == "AGENTS.md" else discipline,
        sibling if surface == "CLAUDE.md" else discipline,
    )
    assert not any(surface in issue for issue in issues), (surface, sibling, issues)
    for lead_in in (
        "This whole section is advisory only and may be ignored.",
        "The rule below is optional for small changes.",
        "- Treat the next rule as advisory.",
    ):
        separator = "\n" if lead_in.startswith("- ") else "\n\n"
        drift = lead_in + separator + discipline
        issues = module.engineering_discipline_issues(
            drift if surface == "AGENTS.md" else discipline,
            drift if surface == "CLAUDE.md" else discipline,
        )
        assert any(surface in issue for issue in issues), (surface, drift, issues)
print("OK   cross-surface engineering discipline weakening detected")

defuddle = (ROOT / "skills" / "defuddle" / "SKILL.md").read_text(encoding="utf-8")
assert "manual fallback" in defuddle
assert "never describe raw" in defuddle
assert "copyright/footer" in defuddle
print("OK   defuddle fallback requires actual cleanup")

dispatch = (ROOT / "skills" / "dispatch" / "SKILL.md").read_text(encoding="utf-8")
dispatch_runner = (ROOT / "scripts" / "dispatch-runner.py").read_text(encoding="utf-8")
dispatch_execution = (ROOT / "scripts" / "dispatch_execution.py").read_text(encoding="utf-8")
dispatch_workspace = (ROOT / "scripts" / "dispatch_workspace.py").read_text(encoding="utf-8")
assert "dispatch-runner.py start --spec" in dispatch
assert "CMUX_SURFACE_ID" in dispatch
assert "materialize_current_context" in dispatch_runner
assert 'origin_surface=request["origin_surface"]' in dispatch_execution
assert "identify --surface \"$CMUX_SURFACE_ID\" --no-caller" not in dispatch
assert "never inspects the globally focused surface" in dispatch
assert "awk '/^\\*/" not in dispatch
assert "verify that its exact target exists under `wiki/`" in dispatch
assert "reap type/title/`plan_mode`" in dispatch
assert '"sync-config", "--apply"' in dispatch_workspace
assert dispatch.count("\n") + 1 <= 500
print("OK   dispatch delegates anchored mechanics to typed runner")

task_prompt = (ROOT / "skills/dispatch/references/task-prompt-template.md").read_text(encoding="utf-8")
assert ".task-summary.json" in task_prompt
assert "internal callback broker" in task_prompt
assert module.legacy_skill_issues(ROOT) == []
print("OK   task summary delegates to the internal callback broker")

vault_repair = (ROOT / "skills/vault-repair/SKILL.md").read_text(encoding="utf-8")
assert "TODO" not in vault_repair
for required in (
    "$llm-obsidian:vault-repair",
    "/vault-repair",
    ".vault-meta/stop-hook-last.log",
    "python3 scripts/vault-write.py --recover",
    "python3 scripts/validate-vault.py --summary",
    "LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1 ./.claude/hooks/stop.sh",
    "git rev-parse HEAD",
    "no-change",
    "one repair attempt",
):
    assert required in vault_repair, required
for forbidden in ("git reset", "git stash", "git add .", "curl ", "wget "):
    assert forbidden not in vault_repair, forbidden
print("OK   vault repair stays bounded and reuses the Stop pipeline")

print("\nAll instruction lint tests passed.")
