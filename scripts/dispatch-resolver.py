#!/usr/bin/env python3
"""Read-only, fail-closed Phase 1 candidate resolver for dispatch."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_schema import FrontmatterError, parse_frontmatter, split_frontmatter


WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_-]{2,}")
SAFE_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
STOP = {
    "this", "that", "with", "from", "into", "для", "это", "как", "чтобы",
    "сделать", "нужно", "надо", "the", "and", "или", "наш", "наша", "наши",
}
RETRIEVAL_TIMEOUT_SECONDS = 8.0


class ResolveError(ValueError):
    pass


def die(message: str) -> NoReturn:
    print(f"dispatch-resolver: {message}", file=sys.stderr)
    raise SystemExit(3)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResolveError(f"cannot read request {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ResolveError("resolver request must be a schema_version 1 object")
    return value


def require_root(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise ResolveError("vault_root must be absolute")
    path = path.resolve()
    if not (path / "wiki").is_dir():
        raise ResolveError("vault_root must contain wiki/")
    return path


def page_frontmatter(page: Path) -> dict[str, Any]:
    text = page.read_text(encoding="utf-8", errors="replace")[:80_000]
    block = split_frontmatter(text)
    if block is None:
        return {}
    try:
        return parse_frontmatter(block)
    except FrontmatterError:
        return {}


def frontmatter_path(page: Path) -> Path | None:
    raw = page_frontmatter(page).get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw.strip()).expanduser()
    return candidate.resolve() if candidate.is_absolute() and candidate.is_dir() else None


def repo_candidates(vault: Path, repo_name: str, projects_root: Path) -> list[dict[str, str]]:
    if not SAFE_REPO_RE.fullmatch(repo_name):
        raise ResolveError("repo_name must be one safe path component")
    found: dict[str, dict[str, str]] = {}
    repos = vault / "wiki" / "repos"
    if repos.is_dir():
        for page in sorted(repos.glob("*.md")):
            if repo_name.casefold() not in page.stem.casefold():
                continue
            candidate = frontmatter_path(page)
            if candidate is not None:
                found[str(candidate)] = {
                    "path": str(candidate), "source": "wiki-repo-page", "page": page.stem,
                }
    if projects_root.is_dir():
        root_depth = len(projects_root.parts)
        for current, dirs, _files in os.walk(projects_root):
            here = Path(current)
            depth = len(here.parts) - root_depth
            if depth >= 4:
                dirs[:] = []
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            if here.name == repo_name and (here / ".git").exists():
                resolved = here.resolve()
                found.setdefault(str(resolved), {"path": str(resolved), "source": "projects-root"})
    return [found[key] for key in sorted(found)]


def plan_candidates(vault: Path, session_id: str, explicit: str) -> list[dict[str, str]]:
    plans = vault / "wiki" / "plans"
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = plans / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(plans.resolve())
        except ValueError as exc:
            raise ResolveError("explicit plan escapes wiki/plans") from exc
        paths = [candidate] if candidate.is_file() else []
    else:
        paths = sorted(plans.glob("*.md"), reverse=True) if plans.is_dir() else []
    result: list[dict[str, str]] = []
    for path in paths:
        frontmatter = page_frontmatter(path)
        if frontmatter.get("status") != "pending":
            continue
        owner = str(frontmatter.get("session_id") or "").strip()
        if explicit or (session_id and owner == session_id):
            result.append({"path": str(path.resolve()), "session_id": owner, "source": "explicit" if explicit else "current-session"})
    return result[:5]


def query_terms(description: str, repo_name: str) -> list[str]:
    values: list[str] = []
    for word in WORD_RE.findall(f"{repo_name} {description}"):
        value = word.casefold()
        if value in STOP or value in values:
            continue
        values.append(value)
    return values[:12]


def lexical_context_candidates(vault: Path, description: str, repo_name: str) -> list[dict[str, Any]]:
    terms = query_terms(description, repo_name)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for page in (vault / "wiki").rglob("*.md"):
        rel = page.relative_to(vault / "wiki").as_posix()
        if rel in {"log.md"} or rel.startswith("plans/"):
            continue
        text = page.read_text(encoding="utf-8", errors="replace")[:80_000].casefold()
        title = page.stem.casefold()
        hits = [term for term in terms if term in title or term in text]
        if not hits:
            continue
        score = sum(3 if term in title else 1 for term in hits)
        scored.append((score, rel, {
            "title": page.stem,
            "path": f"wiki/{rel}",
            "score": score,
            "matched_terms": hits[:5],
        }))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:5]]


def context_candidates(vault: Path, description: str, repo_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prefer the canonical hybrid retriever without mutating derived state."""
    query = f"{repo_name} {description}".strip()
    script = vault / "scripts" / "retrieve.py"
    fallback_reason = "canonical read-only retriever unavailable"
    if script.is_file():
        try:
            result = subprocess.run(
                [sys.executable, str(script), query, "--top", "5", "--json", "--dense", "auto", "--read-only"],
                cwd=vault,
                text=True,
                capture_output=True,
                timeout=RETRIEVAL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = None
            fallback_reason = "canonical read-only retriever timed out"
        if result is not None and result.returncode != 0:
            fallback_reason = "canonical read-only retriever failed"
        if result is not None and result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = {}
            rows = payload.get("results") if isinstance(payload, dict) else None
            meta = payload.get("meta") if isinstance(payload, dict) else None
            if isinstance(rows, list) and isinstance(meta, dict):
                candidates = [
                    {
                        "title": str(row.get("title") or Path(str(row.get("path") or "")).stem),
                        "path": str(row.get("path") or ""),
                        "heading": str(row.get("heading") or ""),
                        "score": float(row.get("score") or 0),
                    }
                    for row in rows if isinstance(row, dict) and str(row.get("path") or "").startswith("wiki/")
                ]
                return candidates[:5], {
                    "source": "canonical-retrieval",
                    "mode": str(meta.get("mode") or "sparse"),
                    "degraded": bool(meta.get("degraded")),
                    "reason": str(meta.get("reason") or ""),
                }
            fallback_reason = "canonical read-only retriever returned malformed output"
    return lexical_context_candidates(vault, description, repo_name), {
        "source": "bounded-lexical-fallback", "mode": "sparse", "degraded": True,
        "reason": fallback_reason,
    }


def resolve_request(raw: dict[str, Any]) -> dict[str, Any]:
    vault = require_root(raw.get("vault_root"))
    description = str(raw.get("description") or "").strip()
    repo_name = str(raw.get("repo_name") or "").strip()
    if not description or not repo_name:
        raise ResolveError("description and repo_name are required")
    projects = Path(str(raw.get("projects_root") or os.environ.get("LLM_OBSIDIAN_PROJECTS_ROOT") or Path.home() / "Projects")).expanduser().resolve()
    repos = repo_candidates(vault, repo_name, projects)
    plans = plan_candidates(vault, str(raw.get("session_id") or "").strip(), str(raw.get("plan") or "").strip())
    context, context_meta = context_candidates(vault, description, repo_name)
    blockers: list[str] = []
    if len(repos) != 1:
        blockers.append("repo-not-found" if not repos else "repo-ambiguous")
    if len(plans) != 1:
        blockers.append("plan-not-found" if not plans else "plan-ambiguous")
    return {
        "schema_version": 1,
        "status": "resolved" if not blockers else "needs-selection",
        "repo_candidates": repos,
        "plan_candidates": plans,
        "context_candidates": context,
        "context_retrieval": context_meta,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(resolve_request(read_json(args.request.resolve())), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ResolveError, OSError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
