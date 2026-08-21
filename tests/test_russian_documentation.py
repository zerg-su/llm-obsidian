#!/usr/bin/env python3
"""Deterministic contracts for the versioned Russian handbook."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "ru"
ARCHITECTURE_GUIDE = ROOT / "docs" / "architecture-workflow-v1.ru.md"
PIPELINE = ROOT / "examples" / "pipelines" / "document-project-v1.json"
sys.path.insert(0, str(ROOT / "scripts"))

from harness.custom_pipelines import (
    CustomPipelinePolicy,
    compile_custom_spec,
    parse_pipeline_spec,
)
from harness.pipeline_builtins import builtin_registry


REQUIRED_PAGES = (
    "index.md",
    "getting-started.md",
    "mental-model.md",
    "first-project.md",
    "skills.md",
    "planning.md",
    "parallel-tasks.md",
    "sessions-and-tasks.md",
    "review.md",
    "pipelines.md",
    "pipeline-dsl.md",
    "documentation-pipeline.md",
    "wiki-memory.md",
    "documents-and-research.md",
    "operations.md",
    "development.md",
    "testing.md",
    "extending.md",
    "upgrading-and-releasing.md",
    "troubleshooting.md",
    "cookbook.md",
    "reference/commands.md",
    "reference/configuration.md",
    "reference/glossary.md",
)
GUIDE_PAGES = (
    "getting-started.md",
    "first-project.md",
    "planning.md",
    "parallel-tasks.md",
    "sessions-and-tasks.md",
    "documentation-pipeline.md",
    "wiki-memory.md",
    "documents-and-research.md",
    "operations.md",
    "development.md",
    "testing.md",
    "extending.md",
    "upgrading-and-releasing.md",
    "troubleshooting.md",
    "cookbook.md",
)
GUIDE_SECTIONS = (
    "## Для кого и результат",
    "## Предварительные условия",
    "## Пример",
    "## Ожидаемый результат и проверка",
    "## Ошибки и восстановление",
    "## Источники истины",
)
FENCE_RE = re.compile(r"```(json|toml)\s*\n(.*?)```", re.DOTALL)
LINK_RE = re.compile(r"\[[^]]+\]\(([^)]+)\)")
HARNESS_OPERATION_COMMAND_RE = re.compile(
    r"python3 scripts/harness-cli\.py (inspect|resume|cancel|close)(?:\s|`|$)"
)


def skill_names(root: Path = ROOT) -> tuple[str, ...]:
    return tuple(sorted(path.parent.name for path in (root / "skills").glob("*/SKILL.md")))


def model_invoked_skill_names(root: Path = ROOT) -> tuple[str, ...]:
    """Return the skills shipped with model-facing invocation metadata."""

    return tuple(
        sorted(
            path.parents[1].name
            for path in (root / "skills").glob("*/agents/openai.yaml")
        )
    )


def claude_skill_inventory_failures(
    body: str, model_invoked: tuple[str, ...]
) -> list[str]:
    """Require the canonical CLAUDE inventory to cover shipped model skills."""

    lines = [line for line in body.splitlines() if line.startswith("**Skills:**")]
    if len(lines) != 1:
        return ["CLAUDE.md must contain exactly one canonical Skills inventory"]
    listed = set(re.findall(r"`/([a-z0-9]+(?:-[a-z0-9]+)*)`", lines[0]))
    return [
        f"CLAUDE.md Skills inventory missing /{name}"
        for name in model_invoked
        if name not in listed
    ]


def markdown_files(root: Path = DOCS) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.md"))) if root.is_dir() else ()


def broken_relative_links(files: tuple[Path, ...]) -> list[str]:
    failures: list[str] = []
    for page in files:
        for target in LINK_RE.findall(page.read_text(encoding="utf-8")):
            clean = target.split("#", 1)[0]
            if not clean or clean.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (page.parent / clean).resolve()
            if not resolved.exists():
                try:
                    label = page.relative_to(ROOT)
                except ValueError:
                    label = page
                failures.append(f"{label} -> {target}")
    return failures


def fenced_data_failures(files: tuple[Path, ...]) -> list[str]:
    failures: list[str] = []
    for page in files:
        for index, (kind, body) in enumerate(
            FENCE_RE.findall(page.read_text(encoding="utf-8")), 1
        ):
            try:
                json.loads(body) if kind == "json" else tomllib.loads(body)
            except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
                try:
                    label = page.relative_to(ROOT)
                except ValueError:
                    label = page
                failures.append(f"{label} fence {index}: {exc}")
    return failures


def trailing_blank_line_failures(files: tuple[Path, ...]) -> list[str]:
    failures: list[str] = []
    for path in files:
        if not path.read_bytes().endswith(b"\n\n"):
            continue
        try:
            label = path.relative_to(ROOT)
        except ValueError:
            label = path
        failures.append(str(label))
    return failures


def harness_operation_argument_failures(files: tuple[Path, ...]) -> list[str]:
    """Reject lifecycle examples that omit the required exact operation ID."""

    failures: list[str] = []
    for page in files:
        for line_number, line in enumerate(
            page.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not HARNESS_OPERATION_COMMAND_RE.search(line):
                continue
            if "<operation-id>" not in line:
                try:
                    label = page.relative_to(ROOT)
                except ValueError:
                    label = page
                failures.append(
                    f"{label}:{line_number}: {line.strip()}"
                )
    return failures


def skill_inventory_failures(body: str, names: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    for name in names:
        for token in (f"`{name}`", f"`/{name}`", f"`$llm-obsidian:{name}`"):
            if token not in body:
                failures.append(token)
    return failures


def skill_reference_contract_failures(
    body: str, names: tuple[str, ...]
) -> list[str]:
    """Require one complete input/output/effect/example row per skill."""

    failures: list[str] = []
    lines = body.splitlines()
    for name in names:
        prefix = f"| `{name}` · `/{name}` · `$llm-obsidian:{name}` |"
        rows = [line for line in lines if line.startswith(prefix)]
        if len(rows) != 1:
            failures.append(f"{name}: expected exactly one catalog row")
            continue
        cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
        if len(cells) != 5 or any(not cell for cell in cells):
            failures.append(f"{name}: row must have five non-empty cells")
            continue
        if "·" not in cells[4]:
            failures.append(f"{name}: permission/effect and example are required")
    return failures


def compile_documentation_pipeline(path: Path = PIPELINE) -> None:
    spec = parse_pipeline_spec(path.read_text(encoding="utf-8"))
    compiled = compile_custom_spec(
        spec,
        builtin_registry(),
        policy=CustomPipelinePolicy.default(),
        capabilities=("route:resolved",),
    )
    if compiled.definition.steps[-1].primitive_id != "review":
        raise AssertionError("documentation pipeline must end at review")


def documentation_pointer_hash_failures(
    pipeline: Path = PIPELINE,
    authority: Path = ROOT / "docs" / "acceptance" / "v2.6.3-documentation-quality-contracts.md",
) -> list[str]:
    value = json.loads(pipeline.read_text(encoding="utf-8"))
    pointers = {
        str(item.get("pointer_id")): item
        for item in value.get("context_pointers", [])
        if isinstance(item, dict)
    }
    pointer = pointers.get("documentation-quality")
    expected = hashlib.sha256(authority.read_bytes()).hexdigest()
    if pointer is None:
        return ["documentation-quality context pointer is missing"]
    if pointer.get("content_sha256") != expected:
        return ["documentation-quality context pointer digest is stale"]
    return []


failures: list[str] = []
for relative in REQUIRED_PAGES:
    if not (DOCS / relative).is_file():
        failures.append(f"missing page: docs/ru/{relative}")

files = markdown_files()
if not ARCHITECTURE_GUIDE.is_file():
    failures.append("missing page: docs/architecture-workflow-v1.ru.md")
else:
    files += (ARCHITECTURE_GUIDE,)
index = (DOCS / "index.md").read_text(encoding="utf-8") if (DOCS / "index.md").is_file() else ""
for relative in REQUIRED_PAGES[1:]:
    if f"]({relative})" not in index:
        failures.append(f"index does not reach: {relative}")
if "../architecture-workflow-v1.ru.md" not in index:
    failures.append("docs/ru/index.md does not reach architecture workflow guide")
if "docs/architecture-workflow-v1.ru.md" not in (
    ROOT / "README.ru.md"
).read_text(encoding="utf-8"):
    failures.append("README.ru.md does not reach architecture workflow guide")

failures.extend(f"broken relative link: {item}" for item in broken_relative_links(files))
failures.extend(f"invalid fenced data: {item}" for item in fenced_data_failures(files))
failures.extend(
    f"trailing blank line: {item}" for item in trailing_blank_line_failures(files)
)
failures.extend(
    f"harness operation command missing <operation-id>: {item}"
    for item in harness_operation_argument_failures(files)
)

skills_page = (DOCS / "skills.md")
skills_body = skills_page.read_text(encoding="utf-8") if skills_page.is_file() else ""
for token in skill_inventory_failures(skills_body, skill_names()):
    failures.append(f"skills inventory missing {token}")
failures.extend(skill_reference_contract_failures(skills_body, skill_names()))
failures.extend(
    claude_skill_inventory_failures(
        (ROOT / "CLAUDE.md").read_text(encoding="utf-8"),
        model_invoked_skill_names(),
    )
)

for relative in GUIDE_PAGES:
    path = DOCS / relative
    body = path.read_text(encoding="utf-8") if path.is_file() else ""
    for section in GUIDE_SECTIONS:
        if section not in body:
            failures.append(f"docs/ru/{relative} missing {section}")

all_docs = "\n".join(page.read_text(encoding="utf-8") for page in files)
for marker in ("TODO", "TBD", "FIXME", "INSERT HERE"):
    if marker in all_docs:
        failures.append(f"placeholder marker remains: {marker}")

matrix = ROOT / "docs" / "acceptance" / "v2.6.3-documentation-matrix.md"
matrix_body = matrix.read_text(encoding="utf-8") if matrix.is_file() else ""
for source in ("AGENTS.md", "CLAUDE.md", "schemas/pipeline-spec-v1.schema.json"):
    if source not in matrix_body:
        failures.append(f"source-of-truth manifest missing {source}")

required_page_tokens = {
    "getting-started.md": (
        "--check",
        "--skip-proxy",
        "--skip-docling",
        "upgrading-and-releasing.md#ошибки-и-восстановление",
    ),
    "testing.md": (
        "make test-harness-coverage",
        "statement-line denominator",
        "git diff --check v2.6.6-rc2..HEAD",
    ),
    "reference/commands.md": (
        "make test-harness-coverage",
        "git diff --check v2.6.6-rc2..HEAD",
    ),
    "upgrading-and-releasing.md": (
        "$RC3_EVIDENCE_ROOT/release/receipt.json",
        "--attempt-ledger-root",
        "--expected-candidate",
        "git diff --check v2.6.6-rc2..HEAD",
    ),
    "parallel-tasks.md": (
        "автоматического task graph",
        "reap.plan_mode=shared",
        "task_id",
        "<operation-id>",
        "integration plan",
        "resolve-conflict",
        "$llm-obsidian:dispatch",
    ),
}
for relative, tokens in required_page_tokens.items():
    body = (DOCS / relative).read_text(encoding="utf-8")
    for token in tokens:
        if token not in body:
            failures.append(f"docs/ru/{relative} missing contract token {token}")

try:
    compile_documentation_pipeline()
except (OSError, ValueError, AssertionError) as exc:
    failures.append(f"documentation PipelineSpec does not compile: {exc}")
failures.extend(documentation_pointer_hash_failures())

# Mutation-sensitive unit checks for the local validators.
with tempfile.TemporaryDirectory(prefix="russian-docs-gate.") as raw:
    scratch = Path(raw)
    broken = scratch / "broken.md"
    broken.write_text("[missing](not-there.md)\n", encoding="utf-8")
    assert broken_relative_links((broken,))

    invalid_data = scratch / "invalid-data.md"
    invalid_data.write_text('```json\n{"open": true\n```\n', encoding="utf-8")
    assert fenced_data_failures((invalid_data,))

    trailing = scratch / "trailing.md"
    trailing.write_text("# Heading\n\n", encoding="utf-8")
    assert trailing_blank_line_failures((trailing,))

    missing_operation = scratch / "missing-operation.md"
    missing_operation.write_text(
        "python3 scripts/harness-cli.py inspect\n", encoding="utf-8"
    )
    assert harness_operation_argument_failures((missing_operation,))

    assert skill_inventory_failures(
        "`save` `/save` `$llm-obsidian:save`",
        ("save", "review"),
    ) == ["`review`", "`/review`", "`$llm-obsidian:review`"]

    assert skill_reference_contract_failures(
        "| `save` · `/save` · `$llm-obsidian:save` | Use | Input | Output | Effect only |",
        ("save",),
    ) == ["save: permission/effect and example are required"]

    invalid_pipeline = scratch / "invalid-pipeline.json"
    invalid_pipeline.write_text("{}\n", encoding="utf-8")
    try:
        compile_documentation_pipeline(invalid_pipeline)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid PipelineSpec mutation was accepted")

    stale_pipeline = scratch / "stale-pointer.json"
    stale_value = json.loads(PIPELINE.read_text(encoding="utf-8"))
    stale_value["context_pointers"][0]["content_sha256"] = "0" * 64
    stale_pipeline.write_text(json.dumps(stale_value), encoding="utf-8")
    assert documentation_pointer_hash_failures(stale_pipeline) == [
        "documentation-quality context pointer digest is stale"
    ]

decision = json.loads(
    (
        ROOT
        / "docs"
        / "acceptance"
        / "v2.6.3-document-project-skill-verdicts.json"
    ).read_text(encoding="utf-8")
)
amendment = decision.get("authoritative_amendment", {})
if (
    decision.get("disposition") != "not-adopted-per-stop-condition"
    or decision.get("coordinator_decision_id")
    != "e81aaee1-3196-4350-af9f-efb352b8d696"
    or amendment.get("vault_page")
    != "[[LLM Obsidian 2.6.3 — E5 capability disposition]]"
    or amendment.get("address") != "c-000100"
    or amendment.get("page_sha256")
    != "86501614f8d1d860c21a920ce8ec778c5b1c4bbe5f21875012b73569c9f113aa"
    or amendment.get("approved_plan_sha256")
    != "db4037cac1967b0907dbf1b6fd5850eefa2bfc5173080d2aa811a239fb36b8dc"
    or amendment.get("outcome_contract_sha256")
    != "2c9728dc7c7fa3bc108ffb6ce5085bb41fcd9ba16310157e76276c6967b5bf5f"
    or decision.get("installed_skill") is not False
    or (ROOT / "skills" / "document-project").exists()
):
    failures.append("E5 no-new-skill disposition is inconsistent")

if failures:
    raise SystemExit("Documentation gate failed:\n- " + "\n- ".join(failures))

print(
    f"OK   Russian handbook: {len(files)} pages, "
    f"{len(skill_names())} skills, compiled PipelineSpec"
)
