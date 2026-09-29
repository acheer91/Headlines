-- Phase 3: Temporal. Idempotent (see app/migrate.py). The handoff calls this 003_temporal.sql; 003 and 004
-- were already taken, and the api applies it at startup like every other file (no manual psql step).

-- Who captured each line: 'pull' (someone opened the app) or 'workflow' (the Temporal worker).
ALTER TABLE odds_snapshots ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'pull';

-- ESPN news, stored twice a day by the Headlines schedule with no AI; Phase 4 writes the Screen A feed from it.
CREATE TABLE IF NOT EXISTS news_items (
    id           SERIAL PRIMARY KEY,
    league       TEXT NOT NULL,
    espn_id      TEXT NOT NULL,              -- ESPN's article id, the dedupe key
    headline     TEXT NOT NULL,
    description  TEXT,
    url          TEXT,
    published_at TIMESTAMPTZ,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (league, espn_id)
);
CREATE INDEX IF NOT EXISTS news_items_league_published ON news_items (league, published_at DESC);
