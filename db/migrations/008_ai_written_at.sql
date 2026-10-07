-- Phase 4 (CTO, 2026-10-01): when the shown text was written. updated_at also moves when a refresh claims or fails,
-- and a failed refresh keeps the last good preview, so the screen's "Updated" time needs its own column.
ALTER TABLE ai_texts ADD COLUMN IF NOT EXISTS written_at TIMESTAMPTZ;
UPDATE ai_texts SET written_at = updated_at
WHERE written_at IS NULL AND status IN ('ready', 'no_sources') AND body IS NOT NULL;
