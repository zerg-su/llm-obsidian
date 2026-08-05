# D-265-EPH-01: bounded ephemeral execution

D-265-EPH-01 supersedes the old blanket `claude -p` prohibition only for a
registered code-owned ephemeral adapter executing bounded review or
schema-producing work. That path requires subscription/billing-profile
preflight, fixed provider-specific argv, a minimal ContextPacket, schema
validation, bounded capabilities, and durable receipts before its result can be
accepted.

Arbitrary direct print-mode reviewer, dispatch, and task-split commands remain
forbidden. Continuable work or a capability unavailable to the ephemeral
profile requires a visible interactive cmux session. An ephemeral failure must
return a typed terminal disposition and must not trigger hidden interactive,
paid-credit, provider, or billing-path fallback.
