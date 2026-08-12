#!/usr/bin/env python3
"""Runtime routing matrix for typed pipeline submit failures."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import AttentionReason  # noqa: E402
from harness.runtime_worker_control import RuntimeWorkerControlMixin  # noqa: E402


def rejected(failure_class: str, error_code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["pipeline-step-submit.py"],
        2,
        "",
        json.dumps(
            {
                "schema_version": 1,
                "status": "rejected",
                "failure_class": failure_class,
                "error_code": error_code,
                "detail": "bounded",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


class Worker(RuntimeWorkerControlMixin):
    def __init__(self) -> None:
        self.corrections: list[tuple[str, str]] = []
        self.attention: list[tuple[str, AttentionReason]] = []

    def request_pipeline_step_correction(
        self, invalid_sha256: str, *, stage: str
    ) -> None:
        self.corrections.append((invalid_sha256, stage))

    def summary_attention(
        self, status: str, reason: AttentionReason
    ) -> None:
        self.attention.append((status, reason))


with tempfile.TemporaryDirectory(prefix="pipeline-submit-boundary.") as raw:
    root = Path(raw)
    callback = root / "callback.json"
    digest = "a" * 64

    semantic = Worker()
    semantic_receipt = root / "semantic" / "submit-failed.json"
    semantic_receipt.parent.mkdir()
    handled = semantic.handle_pipeline_step_submit_failure(
        rejected("model-semantic", "result-semantics-rejected"),
        callback,
        receipt_path=semantic_receipt,
        operation_id="semantic-step",
        invalid_sha256=digest,
        stage="pipeline-fix-submit",
    )
    semantic_row = json.loads(semantic_receipt.read_text(encoding="utf-8"))
    if (
        not handled
        or semantic.corrections != [(digest, "pipeline-fix-submit")]
        or semantic.attention
        or semantic_row["failure_class"] != "model-semantic"
    ):
        raise AssertionError((semantic.corrections, semantic.attention, semantic_row))
    print("OK   invalid model semantics consume the registered correction budget")

    authority = Worker()
    authority_receipt = root / "authority" / "submit-failed.json"
    authority_receipt.parent.mkdir()
    authority.handle_pipeline_step_submit_failure(
        rejected("code-authority", "submit-authority-rejected"),
        callback,
        receipt_path=authority_receipt,
        operation_id="authority-step",
        invalid_sha256=digest,
        stage="pipeline-custom-submit",
    )
    authority_row = json.loads(authority_receipt.read_text(encoding="utf-8"))
    if (
        authority.corrections
        or authority.attention
        != [
            (
                "pipeline-custom-submit-code-authority",
                AttentionReason.CONTRACT_DRIFT,
            )
        ]
        or authority_row["failure_class"] != "code-authority"
    ):
        raise AssertionError((authority.corrections, authority.attention, authority_row))
    print("OK   code-owned authority failures bypass model correction")

    mechanism = Worker()
    mechanism_receipt = root / "mechanism" / "submit-failed.json"
    mechanism_receipt.parent.mkdir()
    mechanism.handle_pipeline_step_submit_failure(
        subprocess.CompletedProcess(["pipeline-step-submit.py"], 2, "", "boom\n"),
        callback,
        receipt_path=mechanism_receipt,
        operation_id="mechanism-step",
        invalid_sha256=digest,
        stage="pipeline-fix-submit",
    )
    if (
        mechanism.corrections
        or mechanism.attention
        != [
            (
                "pipeline-fix-submit-mechanism",
                AttentionReason.ATTENTION_REQUIRED,
            )
        ]
    ):
        raise AssertionError((mechanism.corrections, mechanism.attention))
    print("OK   untyped mechanism failures fail closed without correction")

print("pipeline submit failure boundary: ok")
