-- Weekend columns (Adam, 2026-10-06): one Ringer-style column per league (NFL, NCAAF), written Tuesday (NFL) and Sunday
-- (NCAAF) morning from the weekend's finals, their stored recaps and the news. Like headlines, a row per run with no
-- game: `league` says whose column it is. body: {title, paragraphs: [..]}. Idempotent: applied twice by the tests.
ALTER TABLE ai_texts ADD COLUMN IF NOT EXISTS league TEXT;
ALTER TABLE ai_texts DROP CONSTRAINT IF EXISTS ai_texts_kind_check;
ALTER TABLE ai_texts ADD CONSTRAINT ai_texts_kind_check
    CHECK (kind IN ('preview', 'recap', 'one_liner', 'headlines', 'weekend'));
CREATE INDEX IF NOT EXISTS ai_texts_weekend ON ai_texts (league, created_at DESC) WHERE kind = 'weekend';
