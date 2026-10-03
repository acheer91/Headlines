"""Postgres access and the scoreboard write path. Plain SQL, no ORM."""
from __future__ import annotations

import atexit
import os
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .espn import Game, Team

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://scores:scores@localhost:5432/scores")


# A small pool: opening a Postgres connection costs ~5 ms, a third of a cached scoreboard request.
# Keyed by URL because tests point DATABASE_URL at a throwaway database.
_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def _pool() -> ConnectionPool:
    with _pools_lock:
        pool = _pools.get(DATABASE_URL)
        if pool is None:
            # prepare_threshold=None: no server-side prepared statements, which would go stale when a
            # migration (or a test) rebuilds a table under a pooled connection.
            # timeout=5: if Postgres is down, fail in seconds (/api/health says so) instead of every
            # request waiting psycopg_pool's default 30 s for a connection.
            pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, timeout=5.0,
                                  kwargs={"row_factory": dict_row, "prepare_threshold": None,
                                          "connect_timeout": 3},
                                  open=True, name="scores")
            _pools[DATABASE_URL] = pool
        return pool


def close_pools() -> None:
    """Close pooled connections and their worker threads (app shutdown, process exit)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """A pooled connection. Leaving the block commits (rolls back on an exception) and returns it."""
    with _pool().connection() as conn:
        yield conn


def _upsert_team(cur: psycopg.Cursor, league: str, t: Team) -> int:
    cur.execute(
        """
        INSERT INTO teams (league, espn_id, abbr, name, short_name, logo_url, color)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (league, espn_id) DO UPDATE SET
            abbr = EXCLUDED.abbr, name = EXCLUDED.name, short_name = EXCLUDED.short_name,
            logo_url = COALESCE(EXCLUDED.logo_url, teams.logo_url),
            color = COALESCE(EXCLUDED.color, teams.color)
        RETURNING id
        """,
        (league, t.espn_id, t.abbr, t.name, t.short_name, t.logo_url, t.color),
    )
    return cur.fetchone()["id"]


_BACKWARDS = ("((games.completed IS TRUE AND EXCLUDED.state <> 'post')"
              " OR (games.state = 'in' AND EXCLUDED.state = 'pre'))")


def save_games(conn: psycopg.Connection, games: list[Game], source: str = "espn",
               snapshot_source: str = "pull") -> list[tuple[int, str]]:
    """Upsert games and teams. Adds an odds snapshot only when the line changed,
    so a 30-second refresh cadence doesn't write a row per pull. Returns (game id, stored state) per game.
    snapshot_source tags new odds snapshots: 'pull' (the app) or 'workflow' (the Temporal worker)."""
    saved: list[tuple[int, str]] = []
    with conn.cursor() as cur:
        for g in games:
            home_id = _upsert_team(cur, g.league, g.home)
            away_id = _upsert_team(cur, g.league, g.away)
            cur.execute(
                """
                INSERT INTO games (league, espn_id, start_time, state, status_detail, period, clock,
                                   home_team_id, away_team_id, home_score, away_score, venue,
                                   broadcast, season, week, season_type, completed, time_valid,
                                   source, home_conf, away_conf, home_rank, away_rank, neutral_site,
                                   fetched_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (league, espn_id) DO UPDATE SET
                    -- Never backwards (same rule as update_game_status): a lagging response (a final
                    -- reported as live again, a live game as pre-game) changes nothing about the game,
                    -- scores included; otherwise a stale live score would be regraded as the final.
                    -- A stat correction arrives with the game still 'post', so it isn't blocked.
                    state = CASE WHEN {back} THEN games.state ELSE EXCLUDED.state END,
                    status_detail = CASE WHEN {back} THEN games.status_detail ELSE EXCLUDED.status_detail END,
                    period = CASE WHEN {back} THEN games.period ELSE EXCLUDED.period END,
                    clock = CASE WHEN {back} THEN games.clock ELSE EXCLUDED.clock END,
                    completed = CASE WHEN {back} THEN games.completed ELSE EXCLUDED.completed END,
                    home_score = CASE WHEN {back} THEN games.home_score ELSE EXCLUDED.home_score END,
                    away_score = CASE WHEN {back} THEN games.away_score ELSE EXCLUDED.away_score END,
                    start_time = EXCLUDED.start_time, venue = EXCLUDED.venue,
                    broadcast = EXCLUDED.broadcast, season = EXCLUDED.season,
                    week = EXCLUDED.week, season_type = EXCLUDED.season_type,
                    time_valid = EXCLUDED.time_valid,
                    -- The rank is frozen once a game is completed: next week's poll never relabels an old final.
                    home_conf = EXCLUDED.home_conf, away_conf = EXCLUDED.away_conf,
                    home_rank = CASE WHEN games.completed IS TRUE THEN games.home_rank ELSE EXCLUDED.home_rank END,
                    away_rank = CASE WHEN games.completed IS TRUE THEN games.away_rank ELSE EXCLUDED.away_rank END,
                    neutral_site = EXCLUDED.neutral_site,
                    source = EXCLUDED.source, fetched_at = now()
                RETURNING id, state
                """.replace("{back}", _BACKWARDS),
                (g.league, g.espn_id, g.start_time, g.state, g.status_detail, g.period, g.clock,
                 home_id, away_id, g.home_score, g.away_score, g.venue, g.broadcast,
                 g.season, g.week, g.season_type, g.completed, g.time_valid, source,
                 g.home_conf, g.away_conf, g.home_rank, g.away_rank, g.neutral_site),
            )
            row = cur.fetchone()
            game_id = row["id"]
            saved.append((game_id, row["state"]))

            if g.odds:
                o = g.odds
                cur.execute(
                    """
                    SELECT provider, details, home_spread, total, home_ml, away_ml
                    FROM odds_snapshots WHERE game_id = %s ORDER BY captured_at DESC, id DESC LIMIT 1
                    """,
                    (game_id,),
                )
                last = cur.fetchone()
                new = (o.provider, o.details,
                       None if o.home_spread is None else round(o.home_spread, 1),
                       None if o.total is None else round(o.total, 1),
                       o.home_ml, o.away_ml)
                old = None if last is None else (
                    last["provider"], last["details"],
                    None if last["home_spread"] is None else float(last["home_spread"]),
                    None if last["total"] is None else float(last["total"]),
                    last["home_ml"], last["away_ml"])
                if new != old:
                    cur.execute(
                        """
                        INSERT INTO odds_snapshots (game_id, game_state, provider, details,
                                                    home_spread, total, home_ml, away_ml, source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (game_id, g.state, *new, snapshot_source),
                    )
    conn.commit()
    return saved


def log_fetch(conn: psycopg.Connection, league: str, source: str, ok: bool, *,
              requested_week: int | None = None, games: int | None = None,
              season: int | None = None, week: int | None = None, season_type: int | None = None,
              error: str | None = None, requested_season_type: int | None = None) -> None:
    conn.execute(
        """INSERT INTO fetch_log (league, source, ok, requested_week, games, season, week, season_type, error,
                                  requested_season_type)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (league, source, ok, requested_week, games, season, week, season_type, error, requested_season_type),
    )
    conn.commit()


def last_fetch(conn: psycopg.Connection, league: str, requested_week: int | None = None,
               ok_only: bool = True, requested_season_type: int | None = None) -> dict | None:
    """Last fetch for this request. requested_week=None means the 'current week' request,
    which is cached separately from browsing a specific week; likewise per requested season type
    (preseason, regular season and postseason each have a week 1)."""
    sql = ("SELECT * FROM fetch_log WHERE league = %s AND requested_week IS NOT DISTINCT FROM %s"
           " AND requested_season_type IS NOT DISTINCT FROM %s")
    if ok_only:
        sql += " AND ok"
    sql += " ORDER BY fetched_at DESC, id DESC LIMIT 1"
    return conn.execute(sql, (league, requested_week, requested_season_type)).fetchone()


def save_calendar(conn: psycopg.Connection, league: str, season: int | None, stages: list[dict]) -> None:
    """ESPN's season calendar (espn.parse_calendar), one row per league."""
    if not stages:
        return                            # never overwrite a good calendar with an empty one
    conn.execute(
        """INSERT INTO league_calendar (league, season, stages) VALUES (%s, %s, %s)
           ON CONFLICT (league) DO UPDATE SET season = EXCLUDED.season, stages = EXCLUDED.stages,
                                              fetched_at = now()""",
        (league, season, Jsonb(stages)))


def get_calendar(conn: psycopg.Connection, league: str) -> list[dict]:
    row = conn.execute("SELECT stages FROM league_calendar WHERE league = %s", (league,)).fetchone()
    return row["stages"] if row else []


# The line shown on a card: newest snapshot for games not started; for live or final
# games, the last line captured before kickoff (that's the line Phase 2 grades against).
# If we never saw a pre-game line, fall back to the newest one and flag it (PRD fallback rule).
_GAMES_SQL = """
SELECT g.id, g.league, g.espn_id, g.start_time, g.state, g.status_detail, g.period, g.clock,
       g.home_score, g.away_score, g.venue, g.broadcast, g.season, g.week, g.season_type, g.completed,
       g.time_valid, g.source, g.fetched_at,
       g.home_conf, g.away_conf, g.home_rank, g.away_rank, g.neutral_site,
       h.espn_id AS home_espn_id, a.espn_id AS away_espn_id,
       h.abbr AS home_abbr, h.name AS home_name, h.short_name AS home_short, h.logo_url AS home_logo, h.color AS home_color,
       a.abbr AS away_abbr, a.name AS away_name, a.short_name AS away_short, a.logo_url AS away_logo, a.color AS away_color,
       o.provider AS odds_provider, o.details AS odds_details, o.home_spread, o.total, o.home_ml, o.away_ml,
       o.game_state AS odds_state
FROM games g
JOIN teams h ON h.id = g.home_team_id
JOIN teams a ON a.id = g.away_team_id
LEFT JOIN LATERAL (
    SELECT * FROM odds_snapshots s
    WHERE s.game_id = g.id
    ORDER BY (g.state = 'pre' OR s.game_state = 'pre') DESC, s.captured_at DESC, s.id DESC LIMIT 1
) o ON TRUE
"""


def games_for_week(conn: psycopg.Connection, league: str, season: int, week: int,
                   season_type: int | None) -> list[dict]:
    """One week of one season type: preseason, regular season and postseason all have a week 1."""
    return conn.execute(
        _GAMES_SQL + """ WHERE g.league = %s AND g.season = %s AND g.week = %s
                         AND g.season_type IS NOT DISTINCT FROM %s ORDER BY g.start_time""",
        (league, season, week, season_type),
    ).fetchall()


def game_by_id(conn: psycopg.Connection, game_id: int) -> dict | None:
    return conn.execute(_GAMES_SQL + " WHERE g.id = %s", (game_id,)).fetchone()


def game_by_espn_id(conn: psycopg.Connection, league: str, espn_id: str) -> dict | None:
    return conn.execute(_GAMES_SQL + " WHERE g.league = %s AND g.espn_id = %s", (league, espn_id)).fetchone()


# ---------- Phase 2: game summaries and bet results ----------

def get_summary(conn: psycopg.Connection, game_id: int) -> dict | None:
    return conn.execute(
        "SELECT game_state, payload, fetched_at FROM game_summaries WHERE game_id = %s", (game_id,)
    ).fetchone()


def get_summary_view(conn: psycopg.Connection, game_id: int) -> dict | None:
    """The summary blocks the game screens read, without play-by-play, news, videos or the player box
    score. A final's payload is ~600 KB (mostly `drives.previous`); this is ~50 KB, so a D page reads
    and decodes a tenth of it. Add a block here when a screen starts reading it."""
    return conn.execute(
        """SELECT game_state, fetched_at,
                  jsonb_build_object(
                      'header', payload->'header',
                      'boxscore', jsonb_build_object('teams', payload->'boxscore'->'teams'),
                      'leaders', payload->'leaders',
                      'injuries', payload->'injuries',
                      'pickcenter', payload->'pickcenter',
                      'gameInfo', jsonb_build_object('venue', payload->'gameInfo'->'venue'),
                      'drives', jsonb_build_object('current', payload->'drives'->'current')
                  ) AS payload
           FROM game_summaries WHERE game_id = %s""", (game_id,)
    ).fetchone()


def get_summary_lines(conn: psycopg.Connection, game_id: int) -> dict | None:
    """Just the summary blocks grading reads (header, pickcenter). A final's payload is ~600 KB,
    and every scoreboard refresh re-checks each final's grade, so don't pull the whole thing."""
    return conn.execute(
        """SELECT game_state, fetched_at,
                  jsonb_build_object('header', payload->'header', 'pickcenter', payload->'pickcenter') AS payload
           FROM game_summaries WHERE game_id = %s""", (game_id,)
    ).fetchone()


def save_summary(conn: psycopg.Connection, game_id: int, game_state: str, payload: dict) -> None:
    conn.execute(
        """INSERT INTO game_summaries (game_id, game_state, payload, fetched_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (game_id) DO UPDATE SET
               game_state = EXCLUDED.game_state, payload = EXCLUDED.payload, fetched_at = now()""",
        (game_id, game_state, Jsonb(payload)),
    )


def update_game_status(conn: psycopg.Connection, game_id: int, st: dict) -> None:
    """Move a game's state and score forward from its summary, so opening a game page is a pull
    too (C1 -> C2 -> D without going back to the scoreboard).

    Never backwards: ESPN's summary can lag its scoreboard by a refresh, so a summary saying 'in'
    must not undo a completed final, nor 'pre' undo a live game. (A postponed game is 'post' but not
    completed, and may legitimately go back to 'pre' when rescheduled.) A missing score in the
    summary never blanks a stored one."""
    conn.execute(
        """UPDATE games SET state = %(state)s, status_detail = %(detail)s, period = %(period)s,
                            clock = %(clock)s, completed = %(completed)s,
                            home_score = COALESCE(%(hs)s, home_score),
                            away_score = COALESCE(%(as)s, away_score), fetched_at = now()
           WHERE id = %(id)s
             AND NOT (completed IS TRUE AND %(state)s <> 'post')
             AND NOT (state = 'in' AND %(state)s = 'pre')""",
        {"state": st["state"], "detail": st.get("short_detail") or st.get("detail"), "period": st.get("period"),
         "clock": st.get("clock"), "completed": bool(st.get("completed")), "hs": st.get("home_score"),
         "as": st.get("away_score"), "id": game_id},
    )


def snapshots_for_game(conn: psycopg.Connection, game_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM odds_snapshots WHERE game_id = %s ORDER BY captured_at, id", (game_id,)
    ).fetchall()


def bet_results_for_game(conn: psycopg.Connection, game_id: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM bet_results WHERE game_id = %s ORDER BY market", (game_id,)
    ).fetchall()


def upsert_bet_result(conn: psycopg.Connection, game_id: int, market: str, line, home_score: int,
                      away_score: int, outcome: str, margin) -> None:
    """One row per game per market: insert, or overwrite on regrade. Never a second row."""
    conn.execute(
        """INSERT INTO bet_results (game_id, market, line_source, snapshot_id, provider, home_spread, total,
                                    home_ml, away_ml, home_score, away_score, outcome, margin, graded_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (game_id, market) DO UPDATE SET
               line_source = EXCLUDED.line_source, snapshot_id = EXCLUDED.snapshot_id,
               provider = EXCLUDED.provider, home_spread = EXCLUDED.home_spread, total = EXCLUDED.total,
               home_ml = EXCLUDED.home_ml, away_ml = EXCLUDED.away_ml, home_score = EXCLUDED.home_score,
               away_score = EXCLUDED.away_score, outcome = EXCLUDED.outcome, margin = EXCLUDED.margin,
               graded_at = now()""",
        (game_id, market, line.source, line.snapshot_id, line.provider, line.home_spread, line.total,
         line.home_ml, line.away_ml, home_score, away_score, outcome, margin),
    )


def delete_bet_results(conn: psycopg.Connection, game_id: int, markets: list[str]) -> None:
    if markets:
        conn.execute("DELETE FROM bet_results WHERE game_id = %s AND market = ANY(%s)", (game_id, markets))


def final_game_ids(conn: psycopg.Connection, league: str, season: int, week: int,
                   season_type: int | None) -> list[int]:
    """Every game of the week that has ended ('post'), completed or not; grading skips the
    canceled and postponed ones and says so."""
    return [r["id"] for r in conn.execute(
        """SELECT id FROM games WHERE league = %s AND season = %s AND week = %s
             AND season_type IS NOT DISTINCT FROM %s AND state = 'post' ORDER BY start_time, id""",
        (league, season, week, season_type)).fetchall()]


def get_team_season(conn: psycopg.Connection, league: str, team_espn_id: str, season: int,
                    season_type: int) -> dict | None:
    return conn.execute(
        """SELECT data, fetched_at FROM team_season_stats
           WHERE league = %s AND team_espn_id = %s AND season = %s AND season_type = %s""",
        (league, team_espn_id, season, season_type)).fetchone()


def save_team_season(conn: psycopg.Connection, league: str, team_espn_id: str, season: int,
                     season_type: int, data: dict) -> None:
    conn.execute(
        """INSERT INTO team_season_stats (league, team_espn_id, season, season_type, data, fetched_at)
           VALUES (%s, %s, %s, %s, %s, now())
           ON CONFLICT (league, team_espn_id, season, season_type) DO UPDATE SET
               data = EXCLUDED.data, fetched_at = now()""",
        (league, team_espn_id, season, season_type, Jsonb(data)))


def completed_game_stats(conn: psycopg.Connection, league: str, season: int, season_type: int):
    """(games of the season we've seen finish, kickoff of the latest one). A new final means league ranks
    may have moved; a recent one means ESPN may still be recomputing them."""
    r = conn.execute(
        """SELECT count(*) AS n, max(start_time) AS latest FROM games
           WHERE league = %s AND season = %s AND season_type = %s AND completed""",
        (league, season, season_type)).fetchone()
    return r["n"], r["latest"]


# ---------- Phase 3: news ----------

def team_nicknames(conn: psycopg.Connection, league: str) -> list[str]:
    """Short team names ("Bills", "Ohio State") the headlines' outlet filter reads a story for."""
    rows = conn.execute("""SELECT DISTINCT short_name FROM teams WHERE league = %s AND length(short_name) >= 4""",
                        (league,)).fetchall()
    return sorted(r["short_name"] for r in rows)


def insert_news(conn: psycopg.Connection, league: str, items: list[dict]) -> int:
    """Store news items not seen before (dedupe on ESPN's article id). Returns how many were new."""
    added = 0
    for it in items:
        cur = conn.execute(
            """INSERT INTO news_items (league, espn_id, headline, description, url, published_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (league, espn_id) DO NOTHING""",
            (league, it["espn_id"], it["headline"], it.get("description"), it.get("url"), it.get("published_at")))
        added += cur.rowcount
    conn.commit()
    return added
