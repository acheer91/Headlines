"""Every migration must survive being applied twice: once by hand (or by an old Postgres initdb mount),
then again by the runner. A fresh install broke on 004 exactly this way (2026-09-27 review).
Needs TEST_DATABASE_URL (a throwaway database; it is wiped)."""
import os
from pathlib import Path

import psycopg
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "db" / "migrations").glob("*.sql"))
TABLES = ("team_season_stats", "team_season_leaders", "bet_results", "game_summaries", "fetch_log",
          "odds_snapshots", "games", "teams", "schema_migrations")


def _wipe():
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {', '.join(TABLES)} CASCADE")


def _tables():
    with psycopg.connect(TEST_DB) as conn:
        return {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}


def test_every_file_applied_by_hand_then_by_the_runner():
    from app import migrate
    _wipe()
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        for f in MIGRATIONS:
            conn.execute(f.read_text(encoding="utf-8"))
    assert [f.name for f in MIGRATIONS] == migrate.migrate(TEST_DB)          # runs them all again
    t = _tables()
    assert {"games", "bet_results", "game_summaries", "team_season_stats"} <= t
    assert "team_season_leaders" not in t                                      # 003's old name cleaned up


def test_runner_twice_is_a_no_op():
    from app import migrate
    _wipe()
    assert len(migrate.migrate(TEST_DB)) == len(MIGRATIONS)
    assert migrate.migrate(TEST_DB) == []


def test_each_file_is_idempotent_on_its_own():
    _wipe()
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        for f in MIGRATIONS:
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute(f.read_text(encoding="utf-8"))                        # same file, straight again
    assert "team_season_stats" in _tables()



def test_upgrade_existing_phase2_database_with_rows():
    """A database created before 003 (with real rows) upgrades cleanly and keeps its data."""
    from app import migrate
    _wipe()
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(MIGRATIONS[0].read_text(encoding="utf-8"))            # 001
        conn.execute(MIGRATIONS[1].read_text(encoding="utf-8"))            # 002
        conn.execute("""CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now());
                        INSERT INTO schema_migrations (name) VALUES ('001_init.sql'), ('002_game_details.sql')""")
        conn.execute("INSERT INTO teams (league, espn_id, abbr, name) VALUES ('nfl','1','AAA','A'), ('nfl','2','BBB','B')")
        conn.execute("""INSERT INTO games (league, espn_id, start_time, state, home_team_id, away_team_id,
                                           home_score, away_score, season, week)
                        VALUES ('nfl','g1', now(), 'post', 1, 2, 24, 17, 2026, 3),
                               ('nfl','g2', now(), 'pre', 1, 2, NULL, NULL, 2026, 4)""")
        conn.execute("INSERT INTO fetch_log (league, source, ok, season, week) VALUES ('nfl','espn',true,2026,3)")
    assert migrate.migrate(TEST_DB) == [f.name for f in MIGRATIONS[2:]]
    with psycopg.connect(TEST_DB) as conn:
        rows = conn.execute("SELECT espn_id, season_type, completed, time_valid FROM games ORDER BY espn_id").fetchall()
        assert rows == [("g1", 2, True, True), ("g2", 2, False, True)]
        assert conn.execute("SELECT season_type FROM fetch_log").fetchone() == (2,)
        conn.execute("SELECT data FROM team_season_stats")                  # 004's table exists, 003's name gone
        assert conn.execute("SELECT to_regclass('team_season_leaders')").fetchone() == (None,)
