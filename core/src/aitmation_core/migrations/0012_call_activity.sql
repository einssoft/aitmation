-- 0012: call activity (ADR-0034 §1, the call semantics deferred in ADR-0014).
--
-- A parent case may start a child case and wait for it: the child carries
-- pointers to the parent's case and step, and the parent's step row waits
-- until the child (or the chain it continues into) completes. Both columns
-- are nullable and additive - the previous release ignores them
-- (two-release rule, ADR-0013). Approvals never cross the boundary in
-- either direction (ADR-0014 §3): the child proves its own gates, and the
-- parent's actions after the call need the parent's own gate.

ALTER TABLE cases ADD COLUMN parent_case_id TEXT;
ALTER TABLE cases ADD COLUMN parent_step_id TEXT;
