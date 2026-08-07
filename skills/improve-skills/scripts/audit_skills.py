#!/usr/bin/env python3
"""Deterministic structural audit for installed skill packages."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTMATTER_BOUNDARY = "---"
MAX_SKILL_NAME_LENGTH = 64
LOCAL_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
PASS_NAMES = {
    "invocation",
    "hierarchy",
    "steering",
    "pruning",
    "goal_preservation",
}
VERDICTS = {"fix", "no-change", "defer"}
ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
    "disable-model-invocation",
}
VERDICT_RECORD_FIELDS = {
    "skill",
    "verdict",
    "passes",
    "overall_input",
    "overall_outcome",
    "local_subgoal",
    "completion_proxies",
    "required_outcome_evidence",
    "evidence",
    "change",
    "behavior_proof",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _nonempty_text_list(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must be a non-empty unique text list")
    return value


def validate_verdict_records(
    payload: object,
    inventory: tuple[str, ...],
) -> dict[str, object]:
    """Validate one exhaustive five-pass verdict record per installed skill."""

    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "records",
    }:
        raise ValueError("verdict payload fields changed")
    if payload.get("schema_version") != 1:
        raise ValueError("verdict payload schema_version must be 1")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("verdict records must be a list")

    expected = tuple(sorted(inventory))
    if len(expected) != len(set(expected)) or any(not item for item in expected):
        raise ValueError("inventory skill names must be unique and non-empty")
    observed: list[str] = []
    for index, record in enumerate(records):
        label = f"verdict record {index + 1}"
        if not isinstance(record, dict) or set(record) != VERDICT_RECORD_FIELDS:
            raise ValueError(f"{label} fields changed")
        skill = _nonempty_text(record.get("skill"), f"{label} skill")
        observed.append(skill)
        verdict = record.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"{label} verdict must be fix, no-change, or defer")
        passes = record.get("passes")
        if not isinstance(passes, dict) or set(passes) != PASS_NAMES:
            raise ValueError(f"{label} must contain exactly five named passes")
        for pass_name, result in passes.items():
            _nonempty_text(result, f"{label} {pass_name} result")
        if verdict == "no-change" and set(passes.values()) != {"pass"}:
            raise ValueError(f"{label} no-change verdict requires five pass results")
        if verdict in {"fix", "defer"} and set(passes.values()) == {"pass"}:
            raise ValueError(f"{label} {verdict} verdict requires a finding")
        for field in (
            "overall_input",
            "overall_outcome",
            "local_subgoal",
            "evidence",
            "change",
            "behavior_proof",
        ):
            _nonempty_text(record.get(field), f"{label} {field}")
        _nonempty_text_list(
            record.get("completion_proxies"),
            f"{label} completion_proxies",
        )
        _nonempty_text_list(
            record.get("required_outcome_evidence"),
            f"{label} required_outcome_evidence",
        )

    if tuple(sorted(observed)) != expected or len(observed) != len(set(observed)):
        raise ValueError("verdict records and inventory must contain the same skill names exactly once")
    return payload


def split_frontmatter(path: Path) -> tuple[list[str], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != FRONTMATTER_BOUNDARY:
        raise ValueError("missing opening YAML frontmatter boundary")
    try:
        end = lines.index(FRONTMATTER_BOUNDARY, 1)
    except ValueError as exc:
        raise ValueError("missing closing YAML frontmatter boundary") from exc
    return lines[1:end], "\n".join(lines[end + 1 :]) + "\n"


def _yaml_key(raw: str) -> str:
    token = raw.strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        try:
            decoded = json.loads(token)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid quoted frontmatter key") from exc
        if not isinstance(decoded, str):
            raise ValueError("invalid quoted frontmatter key")
        return decoded
    if len(token) >= 2 and token[0] == token[-1] == "'":
        inner = token[1:-1]
        if "'" in inner.replace("''", ""):
            raise ValueError("invalid quoted frontmatter key")
        return inner.replace("''", "'")
    return token


def _yaml_string(raw: str, continuations: list[str]) -> str | bool | None:
    """Parse the small scalar subset allowed by the skill frontmatter contract."""

    value = raw.strip()
    if not value:
        return None
    if value in {">", ">-", ">+", "|", "|-", "|+"}:
        return " ".join(line.strip() for line in continuations).strip()
    if continuations:
        raise ValueError(
            "frontmatter scalar continuations require an explicit block marker"
        )
    if value[0] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("unsupported quoted frontmatter scalar") from exc
        if not isinstance(decoded, str):
            raise ValueError("quoted frontmatter scalar must be text")
        return decoded
    if value[0] == "'":
        if len(value) < 2 or value[-1] != "'":
            raise ValueError("unterminated quoted frontmatter scalar")
        inner = value[1:-1]
        if "'" in inner.replace("''", ""):
            raise ValueError("unsupported quoted frontmatter scalar")
        return inner.replace("''", "'")
    if value in {"true", "false"}:
        return value == "true"
    implicit_typed = re.fullmatch(
        r"(?:null|~|true|false|yes|no|on|off|"
        r"[-+]?\.(?:inf|nan)|"
        r"[-+]?(?:0b[01_]+|0o[0-7_]+|0x[0-9a-f_]+)|"
        r"[-+]?(?:\d[\d_]*(?:\.\d[\d_]*)?|\.\d[\d_]*)"
        r"(?:e[-+]?\d+)?|"
        r"\d{4}-\d{1,2}-\d{1,2}(?:[tT ]\S+)?|"
        r"\d+(?::\d+)+(?:\.\d+)?)",
        value,
        flags=re.I,
    )
    if (
        implicit_typed
        or value.startswith(("[", "{", "!", "&", "*", "@", "`", "-", "?", ":", "%"))
        or re.search(r":\s|\s#|(?:^|\s)[!&*](?:\S|$)", value)
        or "\t" in value
    ):
        raise ValueError("unsupported or implicitly typed frontmatter scalar")
    return value


def parse_frontmatter(frontmatter: list[str]) -> dict[str, str | bool | None]:
    """Fail closed on duplicate, malformed, or type-drifting top-level YAML."""

    parsed: dict[str, str | bool | None] = {}
    index = 0
    while index < len(frontmatter):
        line = frontmatter[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line.startswith((" ", "\t")):
            raise ValueError("orphaned indented frontmatter line")
        if ":" not in line:
            raise ValueError(f"malformed top-level frontmatter line: {line!r}")
        raw_key, raw_value = line.split(":", 1)
        key = _yaml_key(raw_key)
        if not key:
            raise ValueError("empty top-level frontmatter key")
        if key in parsed:
            raise ValueError(f"duplicate top-level frontmatter key: {key}")
        continuations: list[str] = []
        cursor = index + 1
        while cursor < len(frontmatter) and frontmatter[cursor].startswith((" ", "\t")):
            continuations.append(frontmatter[cursor])
            cursor += 1
        parsed[key] = _yaml_string(raw_value, continuations)
        index = cursor
    return parsed


def strip_code(markdown: str) -> str:
    output: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        output.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(output)


def local_link_findings(path: Path, body: str, audit_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for raw_target in LOCAL_LINK.findall(strip_code(body)):
        target = raw_target.split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith(("#", "mailto:")):
            continue
        if not (target.endswith(".md") or target.startswith(("./", "../"))):
            continue
        resolved = (path.parent / target).resolve()
        try:
            resolved.relative_to(audit_root.resolve())
        except ValueError:
            findings.append(Finding("error", "link-escape", f"local link escapes repository: {raw_target}"))
            continue
        if not resolved.is_file():
            findings.append(Finding("error", "broken-link", f"missing local reference: {raw_target}"))
    return findings


def codex_explicit_only(skill_dir: Path) -> bool:
    path = skill_dir / "agents" / "openai.yaml"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return bool(re.search(r"(?m)^\s*allow_implicit_invocation:\s*false\s*$", text))


def audit_skill(path: Path, audit_root: Path = REPO_ROOT) -> dict[str, object]:
    findings: list[Finding] = []
    try:
        frontmatter, body = split_frontmatter(path)
    except (OSError, ValueError) as exc:
        findings.append(Finding("error", "frontmatter", str(exc)))
        return {"skill": path.parent.name, "path": str(path), "findings": [asdict(item) for item in findings]}

    try:
        metadata = parse_frontmatter(frontmatter)
    except ValueError as exc:
        findings.append(Finding("error", "invalid-frontmatter", str(exc)))
        return {"skill": path.parent.name, "path": str(path), "findings": [asdict(item) for item in findings]}

    folder = path.parent.name
    unknown_keys = sorted(
        set(metadata) - ALLOWED_FRONTMATTER_KEYS
    )
    if unknown_keys:
        findings.append(
            Finding(
                "error",
                "unknown-frontmatter-key",
                "unsupported top-level frontmatter keys: "
                + ", ".join(unknown_keys),
            )
        )
    name = metadata.get("name")
    description = metadata.get("description")
    extension = metadata.get("disable-model-invocation")
    if not isinstance(name, str) or not isinstance(description, str):
        findings.append(
            Finding(
                "error",
                "invalid-frontmatter-value",
                "name and description must be YAML strings",
            )
        )
    elif (
        not re.fullmatch(r"[a-z0-9-]+", name)
        or name.startswith("-")
        or name.endswith("-")
        or "--" in name
        or len(name) > MAX_SKILL_NAME_LENGTH
        or "<" in description
        or ">" in description
        or len(description) > 1024
    ):
        findings.append(
            Finding(
                "error",
                "invalid-frontmatter-value",
                "name/description violate the base skill-creator contract",
            )
        )
    if "disable-model-invocation" in metadata and not isinstance(extension, bool):
        findings.append(
            Finding(
                "error",
                "invalid-frontmatter-value",
                "disable-model-invocation must be literal true or false",
            )
        )
    claude_explicit = extension is True
    codex_explicit = codex_explicit_only(path.parent)

    if name != folder:
        findings.append(Finding("error", "name-mismatch", f"frontmatter name {name!r} != folder {folder!r}"))
    if not description:
        findings.append(Finding("error", "missing-description", "description must be non-empty"))
    if claude_explicit != codex_explicit:
        findings.append(
            Finding(
                "error",
                "invocation-parity",
                "Claude disable-model-invocation and Codex allow_implicit_invocation disagree",
            )
        )
    if re.search(r"\b(?:TODO|TBD)\b", body):
        findings.append(Finding("error", "placeholder", "body contains TODO/TBD placeholder text"))
    if len(body.splitlines()) > 500:
        findings.append(Finding("warning", "sprawl", "SKILL.md body exceeds 500 lines; review disclosure"))
    findings.extend(local_link_findings(path, body, audit_root))

    return {
        "skill": folder,
        "path": str(path.relative_to(audit_root)),
        "description_bytes": len((description or "").encode("utf-8")),
        "body_bytes": len(body.encode("utf-8")),
        "body_lines": len(body.splitlines()),
        "explicit_only": claude_explicit and codex_explicit,
        "findings": [asdict(item) for item in findings],
    }


def audit_directory(skills_dir: Path) -> list[dict[str, object]]:
    audit_root = skills_dir.resolve().parent
    return [
        audit_skill(path, audit_root)
        for path in sorted(skills_dir.resolve().glob("*/SKILL.md"))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, default=REPO_ROOT / "skills")
    parser.add_argument(
        "--verdicts",
        type=Path,
        help="validate one schema-v1 five-pass verdict record per audited skill",
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SKILL",
        help="limit verdict records to this installed skill; repeat as needed",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail on warnings as well as errors")
    args = parser.parse_args()

    rows = audit_directory(args.skills_dir)
    errors = sum(
        finding["severity"] == "error"
        for row in rows
        for finding in row.get("findings", [])
    )
    warnings = sum(
        finding["severity"] == "warning"
        for row in rows
        for finding in row.get("findings", [])
    )
    verdicts_validated = False
    verdicts_error = ""
    if args.scope and args.verdicts is None:
        parser.error("--scope requires --verdicts")
    if args.verdicts is not None:
        try:
            installed = tuple(str(row["skill"]) for row in rows)
            verdict_inventory = tuple(args.scope) if args.scope else installed
            if (
                len(verdict_inventory) != len(set(verdict_inventory))
                or not set(verdict_inventory).issubset(set(installed))
            ):
                raise ValueError("verdict scope must contain unique installed skill names")
            verdict_payload = json.loads(args.verdicts.read_text(encoding="utf-8"))
            validate_verdict_records(
                verdict_payload,
                verdict_inventory,
            )
            verdicts_validated = True
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            verdicts_error = str(exc)
            errors += 1
    payload = {
        "schema_version": 1,
        "skills_dir": str(args.skills_dir),
        "audited": len(rows),
        "errors": errors,
        "warnings": warnings,
        "skills": rows,
    }
    if args.verdicts is not None:
        payload["verdicts_validated"] = verdicts_validated
        payload["verdict_scope"] = args.scope or [str(row["skill"]) for row in rows]
        if verdicts_error:
            payload["verdicts_error"] = verdicts_error

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            for finding in row.get("findings", []):
                print(f"{finding['severity'].upper()} {row['skill']} {finding['code']}: {finding['message']}")
        if verdicts_error:
            print(f"ERROR verdict-records: {verdicts_error}")
        elif args.verdicts is not None:
            print("verdict records: complete five-pass inventory")
        print(f"skill audit: {len(rows)} audited, {errors} errors, {warnings} warnings")

    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
