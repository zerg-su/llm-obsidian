#!/usr/bin/env python3
"""Hermetic agenda scan/collect/report contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSION = "019f0000-0000-7000-8000-000000000002"
sys.path.insert(0, str(ROOT / "scripts"))

from daily_contract import parse_daily_task, task_open_line  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def fixture(parent: Path, name: str) -> Path:
    root = parent / name
    (root / "scripts").mkdir(parents=True)
    (root / "_templates").mkdir()
    (root / ".vault-meta").mkdir()
    for filename in (
        "agenda.py",
        "daily_contract.py",
        "journal-write.py",
        "vault-write.py",
        "plan_lifecycle.py",
        "vault_schema.py",
        "pipeline_events.py",
    ):
        shutil.copy2(ROOT / "scripts" / filename, root / "scripts" / filename)
    shutil.copy2(ROOT / "_templates/daily.md", root / "_templates/daily.md")
    helper = root / "scripts/current-session-id.sh"
    helper.write_text("#!/usr/bin/env bash\necho \"${CODEX_THREAD_ID:-unknown}\"\n", encoding="utf-8")
    helper.chmod(0o755)
    return root


def run(root: Path, script: str, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, LLM_OBSIDIAN_ROOT=str(root), CODEX_THREAD_ID=SESSION)
    return subprocess.run(
        [sys.executable, str(root / "scripts" / script), *args],
        cwd=root,
        env=env,
        text=True,
        input=input_text,
        capture_output=True,
    )


def journal(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(root, "journal-write.py", *args)


def agenda(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(root, "agenda.py", *args)


def daily_path(root: Path, date: str) -> Path:
    return root / f"wiki/daily/{date[:4]}/{date[5:7]}/{date}.md"


def replace_section(path: Path, heading: str, lines: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    parts = text.splitlines()
    start = parts.index(heading) + 1
    end = next((index for index in range(start, len(parts)) if parts[index].startswith("## ")), len(parts))
    parts[start:end] = ["", *lines, ""]
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="agenda-test.") as raw:
    parent = Path(raw)

    metadata_line = (
        "- [/] Metadata task ⏫ ➕ 2026-07-01 🛫 2026-07-02 ⏳ 2026-07-03 "
        "📅 2026-07-04 🔁 every week 🆔 task-1 ⛔ 2026-07-05 🏁 2026-07-06 "
        "^agenda-abcdef123456"
    )
    metadata_task = parse_daily_task(
        metadata_line, date="2026-07-01", section="plans", line_no=1
    )
    check("Tasks metadata parses", metadata_task is not None)
    assert metadata_task is not None
    check(
        "Tasks metadata dictionary complete",
        set(metadata_task.metadata)
        == {"priority_highest", "created", "start", "scheduled", "due", "recurrence", "id", "depends_on", "on_completion"},
        str(metadata_task.metadata),
    )
    check(
        "Tasks metadata round-trips",
        task_open_line(metadata_task, metadata_task.block_id or "")
        == metadata_line.replace("- [/]", "- [ ]", 1),
    )

    root = fixture(parent, "basic")
    check("ensure leap day", journal(root, "ensure", "--date", "2028-02-29").returncode == 0)
    invalid = agenda(root, "scan", "--date", "2027-02-29", "--json")
    check("reject impossible date", invalid.returncode == 3 and "real calendar date" in invalid.stderr)
    check(
        "append old plan",
        journal(root, "append", "--date", "2028-02-29", "--section", "plans", "--text", "Prepare release").returncode == 0,
    )
    check(
        "append reminder",
        journal(root, "append", "--date", "2028-03-01", "--section", "reminders", "--text", "Call supplier").returncode == 0,
    )
    scan = agenda(root, "scan", "--date", "2028-03-02", "--since", "2028-03-01", "--json")
    payload = json.loads(scan.stdout)
    check("since limits scan", scan.returncode == 0 and payload["count"] == 1, scan.stderr)
    check("since retained reminder", payload["items"][0]["section"] == "reminders")
    collect = agenda(root, "collect", "--date", "2028-03-02")
    result = json.loads(collect.stdout)
    target = daily_path(root, "2028-03-02")
    target_text = target.read_text(encoding="utf-8")
    check("collect both sections", collect.returncode == 0 and result["collected"] == 2, collect.stderr)
    check("target has one plan and one reminder", "Prepare release" in target_text and "Call supplier" in target_text)
    check("source plan closed", "- [>] Prepare release" in daily_path(root, "2028-02-29").read_text(encoding="utf-8"))
    check("source reminder closed", "- [>] Call supplier" in daily_path(root, "2028-03-01").read_text(encoding="utf-8"))
    report = root / "wiki/daily/2028/03/2028-03 — Незавершённое.md"
    report_text = report.read_text(encoding="utf-8")
    check("monthly report is declarative", "```tasks" in report_text and "tags do not include #agenda/migrated" in report_text)
    source_report = root / "wiki/daily/2028/02/2028-02 — Незавершённое.md"
    check(
        "cross-month collect refreshes source report",
        source_report.is_file() and "- Перенесено: 1" in source_report.read_text(encoding="utf-8"),
    )
    rerun = agenda(root, "collect", "--date", "2028-03-02")
    rerun_payload = json.loads(rerun.stdout)
    check(
        "rerun is a true no-op",
        rerun.returncode == 0
        and rerun_payload["status"] == "nothing"
        and rerun_payload["written_paths"] == [],
        rerun.stderr or rerun.stdout,
    )
    check("rerun creates no duplicate", target.read_text(encoding="utf-8").count("Prepare release") == 1)
    report_rerun = agenda(root, "report", "--month", "2028-03")
    check(
        "unchanged report is a true no-op",
        report_rerun.returncode == 0
        and json.loads(report_rerun.stdout)["written_paths"] == [],
        report_rerun.stderr,
    )

    legacy_root = fixture(parent, "legacy")
    for date in ("2026-07-01", "2026-07-02"):
        check(f"ensure {date}", journal(legacy_root, "ensure", "--date", date).returncode == 0)
        replace_section(daily_path(legacy_root, date), "## Напоминания", ["- Renew certificate"])
    scan = agenda(legacy_root, "scan", "--date", "2026-07-03", "--json")
    payload = json.loads(scan.stdout)
    check("legacy chain merged in preview", payload["count"] == 1 and payload["items"][0]["ambiguous"])
    check("legacy ambiguity warned", any(item["code"] == "legacy_identity_merged" for item in payload["warnings"]))
    collect = agenda(legacy_root, "collect", "--date", "2026-07-03")
    legacy_target = daily_path(legacy_root, "2026-07-03").read_text(encoding="utf-8")
    check("legacy chain carries once", collect.returncode == 0 and legacy_target.count("Renew certificate") == 1, collect.stderr)

    distinct_root = fixture(parent, "distinct")
    check("ensure distinct source", journal(distinct_root, "ensure", "--date", "2026-07-04").returncode == 0)
    replace_section(
        daily_path(distinct_root, "2026-07-04"),
        "## Планы",
        [
            "- [ ] Same words ^agenda-111111111111",
            "- [/] Same words ^agenda-222222222222",
        ],
    )
    collect = agenda(distinct_root, "collect", "--date", "2026-07-05")
    distinct_target = daily_path(distinct_root, "2026-07-05").read_text(encoding="utf-8")
    check("distinct IDs remain distinct", collect.returncode == 0 and distinct_target.count("Same words") == 2, collect.stderr)
    check("in-progress becomes open target", "- [/] Same words" not in distinct_target)

    nested_root = fixture(parent, "nested")
    check("ensure nested source", journal(nested_root, "ensure", "--date", "2026-07-06").returncode == 0)
    replace_section(
        daily_path(nested_root, "2026-07-06"),
        "## Планы",
        ["- [ ] Parent task", "  - [ ] Child task"],
    )
    scan = agenda(nested_root, "scan", "--date", "2026-07-07", "--json")
    payload = json.loads(scan.stdout)
    check("nested subtree skipped", payload["count"] == 0)
    check("nested skip warned", any(item["code"] == "nested_subtree_skipped" for item in payload["warnings"]))
    empty = agenda(nested_root, "collect", "--date", "2026-07-07", "--dry-run")
    check("empty dry-run still previews report", empty.returncode == 0 and json.loads(empty.stdout)["report"].endswith("Незавершённое.md"))
    check("dry-run writes nothing", not daily_path(nested_root, "2026-07-07").exists())

    missing_section_root = fixture(parent, "missing-section")
    check(
        "ensure missing-section source",
        journal(missing_section_root, "ensure", "--date", "2026-07-08").returncode == 0,
    )
    missing_section_page = daily_path(missing_section_root, "2026-07-08")
    replace_section(
        missing_section_page,
        "## Планы",
        ["- [ ] Surviving canonical section ^agenda-444444444444"],
    )
    missing_section_page.write_text(
        missing_section_page.read_text(encoding="utf-8").replace("## Напоминания\n\n", ""),
        encoding="utf-8",
    )
    scan = agenda(missing_section_root, "scan", "--date", "2026-07-09", "--json")
    payload = json.loads(scan.stdout)
    check(
        "missing section does not abort other work",
        scan.returncode == 0
        and payload["count"] == 1
        and any(
            item["code"] == "section_missing" and item["section"] == "reminders"
            for item in payload["warnings"]
        ),
        scan.stderr,
    )
    collect = agenda(missing_section_root, "collect", "--date", "2026-07-09")
    check(
        "missing section collect carries conforming task",
        collect.returncode == 0
        and "Surviving canonical section"
        in daily_path(missing_section_root, "2026-07-09").read_text(encoding="utf-8"),
        collect.stderr,
    )

    missing_target_root = fixture(parent, "missing-target-section")
    for date in ("2026-07-08", "2026-07-09"):
        check(
            f"ensure missing target section {date}",
            journal(missing_target_root, "ensure", "--date", date).returncode == 0,
        )
    missing_target_source = daily_path(missing_target_root, "2026-07-08")
    missing_target_page = daily_path(missing_target_root, "2026-07-09")
    replace_section(
        missing_target_source,
        "## Напоминания",
        ["- [ ] Restore target heading ^agenda-777777777777"],
    )
    missing_target_page.write_text(
        missing_target_page.read_text(encoding="utf-8").replace("## Напоминания\n\n", ""),
        encoding="utf-8",
    )
    collect = agenda(missing_target_root, "collect", "--date", "2026-07-09")
    target_payload = json.loads(collect.stdout)
    repaired_target = missing_target_page.read_text(encoding="utf-8")
    check(
        "collect restores only a required missing target section",
        collect.returncode == 0
        and repaired_target.count("## Напоминания") == 1
        and "Restore target heading" in repaired_target
        and repaired_target.index("## Планы")
        < repaired_target.index("## Напоминания")
        < repaired_target.index("## Инциденты")
        and any(
            item["code"] == "target_section_created" and item["section"] == "reminders"
            for item in target_payload["warnings"]
        ),
        collect.stderr,
    )

    conflict_identity_root = fixture(parent, "identity-conflict")
    for date in ("2026-07-10", "2026-07-11"):
        check(
            f"ensure identity conflict {date}",
            journal(conflict_identity_root, "ensure", "--date", date).returncode == 0,
        )
    source_page = daily_path(conflict_identity_root, "2026-07-10")
    target_page = daily_path(conflict_identity_root, "2026-07-11")
    replace_section(
        source_page,
        "## Планы",
        ["- [ ] Source meaning ^agenda-333333333333"],
    )
    replace_section(
        target_page,
        "## Планы",
        ["- [ ] Different meaning ^agenda-333333333333"],
    )
    identity_conflict = agenda(conflict_identity_root, "collect", "--date", "2026-07-11")
    identity_payload = json.loads(identity_conflict.stdout)
    check(
        "target identity conflict is not guessed",
        identity_conflict.returncode == 0
        and identity_payload["collected"] == 0
        and any(item["code"] == "target_identity_conflict" for item in identity_payload["warnings"]),
        identity_conflict.stderr,
    )
    check(
        "target identity conflict leaves source active",
        "- [ ] Source meaning" in source_page.read_text(encoding="utf-8"),
    )

    target_guard_root = fixture(parent, "target-guards")
    for date in ("2026-07-12", "2026-07-13"):
        check(
            f"ensure target guard {date}",
            journal(target_guard_root, "ensure", "--date", date).returncode == 0,
        )
    guard_source = daily_path(target_guard_root, "2026-07-12")
    guard_target = daily_path(target_guard_root, "2026-07-13")
    replace_section(
        guard_source,
        "## Планы",
        [
            "- [ ] Already finished ^agenda-555555555555",
            "- [ ] Duplicate target ^agenda-666666666666",
        ],
    )
    replace_section(
        guard_target,
        "## Планы",
        [
            "- [x] Already finished ✅ 2026-07-13 ^agenda-555555555555",
            "- [ ] Duplicate target ^agenda-666666666666",
            "- [/] Duplicate target ^agenda-666666666666",
        ],
    )
    guarded = agenda(target_guard_root, "collect", "--date", "2026-07-13")
    guarded_payload = json.loads(guarded.stdout)
    warning_codes = {item["code"] for item in guarded_payload["warnings"]}
    check(
        "terminal and duplicate target identities are guarded",
        guarded.returncode == 0
        and guarded_payload["collected"] == 0
        and {"target_identity_terminal", "duplicate_target_identity"} <= warning_codes,
        guarded.stderr,
    )
    guard_source_text = guard_source.read_text(encoding="utf-8")
    check(
        "guarded target identities leave both sources active",
        "- [ ] Already finished" in guard_source_text
        and "- [ ] Duplicate target" in guard_source_text,
    )

    missing_source = agenda(
        conflict_identity_root,
        "collect",
        "--source",
        "2026-07-09",
        "--date",
        "2026-07-11",
    )
    check(
        "missing explicit source is rejected",
        missing_source.returncode == 3 and "does not exist" in missing_source.stderr,
        missing_source.stderr,
    )

    # A stale hash in any page aborts the whole canonical writer transaction.
    conflict_root = fixture(parent, "conflict")
    check("ensure conflict pages", journal(conflict_root, "ensure", "--date", "2026-07-08").returncode == 0)
    check("ensure second conflict page", journal(conflict_root, "ensure", "--date", "2026-07-09").returncode == 0)
    first = daily_path(conflict_root, "2026-07-08")
    second = daily_path(conflict_root, "2026-07-09")
    first_old = first.read_text(encoding="utf-8")
    second_old = second.read_text(encoding="utf-8")
    transaction = {
        "actor": "agenda",
        "pages": [
            {
                "op": "update",
                "path": str(first.relative_to(conflict_root)),
                "expected_sha256": hashlib.sha256(first_old.encode()).hexdigest(),
                "content": first_old.replace("# 2026-07-08", "# changed first"),
            },
            {
                "op": "update",
                "path": str(second.relative_to(conflict_root)),
                "expected_sha256": "0" * 64,
                "content": second_old.replace("# 2026-07-09", "# changed second"),
            },
        ],
    }
    conflict = run(conflict_root, "vault-write.py", "--output", "json", input_text=json.dumps(transaction))
    check("transaction conflict rejected", conflict.returncode == 4)
    check("transaction conflict leaves first untouched", first.read_text(encoding="utf-8") == first_old)

print("\nAll agenda tests passed.")
