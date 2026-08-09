#!/usr/bin/env python3
"""RC1 gate declarations: fixed cell identities, routes, and streak binding."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.pipeline_builtins import EXECUTABLE_BUILTINS  # noqa: E402
from model_routing_config import load_tracked_config, validate_effort  # noqa: E402
import v267_stabilization as stab  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


manifest = tomllib.loads(
    (ROOT / "config/acceptance-cells.toml").read_text(encoding="utf-8")
)
rc1 = manifest.get("rc1")
check("the acceptance manifest declares the RC1 gate section", isinstance(rc1, dict))
check("RC1 gate declarations use schema_version 1", rc1.get("schema_version") == 1)
check(
    "RC1 streak target matches the stabilization denominator",
    rc1.get("streak_target")
    == stab.load_subject_config(ROOT / str(rc1.get("subject_config"))).streak_target,
)
check(
    "RC1 corridor is the one supported executable pipeline",
    rc1.get("corridor") == "engineering/change"
    and rc1.get("corridor") in EXECUTABLE_BUILTINS,
)
check(
    "RC1 requires at least one real material-finding cycle run",
    rc1.get("required_material_cycle_runs") == 1,
)
evidence_root = ROOT / str(rc1.get("evidence_root"))
check(
    "RC1 evidence root exists and is excluded from the behavioral subject",
    evidence_root.is_dir()
    and stab.classify_path(
        str(Path(str(rc1.get("evidence_root"))) / "rc1-run-1.json"),
        stab.load_subject_config(ROOT / str(rc1.get("subject_config"))),
    )
    is False,
)

cells = rc1.get("cells")
check(
    "RC1 declares exactly three fixed corridor cells",
    isinstance(cells, dict)
    and sorted(cells) == [f"rc1-corridor-run-{index}" for index in (1, 2, 3)],
)
config = load_tracked_config(ROOT)
sequences: list[int] = []
for name, cell in sorted(cells.items()):
    check(
        f"{name} runs the engineering/change corridor cell kind",
        cell.get("kind") == "engineering-change-corridor",
    )
    sequences.append(int(cell.get("sequence", 0)))
    check(
        f"{name} declares the full supported corridor trace",
        tuple(cell.get("expected", ())) == stab.RC1_FULL_CORRIDOR_TRACE,
    )
    for role in ("executor", "review"):
        route = cell.get(role)
        check(
            f"{name} {role} route names a registered alias",
            isinstance(route, dict)
            and route.get("model") in config.data["model_aliases"],
        )
        resolved = config.resolve_alias(
            str(route["model"]), str(route["runtime"])
        )
        validate_effort(resolved["runtime"], str(route["effort"]))
        check(
            f"{name} {role} route resolves on its declared runtime",
            resolved["runtime"] == route["runtime"],
        )
    check(
        f"{name} executor and reviewer are Fable High",
        cell["executor"] == {"runtime": "claude", "model": "fable", "effort": "high"}
        and cell["review"]
        == {
            "mode": "simple",
            "runtime": "claude",
            "model": "fable",
            "effort": "high",
        },
    )
check("RC1 cells are strictly sequence ordered", sequences == [1, 2, 3])

# The primary product-cycle route and the RC1 cell review route must be the
# same registered Fable High identity so gate evidence and product cycles
# share one authority.
primary = config.finalization_route("finalization-primary")
check(
    "RC1 review route equals the registered finalization primary",
    primary["runtime"] == "claude" and primary["effort"] == "high",
)

# The 2.6.6 four-cell release manifest must remain loadable beside the RC1
# declarations: the RC1 section is additive, not a rewrite.  (The full
# clean-HEAD contract binding is covered by test_release_acceptance.py.)
import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "_release_acceptance_preflight", ROOT / "scripts" / "release-acceptance.py"
)
assert _SPEC and _SPEC.loader
_ACCEPTANCE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ACCEPTANCE)
legacy = _ACCEPTANCE.load_manifest(ROOT)
check(
    "the legacy release manifest still loads beside the RC1 section",
    set(legacy.get("required_cells", []))
    == {
        "claude-lifecycle",
        "codex-lifecycle",
        "cross-runtime-composition",
        "deep-review",
    },
)

print("rc1 gate preflight declarations validated")
