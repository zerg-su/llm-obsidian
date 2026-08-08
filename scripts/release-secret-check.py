#!/usr/bin/env python3
"""Fail closed on new credential material in one exact tagged candidate diff."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from lib_sanitize import residual_credential_kinds


GIT_SHA = re.compile(r"[0-9a-f]{40,64}")
SECRET_PATH = re.compile(
    r"(?i)(^|/)(?:\.env(?:\..*)?|secrets?\.env|runtime\.env|"
    r"credentials?(?:\..*)?|id_(?:rsa|ed25519)|.*\.(?:pem|key|p12|pfx))$"
)
SAFE_PYTHON_CREDENTIAL_REFERENCE = re.compile(
    r"(?i)^\s*(?:token|api[_-]?key|secret|password|passwd)\s*=\s*"
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\([^()\n]*\))?\s*$"
)


class SecretCheckError(RuntimeError):
    pass


def git(root: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        raise SecretCheckError("Git candidate identity is unavailable")
    return result.stdout


def exact_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    actual = Path(str(git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    if actual != root:
        raise SecretCheckError("release secret check requires the exact Git root")
    return root


def resolve_head(root: Path, ref: str) -> str:
    value = str(git(root, "rev-parse", f"{ref}^{{commit}}")).strip()
    if GIT_SHA.fullmatch(value) is None:
        raise SecretCheckError("Git candidate identity is not exact")
    return value


def baseline(root: Path, requested: str) -> str:
    if requested == "nearest-tag":
        tag = str(git(root, "describe", "--tags", "--abbrev=0", "HEAD^")).strip()
        if not tag:
            raise SecretCheckError("tagged release baseline is unavailable")
        return resolve_head(root, tag)
    return resolve_head(root, requested)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="nearest-tag")
    args = parser.parse_args()
    try:
        root = exact_root(args.root)
        head = resolve_head(root, "HEAD")
        base = baseline(root, args.base)
        ancestor = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", base, head],
            check=False,
            capture_output=True,
        )
        if ancestor.returncode != 0 or base == head:
            raise SecretCheckError("release baseline is not a prior candidate ancestor")
        patch = str(
            git(
                root,
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                f"{base}..{head}",
                "--",
            )
        )
        added = []
        current_path = ""
        for line in patch.splitlines():
            if line.startswith("+++ b/"):
                current_path = line[6:]
                continue
            if not line.startswith("+") or line.startswith("+++"):
                continue
            value = line[1:]
            if current_path.endswith(".py") and SAFE_PYTHON_CREDENTIAL_REFERENCE.fullmatch(
                value
            ):
                continue
            added.append(value)
        kinds = residual_credential_kinds("\n".join(added))
        raw_paths = git(
            root,
            "diff",
            "--name-only",
            "--diff-filter=A",
            "-z",
            f"{base}..{head}",
            text=False,
        )
        assert isinstance(raw_paths, bytes)
        paths = [
            value.decode("utf-8", errors="surrogateescape")
            for value in raw_paths.split(b"\0")
            if value
        ]
        secret_paths = sorted(path for path in paths if SECRET_PATH.search(path))
        blocked = bool(kinds or secret_paths)
        print(
            json.dumps(
                {
                    "added_lines": len(added),
                    "baseline_head": base,
                    "credential_kinds": kinds,
                    "schema_version": 1,
                    "secret_paths": secret_paths,
                    "status": "blocked" if blocked else "passed",
                    "subject_head": head,
                },
                sort_keys=True,
            )
        )
        return 1 if blocked else 0
    except (OSError, SecretCheckError, UnicodeError) as exc:
        print(f"release-secret-check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
