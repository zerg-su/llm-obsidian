---
name: research
description: Run protected, cited web research with minimal context. Use for current or niche questions; never as an unsafe-research fallback.
---

# Research

Use for current, multi-source web research that should return one cited artifact
to the coordinator. The harness owns routing, isolated runtime profiles,
callbacks, retries, recovery, and cleanup.

## Safe flow

Run the harness workflow and stop:

```bash
python3 scripts/research-isolation.py start --flow research \
  --owner "$(./scripts/current-session-id.sh)" --topic '<approved question>'
```

Keep the returned operation ID. The code-owned worker validates each stage and
sends an exact `advance` command to the originating surface; run it unchanged.
The first callback closes vaultless fetch and starts networkless synthesis; the
second validates the cited `answer.md` and cleans up exact owned resources.
Read-only recovery is `research-isolation.py status --owner <owner> --operation-id <id>`.
`unsafe-research` remains the only explicit full-context outbound route and is
never a fallback. The workflow passes only a minimal ContextPacket and validated
bounded files; only the coordinator may write the vault. Do not orchestrate
cmux/models manually or retry before reconciliation. Persist only pointers and
hashes; unknown state becomes `attention-required`.
