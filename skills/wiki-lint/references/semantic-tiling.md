# Semantic Tiling (DragonScale Mechanism 3) — полная спецификация

Read on demand из wiki-lint check 10. Детект/делегация, scope, security, bands, калибровка, exit-коды.

## Semantic Tiling (DragonScale Mechanism 3 MVP, opt-in)

**Opt-in feature.** Semantic tiling flags candidate duplicate *pages* (not just concept pages — see Scope below) using embedding cosine similarity. Local ollama only by default; remote endpoints require an explicit override flag.

### Detection and delegation

```bash
if [ -x ./scripts/tiling-check.py ] && command -v python3 >/dev/null 2>&1; then
  ./scripts/tiling-check.py --peek > /tmp/tiling-peek.json 2>/dev/null
  PEEK_EXIT=$?
  case $PEEK_EXIT in
    0)  TILING_READY=1 ;;                                  # ready
    2)  TILING_READY=0 ; echo "tiling ERROR: usage error (exit 2); inspect /tmp/tiling-peek.json" ;;
    3)  TILING_READY=0 ; echo "tiling ERROR: cache corrupt (exit 3); inspect .vault-meta/tiling-cache.json" ;;
    4)  TILING_READY=0 ; echo "tiling ERROR: vault exceeds scale hard-fail (exit 4); batching required" ;;
    10) TILING_READY=0 ; echo "tiling skipped: ollama not reachable (exit 10)" ;;
    11) TILING_READY=0 ; echo "tiling skipped: run 'ollama pull bge-m3' to enable (exit 11)" ;;
    *)  TILING_READY=0 ; echo "tiling ERROR: unexpected exit code $PEEK_EXIT from tiling-check.py --peek" ;;
  esac
else
  TILING_READY=0
  echo "tiling skipped: scripts/tiling-check.py or python3 not available"
fi
```

Inspect `/tmp/tiling-peek.json` (structured diagnostics: script path, python interpreter, ollama URL, cache state, thresholds state) whenever the status is ambiguous. Never collapse unknown exits into "unknown status" silently.

When `TILING_READY=1`:

```bash
./scripts/tiling-check.py --report wiki/meta/reports/tiling-report-YYYY-MM-DD.md
REPORT_EXIT=$?
case $REPORT_EXIT in
  0)  echo "tiling report written" ;;
  2)  echo "tiling ERROR: usage error during --report" ;;
  3)  echo "tiling ERROR: cache corrupt during --report" ;;
  4)  echo "tiling ERROR: scale hard-fail during --report" ;;
  10) echo "tiling ERROR: ollama became unreachable between --peek and --report" ;;
  11) echo "tiling ERROR: model became unavailable between --peek and --report" ;;
  *)  echo "tiling ERROR: unexpected exit code $REPORT_EXIT from tiling-check.py --report" ;;
esac
```

### Scope (what the helper scans)

- Includes: every `.md` under `wiki/` **except** the exclusion set below. The scope is "candidate tileable pages," not just `type: concept`.
- Excludes (path): anything under `wiki/folds/` or `wiki/meta/`.
- Excludes (filename): `_index.md`, `index.md`, `log.md`, `hot.md`, `overview.md`, `dashboard.md`, `Wiki Map.md`, `getting-started.md`.
- Excludes (frontmatter): `type: meta` or `type: fold`.
- Excludes (security): symlinks. Any page file that is a symlink, or whose resolved path escapes the vault root, is skipped.

If you place a real concept under `wiki/meta/` it will be excluded by path regardless of content. Keep concepts in their canonical folders.

### How the helper works

- Computes one embedding per included page via the ollama `bge-m3` model by default.
- Caches embeddings at `.vault-meta/tiling-cache.json`, keyed on `sha256(model + body)` so model drift auto-invalidates. Frontmatter is not part of the hash or the embedding input — pure frontmatter edits (tag changes, status bumps) do not trigger recomputation.
- Orphans are GC'd: when a cached page path no longer exists on disk, its entry is dropped on save.
- Concurrent-safe: exclusive flock on `.vault-meta/.tiling.lock` around cache I/O; per-PID temp file for atomic writes.

### Security posture

- Defaults to `http://127.0.0.1:11434`. `OLLAMA_URL` env override is accepted **only** with `--allow-remote-ollama` because page bodies are POSTed as embedding input.
- Symlinks and vault-root escapes are rejected.

### Bands (model-specific — recalibrate per embedding model)

Thresholds are a property of the embedding model's cosine distribution, not the
vault, so they MUST be recalibrated whenever the model changes. Current default
model `bge-m3` spreads cosines wider than the old `nomic-embed-text` (nomic
compressed cyrillic upward: dups ≥0.93; bge-m3's single highest pair in a
~320-page vault is ~0.93, with almost nothing above 0.92).

| Band | bge-m3 (calibrated) | nomic (historical) | Report section |
|---|---|---|---|
| Error | `>= 0.92` | `>= 0.94` | **Errors** — strong near-duplicate, likely the same concept |
| Review | `0.85 - 0.92` | `0.88 - 0.94` | **Review** — possible tile overlap; human judgement needed |
| Pass | `< 0.85` | `< 0.88` | not emitted |

Fresh installs seed conservative UNcalibrated bge-m3 bands (error 0.90 / review
0.85) via `bin/setup-dragonscale.sh`; run the calibration below before trusting
the report. Published generic reference points (different models/tasks):
Sentence Transformers `community_detection` defaults to 0.75; Quora-duplicate
calibrations land ~0.77-0.84 — these are model-dependent and not directly
comparable to bge-m3 on this vault.

### Calibration procedure (manual, one-time per model)

1. Run the helper with the current model. Capture the **Review** band pairs.
2. Temporarily lower `bands.review` in `.vault-meta/tiling-thresholds.json` to
   surface a wider sample near the distribution knee (for bge-m3 that is ~0.80,
   NOT 0.70 — 0.70 surfaces >1000 pairs). Aim for a labelable set (~40 pairs).
3. Label each pair: `duplicate`, `similar`, `distinct`.
4. Pick bands such that: (a) the `error` band contains >= 95% true duplicates;
   (b) the `review` band captures `similar` pairs without swamping the report
   with `distinct` ones.
5. Edit `.vault-meta/tiling-thresholds.json`: set `model`, new `bands.error` and
   `bands.review`, set `calibrated: true`, set `calibration_pairs_labeled`.
6. Re-run lint. Report footer now says `calibrated: true`.

### Scale

- Cold-cache cost is O(N) POSTs to ollama. Warm-cache cost is O(N^2) cosines in pure Python.
- Helper prints a warning at > 500 pages and hard-fails (exit 4) at > 5000. Revisit the implementation (batching, vectorized cosine, or external tooling) before exceeding either limit.

### Lint report embed

```markdown
## Semantic Tiling
See [[tiling-report-YYYY-MM-DD]] for the full pair listing.
- Errors (>=0.92): N pairs
- Review (0.85-0.92): M pairs
- Calibrated: true|false
```

### Invariants

- Read-only. `tiling-check.py` never modifies wiki pages.
- No auto-merge. Duplicates are listed, never resolved.
- Cache is incremental and model-scoped. Unchanged pages are not re-embedded.
- Exit codes: `0` ok, `2` usage error, `3` cache corrupt, `4` scale hard-fail, `10` ollama unreachable, `11` model missing. Surface all of them; do not collapse into a single "unknown" bucket.

---

