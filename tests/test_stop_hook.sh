#!/usr/bin/env bash
# Hermetic regression suite for the safe/self-healing Stop pipeline.
set -uo pipefail
export LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$ROOT/.claude/hooks/stop.sh"
SANDBOX="$(mktemp -d /tmp/stop-hook-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

pass=0; fail=0; failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s — %s\n' "$1" "$2"; }

python3 - "$ROOT/hooks/hooks.json" <<'PY' >/dev/null 2>&1
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
assert "run-hook.sh" in cmd
assert " stop" in cmd
PY
[[ "$?" == 0 ]] && ok "json-codex-stop-enabled" || bad "json-codex-stop-enabled" "plugin hook guard regressed"

mkdir -p "$SANDBOX/wiki" "$SANDBOX/.raw" "$SANDBOX/.vault-meta" \
  "$SANDBOX/.claude-memory" "$SANDBOX/scripts"
cp "$ROOT/scripts/stop-hook.py" "$ROOT/scripts/reindex.py" \
   "$ROOT/scripts/vault_schema.py" "$ROOT/scripts/vault-write.py" \
   "$ROOT/scripts/plan_lifecycle.py" \
   "$ROOT/scripts/validate-vault.py" "$ROOT/scripts/bm25-index.py" \
   "$ROOT/scripts/retrieve.py" "$ROOT/scripts/memory-backup.py" \
   "$ROOT/scripts/dense-refresh-worker.py" \
   "$ROOT/scripts/lib_sanitize.py" "$ROOT/scripts/pipeline_events.py" \
   "$SANDBOX/scripts/"
chmod +x "$SANDBOX/scripts/"*.py
export OLLAMA_URL="http://127.0.0.1:9"

cat > "$SANDBOX/.gitignore" <<'EOF'
.vault-meta/.stop-hook.lock
.vault-meta/.vault-write.lock
.vault-meta/.bm25.lock
.vault-meta/bm25/
.vault-meta/.retrieval.lock
.vault-meta/.dense-refresh.lock
.vault-meta/retrieval/
.vault-meta/stop-hook-latency.jsonl
.vault-meta/dense-refresh.pending.json
.vault-meta/retrieval-quality.pending.json
.vault-meta/auto-commit.disabled
.vault-meta/memory-backup.json
.vault-meta/pipeline-events.jsonl
.vault-meta/pipeline-events.jsonl.1
.vault-meta/.pipeline-events.lock
EOF
touch "$SANDBOX/.raw/.gitkeep" "$SANDBOX/.claude-memory/.gitkeep"
printf '1\n' > "$SANDBOX/.vault-meta/address-counter.txt"

cat > "$SANDBOX/wiki/seed.md" <<'EOF'
---
type: concept
title: "Seed"
status: draft
created: 2026-01-01
updated: 2026-01-01
tags: [seed]
sessions: []
---

# Seed
EOF
cat > "$SANDBOX/wiki/hot.md" <<'EOF'
---
type: meta
title: "Hot"
status: evergreen
created: 2026-01-01
updated: 2026-01-01
tags: [meta]
sessions: []
---

# Hot

## Last Updated

Seed.

## Key Recent Facts

- Seed.

## Recent Changes

- Seed.

## Active Threads
EOF
cat > "$SANDBOX/wiki/log.md" <<'EOF'
---
type: meta
title: "Log"
status: evergreen
created: 2026-01-01
updated: 2026-01-01
tags: [meta]
sessions: []
---

# Log

## [2026-01-01] seed | Seed

Seed.
EOF
cat > "$SANDBOX/wiki/index.md" <<'EOF'
---
type: meta
title: "Index"
status: evergreen
created: 2026-01-01
updated: 2026-01-01
tags: [meta]
sessions: []
---

# Index
EOF

git -C "$SANDBOX" init -q
git -C "$SANDBOX" config user.email test@test
git -C "$SANDBOX" config user.name test

run_hook() { CLAUDE_PROJECT_DIR="$SANDBOX" bash "$HOOK"; }
commit_count() { git -C "$SANDBOX" rev-list --count HEAD 2>/dev/null || echo 0; }

