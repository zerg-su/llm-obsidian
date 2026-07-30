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

Completion criterion: every in-scope `skills/*/SKILL.md` is in the inventory
and every protected behavior is named before a file is changed.

## 2. Apply the quality model

Read [quality-model.md](references/quality-model.md) completely. Run its four
passes—invocation, information hierarchy, steering, and pruning—against every
in-scope skill. For every changed or new skill, also require a distinct
strong-intent trigger, router false-positive coverage, one verifiable
completion criterion, one authoritative source for each rule, progressive
disclosure, and an explicit authorization boundary. Remove no-op sentences
that cannot change behavior.

Assign exactly one verdict per skill:

- `fix`: name the concrete failure mode, quote or locate its evidence, and
  describe the smallest behavior-preserving correction;
- `no-change`: state that all four passes were checked and no confirmed problem
  remains;
- `defer`: name the improvement that would alter behavior or expand scope.

Potential polish is not a finding. Do not edit a skill merely to make wording
uniform.

Completion criterion: the verdict set and inventory contain the same skill
names with no omissions or duplicates.

## 3. Make the smallest edits

Apply only `fix` verdicts. Keep each meaning in one authoritative place and
preserve repository-specific frontmatter and runtime metadata. Prefer a sharper
completion criterion or context pointer over a rewrite. Move conditional
reference only when a real branch does not need it.

After each bounded batch, inspect the diff against the protected behavior from
step 1. Reclassify any semantic change as `defer` and revert that edit.

Completion criterion: every changed hunk maps to one recorded `fix`, and the
diff changes no protected behavior.

## 4. Prove the result

Run:

```bash
python3 skills/improve-skills/scripts/audit_skills.py --strict
make test-instruction-lint test-skill-budget test-codex-adapter
python3 scripts/release-acceptance.py check
```

Add narrower tests when a deterministic rule or script changes. Run broader
tests required by the approved release scope; do not substitute narrative
confidence for a failing check.

Completion criterion: deterministic audit and applicable tests pass, every
skill has a verdict, and the final report separates changed, unchanged, and
deferred skills.
