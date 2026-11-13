-- 0013: parallel branches and multi-instance steps (ADR-0037).
--
-- A case may hold several open step rows at once: one per parallel branch
-- after a split, one per instance of a multi-instance step. Each row says
-- which branch it belongs to and - for instances - which element of the
-- list it works on. All columns are nullable and additive; a sequential
-- case never writes them, and the previous release reads the table
-- unchanged (two-release rule, ADR-0013). cases.current_step_id keeps
-- pointing at the step opened last.

ALTER TABLE steps ADD COLUMN branch TEXT;
ALTER TABLE steps ADD COLUMN instance_index INTEGER;
ALTER TABLE steps ADD COLUMN instance_total INTEGER;
ALTER TABLE steps ADD COLUMN instance_input TEXT;
