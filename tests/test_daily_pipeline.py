#!/usr/bin/env python3
"""Hermetic daily evidence, validation, and atomic apply tests."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import daily_contract


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def fixture_payloads(date: str = "2026-07-10") -> tuple[dict, dict]:
    evidence = {
        "schema_version": 1,
        "date": date,
        "generated_at": f"{date}T18:00:00+00:00",
        "bundle_id": "",
        "items": [
            {"id": "session:001", "kind": "session", "title": "Wiki pipeline", "text": "Wiki pipeline gained atomic writes and faster retrieval."},
            {"id": "git:001", "kind": "git", "title": "retrieval quality", "text": "retrieval quality gates now pass."},
        ],
        "session_map": [
            {
                "session_id": "45deeb96-5035-4bca-9dee-2033c84ce911",
                "label": "Claude research",
                "runtime": "claude",
            },
            {
                "session_id": "019f0000-0000-7000-8000-000000000001",
                "label": "Wiki pipeline",
                "runtime": "codex",
            },
        ],
    }
    summary = {
        "schema_version": 1,
        "date": date,
        "evidence_bundle_id": "",
        "bullets": [
            {"subject": "Wiki pipeline", "outcome": "укрепил атомарную запись и ускорил завершение рабочих сессий.", "compact": "атомарная запись и быстрый Stop", "evidence_ids": ["session:001"]},
            {"subject": "retrieval quality", "outcome": "добавил измеримые quality gates перед изменением ранжирования.", "compact": "добавил quality gates", "evidence_ids": ["git:001"]},
        ],
        "session_labels": [],
    }
    canonical = json.dumps(
        {"date": date, "items": evidence["items"], "session_map": evidence["session_map"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence["bundle_id"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    summary["evidence_bundle_id"] = evidence["bundle_id"]
    return evidence, summary


def copy_runtime(root: Path) -> None:
    (root / "scripts").mkdir(parents=True)
    (root / "_templates").mkdir()
    for name in (
        "daily_contract.py", "daily_timing.py", "daily-apply.py", "daily-collect.py",
        "daily-summary-save.py", "journal-write.py", "vault-write.py",
        "plan_lifecycle.py", "vault_schema.py", "pipeline_events.py", "session-map.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copy2(ROOT / "_templates" / "daily.md", root / "_templates" / "daily.md")
    helper = root / "scripts" / "current-session-id.sh"
    helper.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"${CODEX_THREAD_ID:-unknown}\"\n", encoding="utf-8")
    helper.chmod(0o755)


def run(root: Path, script: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, LLM_OBSIDIAN_ROOT=str(root), CODEX_THREAD_ID="019f0000-0000-7000-8000-000000000001")
    return subprocess.run([sys.executable, str(root / "scripts" / script), *args], cwd=root, env=env, text=True, input=stdin, capture_output=True)


evidence, summary = fixture_payloads()
daily_contract.validate_summary(summary, evidence)
check("valid summary contract", True)
bad = json.loads(json.dumps(summary))
bad["bullets"][0]["outcome"] += " commit deadbee"
try:
    daily_contract.validate_summary(bad, evidence)
except daily_contract.DailyContractError:
    check("hash leakage rejected", True)
else:
    check("hash leakage rejected", False)
bad = json.loads(json.dumps(summary))
bad["bullets"][0]["subject"] = "Unrelated payroll"
try:
    daily_contract.validate_summary(bad, evidence)
except daily_contract.DailyContractError:
    check("ungrounded subject rejected", True)
else:
    check("ungrounded subject rejected", False)
bad = json.loads(json.dumps(summary))
bad["evidence_bundle_id"] = "sha256:" + "0" * 64
try:
    daily_contract.validate_summary(bad, evidence)
except daily_contract.DailyContractError:
    check("mismatched evidence bundle rejected", True)
else:
    check("mismatched evidence bundle rejected", False)
for label, outcome in (
    ("multiline summary rejected", "укрепил pipeline.\n## Injected"),
    ("Markdown summary rejected", "укрепил [[Unknown page]] без проверки."),
    ("repository path rejected", "укрепил scripts/stop-hook.py без утечки деталей."),
):
    bad = json.loads(json.dumps(summary))
    bad["bullets"][0]["outcome"] = outcome
    try:
        daily_contract.validate_summary(bad, evidence)
    except daily_contract.DailyContractError:
        check(label, True)
    else:
        check(label, False)

spec = importlib.util.spec_from_file_location("daily_collect_test", ROOT / "scripts/daily-collect.py")
daily_collect = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(daily_collect)
original_run = daily_collect.subprocess.run
daily_collect.subprocess.run = lambda *args, **kwargs: subprocess.CompletedProcess(
    args=args, returncode=0, stdout="wiki: hot, log, _index\nfeat: meaningful pipeline change\n", stderr=""
)
try:
    git_rows = daily_collect.collect_git("2026-07-10")
finally:
    daily_collect.subprocess.run = original_run
check("collector filters automatic wiki commits", [item["title"] for item in git_rows] == ["feat: meaningful pipeline change"])

with tempfile.TemporaryDirectory(prefix="daily-pipeline-test.") as raw:
    root = Path(raw)
    copy_runtime(root)
    (root / ".vault-meta").mkdir()
    (root / "wiki/meta/sessions").mkdir(parents=True)
    (root / "wiki/meta/sessions/Fixture.md").write_text(
        "---\ntype: session\ntitle: \"Fixture session\"\ncreated: 2026-07-10\nupdated: 2026-07-10\ntags: [test]\nstatus: resolved\nsessions: []\n---\n\n# Fixture\n\nCompleted the fixture pipeline.\n",
        encoding="utf-8",
    )
    (root / "wiki/log.md").write_text(
        "---\ntype: meta\ntitle: \"Log\"\ncreated: 2026-07-10\nupdated: 2026-07-10\ntags: [meta]\nstatus: evergreen\nsessions: []\n---\n\n# Log\n\n## [2026-07-10] test | Fixture work\n\nCompleted fixture work.\n",
        encoding="utf-8",
    )
    (root / "scripts/session-map.py").write_text(
        "import json\n"
        "print(json.dumps({'date': '2026-07-10', 'sessions': ["
        "{'session': '019f0000-0000-7000-8000-000000000001', "
        "'label': 'Fixture session', 'runtime': 'codex'}]}))\n",
        encoding="utf-8",
    )
    collected_file = root / ".vault-meta" / "collected.json"
    result = run(root, "daily-collect.py", "--date", "2026-07-10", "--output", str(collected_file))
    collected = json.loads(collected_file.read_text(encoding="utf-8")) if collected_file.is_file() else {}
    check("collector finds grounded sources", result.returncode == 0 and {item["kind"] for item in collected["items"]} >= {"session", "log"}, result.stderr)
    check("collector session map parsed", collected["session_map"][0]["label"] == "Fixture session")
    check("collector session runtime parsed", collected["session_map"][0]["runtime"] == "codex")
    event_log = root / ".vault-meta" / "pipeline-events.jsonl"
    events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    collect_event = next(item for item in events if item["op"] == "daily-collect")
    check("collector emits numeric timing", isinstance(collect_event["counts"].get("duration_ms"), (int, float)))
    check("collector timing is content-free", "Fixture session" not in json.dumps(collect_event))
    evidence_file = root / ".vault-meta" / "evidence.json"
    summary_file = root / ".vault-meta" / "summary.json"
    evidence_file.write_text(json.dumps(evidence), encoding="utf-8")
    summary_file.write_text(json.dumps(summary), encoding="utf-8")

    result = run(root, "daily-apply.py", "--evidence", str(evidence_file), "--input", str(summary_file), "--no-clipboard")
    check("daily apply succeeds", result.returncode == 0, result.stderr)
    daily = root / "wiki/daily/2026/07/2026-07-10.md"
    status_log = root / "wiki/routines/Daily Status Log.md"
    check("daily page created", daily.is_file())
    check("status log created", status_log.is_file())
    check("full bullets written", "укрепил атомарную" in daily.read_text(encoding="utf-8"))
    daily_text = daily.read_text(encoding="utf-8")
    check("session map written", "019f0000-0000" in daily_text)
    check(
        "session map grouped by runtime",
        "### Claude\n\n- Claude research" in daily_text
        and "### Codex\n\n- Wiki pipeline" in daily_text
        and daily_text.index("### Claude") < daily_text.index("### Codex"),
    )
    events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    run_event = next(item for item in events if item["op"] == "daily-run" and item["status"] == "ok")
    check("daily run emits post-collection timing", run_event["counts"]["duration_ms"] >= run_event["counts"]["apply_ms"] >= 0)
    check("daily run timing names written paths only", run_event["paths"] == ["wiki/daily/2026/07/2026-07-10.md", "wiki/routines/Daily Status Log.md"])

    summary["bullets"][0]["outcome"] = "укрепил атомарную запись; повторный запуск заменил прежний текст без дубля."
    summary_file.write_text(json.dumps(summary), encoding="utf-8")
    result = run(root, "daily-apply.py", "--evidence", str(evidence_file), "--input", str(summary_file), "--no-clipboard")
    check("daily rerun succeeds", result.returncode == 0, result.stderr)
    check("date block upserted once", status_log.read_text(encoding="utf-8").count("### 2026-07-10") == 1)
    text = daily.read_text(encoding="utf-8")
    check("old daily text replaced", "повторный запуск" in text and "ускорил завершение" not in text)

    later_evidence, later_summary = fixture_payloads("2026-07-11")
    later_evidence_file = root / ".vault-meta/later-evidence.json"
    later_summary_file = root / ".vault-meta/later-summary.json"
    later_evidence_file.write_text(json.dumps(later_evidence), encoding="utf-8")
    later_summary_file.write_text(json.dumps(later_summary), encoding="utf-8")
    result = run(root, "daily-apply.py", "--evidence", str(later_evidence_file), "--input", str(later_summary_file), "--no-clipboard")
    check("later date applies", result.returncode == 0, result.stderr)
    result = run(root, "daily-apply.py", "--evidence", str(evidence_file), "--input", str(summary_file), "--no-clipboard")
    status_text = status_log.read_text(encoding="utf-8")
    check("backdated rerun keeps newest-first order", result.returncode == 0 and status_text.index("### 2026-07-11") < status_text.index("### 2026-07-10"), result.stderr)
    check("backdated rerun preserves last_done", "last_done: 2026-07-11" in status_text)
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    check("backdated rerun keeps mutation date", f"updated: {today}" in status_text)

    before = daily.read_text(encoding="utf-8")
    status_log.write_text(status_log.read_text(encoding="utf-8").replace("## Журнал", "## Broken"), encoding="utf-8")
    result = run(root, "daily-apply.py", "--evidence", str(evidence_file), "--input", str(summary_file), "--no-clipboard")
    check("malformed second target rejected", result.returncode == 3)
    check("no partial first-target write", daily.read_text(encoding="utf-8") == before)

    private = root / ".vault-meta" / "private-summary.json"
    result = run(root, "daily-summary-save.py", "--evidence", str(evidence_file), "--output", str(private), stdin=json.dumps(summary))
    check("summary save succeeds", result.returncode == 0, result.stderr)
    check("summary artifact mode 0600", stat.S_IMODE(private.stat().st_mode) == 0o600)
    events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    synthesis_event = next(item for item in events if item["op"] == "daily-synthesis" and item["status"] == "ok")
    check("synthesis emits model-phase timing", synthesis_event["counts"]["duration_ms"] >= synthesis_event["counts"]["script_ms"] >= 0)

agent = (ROOT / ".codex/agents/daily-summarizer.toml").read_text(encoding="utf-8")
agent_config = tomllib.loads(agent)
mcp_servers = agent_config.get("mcp_servers", {})
check("daily model is not hardcoded", "\nmodel = " not in agent)
check("daily medium effort pinned", 'model_reasoning_effort = "medium"' in agent)
check("daily agent read-only", 'sandbox_mode = "read-only"' in agent and 'web_search = "disabled"' in agent)
check("daily agent hooks disabled", 'hooks = false' in agent and 'approval_policy = "never"' in agent)
check(
    "daily Codex agent pins the exact summary object shape",
    '"schema_version": 1' in agent
    and '"bullets": [' in agent
    and '"evidence_ids": ["session:001"]' in agent
    and "never strings" in agent,
)
check(
    "daily Codex agent rejects the observed legacy shape",
    'schema_version as "daily-summary-v1"' in agent
    and all(field in agent for field in ("headline", "completed", "source_ids")),
)
daily_skill = (ROOT / "skills/daily/SKILL.md").read_text(encoding="utf-8")
check(
    "daily Codex delegation stays in the native agent tool",
    "through built-in Agent" in daily_skill
    and "never shell out to Codex" in daily_skill,
)
check(
    "daily Codex invalid JSON reuses the same agent thread",
    "paste" in daily_skill
    and "correct once in the same agent thread" in daily_skill
    and "validator error" in daily_skill
    and "never spawn/fallback" in daily_skill,
)
check(
    "disabled MCP servers retain valid transports",
    bool(mcp_servers)
    and all(
        config.get("enabled") is False and bool(config.get("url")) != bool(config.get("command"))
        for config in mcp_servers.values()
    ),
)

print("\nAll daily pipeline tests passed.")
