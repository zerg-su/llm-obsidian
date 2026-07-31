#!/usr/bin/env python3
"""Bounded RT03 experiment for a same-session review delta packet.

Run with:

    python3 prototypes/rt03-review-delta-packet.py

The experiment creates an isolated temporary Git repository and emits one
bounded JSON result. It does not change the product checkout.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.context import ContextBuilder, ContextInput  # noqa: E402


QUESTION = (
    "Can an exact same-session reviewer receive a machine-built delta packet "
    "that preserves the original context binding and material resolution evidence?"
)
SUCCESS = (
    "The packet is deterministic, binds the original manifest and both HEADs, "
    "covers net changes across all resolution commits, and produces the same "
    "bounded oracle verdict as inspection of the exact resolved tree."
)


def run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("bounded Git fixture failed")
    return result.stdout


def commit(repo: Path, message: str, *paths: str) -> str:
    run(repo, "add", "--", *paths)
    run(repo, "commit", "-m", message)
    return run(repo, "rev-parse", "HEAD").strip()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def packet_manifest(
    packet_root: Path, packet_id: str
) -> tuple[Path, dict[str, object]]:
    path = packet_root / packet_id / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("prototype manifest is invalid")
    return path, value


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rt03-review-delta.") as raw:
        root = Path(raw)
        repo = root / "product"
        packets = root / "packets"
        repo.mkdir()
        run(repo, "init", "-b", "main")
        run(repo, "config", "user.email", "rt03@example.invalid")
        run(repo, "config", "user.name", "RT03 Prototype")

        (repo / "totals.py").write_text(
            "def total(items):\n    return len(items)\n", encoding="utf-8"
        )
        reviewed_head = commit(repo, "reviewed candidate", "totals.py")

        original = ContextBuilder(packets).build(
            "rt03-original",
            (
                ContextInput(
                    "task.md",
                    "approved-task",
                    b"Return the numeric total and preserve regression coverage.\n",
                    role="task",
                ),
                ContextInput(
                    "review-standard.md",
                    "review-skill",
                    b"Report material correctness and regression gaps.\n",
                    role="instructions",
                ),
                ContextInput(
                    "reviewed-head.txt",
                    "git:reviewed-head",
                    (reviewed_head + "\n").encode(),
                    role="head",
                ),
            ),
            metadata={"head_sha": reviewed_head, "phase": "initial-review"},
        )
        original_manifest_path, _original_manifest = packet_manifest(
            packets, original.packet_id
        )
        original_manifest_bytes = original_manifest_path.read_bytes()
        original_manifest_sha = hashlib.sha256(original_manifest_bytes).hexdigest()

        finding = {
            "finding_id": "F-rt03-1",
            "severity": "important",
            "file": "totals.py",
            "summary": "total returns the item count instead of the numeric sum",
        }
        finding_bytes = canonical(finding) + b"\n"

        (repo / "totals.py").write_text(
            "def total(items):\n    return sum(items)\n", encoding="utf-8"
        )
        commit(repo, "resolve material finding", "totals.py")
        (repo / "test_totals.py").write_text(
            "from totals import total\n\nassert total([2, 3]) == 5\n",
            encoding="utf-8",
        )
        resolved_head = commit(repo, "add regression coverage", "test_totals.py")

        head_only_diff = run(
            repo,
            "show",
            "--format=fuller",
            "--stat",
            "--patch",
            "--find-renames",
            "HEAD",
        ).encode()
        head_only_files = tuple(
            row
            for row in run(
                repo,
                "show",
                "--format=",
                "--name-only",
                "HEAD",
                "--",
            ).splitlines()
            if row
        )
        resolution_diff = run(
            repo,
            "diff",
            "--binary",
            "--find-renames",
            reviewed_head,
            resolved_head,
            "--",
        ).encode()
        changed_files = tuple(
            row
            for row in run(
                repo,
                "diff",
                "--name-only",
                reviewed_head,
                resolved_head,
                "--",
            ).splitlines()
            if row
        )

        inputs = (
            ContextInput.pointer(
                "original-context-manifest.json",
                str(original_manifest_path),
                byte_count=len(original_manifest_bytes),
                content_sha256=original_manifest_sha,
                role="base",
            ),
            ContextInput(
                "review-finding.json",
                "review-gate:awaiting-resolution",
                finding_bytes,
                role="finding",
            ),
            ContextInput(
                "reviewed-head.txt",
                "git:reviewed-head",
                (reviewed_head + "\n").encode(),
                role="head",
            ),
            ContextInput(
                "resolved-head.txt",
                "git:resolved-head",
                (resolved_head + "\n").encode(),
                role="head",
            ),
            ContextInput(
                "resolution.patch",
                f"git:diff:{reviewed_head}..{resolved_head}",
                resolution_diff,
                role="resolution",
            ),
            ContextInput(
                "verification.json",
                "prototype:bounded-oracle",
                canonical({"changed_files": list(changed_files)}) + b"\n",
                role="verification",
            ),
        )
        metadata = {
            "base_context_sha256": original_manifest_sha,
            "head_sha": resolved_head,
            "phase": "same-session-verification",
            "reviewed_head_sha": reviewed_head,
        }
        one = ContextBuilder(packets).build(
            "rt03-verification", inputs, metadata=metadata
        )
        two = ContextBuilder(packets).build(
            "rt03-verification",
            tuple(reversed(inputs)),
            metadata=dict(reversed(tuple(metadata.items()))),
        )
        _delta_manifest_path, delta_manifest = packet_manifest(
            packets, one.packet_id
        )
        manifest_inputs = delta_manifest.get("inputs")
        if not isinstance(manifest_inputs, list):
            raise RuntimeError("prototype delta manifest inputs are invalid")
        original_row = next(
            row
            for row in manifest_inputs
            if isinstance(row, dict)
            and row.get("name") == "original-context-manifest.json"
        )

        exact_tree_verdict = (
            "resolved"
            if (repo / "totals.py").read_text(encoding="utf-8")
            == "def total(items):\n    return sum(items)\n"
            and (repo / "test_totals.py").is_file()
            else "changes-requested"
        )
        delta_verdict = (
            "resolved"
            if finding["file"] in changed_files
            and b"return sum(items)" in resolution_diff
            and b"test_totals.py" in resolution_diff
            else "changes-requested"
        )
        checks = {
            "deterministic_packet": one == two,
            "original_context_exactly_bound": (
                original_row.get("storage") == "pointer"
                and original_row.get("sha256") == original_manifest_sha
                and delta_manifest.get("metadata", {}).get("base_context_sha256")
                == original_manifest_sha
            ),
            "review_boundary_exactly_bound": (
                delta_manifest.get("metadata", {}).get("reviewed_head_sha")
                == reviewed_head
                and delta_manifest.get("metadata", {}).get("head_sha")
                == resolved_head
            ),
            "multi_commit_resolution_covered": (
                changed_files == ("test_totals.py", "totals.py")
                and b"totals.py" in resolution_diff
                and b"test_totals.py" in resolution_diff
            ),
            "head_only_packet_misses_earlier_fix": (
                finding["file"] not in head_only_files
            ),
            "bounded_oracle_verdict_matches_exact_tree": (
                delta_verdict == exact_tree_verdict == "resolved"
            ),
        }
        result = {
            "schema_version": 1,
            "operation_id": "rt03-review-delta-prototype",
            "question": QUESTION,
            "success_criterion": SUCCESS,
            "checks": checks,
            "metrics": {
                "changed_file_count": len(changed_files),
                "head_only_changed_file_count": len(head_only_files),
                "delta_bytes": len(resolution_diff),
                "head_only_bytes": len(head_only_diff),
                "packet_bytes": one.byte_count,
            },
            "decision": "structural-pass-live-review-quality-unproven",
            "limitation": (
                "The deterministic oracle proves evidence completeness for this fixture; "
                "it does not measure a live model reviewer's judgment quality."
            ),
        }
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
