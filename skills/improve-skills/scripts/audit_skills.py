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
LOCAL_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str


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


def scalar(frontmatter: list[str], key: str) -> str | None:
    prefix = f"{key}:"
    for index, line in enumerate(frontmatter):
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if value in {">", ">-", "|", "|-"}:
            folded: list[str] = []
            for continuation in frontmatter[index + 1 :]:
                if continuation.startswith((" ", "\t")):
                    folded.append(continuation.strip())
                else:
                    break
            return " ".join(folded)
        return value.strip("\"'")
    return None


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

    folder = path.parent.name
    name = scalar(frontmatter, "name")
    description = scalar(frontmatter, "description")
    claude_explicit = scalar(frontmatter, "disable-model-invocation") == "true"
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
    payload = {
        "schema_version": 1,
        "skills_dir": str(args.skills_dir),
        "audited": len(rows),
        "errors": errors,
        "warnings": warnings,
        "skills": rows,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            for finding in row.get("findings", []):
                print(f"{finding['severity'].upper()} {row['skill']} {finding['code']}: {finding['message']}")
        print(f"skill audit: {len(rows)} audited, {errors} errors, {warnings} warnings")

    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
