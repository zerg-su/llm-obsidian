#!/usr/bin/env python3
"""Behavior tests for the release-owned Architecture Workflow pressure runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "architecture_workflow_pressure.py"
spec = importlib.util.spec_from_file_location("architecture_workflow_pressure_test", RUNNER)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def check(label: str, value: bool, detail: str = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def pressure_case(letter: str, capability: str, effect_mode: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": f"architecture-workflow.{letter}",
        "capability": capability,
        "input": {
            "scenario": f"Exercise Architecture Workflow pressure case {letter.upper()}.",
        },
        "response_contract": {
            "carrier": ["architecture", "decompose", "implementation-plan"],
        },
        "assertions": [
            {"kind": "equals", "path": "artifacts.carrier", "value": capability},
            {
                "kind": "equals",
                "path": "artifacts.contract_observed",
                "value": True,
            },
        ],
        "pressure": {
            "case_id": letter,
            "expected_carrier": capability,
            "effect_mode": effect_mode,
            "fixture_vault": None if effect_mode == "read-only" else f"fixture-{letter}",
            "assertions": [f"case {letter.upper()} observed its carrier boundary"],
        },
    }


check(
    "Architecture Workflow capabilities bind their governing sources",
    module.SOURCES
    == {
        "architecture": (
            "skills/architecture/SKILL.md",
            "docs/skill-references/architecture-artifacts.md",
        ),
        "decompose": (
            "skills/decompose/SKILL.md",
            "docs/skill-references/architecture-artifacts.md",
        ),
        "implementation-plan": (
            "skills/implementation-plan/SKILL.md",
            "docs/skill-references/engineering-quality-contract.md",
            "docs/skill-references/architecture-artifacts.md",
        ),
    },
)

for invalid_session in ("", "fresh", "not-a-codex-thread", "A" * 36):
    try:
        module.validate_child_session_id(invalid_session)
    except module.RunnerError:
        pass
    else:
        raise AssertionError(f"arbitrary child session identity was accepted: {invalid_session!r}")
print("OK   child provenance requires a canonical Codex thread identity")


with tempfile.TemporaryDirectory(prefix="architecture-pressure-runner.") as raw:
    tmp = Path(raw)
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
cwd = pathlib.Path(args[args.index("--cd") + 1])
prompt = sys.stdin.read() if args[-1] == "-" else args[-1]
mode = args[args.index("--sandbox") + 1]
if mode == "workspace-write" and "--skip-git-repo-check" not in args:
    raise SystemExit(7)
if mode == "read-only" and "--skip-git-repo-check" in args:
    raise SystemExit(9)
if mode not in {"workspace-write", "read-only"}:
    raise SystemExit(10)
if "sandbox_workspace_write.network_access=false" not in args:
    raise SystemExit(8)
capability = prompt.split("Capability: ", 1)[1].splitlines()[0]
if "MUTATE_FIXTURE" in prompt:
    (cwd / "provider-effect.txt").write_text("bounded fixture effect\\n", encoding="utf-8")
if "IMPORT_FIXTURE_MODULE" in prompt:
    subprocess.run([sys.executable, str(cwd / "verify-fixture.py")], cwd=cwd, check=True)
result = {
    "output": "Observed contract at /Users/operator/private",
    "artifacts": {"carrier": capability, "contract_observed": True},
}
output.write_text(json.dumps(result), encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "019f72c4-816e-7200-a399-505adaa350e0"}))
print(json.dumps({"type": "turn.completed"}))
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{env_path}"
    evidence_root = tmp / "evidence"
    subject_head = "a" * 40
    fixture_context = tempfile.TemporaryDirectory(
        prefix=f"architecture-workflow-v1-{subject_head}-"
    )
    fixture_parent = Path(fixture_context.name)
    (fixture_parent / module.FIXTURE_PARENT_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "architecture-pressure-parent",
                "subject_head": subject_head,
            }
        ),
        encoding="utf-8",
    )
    zero_fixture = fixture_parent / "f"
    zero_fixture.mkdir()
    (zero_fixture / module.FIXTURE_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "architecture-pressure-fixture",
                "subject_head": subject_head,
                "case_id": "f",
                "fixture_id": "fixture-f",
            }
        ),
        encoding="utf-8",
    )
    (zero_fixture / "seed.txt").write_text("stable\n", encoding="utf-8")
    mutation_fixture = fixture_parent / "g"
    mutation_fixture.mkdir()
    (mutation_fixture / module.FIXTURE_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "architecture-pressure-fixture",
                "subject_head": subject_head,
                "case_id": "g",
                "fixture_id": "fixture-g",
            }
        ),
        encoding="utf-8",
    )
    (mutation_fixture / "seed.txt").write_text("before\n", encoding="utf-8")
    try:
        zero_record = module.execute_pressure_case(
            pressure_case("f", "decompose", "zero-effect"),
            route_alias="terra",
            effort="medium",
            timeout=30.0,
            subject_head=subject_head,
            fixture_root=zero_fixture,
            evidence_root=evidence_root,
        )
        mutation_case = pressure_case("g", "decompose", "fixture-mutation")
        mutation_case["input"]["scenario"] += " MUTATE_FIXTURE"
        mutation_record = module.execute_pressure_case(
            mutation_case,
            route_alias="terra",
            effort="medium",
            timeout=30.0,
            subject_head=subject_head,
            fixture_root=mutation_fixture,
            evidence_root=evidence_root,
        )
        read_only_record = module.execute_pressure_case(
            pressure_case("h", "architecture", "read-only"),
            route_alias="terra",
            effort="medium",
            timeout=30.0,
            subject_head=subject_head,
            fixture_root=None,
            evidence_root=evidence_root,
        )
    finally:
        os.environ["PATH"] = env_path

    routing = module.load_config(module.ROOT)
    check(
        "pressure receipt binds the registered alias and exact resolved route",
        zero_record["execution_provenance"]["route"]
        == {
            "alias": "terra",
            "runtime": "codex",
            "model": "gpt-5.6-terra",
            "effort": "medium",
            "routing_sha256": routing.fingerprint,
        },
    )
    check(
        "pressure receipt binds child and executing coordinator sessions",
        zero_record["execution_provenance"]["session_id"]
        == "019f72c4-816e-7200-a399-505adaa350e0"
        and zero_record["execution_provenance"]["coordinator_session_id"]
        == module.current_coordinator_session_id()
        and zero_record["subject_head"] == subject_head,
    )
    check(
        "zero-effect fixture state is unchanged",
        zero_record["pre_state_sha256"] == zero_record["post_state_sha256"]
        and zero_record["pre_state_manifest"] == zero_record["post_state_manifest"]
        and zero_record["fixture_vault"] == "fixture-f",
    )
    check(
        "fixture mutation is measured by the runner",
        mutation_record["pre_state_sha256"] != mutation_record["post_state_sha256"]
        and mutation_record["pre_state_manifest"] != mutation_record["post_state_manifest"]
        and (mutation_fixture / "provider-effect.txt").is_file(),
    )
    check(
        "fixture trust bypass is scoped away from repository read-only runs",
        read_only_record["effect_mode"] == "read-only",
    )
    transcript = evidence_root / "pressure" / "f.md"
    record_path = evidence_root / "pressure" / "f.json"
    transcript_text = transcript.read_text(encoding="utf-8")
    check(
        "one sanitized digest-bound transcript and record are published per case",
        record_path.is_file()
        and zero_record == json.loads(record_path.read_text(encoding="utf-8"))
        and zero_record["execution_provenance"]["transcript_path"].endswith(
            "/pressure/f.md"
        )
        and zero_record["execution_provenance"]["transcript_sha256"]
        == hashlib.sha256(transcript.read_bytes()).hexdigest()
        and "/Users/operator" not in transcript_text
    )
    check(
        "grading authority stays out of the model transcript",
        '"expected_carrier"' not in transcript_text
        and '"assertions"' not in transcript_text,
    )
    original_pressure_run = module._pressure_provider_run
    credential_publication_failures: list[str] = []
    credential_vectors = (
        (
            "scenario-input",
            "token=abcdefghijklmnop",
            "Clean model output.",
            "019f72c4-816e-7200-a399-505adaa350e2",
        ),
        (
            "model-output",
            "No credential in this scenario.",
            "api_key=abcdefghijklmnop",
            "019f72c4-816e-7200-a399-505adaa350e3",
        ),
    )
    try:
        for label, scenario_suffix, output, child_session in credential_vectors:
            credential_case = pressure_case("h", "architecture", "read-only")
            credential_case["input"]["scenario"] += f" {scenario_suffix}"
            response = {
                "output": output,
                "artifacts": {
                    "carrier": "architecture",
                    "contract_observed": True,
                },
            }

            def credential_provider(case, **kwargs):
                return response, module.pressure_prompt_for(case), child_session

            module._pressure_provider_run = credential_provider
            credential_evidence = tmp / f"credential-{label}-evidence"
            try:
                module.execute_pressure_case(
                    credential_case,
                    route_alias="terra",
                    effort="medium",
                    timeout=30.0,
                    subject_head=subject_head,
                    fixture_root=None,
                    evidence_root=credential_evidence,
                )
            except module.RunnerError as exc:
                if "credential" not in str(exc) or "record" not in str(exc):
                    credential_publication_failures.append(
                        f"{label} returned the wrong rejection: {exc}"
                    )
            else:
                credential_publication_failures.append(f"{label} was accepted")
            published = (
                list((credential_evidence / "pressure").iterdir())
                if (credential_evidence / "pressure").is_dir()
                else []
            )
            if published:
                credential_publication_failures.append(
                    f"{label} published: {', '.join(path.name for path in published)}"
                )
    finally:
        module._pressure_provider_run = original_pressure_run
    check(
        "credential-shaped scenario and model output publish no evidence",
        not credential_publication_failures,
        "; ".join(credential_publication_failures),
    )
    try:
        module.execute_pressure_case(
            pressure_case("h", "architecture", "read-only"),
            route_alias="gpt-5.6-terra",
            effort="medium",
            timeout=30.0,
            subject_head=subject_head,
            fixture_root=None,
            evidence_root=evidence_root,
        )
    except module.RunnerError as exc:
        check(
            "pressure execution rejects concrete models in place of route aliases",
            "registered routing alias" in str(exc),
            str(exc),
        )
    else:
        raise AssertionError("pressure execution accepted a concrete model")

    arbitrary_fixture = tmp / "valuable-unowned-directory"
    arbitrary_fixture.mkdir()
    (arbitrary_fixture / "valuable.txt").write_text("preserve\n", encoding="utf-8")
    original_pressure_run = module._pressure_provider_run
    module._pressure_provider_run = lambda case, **kwargs: (
        {
            "output": "Unsafe workspace was admitted.",
            "artifacts": {"carrier": "decompose", "contract_observed": True},
        },
        module.pressure_prompt_for(case),
        "019f72c4-816e-7200-a399-505adaa350e1",
    )
    try:
        try:
            module.execute_pressure_case(
                pressure_case("m", "decompose", "zero-effect"),
                route_alias="terra",
                effort="medium",
                timeout=30.0,
                subject_head=subject_head,
                fixture_root=arbitrary_fixture,
                evidence_root=evidence_root,
            )
        except module.RunnerError as exc:
            check(
                "pressure execution rejects an arbitrary markerless workspace",
                "fixture" in str(exc),
                str(exc),
            )
        else:
            raise AssertionError("pressure execution accepted an arbitrary workspace")
    finally:
        module._pressure_provider_run = original_pressure_run

    symlink_fixture = fixture_parent / "m"
    symlink_fixture.mkdir()
    (symlink_fixture / module.FIXTURE_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "architecture-pressure-fixture",
                "subject_head": subject_head,
                "case_id": "m",
                "fixture_id": "fixture-m",
            }
        ),
        encoding="utf-8",
    )
    (symlink_fixture / "unsafe-link").symlink_to(arbitrary_fixture / "valuable.txt")
    try:
        module.execute_pressure_case(
            pressure_case("m", "decompose", "zero-effect"),
            route_alias="terra",
            effort="medium",
            timeout=30.0,
            subject_head=subject_head,
            fixture_root=symlink_fixture,
            evidence_root=evidence_root,
        )
    except module.RunnerError as exc:
        check(
            "pressure execution rejects a symlinked fixture entry before launch",
            "symlink" in str(exc),
            str(exc),
        )
    else:
        raise AssertionError("pressure execution accepted a symlinked fixture")

    import_fixture = fixture_parent / "s"
    (import_fixture / "scripts").mkdir(parents=True)
    (import_fixture / module.FIXTURE_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "architecture-pressure-fixture",
                "subject_head": subject_head,
                "case_id": "s",
                "fixture_id": "fixture-s",
            }
        ),
        encoding="utf-8",
    )
    (import_fixture / "scripts/fixture_module.py").write_text(
        "VALUE = 'stable'\n", encoding="utf-8"
    )
    (import_fixture / "verify-fixture.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent / 'scripts'))\n"
        "import fixture_module\nassert fixture_module.VALUE == 'stable'\n",
        encoding="utf-8",
    )
    import_case = pressure_case("s", "decompose", "zero-effect")
    import_case["input"]["scenario"] += " IMPORT_FIXTURE_MODULE"
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{env_path}"
    try:
        import_record = module.execute_pressure_case(
            import_case,
            route_alias="terra",
            effort="medium",
            timeout=30.0,
            subject_head=subject_head,
            fixture_root=import_fixture,
            evidence_root=evidence_root,
        )
    finally:
        os.environ["PATH"] = env_path
    check(
        "fixture-local Python inspection preserves the full zero-effect denominator",
        import_record["pre_state_manifest"] == import_record["post_state_manifest"]
        and not (import_fixture / "scripts/__pycache__").exists(),
    )
    fixture_context.cleanup()

print("\nAll Architecture Workflow pressure runner tests passed.")