# Happy path: required sparse index is built; dense refresh is scheduled without blocking.
out=$(run_hook); rc=$?
[[ "$rc" == 0 ]] && ok "sh-happy-exit0" || bad "sh-happy-exit0" "exit $rc"
[[ "$(commit_count)" == 1 ]] && ok "sh-happy-commit" || bad "sh-happy-commit" "commits=$(commit_count)"
[[ -s "$SANDBOX/.vault-meta/bm25/index.json" ]] && ok "sh-bm25-created" || bad "sh-bm25-created" "index missing"
[[ -s "$SANDBOX/.vault-meta/retrieval/index.json" ]] && ok "sh-section-index-created" || bad "sh-section-index-created" "section index missing"
printf '%s' "$out" | grep -q 'DENSE_DEFERRED' && ok "sh-dense-deferred" || bad "sh-dense-deferred" "no deferred notice"
[[ -s "$SANDBOX/.vault-meta/dense-refresh.pending.json" ]] && ok "sh-dense-retry-marker" || bad "sh-dense-retry-marker" "marker missing"
[[ -s "$SANDBOX/.vault-meta/retrieval-quality.pending.json" ]] && ok "sh-retrieval-quality-pending" || bad "sh-retrieval-quality-pending" "quality marker missing"

python3 - "$SANDBOX/scripts/stop-hook.py" "$SANDBOX" <<'PY' >/dev/null 2>&1
import importlib.util
import os
import sys
from pathlib import Path

script, root = map(Path, sys.argv[1:])
os.environ["CLAUDE_PROJECT_DIR"] = str(root)
spec = importlib.util.spec_from_file_location("stop_hook_dense_schedule_test", script)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
seen = []

class FakeProcess:
    pass

def fake_popen(args, **kwargs):
    seen.append((args, kwargs))
    return FakeProcess()

module.subprocess.Popen = fake_popen
ok, status = module.schedule_dense_refresh(True)
assert ok and status == "scheduled"
assert len(seen) == 1 and seen[0][1]["start_new_session"] is True
module.DENSE_RETRY.write_text("{broken", encoding="utf-8")
assert module.dense_refresh_due()
module.mark_dense_pending()
marker = __import__("json").loads(module.DENSE_RETRY.read_text(encoding="utf-8"))
assert marker["schema_version"] == 2 and marker["source_fingerprint"]

json = __import__("json")
sparse_path = root / ".vault-meta/retrieval/index.json"
dense_path = root / ".vault-meta/retrieval/dense.json"
sparse = json.loads(sparse_path.read_text(encoding="utf-8"))
fingerprint = sparse["source_fingerprint"]
docs = sparse["docs"]
dense = {
    "schema_version": sparse["schema_version"],
    "chunk_config": sparse["chunk_config"],
    "source_fingerprint": fingerprint,
    "model": "bge-m3",
    "complete": True,
    "embeddings": {chunk_id: [1.0] for chunk_id in docs},
}
dense_path.write_text(json.dumps(dense), encoding="utf-8")
module.DENSE_RETRY.write_text(
    json.dumps({
        "schema_version": 2,
        "source_fingerprint": fingerprint,
        "next_retry_at": 0,
    }),
    encoding="utf-8",
)
assert not module.dense_refresh_due(now=1)

dense["complete"] = False
dense_path.write_text(json.dumps(dense), encoding="utf-8")
module.DENSE_RETRY.write_text(
    json.dumps({
        "schema_version": 2,
        "source_fingerprint": fingerprint,
        "next_retry_at": 100,
    }),
    encoding="utf-8",
)
assert not module.dense_refresh_due(now=1)
assert module.dense_refresh_due(now=100)

module.DENSE_RETRY.write_text(
    json.dumps({
        "schema_version": 2,
        "source_fingerprint": "old-corpus",
        "next_retry_at": 100,
    }),
    encoding="utf-8",
)
assert module.dense_refresh_due(now=1)
PY
[[ "$?" == 0 ]] && ok "sh-dense-detached-worker" || bad "sh-dense-detached-worker" "worker was not detached"

