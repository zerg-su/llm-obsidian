# ADR: defer production delta packets pending live review-quality evidence

Status: proposed experiment; production change rejected for now

Date: 2026-07-31

Applies to: RT03 dogfood of LLM Obsidian 2.5

## Problem

Same-session review verification should receive the smallest deterministic
evidence needed to judge a resolution without losing the approved task,
review standard, original findings, or exact revision boundary. The bounded
question is whether a machine-built delta packet can replace the current
rebuilt verification packet without reducing review quality.

This decision does not change review routing, provider lifecycle, callback
schemas, verification profiles, permissions, or review budgets. It does not
store a conversation transcript or make a delta packet authoritative over the
exact product HEAD.

## Facts and invariants

- `verify_review_lane` already enforces the same axis, lane, surface, and parent
  reviewer operation. The provider session therefore retains its original
  conversational context.
- `task-review-runner.py` rebuilds a ContextPacket for the new HEAD before
  same-session verification. It packages the approved plan, review skill, task
  metadata, product instructions, exact HEAD, and `git show HEAD`.
- `git show HEAD` covers only the final resolution commit. A valid executor may
  resolve a finding across more than one commit, so this packaged diff is not a
  complete resolution-boundary delta. The reviewer can still inspect the exact
  read-only product HEAD.
- The gate retains the reviewed HEAD while awaiting resolution, but the rebuilt
  packet does not bind that HEAD, the original packet digest, or the material
  finding evidence.
- ContextPacket inputs must remain deterministic, bounded, owner-readable, and
  free of raw conversation. Exact HEAD/profile validation and product-HEAD
  inspection remain authoritative.

## Alternatives

### 1. Keep rebuilding the current packet

This has the lowest implementation risk and preserves the exact product HEAD.
It duplicates stable plan/instruction inputs and relies on provider-session
memory for the reviewed boundary and findings. Its packaged diff can omit
earlier commits in a multi-commit resolution.

### 2. Continue the same session with an anchored delta supplement

Build a verification-only packet containing the original manifest pointer and
digest, reviewed and resolved HEADs, exact findings, `git diff
<reviewed>..<resolved>`, and bounded verification evidence. The original
session plus immutable original packet preserves prior context; the supplement
focuses the reviewer on everything changed since its decision. The reviewer
must still inspect the exact product HEAD and the gate must still validate the
unchanged verification profile.

This is the recommended target design if live evidence demonstrates equivalent
or better material findings.

### 3. Restart a fresh reviewer with a composed full packet

A fresh session could receive both original context and the delta, but it would
discard the proven same-session identity, add restart/budget behavior, and make
context reconstruction responsible for conversational continuity. It is not
justified for this boundary.

## Prototype and evidence

Run exactly:

```bash
python3 prototypes/rt03-review-delta-packet.py
```

The disposable fixture creates a reviewed commit and two resolution commits.
It builds an original ContextPacket and a verification delta packet with the
existing `ContextBuilder`, then checks:

- byte-stable packet identity under reordered inputs and metadata;
- exact original-manifest digest and reviewed/resolved HEAD bindings;
- complete multi-commit resolution coverage;
- preservation of a material finding and regression evidence; and
- the same bounded oracle verdict from the delta as from the exact resolved
  tree.

The experiment passes structurally and demonstrates a concrete advantage:
`git show HEAD` misses the earlier fix while the reviewed-to-resolved delta
covers both the fix and its regression test. In the bounded fixture, the
HEAD-only change set contained one file while the resolution range contained
two; the range diff was 373 bytes and the complete generated packet was 2,941
bytes.

It does **not** compare live reviewer outputs. A deterministic fixture can prove
packet identity and evidence coverage, but it cannot establish that a model
will assign the same severities, notice the same unrelated regressions, or use
its retained session context reliably. Treating structural completeness as
review-quality proof would overstate the result.

## Decision

Do not change the production review runner in RT03. Retain exact product-HEAD
inspection and the current same-session gate. Keep alternative 2 as the
ADR-quality candidate, with these required fields:

1. original ContextPacket pointer and digest;
2. reviewed HEAD and resolved HEAD;
3. exact unresolved material findings;
4. complete reviewed-to-resolved Git delta, fail-closed on invalid ancestry or
   budget overflow rather than silently truncating; and
5. verification profile identity and bounded command evidence.

Promotion requires a separate live paired experiment: give the same reviewer
session the current packet and the candidate delta on equivalent seeded
material findings, compare material finding/approval decisions against exact
tree inspection, and confirm no increase in missed findings. Until then the
quality success criterion is unproven, so no production improvement is clearly
authorized by the prototype result.

## Control flow and recovery

The candidate flow is: initial immutable packet → material callback → executor
resolution commit(s) → machine-built anchored delta → exact same-session
continuation → typed verification callback. Packet build failure leaves the
gate at `awaiting-resolution`; it must not send a partial prompt or open a new
surface. Replay of identical inputs must reproduce the same packet identity.

If a future rollout regresses review quality or packet construction, rollback
is a local return to the existing full rebuilt packet; review gate state,
callback schemas, and provider ownership need no migration.

## Acceptance for a future production change

- Hermetic tests cover one- and multi-commit resolution ranges, deterministic
  replay, original packet/finding identity, ancestry rejection, and byte caps.
- A live paired review shows no material-finding loss relative to exact product
  inspection and the current flow.
- Same-session axis/lane/surface identity and verification-profile binding are
  unchanged.
- No raw transcript, new dependency, public schema, second store, or second
  provider session is introduced.
