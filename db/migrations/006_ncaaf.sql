-- Phase 5a: NCAAF. Conference and poll rank are stored per game, not per team:
-- teams change conferences between seasons and ranks move every week.
ALTER TABLE games ADD COLUMN IF NOT EXISTS home_conf INT;      -- ESPN conferenceId
ALTER TABLE games ADD COLUMN IF NOT EXISTS away_conf INT;
ALTER TABLE games ADD COLUMN IF NOT EXISTS home_rank INT;      -- AP/CFP rank 1-25; NULL = unranked
ALTER TABLE games ADD COLUMN IF NOT EXISTS away_rank INT;
ALTER TABLE games ADD COLUMN IF NOT EXISTS neutral_site BOOLEAN NOT NULL DEFAULT FALSE;

-- Browsing across season types (step 11b): a cache key, and ESPN's season calendar per league.
ALTER TABLE fetch_log ADD COLUMN IF NOT EXISTS requested_season_type INT;
CREATE TABLE IF NOT EXISTS league_calendar (
    league      TEXT PRIMARY KEY,
    season      INT,
    stages      JSONB NOT NULL,   -- [{"season_type": 2, "week": 1, "label": "Week 1", "start": ..., "end": ...}, ...]
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
