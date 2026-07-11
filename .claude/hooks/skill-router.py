#!/usr/bin/env python3
"""
Skill router for claude-obsidian.

Reads UserPromptSubmit hook payload from stdin (JSON), matches the prompt
against .claude/skill-rules.json (skills + agents), and prints soft-suggest
hints to stdout. Tone is intentionally non-mandatory — Claude decides whether
to honor the hint. See memory rule `feedback_router_tone_soft`.

Behavior:
  - Exits 0 even on no-match (empty stdout = no hint).
  - Up to 3 skill candidates + up to 2 agent candidates, ranked by number
    of distinct patterns matched.
  - SKILL_ROUTER_MUTE=1 → no-op (empty stdout).
  - Logs content-free match counters to .vault-meta/router-hits.jsonl.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


REPO_ROOT = Path(
    os.environ.get("LLM_OBSIDIAN_PROJECT_ROOT")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[2]
).resolve()
RULES_PATH = REPO_ROOT / ".claude" / "skill-rules.json"
LOG_PATH = REPO_ROOT / ".vault-meta" / "router-hits.jsonl"

MAX_SKILL_HINTS = 3
MAX_AGENT_HINTS = 3


def load_rules() -> dict:
    if not RULES_PATH.exists():
        return {"skill_rules": [], "agent_rules": []}
    try:
        return json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"skill_rules": [], "agent_rules": []}


def match_rules(prompt: str, rules: list[dict], key: str) -> list[dict]:
    matched: list[dict] = []
    for rule in rules:
        hits = 0
        for pattern in rule.get("patterns", []):
            try:
                if re.search(pattern, prompt):
                    hits += 1
            except re.error:
                continue
        if hits > 0:
            matched.append({"name": rule[key], "hint": rule.get("hint"), "hits": hits})
    matched.sort(key=lambda m: m["hits"], reverse=True)
    return matched


def format_skill_hint(matches: list[dict]) -> str:
    if not matches:
        return ""
    lines = []
    for m in matches[:MAX_SKILL_HINTS]:
        suffix = f" — {m['hint']}" if m.get("hint") else ""
        lines.append(f'  Skill("{m["name"]}"){suffix}')
    return (
        "Hint: this prompt seems to match the following skill(s). "
        "Consider invoking if it fits; ignore otherwise.\n" + "\n".join(lines)
    )


def format_agent_hint(matches: list[dict]) -> str:
    if not matches:
        return ""
    lines = []
    for m in matches[:MAX_AGENT_HINTS]:
        suffix = f" — {m['hint']}" if m.get("hint") else ""
        lines.append(f'  Agent("{m["name"]}"){suffix}')
    return (
        "Hint: this prompt also seems in scope of the following sub-agent(s). "
        "Consider delegating via Task/Agent tool if deep audit is needed.\n"
        + "\n".join(lines)
    )


def runtime_name() -> str:
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"):
        return "codex"
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    return "unknown"


def log_hit(payload: dict, skill_matches: list[dict], agent_matches: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    session = (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or payload.get("session_id")
        or payload.get("sessionId")
        or "unknown"
    )
    record = {
        "ts": int(time.time()),
        "runtime": runtime_name(),
        "session": str(session),
        "skill_matches": [
            {"name": m["name"], "hits": m["hits"]} for m in skill_matches
        ],
        "agent_matches": [
            {"name": m["name"], "hits": m["hits"]} for m in agent_matches
        ],
    }
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    if os.environ.get("SKILL_ROUTER_MUTE") == "1":
        return 0

    raw = sys.stdin.read()
    if not raw.strip():
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"prompt": raw}

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0

    rules = load_rules()
    skill_matches = match_rules(prompt, rules.get("skill_rules", []), "skill")
    agent_matches = match_rules(prompt, rules.get("agent_rules", []), "agent")

    log_hit(payload, skill_matches, agent_matches)

    output_parts = []
    skill_hint = format_skill_hint(skill_matches)
    if skill_hint:
        output_parts.append(skill_hint)
    agent_hint = format_agent_hint(agent_matches)
    if agent_hint:
        output_parts.append(agent_hint)

    if output_parts:
        print("\n\n".join(output_parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