# Missing section index self-heals even with no wiki diff.
rm -f "$SANDBOX/.vault-meta/retrieval/index.json"
before=$(commit_count)
out=$(run_hook)
[[ -s "$SANDBOX/.vault-meta/retrieval/index.json" ]] && ok "sh-sparse-self-heal-clean-tree" || bad "sh-sparse-self-heal-clean-tree" "index not rebuilt"
[[ "$(commit_count)" == "$before" ]] && ok "sh-sparse-derived-no-commit" || bad "sh-sparse-derived-no-commit" "unexpected commit"

# A clean tree can contain a newer committed corpus than the sparse/dense
# snapshots when another session committed first. The Stop hook must catch up
# both indexes even when an old fingerprint is still in retry backoff.
printf '\nnew committed corpus term\n' >> "$SANDBOX/wiki/index.md"
git -C "$SANDBOX" add wiki/index.md
git -C "$SANDBOX" commit -qm "test: committed corpus drift"
python3 - "$SANDBOX/.vault-meta/dense-refresh.pending.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.write_text(json.dumps({
    "schema_version": 2,
    "source_fingerprint": "old-corpus",
    "next_retry_at": 4102444800,
}), encoding="utf-8")
PY
out=$(run_hook)
printf '%s' "$out" | grep -q 'DENSE_DEFERRED' \
  && ok "sh-clean-committed-corpus-dense-catchup" || bad "sh-clean-committed-corpus-dense-catchup" "dense catch-up not scheduled"
python3 - "$SANDBOX/.vault-meta/retrieval/index.json" "$SANDBOX/.vault-meta/retrieval-quality.pending.json" <<'PY'
import json
import sys
from pathlib import Path

index_path, marker_path = map(Path, sys.argv[1:])
index = json.loads(index_path.read_text(encoding="utf-8"))
marker = json.loads(marker_path.read_text(encoding="utf-8"))
assert marker["corpus_sha256"] == index["source_fingerprint"]
PY
[[ "$?" == 0 ]] && ok "sh-clean-committed-corpus-quality-marker" || bad "sh-clean-committed-corpus-quality-marker" "quality marker did not follow rebuilt sparse index"

# Required phases use a configurable deadline and identify an exact repair command.
mv "$SANDBOX/scripts/reindex.py" "$SANDBOX/scripts/reindex.real.py"
cat > "$SANDBOX/scripts/reindex.py" <<'PY'
#!/usr/bin/env python3
import time
time.sleep(2)
PY
chmod +x "$SANDBOX/scripts/reindex.py"
before=$(commit_count)
out=$(LLM_OBSIDIAN_STOP_REQUIRED_TIMEOUT_SEC=1 run_hook)
printf '%s' "$out" | grep -q 'reindex timed out after 1s' \
  && ok "sh-required-timeout-phase" || bad "sh-required-timeout-phase" "phase-specific timeout missing"
printf '%s' "$out" | grep -q 'python3 scripts/reindex.py --quiet --folder-indexes' \
  && ok "sh-required-timeout-hint" || bad "sh-required-timeout-hint" "repair command missing"
[[ "$(commit_count)" == "$before" ]] && ok "sh-required-timeout-no-commit" || bad "sh-required-timeout-no-commit" "commit after required timeout"
mv "$SANDBOX/scripts/reindex.real.py" "$SANDBOX/scripts/reindex.py"

out=$(LLM_OBSIDIAN_STOP_REQUIRED_TIMEOUT_SEC=invalid run_hook)
printf '%s' "$out" | grep -q "STOP_CONFIG_WARN: LLM_OBSIDIAN_STOP_REQUIRED_TIMEOUT_SEC" \
  && ok "sh-invalid-required-timeout-fallback" || bad "sh-invalid-required-timeout-fallback" "required fallback warning missing"

