-- Phase 2: game detail cache and bet results.
-- Applied by the api's migration runner (app/migrate.py). Idempotent.

-- The raw ESPN summary (/summary?event=<id>) per game, parsed on read. One row per game,
-- overwritten on refresh. game_state is the state ESPN reported in this payload, so a summary
-- saved while live is refetched once the game is final; a final summary is kept.
CREATE TABLE IF NOT EXISTS game_summaries (
    game_id     INT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    game_state  TEXT NOT NULL,                -- pre | in | post, from the payload's own header
    payload     JSONB NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One graded result per game per market, overwritten on regrade (stat corrections), never duplicated.
-- A market with no usable line has no row; the screens show it as "Ungraded".
CREATE TABLE IF NOT EXISTS bet_results (
    id           SERIAL PRIMARY KEY,
    game_id      INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    market       TEXT NOT NULL CHECK (market IN ('moneyline', 'spread', 'total')),
    line_source  TEXT NOT NULL CHECK (line_source IN ('pre_game', 'espn_close', 'in_game')),
    snapshot_id  INT REFERENCES odds_snapshots(id) ON DELETE SET NULL,  -- NULL when the line came from ESPN's close
    provider     TEXT,
    home_spread  NUMERIC(5,1),                -- the line used: spread market
    total        NUMERIC(5,1),                -- the line used: total market
    home_ml      INT,                         -- the prices used: moneyline market
    away_ml      INT,
    home_score   INT NOT NULL,                -- the final score this result was graded on
    away_score   INT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('home', 'away', 'over', 'under', 'push')),
    margin       NUMERIC(5,1) NOT NULL,       -- always >= 0: by how much the outcome won
    graded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (game_id, market)
);
