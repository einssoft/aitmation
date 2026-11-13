-- 0002: chains (ADR-0014).
--
-- A business process is a chain of sub-process cases sharing one chain_id.
-- Additive change only - the previous application version ignores the new
-- column and keeps running (two-release rule, ADR-0013).

ALTER TABLE cases ADD COLUMN chain_id TEXT;

CREATE INDEX idx_cases_chain ON cases (chain_id);