# Validation failure blocks commit and leaves vault dirty.
printf '\n[[Missing Target]]\n' >> "$SANDBOX/wiki/seed.md"
before=$(commit_count)
out=$(run_hook); rc=$?
printf '%s' "$out" | grep -q 'COMMIT_BLOCKED' && ok "sh-schema-block-hint" || bad "sh-schema-block-hint" "no block notice"
[[ "$(commit_count)" == "$before" ]] && ok "sh-schema-block-no-commit" || bad "sh-schema-block-no-commit" "invalid vault committed"
git -C "$SANDBOX" status --short wiki/seed.md | grep -q . && ok "sh-schema-block-leaves-dirty" || bad "sh-schema-block-leaves-dirty" "change disappeared"
perl -0pi -e 's/\n\[\[Missing Target\]\]\n/\n/' "$SANDBOX/wiki/seed.md"
out=$(run_hook)

# Unrelated staging survives and is not included in scoped commit.
printf 'user work\n' > "$SANDBOX/user.txt"
git -C "$SANDBOX" add user.txt
printf '\nvalid scoped change\n' >> "$SANDBOX/wiki/seed.md"
before=$(commit_count)
out=$(run_hook)
[[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-scoped-commit" || bad "sh-scoped-commit" "scoped commit missing"
git -C "$SANDBOX" show --name-only --format= HEAD | grep -q '^user.txt$' \
  && bad "sh-unrelated-not-committed" "user.txt entered commit" || ok "sh-unrelated-not-committed"
git -C "$SANDBOX" diff --cached --name-only | grep -q '^user.txt$' \
  && ok "sh-unrelated-staging-preserved" || bad "sh-unrelated-staging-preserved" "user.txt no longer staged"

# Python fcntl lock contention: loser skips safely without external flock CLI.
printf '\nlock probe\n' >> "$SANDBOX/wiki/seed.md"
python3 - "$SANDBOX/.vault-meta/.stop-hook.lock" "$SANDBOX/.lock-ready" <<'PY' &
import fcntl, pathlib, sys, time
with open(sys.argv[1], "a+") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    pathlib.Path(sys.argv[2]).touch()
    time.sleep(4)
PY
holder=$!
for _ in $(seq 1 40); do [[ -e "$SANDBOX/.lock-ready" ]] && break; sleep 0.05; done
before=$(commit_count)
out=$(run_hook); rc=$?
[[ "$rc" == 0 ]] && ok "sh-busy-exit0" || bad "sh-busy-exit0" "exit $rc"
printf '%s' "$out" | grep -q 'STOP_LOCK_BUSY' && ok "sh-busy-hint" || bad "sh-busy-hint" "no busy hint"
[[ "$(commit_count)" == "$before" ]] && ok "sh-busy-no-commit" || bad "sh-busy-no-commit" "commit under lock"
wait "$holder" 2>/dev/null
rm -f "$SANDBOX/.lock-ready"
out=$(run_hook)
[[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-catchup-commit" || bad "sh-catchup-commit" "next Stop did not catch up"

# Explicit opt-out short-circuits all turn-end mutations.
printf '\noptout probe\n' >> "$SANDBOX/wiki/seed.md"
touch "$SANDBOX/.vault-meta/auto-commit.disabled"
before=$(commit_count)
out=$(run_hook); rc=$?
printf '%s' "$out" | grep -q 'AUTO_COMMIT_DISABLED' && ok "sh-optout-hint" || bad "sh-optout-hint" "no optout hint"
[[ "$(commit_count)" == "$before" ]] && ok "sh-optout-no-commit" || bad "sh-optout-no-commit" "commit despite optout"
rm "$SANDBOX/.vault-meta/auto-commit.disabled"
out=$(run_hook)
[[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-optout-reenable" || bad "sh-optout-reenable" "commit did not resume"

# Explicit opt-in owns .claude-memory; disabled mode leaves that path untouched.
mkdir -p "$SANDBOX/memory-source"
printf 'safe memory\n' > "$SANDBOX/memory-source/clean.md"
printf '{"enabled": true, "source": "%s"}\n' "$SANDBOX/memory-source" > "$SANDBOX/.vault-meta/memory-backup.json"
printf '\nclean memory opt-in probe\n' >> "$SANDBOX/wiki/seed.md"
before=$(commit_count)
out=$(run_hook)
[[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-memory-optin-commit" || bad "sh-memory-optin-commit" "commit missing"
git -C "$SANDBOX" show --name-only --format= HEAD | grep -q '^.claude-memory/clean.md$' \
  && ok "sh-memory-optin-owned" || bad "sh-memory-optin-owned" "clean backup not committed"

rm "$SANDBOX/.vault-meta/memory-backup.json"
printf 'manual disabled edit\n' >> "$SANDBOX/.claude-memory/clean.md"
printf '\ndisabled memory ownership probe\n' >> "$SANDBOX/wiki/seed.md"
before=$(commit_count)
out=$(run_hook)
[[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-memory-disabled-wiki-commit" || bad "sh-memory-disabled-wiki-commit" "wiki commit missing"
git -C "$SANDBOX" show --name-only --format= HEAD | grep -q '^.claude-memory/' \
  && bad "sh-memory-disabled-not-owned" "disabled memory entered commit" || ok "sh-memory-disabled-not-owned"
git -C "$SANDBOX" status --short -- .claude-memory/clean.md | grep -q . \
  && ok "sh-memory-disabled-left-dirty" || bad "sh-memory-disabled-left-dirty" "manual edit disappeared"
git -C "$SANDBOX" show HEAD:.claude-memory/clean.md > "$SANDBOX/.claude-memory/clean.md"

# An enabled backup with a residual credential fails closed before commit.
printf '%s\n%s\n%s\n' \
  '-----BEGIN '"PRIVATE KEY"'-----' 'not-a-real-test-key' '-----END '"PRIVATE KEY"'-----' \
  > "$SANDBOX/memory-source/leak.md"
printf '{"enabled": true, "source": "%s"}\n' "$SANDBOX/memory-source" > "$SANDBOX/.vault-meta/memory-backup.json"
printf '\nmemory safety probe\n' >> "$SANDBOX/wiki/seed.md"
before=$(commit_count)
out=$(run_hook); rc=$?
printf '%s' "$out" | grep -q 'MEMORY_BACKUP_BLOCKED' && ok "sh-memory-secret-block-hint" || bad "sh-memory-secret-block-hint" "no block notice"
[[ "$(commit_count)" == "$before" ]] && ok "sh-memory-secret-no-commit" || bad "sh-memory-secret-no-commit" "commit despite residual secret"
[[ ! -e "$SANDBOX/.claude-memory/leak.md" ]] && ok "sh-memory-secret-no-copy" || bad "sh-memory-secret-no-copy" "secret copied"
rm "$SANDBOX/.vault-meta/memory-backup.json" "$SANDBOX/memory-source/leak.md"
out=$(run_hook)
[[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-memory-disable-catchup" || bad "sh-memory-disable-catchup" "commit did not resume"

# Latency telemetry remains machine-local and structured.
LAT="$SANDBOX/.vault-meta/stop-hook-latency.jsonl"
tail -1 "$LAT" | python3 -c '
import json, sys
d=json.loads(sys.stdin.read())
assert isinstance(d["total_s"], (int,float))
assert all(k in d for k in ("reindex_s","bm25_s","sparse_s","dense_s","validate_s","commit_s","lock","wiki_dirty","commit_blocked"))
assert d["dense_s"] < 0.25
' 2>/dev/null && ok "sh-latency-schema" || bad "sh-latency-schema" "invalid latency record"

latency_p95=$(python3 - "$LAT" <<'PY'
import json, math, sys
rows=[json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
values=sorted(row["total_s"] for row in rows if row.get("wiki_dirty") and not row.get("commit_blocked"))
print(values[max(0, math.ceil(len(values)*0.95)-1)] if values else 999)
PY
)
python3 - "$latency_p95" <<'PY' >/dev/null 2>&1
import sys
assert float(sys.argv[1]) < 1.0
PY
[[ "$?" == 0 ]] && ok "sh-dirty-stop-p95-under-1s" || bad "sh-dirty-stop-p95-under-1s" "p95=${latency_p95}s"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
if (( fail > 0 )); then
  printf '\nFailures:\n'
  for item in "${failures[@]}"; do printf '  - %s\n' "$item"; done
  exit 1
fi
