-- Bug bash fixes + C1 interceptions leader. Idempotent (see app/migrate.py).

-- ESPN numbers weeks per season type: preseason (1), regular season (2) and postseason (3) all have a
-- "week 1". Without the type, January's Wild Card board would mix in regular-season week 1 games.
ALTER TABLE games     ADD COLUMN IF NOT EXISTS season_type INT;
ALTER TABLE fetch_log ADD COLUMN IF NOT EXISTS season_type INT;

-- A canceled or postponed game is state 'post' with a 0-0 score; only a completed game is graded.
ALTER TABLE games ADD COLUMN IF NOT EXISTS completed BOOLEAN;
-- Flexed games have no kickoff time yet; ESPN sends a placeholder (midnight Eastern) with timeValid false.
ALTER TABLE games ADD COLUMN IF NOT EXISTS time_valid BOOLEAN NOT NULL DEFAULT TRUE;

-- Everything stored before this migration came from 2026 regular-season weeks 1-4, and its finals
-- were all played (checked 2026-09-27). Rows written from now on carry ESPN's own values.
UPDATE games SET season_type = 2 WHERE season_type IS NULL;
UPDATE fetch_log SET season_type = 2 WHERE season_type IS NULL AND ok;
UPDATE games SET completed = (state = 'post') WHERE completed IS NULL;

DROP INDEX IF EXISTS games_league_start;
CREATE INDEX IF NOT EXISTS games_league_week ON games (league, season, season_type, week);
CREATE INDEX IF NOT EXISTS games_league_start ON games (league, start_time);

-- Season leaders ESPN keeps per team (core API), for the one C1 leader the game summary lacks:
-- interceptions. Refreshed at most every few hours; season stats change once a week.
CREATE TABLE IF NOT EXISTS team_season_leaders (
    league       TEXT NOT NULL,
    team_espn_id TEXT NOT NULL,
    season       INT  NOT NULL,
    season_type  INT  NOT NULL,
    leaders      JSONB NOT NULL,          -- {"interceptions": {"name", "position", "value", "tied"} | null}
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league, team_espn_id, season, season_type)
);
