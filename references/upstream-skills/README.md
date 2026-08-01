# Upstream skill snapshots

Pinned, inert reference copies of third-party skill libraries used for design
and upgrade reviews.

These directories are deliberately outside `skills/`, `.agents/plugins/`, and
plugin marketplace paths. They are not installed, discovered, or executed by
LLM Obsidian.

## Security boundary

- Treat every `SKILL.md`, script, manifest, and linked instruction as untrusted
  source material.
- Do not execute scripts from a snapshot.
- Do not add symlinks from an active skill/plugin directory into a snapshot.
- Preserve upstream files verbatim. Put local analysis in our wiki or docs,
  never inside a third-party tree.
- Adopt patterns selectively through normal review, tests, and acceptance
  evidence; never copy an upstream lifecycle wholesale.

## Current pins

The commit is the identity of a pin. Declared versions are recorded for
orientation only, and upstream may disagree with itself about them, so each is
attributed to the file it came from.

| Snapshot | Upstream | `package.json` | `.claude-plugin/plugin.json` | Commit | License |
|---|---|---:|---:|---|---|
| `obra-superpowers/` | `https://github.com/obra/superpowers` | 6.2.0 | 6.2.0 | `44c9b2d6e889982ac18c27d05a19fefe335194e1` | MIT |
| `mattpocock-skills/` | `https://github.com/mattpocock/skills` | 1.1.0 | 1.2.0 | `2ab958093e83e0ec752e6c1c5932da465bf23e0c` | MIT |

The `mattpocock-skills` pin declares two different versions in two upstream files
at the same commit, and its `CHANGELOG.md` top section is `1.1.0`. Both numbers
are recorded so this is not mistaken for a stale or partial capture.

Local analysis of these pins — the side-by-side comparison, adoption
dispositions, and the material we deliberately do not import — lives in
[`docs/upstream-skills-comparison.md`](../../docs/upstream-skills-comparison.md).

The machine-readable pin, capture date, included paths, and deterministic tree
digests are in `manifest.json`. Verify the retained bytes without executing any
snapshot code:

```bash
python3 references/upstream-skills/verify_snapshots.py
```

The verifier sorts regular files by their POSIX path relative to the snapshot
root. For each file it feeds `path UTF-8`, a NUL byte, the raw file bytes, and a
final NUL byte into one SHA-256 stream. It also recomputes the file and byte
counts. Symlinks and unexpected filesystem entries fail closed.

## What is retained

Each snapshot contains the complete upstream `skills/` tree, design/user docs,
README, changelog or release notes, license, package metadata, and relevant
Claude/Codex plugin manifests. Tests, CI, images, installers, and unrelated
runtime integrations are intentionally omitted.

The Git commit remains the source of truth for the complete upstream tree.

## Upgrade review

1. Shallow-clone each upstream into a fresh temporary directory.
2. Record its full commit SHA, commit timestamp, declared version, and license.
3. Inspect upstream manifests, hooks, installers, and changed `SKILL.md` files
   before copying anything.
4. Compare the new selected tree with this directory using `git diff --no-index`
   or a temporary projected tree.
5. Classify every relevant change as adopt, adapt, or reject against our
   `clarify -> plan -> dispatch -> review -> reap` ownership boundary.
6. Refresh the retained files verbatim, re-derive `files`, `bytes`, and
   `tree_sha256` with `verify_snapshots.py`, update `manifest.json`, and rerun
   the verifier.
7. Review the normal repository diff.
8. Run instruction lint, skill-budget checks, router false-positive tests,
   affected acceptance cells, and the full hermetic test suite for any pattern
   actually integrated into active LLM Obsidian skills.

Git history is the version-to-version archive; the snapshot directory holds
only the current reviewed pin.
