---
name: improve-skills
description: >-
  Audit and improve skill predictability without changing workflow semantics.
  Manual-only: use for an explicit skill-quality audit or requested skill edit.
disable-model-invocation: true
allowed-tools: Read Write Edit Glob Grep Bash
---

# Improve Skills

Use this skill only after the user explicitly asks to create, edit, audit, or
improve skills. Its root virtue is **predictability**: preserve the same
process while removing confirmed sources of variance or wasted context.

## 1. Establish the boundary

Read the repository instructions and inspect the working tree. Enumerate the
exact installed inventory:

```bash
python3 skills/improve-skills/scripts/audit_skills.py --json
```

Record protected behavior before editing: triggers, action/permission
boundaries, tools, scripts, schemas, lifecycle, writer paths, and runtime
orchestration. A quality edit may clarify or compress these contracts; changing
one is a separate product change and stays deferred.

For an explicit capability-integration audit against named references, also
read [capability-gap-model.md](references/capability-gap-model.md) completely.
Classify the reference denominator before judging the installed inventory. No
relevant reference capability remains unclassified; this integration mode may
propose bounded new carriers, but it does not silently authorize implementation.

Completion criterion: every in-scope `skills/*/SKILL.md` is in the inventory
and every protected behavior is named before a file is changed.

## 2. Apply the quality model

Read [quality-model.md](references/quality-model.md) completely. Run its five
passes—invocation, information hierarchy, steering, pruning, and goal
preservation—against every in-scope skill. The fifth pass records the approved
overall input and outcome, the skill's permitted local subgoal, completion
proxies that must not close the task, and the outcome evidence required before
claiming success. For every changed or new skill, also require a distinct
strong-intent trigger, router false-positive coverage, one verifiable
completion criterion, one authoritative source for each rule, progressive
disclosure, and an explicit authorization boundary. Remove no-op sentences
that cannot change behavior.

Assign exactly one verdict per skill:

- `fix`: name the concrete failure mode, quote or locate its evidence, and
  describe the smallest behavior-preserving correction;
- `no-change`: state that all five passes were checked and no confirmed problem
  remains;
- `defer`: name the improvement that would alter behavior or expand scope.

Potential polish is not a finding. Do not edit a skill merely to make wording
uniform.

Write one schema-v1 verdict record per skill using the exact shape in the
quality model. Validate the record set against the same inventory:

```bash
python3 skills/improve-skills/scripts/audit_skills.py \
  --verdicts <verdict-records.json> \
  --scope <skill> [--scope <skill> ...] --strict
```

Completion criterion: the validated verdict set and inventory contain the same
skill names exactly once, every record contains all five passes, and no local
completion proxy substitutes for its required outcome evidence.

## 3. Make the smallest edits

Apply only `fix` verdicts. Preserve repo-specific frontmatter/runtime metadata
and one rule source. Use `>-`/`|-` for multi-line descriptions; plain
continuations fail closed. Prefer a sharp criterion/pointer to a rewrite. Hide
conditional references only when a branch can skip them.

After each bounded batch, inspect the diff against step 1's protected behavior.
Reclassify any semantic change as `defer` and revert that edit.

Completion criterion: every changed hunk maps to one recorded `fix`, and the
diff changes no protected behavior.

## 4. Prove the result

Run:

```bash
python3 skills/improve-skills/scripts/audit_skills.py --strict
python3 skills/improve-skills/scripts/audit_skills.py \
  --verdicts <verdict-records.json> \
  --scope <skill> [--scope <skill> ...] --strict
make test-instruction-lint test-skill-budget test-codex-adapter
python3 scripts/release-acceptance.py check
```

Add narrower tests when a deterministic rule or script changes. Run broader
tests required by the approved release scope; do not substitute narrative
confidence for a failing check.

Completion criterion: deterministic audit and applicable tests pass, every
skill has one validated five-pass verdict, and the final report separates
changed, unchanged, and deferred skills while binding completion claims to the
approved outcome evidence.
