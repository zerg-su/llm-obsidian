# Capability-gap audit

Use only for an explicitly authorized integration audit against named reference
skills or practices. Preservation mode remains the default.

Build an exhaustive matrix with one row per relevant reference capability and
exactly one verdict:

- `adopted`: a local carrier implements it and evidence demonstrates behavior;
- `equivalent`: a different local mechanism establishes the same outcome;
- `missing`: no carrier exists; propose the smallest bounded carrier;
- `rejected`: the capability is technology-specific, unsafe, or outside the
  approved product outcome; record the rationale;
- `deferred`: it is relevant but current evidence or authority is insufficient;
  record an owner and the missing evidence.

Classify general engineering judgment separately from upstream tooling,
trackers, installers, orchestration, and publishing mechanics. A tidy installed
inventory does not prove completeness. No audit may report complete while a
relevant row is absent or unclassified.

For every adopted/equivalent discipline, require a pressure scenario that can
fail when the semantic rule is removed. Structural text checks may guard wiring
or metadata, but they do not by themselves establish model behavior.
