-- Phase 4 (Adam, 2026-10-01): a text that failed for a reason retrying the same inputs won't fix (the fact check
-- rejected it twice, or a non-retryable error) is not written again after store.REJECTION_CAP (2) such failed
-- writes in total, the first and one retry: it waits for new inputs (a new game day or score, or a changed fact sheet
-- or article set).
-- Counts failures with no retry_after, for the claim's inputs; the next claim for different inputs resets it, and a
-- ready text clears it. Rate limits, 5xx errors and a missing key or checker never count. Headlines are one row per run: each failed row
-- carries 1, and the run counts the failed rows for its inputs since the last ready set.
ALTER TABLE ai_texts ADD COLUMN IF NOT EXISTS rejections INT NOT NULL DEFAULT 0;
