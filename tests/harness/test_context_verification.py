#!/usr/bin/env python3
"""Deterministic context, constrained git, and HEAD-bound verification tests."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import json
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness import capabilities
from harness.contracts import RuntimeRoute
from harness.context import (
    CONTEXT_ROLES,
    OUTCOME_POINTER_ID,
    ContextBuilder,
    ContextInput,
    outcome_contract_input,
)
from harness.git_ops import GitAdapter, GitError
from harness.verification import (
    compose_commands,
    load_profiles,
    output_binding_valid,
    run_profile,
    valid_for,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="harness-context.") as raw:
    root = Path(raw)
    inputs = (
        ContextInput("plan", "wiki/plan.md", b"approved plan", role="plan"),
        ContextInput("instructions", "AGENTS.md", b"repo rules", role="instructions"),
        ContextInput("exact-head.txt", "git:HEAD", b"abc\n", role="head"),
        ContextInput(
            "head-diff.patch", "git:show:HEAD", b"diff --git\n", role="diff"
        ),
        ContextInput.pointer(
            "diff",
            ".vault-meta/context/diff.patch",
            byte_count=500_000,
            content_sha256="d" * 64,
            role="diff",
        ),
    )
    one = ContextBuilder(root / "packets").build("op-1", inputs, metadata={"head": "abc", "base": "def"})
    two = ContextBuilder(root / "packets").build("op-1", tuple(reversed(inputs)), metadata={"base": "def", "head": "abc"})
    check("unchanged inputs produce byte-identical manifest", one == two)
    packet_dir = root / "packets" / one.packet_id
    manifest = json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
    check(
        "ContextPacket carries typed deterministic roles",
        [row["role"] for row in manifest["inputs"]]
        == ["diff", "diff", "head", "instructions", "plan"],
    )
    check(
        "large ContextPacket inputs remain pointers",
        manifest["inputs"][0]["storage"] == "pointer"
        and not any("000-diff-diff" in name for name in one.files),
    )
    check(
        "text ContextPacket inputs keep readable validated extensions",
        any(name.endswith("-plan-plan.md") for name in one.files)
        and any(
            name.endswith("-instructions-instructions.md")
            for name in one.files
        )
        and any(name.endswith("-head-exact-head.txt") for name in one.files)
        and any(name.endswith("-diff-head-diff.patch") for name in one.files),
    )
    check(
        "ContextPacket role vocabulary covers the approved handoff",
        {
            "task",
            "plan",
            "instructions",
            "reference",
            "base",
            "head",
            "diff",
            "finding",
            "resolution",
            "fix",
            "route",
            "permissions",
            "verification",
            "outcome",
        }
        <= CONTEXT_ROLES,
    )
    outcome_plan = root / "approved-plan.md"
    outcome_plan.write_text(
        "# Plan\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Preserve the outcome.",'
        '"success_evidence":[{"evidence_id":"packet-delivered",'
        '"observable":"The canonical contract is in the ContextPacket."}],'
        '"non_goals":["No permission expansion."]}\n```\n',
        encoding="utf-8",
    )
    outcome_input = outcome_contract_input(
        outcome_plan,
        expected_sha256=(
            "533bee36ee156939bf3311cde3ea75d4eafb374d8e0aae5db0cf8da16f97f8f2"
        ),
    )
    outcome_packet = ContextBuilder(root / "outcome-packets").build(
        "op-outcome", (outcome_input,), metadata={"task": "fixture"}
    )
    outcome_manifest = json.loads(
        (
            root
            / "outcome-packets"
            / outcome_packet.packet_id
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "built-in ContextPacket delivers the reserved canonical outcome input",
        outcome_manifest["inputs"] == [
            {
                "name": "outcome-contract.json",
                "role": "outcome",
                "source": str(outcome_plan.resolve()),
                "storage": "inline",
                "bytes": len(outcome_input.content or b""),
                "sha256": outcome_input.content_sha256,
                "pointer_id": OUTCOME_POINTER_ID,
            }
        ],
    )
    try:
        outcome_contract_input(outcome_plan, expected_sha256="0" * 64)
    except (ValueError, RuntimeError):
        check("ContextPacket rejects outcome identity drift", True)
    else:
        check("ContextPacket rejects outcome identity drift", False)
    try:
        ContextBuilder(root / "bad").build(
            "op-raw",
            (ContextInput("conversation", "raw-conversation", b"verbatim chat"),),
            metadata={},
        )
    except (ValueError, RuntimeError):
        check("raw conversation is excluded from ContextPacket", True)
    else:
        check("raw conversation is excluded from ContextPacket", False)
    try:
        ContextBuilder(root / "too-large", max_inline_bytes=4).build(
            "op-large",
            (ContextInput("task", "task.md", b"too large", role="task"),),
            metadata={},
        )
    except (ValueError, RuntimeError):
        check("large inline content must use a pointer", True)
    else:
        check("large inline content must use a pointer", False)

    calls: list[list[str]] = []
    def fake_git(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["rev-parse", "HEAD"]:
            output = "a" * 40 + "\n"
        elif command[1] == "merge-base":
            output = "b" * 40 + "\n"
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    git = GitAdapter(root, fake_git)
    snapshot = git.inspect("main")
    check("git snapshot binds base and head", snapshot.head == "a" * 40 and snapshot.base == "b" * 40)
    try:
        git._run(["push"])
    except GitError:
        check("git adapter rejects push", True)
    else:
        check("git adapter rejects push", False)
    try:
        git.stage_exact(["../outside"])
    except GitError:
        check("git adapter rejects broad/escaping stage", True)
    else:
        check("git adapter rejects broad/escaping stage", False)
    for pathspec in (".", "*.py", "scripts/"):
        try:
            git.stage_exact([pathspec])
        except GitError:
            check(f"git adapter rejects broad pathspec {pathspec}", True)
        else:
            check(f"git adapter rejects broad pathspec {pathspec}", False)
    git.stage_exact(["scripts/example.py"], authorized=True)
    check(
        "git adapter preserves one exact literal stage path",
        calls[-1] == ["git", "add", "--", "scripts/example.py"],
    )
    try:
        git.stage_exact(["scripts/example.py"], authorized=False)
    except GitError:
        check("git adapter requires explicit stage authorization", True)
    else:
        check("git adapter requires explicit stage authorization", False)

    repo = root / "source"
    target = root / "owned-worktree"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "harness@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Harness Test"],
        cwd=repo,
        check=True,
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    main_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (repo / "user-dirty.txt").write_text("preserve\n", encoding="utf-8")
    real_git = GitAdapter(repo)
    child = real_git.create_worktree(target, "prototype/op-1", "HEAD")
    check(
        "git adapter creates one exact owned non-main worktree",
        child.root == target.resolve() and child.branch == "prototype/op-1",
    )
    (target / "probe.txt").write_text("disposable\n", encoding="utf-8")
    try:
        real_git.cleanup_owned_worktree(target)
    except GitError:
        check("owned cleanup rejects implicit dirty discard", True)
    else:
        check("owned cleanup rejects implicit dirty discard", False)
    real_git.cleanup_owned_worktree(target, discard=True)
    current_main = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    check(
        "owned cleanup preserves main and unrelated dirty user work",
        not target.exists()
        and current_main == main_head
        and (repo / "user-dirty.txt").read_text(encoding="utf-8")
        == "preserve\n",
    )

    callback_dir = root / "missing-callback-dir"
    route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "reviewer-callback",
        "a" * 64,
    )
    original_which = capabilities.shutil.which
    capabilities.shutil.which = lambda name: f"/usr/bin/{name}"
    try:
        capabilities.check(route, callback_dir=callback_dir)
    finally:
        capabilities.shutil.which = original_which
    check(
        "capability handshake never creates callback state",
        not callback_dir.exists(),
    )
    callback_dir.mkdir()

    def incomplete_cmux_probe(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "generic help\n", "")

    incomplete = capabilities.check(
        route,
        callback_dir=callback_dir,
        which=lambda name: f"/usr/bin/{name}",
        runner=incomplete_cmux_probe,
    )
    check(
        "capability handshake rejects incomplete cmux command support",
        not incomplete.compatible,
    )

    def complete_cmux_probe(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        args = command[1:]
        if args == ["new-split", "--help"]:
            output = "--surface --focus --json\n"
        elif args == ["workspace", "create", "--help"]:
            output = "create [flags] close <workspace>\n"
        elif args == ["new-workspace", "--help"]:
            output = "--window --focus --json\n"
        elif args == ["workspace", "close", "--help"]:
            output = "close <workspace> --window\n"
        elif args == ["identify", "--help"]:
            output = "--surface --json\n"
        elif args == ["surface", "resume", "--help"]:
            output = (
                "resume get resume set resume show resume clear "
                "--surface --json\n"
            )
        elif args == ["close-surface", "--help"]:
            output = "--surface\n"
        elif args == ["--help"]:
            output = "--model --config --sandbox --ask-for-approval\n"
        elif args == ["login", "status"]:
            output = "Logged in with ChatGPT\n"
        else:
            return subprocess.CompletedProcess(command, 1, "", "unsupported")
        return subprocess.CompletedProcess(command, 0, output, "")

    invalid_profile = capabilities.check(
        replace(route, profile="unknown-profile"),
        callback_dir=callback_dir,
        which=lambda name: f"/usr/bin/{name}",
        runner=complete_cmux_probe,
    )
    check(
        "capability handshake rejects unsupported provider profile",
        not invalid_profile.compatible,
    )
    complete = capabilities.check(
        route,
        callback_dir=callback_dir,
        which=lambda name: f"/usr/bin/{name}",
        runner=complete_cmux_probe,
    )
    check(
        "capability handshake proves the read-only host contract",
        complete.compatible
        and {
            "cmux:anchored-split",
            "cmux:typed-resume",
            "provider:model-effort-profile",
            "callback:writable",
        }
        <= set(complete.capabilities),
    )

    claude_route = replace(route, runtime="claude", model="claude-opus-5")
    claude_help_calls: list[str] = []

    def truncated_claude_help_probe(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        args = command[1:]
        if Path(command[0]).name == "cmux":
            return complete_cmux_probe(command, **kwargs)
        if args == ["--help"]:
            if kwargs.get("capture_output"):
                claude_help_calls.append("pipe")
                return subprocess.CompletedProcess(
                    command, 0, "Claude Code\n" + ("x" * 500), ""
                )
            claude_help_calls.append("regular-file")
            output = kwargs["stdout"]
            assert hasattr(output, "write")
            output.write("--model --effort --permission-mode\n")
            output.flush()
            return subprocess.CompletedProcess(command, 0, "", "")
        if args == ["auth", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "claude.ai",
                        "apiProvider": "firstParty",
                        "subscriptionType": "team",
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 1, "", "unsupported")

    truncated_claude = capabilities.check(
        claude_route,
        callback_dir=callback_dir,
        which=lambda name: f"/usr/bin/{name}",
        runner=truncated_claude_help_probe,
    )
    check(
        "Claude capability handshake recovers pipe-truncated help from a regular file",
        truncated_claude.compatible
        and claude_help_calls == ["pipe", "regular-file"],
    )

    def incomplete_claude_help_probe(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        result = truncated_claude_help_probe(command, **kwargs)
        if command[1:] == ["--help"] and not kwargs.get("capture_output"):
            output = kwargs["stdout"]
            output.seek(0)
            output.truncate(0)
            output.write("--model --effort\n")
            output.flush()
        return result

    incomplete_claude = capabilities.check(
        claude_route,
        callback_dir=callback_dir,
        which=lambda name: f"/usr/bin/{name}",
        runner=incomplete_claude_help_probe,
    )
    check(
        "Claude capability handshake rejects incomplete regular-file help",
        not incomplete_claude.compatible,
    )

profiles = load_profiles(ROOT / "config/verification-profiles.toml")
check(
    "six verification profiles load",
    set(profiles)
    == {
        "baseline",
        "scoped",
        "full",
        "conflict",
        "vault",
        "research-cited-artifact",
    },
)
check(
    "model checks append after the configured verification gate",
    compose_commands(
        profiles["baseline"],
        ("python3 -m py_compile scripts/harness/context.py",),
    )
    == profiles["baseline"].commands
    + ("python3 -m py_compile scripts/harness/context.py",),
)
with tempfile.TemporaryDirectory(dir=ROOT, prefix=".verify-test.") as raw:
    evidence_dir = Path(raw).relative_to(ROOT) / "evidence"
    invoked: list[tuple[str, ...]] = []
    def fake_verify(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "c" * 40 + "\n", "")
        invoked.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "green\n", "")
    evidence = run_profile(
        profiles["baseline"],
        root=ROOT,
        evidence_dir=ROOT / evidence_dir,
        runner=fake_verify,
        extra_commands=("python3 -m py_compile scripts/harness/context.py",),
    )
    check(
        "verification executes configured order before appended model checks",
        invoked
        == [
            tuple(command.split())
            for command in (
                *profiles["baseline"].commands,
                "python3 -m py_compile scripts/harness/context.py",
            )
        ],
    )
    check("verification evidence binds HEAD and profile", all(valid_for(row, head="c" * 40, profile=profiles["baseline"]) for row in evidence))
    check(
        "verification evidence binds the exact persisted output bytes",
        all(
            row.schema_version == 2
            and row.output_sha256
            and row.output_bytes == len(b"green\n")
            and output_binding_valid(row, pointer_root=ROOT)
            for row in evidence
        ),
    )
    first_output = ROOT / evidence[0].output_pointer
    original_output = first_output.read_bytes()
    first_output.write_bytes(original_output + b"tampered\n")
    check(
        "verification output tamper invalidates the evidence binding",
        not output_binding_valid(evidence[0], pointer_root=ROOT),
    )
    first_output.write_bytes(original_output)
    check("stale HEAD evidence rejected", not valid_for(evidence[0], head="d" * 40, profile=profiles["baseline"]))
    check(
        "failed command evidence is never reusable",
        not valid_for(
            replace(evidence[0], exit_code=1),
            head="c" * 40,
            profile=profiles["baseline"],
        ),
    )
    check(
        "wrong named profile evidence is rejected",
        not valid_for(
            replace(evidence[0], profile="scoped"),
            head="c" * 40,
            profile=profiles["baseline"],
        ),
    )
