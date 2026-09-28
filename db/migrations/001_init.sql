-- Phase 1 schema. Built for all five leagues so later phases add rows, not tables.
-- Applied by the api at startup (app/migrate.py), like every file in this folder.

CREATE TABLE IF NOT EXISTS teams (
    id            SERIAL PRIMARY KEY,
    league        TEXT NOT NULL,              -- nfl | ncaaf | nba | epl | mls
    espn_id       TEXT NOT NULL,
    abbr          TEXT NOT NULL,
    name          TEXT NOT NULL,              -- "Buffalo Bills"
    short_name    TEXT,                       -- "Bills"
    logo_url      TEXT,
    color         TEXT,                       -- hex without '#', from ESPN
    UNIQUE (league, espn_id)
);

CREATE TABLE IF NOT EXISTS games (
    id              SERIAL PRIMARY KEY,
    league          TEXT NOT NULL,
    espn_id         TEXT NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    state           TEXT NOT NULL,            -- pre | in | post  (routes to C1 | C2 | D)
    status_detail   TEXT,                     -- "Sun, Sep 28th at 1:00 PM EDT" / "Q3 4:12" / "Final"
    period          INT,
    clock           TEXT,
    home_team_id    INT NOT NULL REFERENCES teams(id),
    away_team_id    INT NOT NULL REFERENCES teams(id),
    home_score      INT,
    away_score      INT,
    venue           TEXT,
    broadcast       TEXT,
    season          INT,
    week            INT,
    source          TEXT NOT NULL DEFAULT 'espn',   -- 'espn' or a backup source name
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (league, espn_id)
);
CREATE INDEX IF NOT EXISTS games_league_start ON games (league, start_time);

-- Every time we see a line we keep it. The first pre-game row per game is the grading line
-- (Phase 2). Nothing here is ever updated in place.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id              SERIAL PRIMARY KEY,
    game_id         INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    game_state      TEXT NOT NULL,            -- state of the game when captured
    provider        TEXT,                     -- sportsbook name, shown as a label
    details         TEXT,                     -- ESPN's own string, e.g. "BUF -3.5"
    home_spread     NUMERIC(5,1),             -- negative = home favored
    total           NUMERIC(5,1),
    home_ml         INT,
    away_ml         INT
);
CREATE INDEX IF NOT EXISTS odds_game_time ON odds_snapshots (game_id, captured_at);

-- One row per scoreboard fetch, so we can see freshness and failures.
CREATE TABLE IF NOT EXISTS fetch_log (
    id          SERIAL PRIMARY KEY,
    league      TEXT NOT NULL,
    source      TEXT NOT NULL,
    ok          BOOLEAN NOT NULL,
    games       INT,
    requested_week INT,                       -- NULL = "current week" request
    season      INT,
    week        INT,
    error       TEXT,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fetch_log_league_time ON fetch_log (league, fetched_at DESC);
