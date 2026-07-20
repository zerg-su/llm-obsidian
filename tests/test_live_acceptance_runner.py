#!/usr/bin/env python3
"""Hermetic contract tests for the repo-shipped live acceptance runner."""

from __future__ import annotations

import json
import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "live-acceptance-runner.py"
RELEASE = ROOT / "scripts" / "release-acceptance.py"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"OK   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}: {detail}")


def row(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "phase": "final",
        "skill": "clarify",
        "runtime": "codex",
        "scenario": "conversation-readonly",
        "expected": "Ask one material question at a time and make no repository mutation.",
    }
    value.update(updates)
    return value


def run(*args: str, payload: dict[str, object], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        input=json.dumps(payload) + "\n",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


with tempfile.TemporaryDirectory(prefix="live-acceptance-runner-test.") as raw:
    tmp = Path(raw)
    backend = tmp / "backend.py"
    backend.write_text(
        "import json,sys\n"
        "r=json.load(sys.stdin)\n"
        "print(json.dumps({**r,'verdict':'pass','model':'fixture-model','effort':'high',"
        "'actual':'asked one bounded question','cleanup':'no files changed',"
        "'evidence':'clean git status'}))\n",
        encoding="utf-8",
    )
    result = run("--backend-command", sys.executable, str(backend), payload=row())
    payload = json.loads(result.stdout)
    check("backend pass exits zero", result.returncode == 0, result.stderr)
    check("backend row identity preserved", payload["skill"] == "clarify" and payload["runtime"] == "codex")
    check("backend evidence preserved", payload["verdict"] == "pass" and payload["evidence"] == "clean git status")

    mismatch = tmp / "mismatch.py"
    mismatch.write_text(
        "import json,sys\n"
        "r=json.load(sys.stdin); r['skill']='daily'; r.update(verdict='pass',model='x',effort='high',actual='x',cleanup='x',evidence='x')\n"
        "print(json.dumps(r))\n",
        encoding="utf-8",
    )
    result = run("--backend-command", sys.executable, str(mismatch), payload=row())
    blocked = json.loads(result.stdout)
    check("mismatched backend is bounded data", result.returncode == 0 and blocked["verdict"] == "blocked", result.stderr)
    check("mismatched backend names identity defect", "does not match" in blocked["defect"])

    secret = tmp / "secret.py"
    secret.write_text(
        "import json,sys\n"
        "r=json.load(sys.stdin); r.update(verdict='pass',model='x',effort='high',actual='token=abcdefghijk',cleanup='x',evidence='x')\n"
        "print(json.dumps(r))\n",
        encoding="utf-8",
    )
    result = run("--backend-command", sys.executable, str(secret), payload=row())
    blocked = json.loads(result.stdout)
    check("credential-like backend result is sanitized", blocked["verdict"] == "pass")
    check("credential value omitted", "abcdefghijk" not in result.stdout)

    env = os.environ.copy()
    env.pop("CMUX_SURFACE_ID", None)
    result = run(payload=row(), env=env)
    blocked = json.loads(result.stdout)
    check("missing cmux is a valid blocked cell", result.returncode == 0 and blocked["verdict"] == "blocked")
    check("missing cmux is actionable", "CMUX_SURFACE_ID" in blocked["defect"])

    result = run(payload=row(scenario="unregistered"))
    check("unknown scenario fails before operation", result.returncode == 3 and not result.stdout, result.stderr)

    result = run(payload=row(expected="substituted expectation"))
    check("substituted fixture contract is rejected", result.returncode == 3 and not result.stdout, result.stderr)

    fixture_registry = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
    missing_fixture = tmp / "missing-fixture.json"
    missing_fixture_data = json.loads(json.dumps(fixture_registry))
    missing_fixture_data["skills"]["clarify"].pop("fixture")
    missing_fixture.write_text(json.dumps(missing_fixture_data), encoding="utf-8")
    result = run("--skills", str(missing_fixture), payload=row())
    check("missing skill fixture is rejected", result.returncode == 3 and not result.stdout, result.stderr)

    release = subprocess.run(
        [sys.executable, str(RELEASE), "check"], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )
    check("release check validates scenario parity", release.returncode == 0 and "skills x 2 runtimes" in release.stdout, release.stderr)

    spec = importlib.util.spec_from_file_location("live_acceptance_runner_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    override_repo = tmp / "override-repo"
    (override_repo / "config").mkdir(parents=True)
    module.install_acceptance_model_overrides(override_repo, {"claude": "sonnet"})
    override_text = (override_repo / "config/model-routing.local.toml").read_text(encoding="utf-8")
    from model_routing import validate_local_config
    from acceptance_fingerprints import canonical_generation, read_manifest

    manifest = read_manifest(ROOT)
    check(
        "Codex Sol and Terra share one major generation",
        canonical_generation("gpt-5.6-sol", manifest)
        == canonical_generation("gpt-5.6-terra", manifest)
        == "codex:5.6",
    )

    validated_override = validate_local_config(ROOT, override_text)
    check(
        "live acceptance supports sandbox-only cheaper model aliases",
        '[runtimes.claude]' in override_text
        and '[roles.review.claude]' in override_text
        and 'model = "sonnet"' in override_text
        and '"sonnet" = "claude"' in override_text
        and validated_override.runtime_default("claude")["model"] == "sonnet",
    )
    effort_repo = tmp / "effort-override-repo"
    (effort_repo / "config").mkdir(parents=True)
    module.install_acceptance_model_overrides(
        effort_repo, {"claude": "sonnet", "codex": "gpt-5.6-terra"}, "medium"
    )
    effort_text = (effort_repo / "config/model-routing.local.toml").read_text(
        encoding="utf-8"
    )
    check(
        "live acceptance effort override is code-owned",
        effort_text.count('effort = "medium"') == 4,
    )
    module.disable_acceptance_autocommit(override_repo)
    autocommit_guard = json.loads(
        (override_repo / ".vault-meta/auto-commit.disabled").read_text(encoding="utf-8")
    )
    check(
        "live clone disables host turn-end auto-commit",
        autocommit_guard == {"schema_version": 1, "reason": "live-acceptance"}
        and (override_repo / ".vault-meta/auto-commit.disabled").stat().st_mode & 0o777 == 0o600,
    )
    previous_claude_override = os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_CLAUDE_MODEL")
    os.environ["LLM_OBSIDIAN_ACCEPTANCE_CLAUDE_MODEL"] = "sonnet"
    try:
        blocked_override = module.blocked(
            row(runtime="claude"), "bounded fixture timeout"
        )
    finally:
        if previous_claude_override is None:
            os.environ.pop("LLM_OBSIDIAN_ACCEPTANCE_CLAUDE_MODEL", None)
        else:
            os.environ["LLM_OBSIDIAN_ACCEPTANCE_CLAUDE_MODEL"] = previous_claude_override
    check(
        "blocked live evidence records the actual test model override",
        blocked_override["model"] == "sonnet",
    )
    repo = tmp / "cleanup-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()
    original_root = module.ROOT
    original_pin = os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT")
    module.ROOT = repo
    (repo / "tracked.txt").write_text("dirty but uncommitted\n", encoding="utf-8")
    os.environ["LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT"] = commit
    try:
        check("pinned source commit ignores later worktree drift", module.git_head() == commit)
        os.environ["LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT"] = "not-a-commit"
        try:
            module.git_head()
        except module.AcceptanceRunnerError as exc:
            check("invalid source pin fails closed", "invalid pinned" in str(exc), str(exc))
        else:
            check("invalid source pin fails closed", False)
    finally:
        module.ROOT = original_root
        if original_pin is None:
            os.environ.pop("LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT", None)
        else:
            os.environ["LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT"] = original_pin
        (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    (repo / ".acceptance-sandbox.json").write_text("{}\n", encoding="utf-8")
    clean, _ = module.sandbox_cleanup_proof(repo, commit)
    check("runner-owned marker is cleanup-neutral", clean)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("independent cleanup proof catches product drift", not clean and "changes" in reason)
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    (repo / ".vault-meta").mkdir()
    (repo / "wiki" / "sources").mkdir(parents=True)
    (repo / ".vault-meta" / "address-counter.txt").write_text("1\n", encoding="utf-8")
    (repo / "wiki" / "sources" / "_index.md").write_text("# Sources\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".vault-meta/address-counter.txt", "wiki/sources/_index.md"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "bookkeeping fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()
    (repo / ".vault-meta" / "address-counter.txt").write_text("2\n", encoding="utf-8")
    (repo / "wiki" / "sources" / "_index.md").write_text(
        "# Sources\n- disposable\n", encoding="utf-8"
    )
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("runner accepts disposable vault bookkeeping", clean and "bookkeeping" in reason, reason)
    (repo / "unexpected.md").write_text("product residue\n", encoding="utf-8")
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("runner rejects untracked product output", not clean and "changes" in reason, reason)
    (repo / "unexpected.md").unlink()
    (repo / ".raw").mkdir()
    (repo / ".raw" / ".manifest.json").write_text(
        '{"sources":{},"address_map":{}}\n', encoding="utf-8"
    )
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check(
        "runner accepts an exact untracked disposable ingest manifest",
        clean and "bookkeeping" in reason,
        reason,
    )
    (repo / ".raw" / ".manifest.json").unlink()
    (repo / ".raw").rmdir()

    claude_argv, _ = module.agent_argv("claude", repo, "fixture-model", "high", "prompt")
    check("Claude acceptance runs outside project hooks", "--add-dir" in claude_argv and str(repo) in claude_argv)
    check(
        "Claude acceptance loads the repo-local plugin",
        claude_argv[claude_argv.index("--plugin-dir") + 1] == str(repo),
    )
    check(
        "Claude acceptance cannot block on interactive questions",
        claude_argv[claude_argv.index("--disallowedTools") + 1] == "AskUserQuestion",
    )
    module.validated_cmux_socket_path = lambda: Path("/tmp/fixture-cmux.sock")
    codex_argv, _ = module.agent_argv("codex", repo, "fixture-model", "high", "prompt")
    check("Codex acceptance disables hooks", "--disable" in codex_argv and "hooks" in codex_argv)
    check("Codex acceptance keeps Fast user-only", 'service_tier="default"' in codex_argv)
    check(
        "Codex acceptance grants only disposable Git metadata",
        codex_argv[codex_argv.index("--add-dir") + 1] == str(module.resolved_git_common_dir(repo)),
    )
    scratch = tmp / "scratch"
    scratch.mkdir()
    _argv, scratch_env = module.agent_argv(
        "claude", repo, "fixture-model", "high", "prompt", scratch_root=scratch,
        surface="00000000-0000-0000-0000-000000000003",
    )
    check("acceptance temp files are operation-scoped", all(scratch_env[name] == str(scratch) for name in ("TMPDIR", "TMP", "TEMP")))
    check(
        "acceptance agent is anchored to its exact surface",
        scratch_env["CMUX_SURFACE_ID"] == "00000000-0000-0000-0000-000000000003",
    )
    acceptance_session = "acceptance-00000000-0000-0000-0000-000000000004"
    _argv, session_env = module.agent_argv(
        "codex", repo, "fixture-model", "high", "prompt",
        session_id=acceptance_session,
    )
    detected_session = subprocess.run(
        [str(ROOT / "scripts/current-session-id.sh")],
        text=True,
        capture_output=True,
        env=session_env,
        check=False,
    )
    check(
        "acceptance runner supplies its captured route identity",
        detected_session.returncode == 0
        and detected_session.stdout.strip() == acceptance_session,
        detected_session.stderr,
    )
    try:
        module.agent_argv(
            "codex", repo, "fixture-model", "high", "prompt",
            session_id="../invalid",
        )
    except module.AcceptanceRunnerError:
        check("acceptance route identity rejects path syntax", True)
    else:
        check("acceptance route identity rejects path syntax", False)

    settling = tmp / "settling-outbox.json"
    state: dict[str, object] = {}
    settling.write_text('{"schema_version":', encoding="utf-8")
    check("partial outbox waits during grace", module.settled_outbox(settling, state, 10.0) is None)
    settling.write_text('{"schema_version": 1}', encoding="utf-8")
    check("first valid outbox sample waits for stability", module.settled_outbox(settling, state, 10.2) is None)
    check("stable valid outbox is accepted", module.settled_outbox(settling, state, 11.3) == {"schema_version": 1})

    invalid = tmp / "invalid-outbox.json"
    invalid.write_text("{", encoding="utf-8")
    invalid_state: dict[str, object] = {}
    module.settled_outbox(invalid, invalid_state, 20.0)
    try:
        module.settled_outbox(invalid, invalid_state, 25.1)
    except module.AcceptanceRunnerError as exc:
        check("persistently invalid outbox fails after bounded grace", "grace period" in str(exc))
    else:
        check("persistently invalid outbox fails after bounded grace", False, "no error")

    symlink = tmp / "symlink-outbox.json"
    symlink.symlink_to(settling)
    try:
        module.settled_outbox(symlink, {}, 30.0)
    except module.AcceptanceRunnerError as exc:
        check("outbox symlink is rejected", "non-symlink" in str(exc))
    else:
        check("outbox symlink is rejected", False, "no error")

    oversized = tmp / "oversized-outbox.json"
    oversized.write_bytes(b"x" * (module.OUTBOX_MAX_BYTES + 1))
    try:
        module.settled_outbox(oversized, {}, 40.0)
    except module.AcceptanceRunnerError as exc:
        check("oversized outbox is rejected", "size limit" in str(exc))
    else:
        check("oversized outbox is rejected", False, "no error")

    exited = tmp / "agent-exit.json"
    exited.write_text('{"schema_version": 1}\n', encoding="utf-8")
    close_calls: list[list[str]] = []
    original_run = module.subprocess.run
    original_send_surface = module.send_surface
    original_close_exact = module.close_surface_exact
    module.send_surface = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("already-exited agent must not receive /exit")
    )
    module.close_surface_exact = lambda surface, _runner: close_calls.append([surface]) or "closed"
    try:
        close_result = module.close_surface("00000000-0000-0000-0000-000000000001", "codex", exited)
    finally:
        module.subprocess.run = original_run
        module.send_surface = original_send_surface
        module.close_surface_exact = original_close_exact
    check("interrupted exited agent closes without a second command", close_result == "exact surface closed")
    check("interrupted cleanup targets exact surface once", close_calls == [[
        "00000000-0000-0000-0000-000000000001"
    ]])

    forced_exit = tmp / "forced-agent-exit.json"
    force_calls: list[list[str]] = []
    module.send_surface = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("forced interrupt cleanup must not wait for agent commands")
    )
    module.close_surface_exact = lambda surface, _runner: force_calls.append([surface]) or "closed"
    try:
        forced_result = module.close_surface(
            "00000000-0000-0000-0000-000000000007",
            "codex",
            forced_exit,
            force=True,
        )
    finally:
        module.send_surface = original_send_surface
        module.close_surface_exact = original_close_exact
    check("forced interrupt closes without an exit-marker wait", forced_result == "exact surface closed")
    check("forced interrupt targets the exact surface once", force_calls == [[
        "00000000-0000-0000-0000-000000000007"
    ]])

    confirming_exit = tmp / "confirming-agent-exit.json"
    confirm_calls: list[list[str]] = []
    module.send_surface = lambda *_args, **_kwargs: None
    def confirm_run(argv, **_kwargs):
        confirm_calls.append(list(argv))
        if argv[1] == "read-screen":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    "Background work is run\nning\n"
                    "The following will stop when you ex\nit:\n"
                    "1. Exit any\nway\n2. Move to background and ex\nit\n"
                    "3. St\nay\nEnter to con\nfirm\n"
                ),
                stderr="",
            )
        if argv[1] == "send-key":
            confirming_exit.write_text('{"schema_version": 1}\n', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")
    module.subprocess.run = confirm_run
    module.close_surface_exact = lambda surface, _runner: confirm_calls.append(
        ["close-exact", surface]
    ) or "closed"
    try:
        close_result = module.close_surface("00000000-0000-0000-0000-000000000002", "claude", confirming_exit)
    finally:
        module.subprocess.run = original_run
        module.send_surface = original_send_surface
        module.close_surface_exact = original_close_exact
    check("Claude background-task exit confirmation is handled", close_result == "exact surface closed")
    check("Claude exact exit confirmation is submitted", any(call[1:3] == ["send-key", "--surface"] for call in confirm_calls))
    check("agent exit grace covers slow interactive shutdown", module.AGENT_EXIT_GRACE_SECONDS >= 300)

    prompt = module.prompt_text(
        row(), module.load_scenarios()["conversation-readonly"], repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model", "high", commit, "Ask the exact fixture question and finish.",
    )
    check("prompt embeds exact per-skill fixture", "Ask the exact fixture question and finish." in prompt)
    check("conversation fixture forbids human wait", "instead of waiting for another human message" in prompt)
    check(
        "conversation scenario requires plain output instead of interactive UI",
        "Do not invoke an interactive question tool" in prompt,
    )
    check("prompt delegates runner-owned cleanup", "Do not run `git restore`" in prompt and "run-scoped temporary directory" in prompt)
    check("prompt forbids temp-root drift", "pass `--tmp-root`/`--state-root`" in prompt and "`TMPDIR`/`TMP`/`TEMP`" in prompt)
    check("prompt forbids duplicate dry-run", "Do not precede it with a `--no-spawn`" in prompt)
    check(
        "acceptance prompt delegates product repair to the outer coordinator",
        "must not repair or edit product scripts" in prompt
        and "outer coordinator owns any fix and rerun" in prompt,
    )
    check("prompt pins runner-owned nested worktrees", "Use `LLM_OBSIDIAN_WORKTREES`" in prompt)
    check("prompt validates before disposable cleanup", "Validate product output before removing" in prompt)
    check(
        "prompt keeps append-only cleanup bookkeeping writer-owned",
        "Never put\n  `wiki/log.md` or `wiki/hot.md` in cleanup `pages` operations" in prompt
        and "instead of requiring a second whole-vault validation" in prompt,
    )
    check(
        "prompt permits native subscription without exposing credentials",
        "already authenticated" in prompt and "Never read, copy, print, export" in prompt,
    )

    autoresearch_prompt = module.prompt_text(
        row(
            skill="autoresearch",
            scenario="protected-web",
            expected="Complete one bounded read-only protected web research flow and file validated output.",
        ),
        module.load_scenarios()["protected-web"],
        repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model",
        "high",
        commit,
        "Research one bounded public URL.",
    )
    check(
        "autoresearch prompt delegates exact output cleanup to runner",
        "Leave the exact filed output pages" in autoresearch_prompt
        and "runner-owned cleanup begins afterward" in autoresearch_prompt
        and "Do not poll marker/state files" in autoresearch_prompt,
    )

    autoresearch_repo = tmp / "autoresearch-cleanup-repo"
    (autoresearch_repo / "wiki" / "sources").mkdir(parents=True)
    (autoresearch_repo / "wiki" / "questions").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=autoresearch_repo, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=autoresearch_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=autoresearch_repo, check=True)
    index_page = autoresearch_repo / "wiki" / "sources" / "_index.md"
    index_page.write_text("# Sources\n", encoding="utf-8")
    merged_page = autoresearch_repo / "wiki" / "questions" / "Existing Research.md"
    merged_page.write_text("---\ntype: question\n---\n\nOriginal.\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "wiki/sources/_index.md", "wiki/questions/Existing Research.md"],
        cwd=autoresearch_repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=autoresearch_repo, check=True)
    autoresearch_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=autoresearch_repo,
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    output_page = autoresearch_repo / "wiki" / "sources" / "Acceptance Result.md"
    output_page.write_text("---\ntype: source\n---\n\nValidated output.\n", encoding="utf-8")
    merged_page.write_text("---\ntype: question\n---\n\nOriginal plus accepted research.\n", encoding="utf-8")
    index_page.write_text("# Sources\n- [[Acceptance Result]]\n", encoding="utf-8")
    research_run_id = "11111111-2222-4333-8444-555555555555"
    coordinator_surface = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    operation_dir = (
        autoresearch_repo / ".vault-meta" / "task-sessions" / "projects" / "project"
        / "tasks" / "task" / "lanes" / "lane" / "operations" / research_run_id
    )
    operation_dir.mkdir(parents=True)
    locator_dir = autoresearch_repo / ".vault-meta" / "research-runs" / research_run_id
    locator_dir.mkdir(parents=True)
    (locator_dir / "locator.json").write_text(json.dumps({
        "schema_version": 1,
        "run_id": research_run_id,
        "vault": str(autoresearch_repo),
        "operation_dir": str(operation_dir),
    }), encoding="utf-8")
    (operation_dir / "state.json").write_text(json.dumps({
        "schema_version": 1,
        "run_id": research_run_id,
        "vault": str(autoresearch_repo),
        "operation_dir": str(operation_dir),
        "status": "complete",
        "fetch_artifact_status": "accepted",
        "coordinator_surface": coordinator_surface,
        "outputs": [
            "wiki/sources/Acceptance Result.md",
            "wiki/questions/Existing Research.md",
        ],
    }), encoding="utf-8")
    cleanup_calls: list[list[str]] = []
    cleanup_payloads: list[dict[str, object]] = []
    original_run_checked = module.run_checked
    def fake_run_checked(argv, *, cwd, input_text=None, **_kwargs):
        cleanup_calls.append(list(argv))
        if input_text is not None:
            payload = json.loads(input_text)
            cleanup_payloads.append(payload)
            for page in payload["pages"]:
                target = Path(cwd) / page["path"]
                if page["op"] == "delete":
                    target.unlink()
                else:
                    target.write_text(page["content"], encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
    module.run_checked = fake_run_checked
    try:
        clean, reason = module.autoresearch_acceptance_cleanup(
            autoresearch_repo, autoresearch_commit, coordinator_surface
        )
        check(
            "runner transactionally deletes bound autoresearch output",
            clean and not output_page.exists() and "transactionally restored" in reason,
            reason,
        )
        check(
            "runner restores tracked product index in the same transaction",
            index_page.read_text(encoding="utf-8") == "# Sources\n"
            and len(cleanup_payloads) == 1
            and {page["op"] for page in cleanup_payloads[0]["pages"]} == {"delete", "update"},
        )
        check(
            "runner restores a tracked deduplicated autoresearch page",
            merged_page.read_text(encoding="utf-8")
            == "---\ntype: question\n---\n\nOriginal.\n",
        )
        locator = json.loads((locator_dir / "locator.json").read_text(encoding="utf-8"))
        locator["operation_dir"] = str(tmp / "escaped-operation")
        (locator_dir / "locator.json").write_text(json.dumps(locator), encoding="utf-8")
        escaped, escaped_reason = module.autoresearch_acceptance_cleanup(
            autoresearch_repo, autoresearch_commit, coordinator_surface
        )
        check(
            "runner rejects autoresearch locator escape",
            not escaped and "escapes task sessions" in escaped_reason,
            escaped_reason,
        )
    finally:
        module.run_checked = original_run_checked

    close_fixture = module.close_acceptance_fixture("abcdef12-0000-0000-0000-000000000000")
    close_prompt = module.prompt_text(
        row(
            skill="close",
            scenario="cmux-lifecycle",
            expected="Save and terminate the agent process without closing its cmux surface.",
        ),
        module.load_scenarios()["cmux-lifecycle"],
        repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model",
        "high",
        commit,
        module.close_fixture_prompt(close_fixture),
    )
    check(
        "close fixture reuses the runner-created surface",
        "do not create another cmux surface" in close_prompt
        and "do not create another surface"
        in module.load_skill_fixtures()["close"]["fixture"].lower(),
    )
    check(
        "close fixture refreshes derived indexes before validation",
        "run scripts/reindex.py before full-vault validation" in close_prompt,
    )
    check(
        "backlog fixture carries its explicit promotion target",
        "already selected Wiki decision" in module.load_skill_fixtures()["backlog"]["fixture"]
        and "do not invoke AskUserQuestion" in module.load_skill_fixtures()["backlog"]["fixture"],
    )
    check(
        "close outbox precedes one final graceful exit",
        "typed outbox is the penultimate action" in close_prompt
        and "python3 scripts/queue-session-exit.py" in close_prompt,
    )

    close_repo = tmp / "close-proof"
    close_page = close_repo / close_fixture["page_rel"]
    close_page.parent.mkdir(parents=True)
    close_page.write_text(
        f"---\ntype: session\ntitle: \"{close_fixture['title']}\"\n"
        "sessions:\n  - fixture-session\n---\n",
        encoding="utf-8",
    )
    missing_address_clean, _ = module.close_acceptance_proof(close_repo, close_fixture)
    check(
        "close proof enforces the save contract address",
        not missing_address_clean and close_page.exists(),
    )
    close_page.write_text(
        f"---\ntype: session\ntitle: \"{close_fixture['title']}\"\n"
        "address: c-000001\nsessions:\n  - fixture-session\n---\n\n"
        "Disposable local acceptance record for exact-surface graceful exit.\n",
        encoding="utf-8",
    )
    original_checked = module.run_checked
    original_run = module.subprocess.run
    delete_payloads: list[dict[str, object]] = []
    module.subprocess.run = lambda argv, **_kwargs: subprocess.CompletedProcess(
        argv, 0, stdout="OK", stderr=""
    )

    def fake_checked(_argv, *, cwd, input_text=None):
        delete_payloads.append(json.loads(input_text or "{}"))
        close_page.unlink()
        return "{}"

    module.run_checked = fake_checked
    try:
        close_clean, close_proof = module.close_acceptance_proof(close_repo, close_fixture)
    finally:
        module.run_checked = original_checked
        module.subprocess.run = original_run
    check(
        "runner proves and transactionally deletes the close fixture",
        close_clean
        and "transactionally removed" in close_proof
        and delete_payloads[0]["pages"][0]["op"] == "delete",
    )

    dispatch_fixture = module.load_skill_fixtures()["dispatch"]["fixture"]
    check(
        "dispatch fixture delegates deterministic proof and cleanup to the runner",
        "runner-prepared approved plan" in dispatch_fixture
        and "typed approve review" in dispatch_fixture
        and "independent runner proof" in dispatch_fixture
        and "substitute narrative evidence" in dispatch_fixture,
    )
    check(
        "Codex dispatch fixture provisions ignored runtime config only",
        "runtime.env.example" in inspect.getsource(module.dispatch_acceptance_fixture)
        and "codex-sync" not in inspect.getsource(module.dispatch_acceptance_fixture),
    )
    runtime_fixture = tmp / "runtime-fixture"
    (runtime_fixture / "scripts/mcp-gateway").mkdir(parents=True)
    (runtime_fixture / "scripts/mcp-gateway/runtime.env.example").write_text(
        "LLM_OBSIDIAN_TEST=1\n", encoding="utf-8"
    )
    module.install_acceptance_runtime_fixture(runtime_fixture)
    module.install_acceptance_runtime_fixture(runtime_fixture)
    check(
        "lifecycle acceptance provisions one idempotent local runtime fixture",
        (runtime_fixture / "scripts/mcp-gateway/runtime.env").read_text(encoding="utf-8")
        == "LLM_OBSIDIAN_TEST=1\n"
        and "dispatch-review-reap" in inspect.getsource(module.run_live),
    )

    daily_repo = tmp / "daily-cleanup-proof"
    daily_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=daily_repo, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=daily_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=daily_repo, check=True)
    (daily_repo / "tracked.txt").write_text("release\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=daily_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "release"], cwd=daily_repo, check=True)
    daily_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=daily_repo,
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    daily_page = daily_repo / "wiki/meta/sessions/Acceptance daily fixture.md"
    daily_page.parent.mkdir(parents=True)
    daily_page.write_text("---\ntype: session\n---\n", encoding="utf-8")
    subprocess.run(["git", "add", str(daily_page.relative_to(daily_repo))], cwd=daily_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "daily evidence"], cwd=daily_repo, check=True)
    daily_page.unlink()
    (daily_repo / ".acceptance-sandbox.json").write_text("{}\n", encoding="utf-8")
    daily_clean, daily_proof = module.daily_acceptance_cleanup(daily_repo, daily_commit)
    check(
        "daily cleanup accepts one exact removed evidence commit",
        daily_clean and "bounded daily evidence" in daily_proof,
        daily_proof,
    )
    (daily_repo / "unexpected.md").write_text("residue\n", encoding="utf-8")
    daily_clean, daily_proof = module.daily_acceptance_cleanup(daily_repo, daily_commit)
    check(
        "daily cleanup still rejects unrelated residue",
        not daily_clean and "retained" in daily_proof,
        daily_proof,
    )

    prepared = {
        "task_name": "acceptance-dispatch-deadbeef",
        "branch": "task/acceptance-dispatch-deadbeef",
        "plan_path": str(repo / "wiki/plans/acceptance.md"),
        "fixture_rel": "acceptance-dispatch-deadbeef.txt",
        "fixture_text": "dispatch acceptance deadbeef\n",
        "result_title": "Acceptance dispatch deadbeef result",
        "nested_worktree": str(repo / ".vault-meta/acceptance-worktrees/sandbox-acceptance-dispatch-deadbeef"),
        "dispatch_spec": str(repo / ".vault-meta/acceptance/dispatch-request.json"),
        "request_id": "11111111-1111-4111-8111-111111111111",
        "coordinator_runtime": "codex",
    }
    exact_dispatch = module.dispatch_fixture_prompt(prepared)
    dispatch_prompt = module.prompt_text(
        row(skill="dispatch", scenario="dispatch-review-reap",
            expected="Start one isolated task worktree and deliver its approved plan exactly once."),
        module.load_scenarios()["dispatch-review-reap"], repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model", "high", commit, exact_dispatch, prepared,
    )
    check(
        "dispatch prompt pins one exact runner-prepared operation",
        all(value in dispatch_prompt for value in (
            prepared["task_name"], prepared["branch"], prepared["plan_path"],
            prepared["nested_worktree"], prepared["result_title"], prepared["dispatch_spec"],
        )),
    )
    review_send_prompt = module.prompt_text(
        row(skill="review-send", scenario="dispatch-review-reap",
            expected="Submit one typed product-read-only reviewer result exactly once."),
        module.load_scenarios()["dispatch-review-reap"], repo,
        repo / ".vault-meta" / "acceptance" / "agent-outbox.json",
        "fixture-model", "high", commit,
        fixture_registry["skills"]["review-send"]["fixture"],
    )
    check(
        "review rows preserve runner-owned nested lifecycle state",
        "do not\n  manually remove, prune" in review_send_prompt
        and "leave the runner-owned nested lane" in review_send_prompt
        and "Clean every disposable page, branch, worktree" not in review_send_prompt,
        review_send_prompt,
    )
    check(
        "review-dispatch fixture exercises a resolvable warning",
        "non-blocking maintainability warning"
        in fixture_registry["skills"]["review-dispatch"]["fixture"]
        and "blocking finding exceeds this fixture"
        in fixture_registry["skills"]["review-dispatch"]["fixture"],
    )
    check(
        "dispatch prompt uses mechanical runner once",
        "dispatch-runner.py start --spec" in dispatch_prompt
        and "do not reproduce its setup commands manually" in dispatch_prompt,
    )
    check(
        "dispatch acceptance forbids invented summary links",
        "typed summary body free of invented" in dispatch_prompt
        and "attaches the validated review archive link itself" in dispatch_prompt,
    )
    check(
        "dispatch coordinator returns idle for typed callbacks",
        "finish the coordinator turn and return" in dispatch_prompt
        and "Do not shell-poll task files" in dispatch_prompt
        and "agent wait tools" in dispatch_prompt,
    )
    check(
        "dispatch launch turn cannot publish final acceptance",
        "Do not publish the acceptance agent outbox in that launch turn" in dispatch_prompt
        and "returning idle without an outbox keeps this cell running" in dispatch_prompt,
    )
    check(
        "dispatch start failure has a bounded immediate outbox path",
        "start` invocation exits non-zero" in dispatch_prompt
        and "not retry it, perform open-ended diagnosis" in dispatch_prompt
        and "Publish the typed fail/blocked outbox immediately" in dispatch_prompt,
    )
    check(
        "dispatch successful reap avoids duplicate model-side proof",
        "final reap runner returns `status: complete`" in dispatch_prompt
        and "publish the\n  typed pass outbox immediately" in dispatch_prompt
        and "Do not enumerate proof files" in dispatch_prompt
        and "outer acceptance runner performs" in dispatch_prompt,
    )
    module.write_dispatch_acceptance_request(
        repo,
        prepared,
        source_commit=commit,
        coordinator_surface="22222222-2222-4222-8222-222222222222",
        coordinator_model="gpt-5.6-sol",
        coordinator_effort="high",
    )
    prepared_request = json.loads(Path(prepared["dispatch_spec"]).read_text(encoding="utf-8"))
    check(
        "acceptance request delegates current session route to runner",
        "origin_session" not in prepared_request
        and prepared_request["session_route"]["source"] == "acceptance-runner",
    )
    check(
        "acceptance request binds exact coordinator surface",
        prepared_request["origin_surface"] == "22222222-2222-4222-8222-222222222222",
    )
    Path(prepared["dispatch_spec"]).unlink()
    check(
        "dispatch prompt preserves durable proof artifacts",
        "Leave them in place" in dispatch_prompt and "independently proves the exact commit" in dispatch_prompt,
    )

    cleanup_run = tmp / "cleanup-run"
    cleanup_sandbox = cleanup_run / "sandbox"
    system_tmp = tmp / "system-tmp"
    system_tmp.mkdir()
    original_gettempdir = module.tempfile.gettempdir
    module.tempfile.gettempdir = lambda: str(system_tmp)
    cleanup_scratch = module.scratch_root_for(cleanup_run)
    cleanup_sandbox.mkdir(parents=True)
    cleanup_scratch.mkdir()
    (cleanup_sandbox / ".acceptance-sandbox.json").write_text("{}\n", encoding="utf-8")
    (cleanup_scratch / ".acceptance-scratch.json").write_text(
        json.dumps({"schema_version": 1, "run_dir": str(cleanup_run)}) + "\n",
        encoding="utf-8",
    )
    (cleanup_scratch / "nested").mkdir()
    (cleanup_scratch / "nested" / "artifact.json").write_text("{}\n", encoding="utf-8")
    module.safe_cleanup(cleanup_run)
    module.tempfile.gettempdir = original_gettempdir
    check("runner removes exact sandbox and scratch roots", not cleanup_sandbox.exists() and not cleanup_scratch.exists())

    proof_repo = tmp / "dispatch-proof"
    proof_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proof_repo, check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=proof_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=proof_repo, check=True)
    (proof_repo / ".gitignore").write_text(
        ".vault-meta/task-sessions/\n", encoding="utf-8",
    )
    (proof_repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "base.txt"], cwd=proof_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=proof_repo, check=True)
    source = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proof_repo, text=True, capture_output=True, check=True,
    ).stdout.strip()
    nested = proof_repo / ".vault-meta/acceptance-worktrees/sandbox-acceptance-dispatch-proof"
    nested.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(proof_repo), str(nested)], check=True)
    subprocess.run(["git", "config", "user.email", "acceptance@example.invalid"], cwd=nested, check=True)
    subprocess.run(["git", "config", "user.name", "Acceptance Test"], cwd=nested, check=True)
    fixture_text = "dispatch acceptance proof\n"
    (nested / "acceptance-dispatch-proof.txt").write_text(fixture_text, encoding="utf-8")
    subprocess.run(["git", "add", "acceptance-dispatch-proof.txt"], cwd=nested, check=True)
    subprocess.run(["git", "commit", "-qm", "add exact acceptance fixture"], cwd=nested, check=True)
    plan = proof_repo / "wiki/plans/acceptance-dispatch-proof.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\ntype: plan\nstatus: executed\n---\n# Proof plan\n\nРезультат: [[Acceptance dispatch proof result]]\n",
        encoding="utf-8",
    )
    result_page = proof_repo / "wiki/meta/sessions/Acceptance dispatch proof result.md"
    result_page.parent.mkdir(parents=True)
    result_page.write_text("# Acceptance dispatch proof result\n", encoding="utf-8")
    project_id = "11111111-1111-4111-8111-111111111111"
    task_id = "22222222-2222-4222-8222-222222222222"
    meta = {
        "version": 3,
        "project_id": project_id,
        "task_id": task_id,
        "task_name": "acceptance-dispatch-proof",
        "branch": "task/acceptance-dispatch-proof",
        "plan_file": str(plan),
        "review_policy": {"mode": "light"},
        "reap_policy": {"mode": "final", "title": "Acceptance dispatch proof result"},
    }
    (nested / ".task-meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    task_root = proof_repo / ".vault-meta/task-sessions/projects" / project_id / "tasks" / task_id
    operation = task_root / "lanes/review/operations/one"
    operation.mkdir(parents=True)
    (task_root / "task.json").write_text(json.dumps({
        "project_id": project_id, "task_id": task_id,
        "status": "archived", "worktrees": [str(nested.resolve())],
    }) + "\n", encoding="utf-8")
    review = operation / ".task-review.json"
    review.write_text(json.dumps({
        "schema_version": 1, "mode": "light", "verdict": "approve",
    }) + "\n", encoding="utf-8")
    review_page = proof_repo / "wiki/meta/reviews/Acceptance dispatch proof review.md"
    review_page.parent.mkdir(parents=True)
    review_page.write_text("# Acceptance dispatch proof review\n", encoding="utf-8")
    (operation / ".review-archive.json").write_text(json.dumps({
        "schema_version": 1,
        "status": "already-current",
        "path": "wiki/meta/reviews/Acceptance dispatch proof review.md",
        "content_sha256": module.hashlib.sha256(review_page.read_bytes()).hexdigest(),
    }) + "\n", encoding="utf-8")
    (nested / ".task-reap-complete.json").write_text(json.dumps({
        "validated": True,
        "task_session_status": "archived",
        "plan_path": str(plan),
        "result_path": str(result_page),
        "result_sha256": module.hashlib.sha256(result_page.read_bytes()).hexdigest(),
    }) + "\n", encoding="utf-8")
    proof_fixture = {
        "task_name": "acceptance-dispatch-proof",
        "branch": "task/acceptance-dispatch-proof",
        "plan_rel": "wiki/plans/acceptance-dispatch-proof.md",
        "plan_path": str(plan.resolve()),
        "fixture_rel": "acceptance-dispatch-proof.txt",
        "fixture_text": fixture_text,
        "result_title": "Acceptance dispatch proof result",
        "nested_worktree": str(nested.resolve()),
    }
    proved, reason = module.dispatch_acceptance_proof(proof_repo, source, proof_fixture)
    check("dispatch proof accepts the complete durable lifecycle", proved, reason)
    review.unlink()
    proved, reason = module.dispatch_acceptance_proof(proof_repo, source, proof_fixture)
    check("dispatch proof rejects a narrative-only pass without typed review", not proved and "typed approve" in reason, reason)

    owned_nested = repo / ".vault-meta" / "acceptance-worktrees" / "task-one"
    owned_nested.mkdir(parents=True)
    (owned_nested / "fixture.txt").write_text("runner owned\n", encoding="utf-8")
    clean, reason = module.sandbox_cleanup_proof(repo, commit)
    check("runner contains its exact nested worktree root", clean, reason)

    child_root = tmp / "child-sandbox"
    child_state = child_root / ".vault-meta/task-sessions/projects/p/tasks/t/lanes/l/operations/o/state.json"
    child_state.parent.mkdir(parents=True)
    child_state.write_text(json.dumps({
        "coordinator_surface": "00000000-0000-0000-0000-000000000003",
        "fetch_surface": "00000000-0000-0000-0000-000000000004",
    }), encoding="utf-8")
    standalone_state = child_root / ".vault-meta/research-runs/r/state.json"
    standalone_state.parent.mkdir(parents=True)
    standalone_state.write_text(json.dumps({
        "coordinator_surface": "00000000-0000-0000-0000-000000000003",
        "synth_surface": "00000000-0000-0000-0000-000000000005",
    }), encoding="utf-8")
    dispatched_meta = child_root / ".vault-meta/acceptance-worktrees/task-one/.task-meta.json"
    dispatched_meta.parent.mkdir(parents=True)
    dispatched_meta.write_text(json.dumps({
        "wiki_surface": "00000000-0000-0000-0000-000000000003",
        "task_surface": "00000000-0000-0000-0000-000000000006",
    }), encoding="utf-8")
    child_calls: list[list[str]] = []
    module.close_surface_exact = lambda surface, _runner: child_calls.append([surface]) or "closed"
    try:
        child_closed, child_failures = module.close_operation_children(
            child_root, "00000000-0000-0000-0000-000000000003"
        )
    finally:
        module.close_surface_exact = original_close_exact
    check("interrupted operation closes exact registered children", child_closed == 3 and not child_failures)
    check("registered child close never targets coordinator", {call[0] for call in child_calls} == {
        "00000000-0000-0000-0000-000000000004",
        "00000000-0000-0000-0000-000000000005",
        "00000000-0000-0000-0000-000000000006",
    })
    surface_order: list[str] = []
    original_close_surface = module.close_surface
    original_close_children = module.close_operation_children
    original_wait_children = module.wait_for_operation_children
    module.close_surface = lambda *_args, **_kwargs: surface_order.append("coordinator") or "exact surface closed"
    module.wait_for_operation_children = lambda *_args, **_kwargs: surface_order.append("wait")
    module.close_operation_children = lambda *_args, **_kwargs: (surface_order.append("children") or (2, []))
    try:
        settled = module.settle_operation_surfaces(
            child_root,
            "00000000-0000-0000-0000-000000000003",
            "codex",
            child_root / "agent-exit.json",
        )
    finally:
        module.close_surface = original_close_surface
        module.wait_for_operation_children = original_wait_children
        module.close_operation_children = original_close_children
    check(
        "cleanup stops coordinator and gives children an auto-close grace",
        surface_order == ["coordinator", "wait", "children"]
        and settled == ("exact surface closed", 2, [])
        and module.CHILD_SURFACE_SETTLE_SECONDS >= 30,
    )
    surface_order = []
    module.close_surface = lambda *_args, **kwargs: surface_order.append(
        f"coordinator-force={kwargs.get('force')}"
    ) or "exact surface closed"
    module.wait_for_operation_children = lambda *_args, **_kwargs: surface_order.append("wait")
    module.close_operation_children = lambda *_args, **_kwargs: (surface_order.append("children") or (2, []))
    try:
        forced_settled = module.settle_operation_surfaces(
            child_root,
            "00000000-0000-0000-0000-000000000003",
            "codex",
            child_root / "agent-exit.json",
            force=True,
        )
    finally:
        module.close_surface = original_close_surface
        module.wait_for_operation_children = original_wait_children
        module.close_operation_children = original_close_children
    check(
        "forced interrupt skips grace and closes exact children immediately",
        surface_order == ["coordinator-force=True", "children"]
        and forced_settled == ("exact surface closed", 2, []),
    )

registry = json.loads((ROOT / "evals/acceptance/scenarios.json").read_text(encoding="utf-8"))
skills = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
check("acceptance runtime is gitignored", ".vault-meta/acceptance/" in (ROOT / ".gitignore").read_text(encoding="utf-8"))
check(
    "turn markers are gitignored",
    subprocess.run(
        ["git", "check-ignore", "-q", ".vault-meta/turn-markers/session.json"],
        cwd=ROOT,
        check=False,
    ).returncode == 0,
)
check(
    "scenario registry exactly covers matrix",
    set(registry["scenarios"]) == {item["scenario"] for item in skills["skills"].values()},
)
check(
    "every skill has one bounded live fixture",
    all(isinstance(item.get("fixture"), str) and item["fixture"].strip() for item in skills["skills"].values()),
)
check(
    "autoresearch fixture pins its bounded URL and topic boundary",
    "https://docs.python.org/3/library/functions.html#len" in skills["skills"]["autoresearch"]["fixture"]
    and "only the research request" in skills["skills"]["autoresearch"]["fixture"],
)

if failures:
    print(f"\n{len(failures)} live acceptance runner test(s) failed")
    raise SystemExit(1)
print("\nAll live acceptance runner tests passed.")
