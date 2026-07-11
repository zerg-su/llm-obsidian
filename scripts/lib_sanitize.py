"""Shared credential sanitizer.

Single source of truth for redaction rules, used by:
  - scripts/memory-backup.py       (memory backup into .claude-memory/)
  - .claude/hooks/plan-trim.py     (command-capture into command-log.jsonl)

Redaction is line-oriented and conservative: it masks the secret VALUE, keeps
the surrounding context readable.
"""

from __future__ import annotations

import re

# Each rule: (compiled regex, replacement). Mask the value, keep the label.
REDACT_RULES = [
    # AWS access key ids (AKIA/ASIA + 16 chars)
    (re.compile(r"\b(A[KS]IA)[A-Z0-9]{12,}\b"), r"\1****REDACTED"),
    # password mentioned inline, declined RU forms included: «с паролем `X`», «пароль общий `X`»
    (re.compile(r"(?i)(парол\w+(?:\s+общий)?|password)\s+`[^`]+`"), r"\1 `REDACTED`"),
    (re.compile(r"(?i)(парол\w+|password)[:=]\s*\S+"), r"\1: REDACTED"),
    # sshpass invocations carry the password as an argument / env var (quoted or bare)
    (re.compile(r"(?i)(sshpass\s+-p\s+)(['\"])[^'\"]+\2"), r"\1\2REDACTED\2"),
    (re.compile(r"(?i)(sshpass\s+-p\s+)[^\s'\"]+"), r"\1REDACTED"),
    (re.compile(r"(?i)(SSHPASS=)(['\"]?)[^'\"\s]+\2"), r"\1\2REDACTED\2"),
    # bearer/api tokens in backticks after the word token/key/secret
    (re.compile(r"(?i)(token|api[_-]?key|secret)\s*[:=]?\s*`[A-Za-z0-9_\-\.]{16,}`"), r"\1 `REDACTED`"),
    # bare token/key/secret assignments on a command line (token=..., --token ..., -p secret=...)
    (re.compile(r"(?i)\b(token|api[_-]?key|secret|passwd)([=:])[A-Za-z0-9_\-\./+]{6,}"), r"\1\2REDACTED"),
    # generic long hex/base64-looking secrets glued to known prefixes
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}\b"), "glpat-REDACTED"),
    (re.compile(r"\bglsa_[A-Za-z0-9_]{10,}\b"), "glsa_REDACTED"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_REDACTED"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh*_REDACTED"),
    (re.compile(r"\bctx7sk-[A-Za-z0-9\-]{10,}\b"), "ctx7sk-REDACTED"),
    (re.compile(r"\bxox[bap]-[A-Za-z0-9\-]{10,}\b"), "xox*-REDACTED"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{16,}"), "Bearer REDACTED"),
]

# A second, fail-closed layer for data that is about to enter a durable backup.
# Labels are safe to log; matching secret text is deliberately never returned.
RESIDUAL_CREDENTIAL_RULES = [
    ("private-key", re.compile(r"-----BEGIN " r"(?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bA[KS]IA[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")),
    ("gitlab-token", re.compile(r"\b(?:glpat-[A-Za-z0-9_\-]{10,}|glsa_[A-Za-z0-9_]{10,})\b")),
    ("context7-token", re.compile(r"\bctx7sk-[A-Za-z0-9\-]{10,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+(?!REDACTED\b)[A-Za-z0-9_\-.=+/]{16,}")),
    (
        "credential-assignment",
        re.compile(
            r"(?im)\b(?:token|api[_-]?key|secret|password|passwd)\s*[:=]\s*"
            r"(?!REDACTED\b)(?:['\"][^'\"\n]{6,}['\"]|[^\s`<>{}\[\]]{6,})"
        ),
    ),
]


def sanitize(text: str) -> tuple[str, int]:
    count = 0
    for rx, repl in REDACT_RULES:
        text, n = rx.subn(repl, text)
        count += n
    return text, count


def residual_credential_kinds(text: str) -> list[str]:
    """Return only safe rule labels for credential-like text left after sanitize."""

    return [label for label, pattern in RESIDUAL_CREDENTIAL_RULES if pattern.search(text)]
