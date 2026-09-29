"""Temporal activities: the real work (ESPN calls, database writes), wrapping Phase 1 and 2 code.

Plain synchronous functions; the worker runs them in a thread pool. Every one is safe to run twice:
games and results are upserts, odds snapshots are added only when the line changed, news dedupes on
ESPN's article id. Temporal retries a failed activity (see the policies in workflows.py), so an
activity raises on any ESPN or database failure instead of swallowing it.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .. import db, espn, games, summary
from .models import GameRef, GameState

log = logging.getLogger(__name__)

ESPN_BASE = os.environ.get("ESPN_BASE", espn.BASE)   # point at a bad host to test retries (step 3.7)
PACIFIC = ZoneInfo("America/Los_Angeles")
PREVIEW_HOUR = 8                                      # game-morning preview, Pacific time


def preview_iso(start: datetime) -> str:
    """8:00 AM Pacific on the game's Pacific-time date, as a UTC ISO string."""
    day = start.astimezone(PACIFIC).date()
    return datetime.combine(day, time(PREVIEW_HOUR), PACIFIC).astimezone(timezone.utc).isoformat()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(payload: dict, league: str) -> list[espn.Game]:
    parsed, errors = espn.parse_scoreboard(payload, league)
    for e in errors:
        log.warning("scoreboard parse error %s", e)
    if payload.get("events") and not parsed:
        # A format change, not an outage: retrying won't help, but fail so it shows in the UI.
        raise ApplicationError(f"none of ESPN's {len(payload['events'])} games could be read: {errors[:3]}")
    return parsed


def _save(league: str, payload: dict) -> tuple[list[espn.Game], list[tuple[int, str]]]:
    parsed = _parse(payload, league)
    with db.connect() as conn:
        saved = db.save_games(conn, parsed, snapshot_source="workflow")
    return parsed, saved


@activity.defn
def sync_schedule(league: str) -> list[GameRef]:
    """Fetch ESPN's current week and the next one, save them, and return every game not yet final.
    Postponed games are included (a new date may come); canceled ones and finals are not."""
    cur = espn.fetch_scoreboard(league, base_url=ESPN_BASE)
    boards = [cur]
    season_type = espn._int(espn._get(cur, "season", "type"))
    week = espn._int(espn._get(cur, "week", "number"))
    if season_type and week:
        nxt = espn.fetch_scoreboard(league, week=week + 1, season_type=season_type, base_url=ESPN_BASE)
        if not nxt.get("events") and season_type < 3:
            # Last week of preseason or regular season: next is week 1 of the next season type.
            nxt = espn.fetch_scoreboard(league, week=1, season_type=season_type + 1, base_url=ESPN_BASE)
        boards.append(nxt)
    refs: dict[str, GameRef] = {}
    for board in boards:
        parsed, saved = _save(league, board)
        for g, (_gid, stored_state) in zip(parsed, saved):
            # The stored state, not ESPN's: a lagging response never moves a game backwards.
            if stored_state != "post" or g.postponed:
                refs[g.espn_id] = GameRef(league, g.espn_id, _iso(g.start_time), preview_iso(g.start_time))
    activity.logger.info("%s: %d games not yet final", league, len(refs))
    return sorted(refs.values(), key=lambda r: (r.start_iso, r.espn_id))


def _stored(league: str, espn_id: str) -> dict:
    with db.connect() as conn:
        row = db.game_by_espn_id(conn, league, espn_id)
    if row is None:
        raise ApplicationError(f"{league} game {espn_id} is not stored", non_retryable=True)
    return row


# On a Sunday most games poll at the same moments (every 2.5 minutes from a shared kickoff) and each poll
# needs the same week's scoreboard. Within BOARD_CACHE_SECONDS the first poll fetches and saves it; the
# others reuse that result instead of calling ESPN again. Only successful fetches are kept.
BOARD_CACHE_SECONDS = 60.0
_boards: dict[tuple, tuple[float, list[espn.Game]]] = {}
_board_locks: dict[tuple, threading.Lock] = {}
_boards_lock = threading.Lock()


def _week_board(league: str, week: int | None, season_type: int | None) -> list[espn.Game]:
    key = (league, week, season_type)
    with _boards_lock:
        lock = _board_locks.setdefault(key, threading.Lock())
    with lock:   # concurrent polls for the same week wait for one fetch instead of each calling ESPN
        hit = _boards.get(key)
        if hit and _time.monotonic() - hit[0] < BOARD_CACHE_SECONDS:
            return hit[1]
        payload = espn.fetch_scoreboard(league, week=week, season_type=season_type, base_url=ESPN_BASE)
        parsed, _ = _save(league, payload)
        _boards[key] = (_time.monotonic(), parsed)
        return parsed


def _board_for(league: str, row: dict) -> espn.Game | None:
    """The game's entry in its scoreboard week (fetched and saved, or from the last minute's fetch). None if
    ESPN no longer lists it in that week, e.g. a postponed game moved to another week."""
    parsed = _week_board(league, row["week"], row["season_type"])
    return next((g for g in parsed if g.espn_id == row["espn_id"]), None)


@activity.defn
def save_line(league: str, espn_id: str) -> None:
    """Save the game's scoreboard week (source 'workflow'); an odds snapshot is added only if the line changed."""
    game = _board_for(league, _stored(league, espn_id))
    if game is None:
        activity.logger.warning("%s %s not in its stored week's scoreboard; line not saved", league, espn_id)
    elif game.odds is None:
        activity.logger.info("%s %s: ESPN has no line yet", league, espn_id)


@activity.defn
def fetch_game_state(league: str, espn_id: str) -> GameState:
    """State, score and kickoff for one game, from its scoreboard week (saved on the way). Falls back to
    the game summary when the week no longer lists the game."""
    row = _stored(league, espn_id)
    game = _board_for(league, row)
    if game is not None:
        with db.connect() as conn:
            row = db.game_by_espn_id(conn, league, espn_id)   # stored values: never backwards
        return GameState(row["state"], row["home_score"], row["away_score"], _iso(game.start_time),
                         game.postponed, bool(row["completed"]) and row["state"] == "post")
    payload = espn.fetch_summary(league, espn_id, base_url=ESPN_BASE)
    st = summary.status(payload)
    if not st.get("state"):
        raise ApplicationError(f"summary for {league} {espn_id} has no state")
    date = espn._get(payload, "header", "competitions", 0, "date")
    start = espn._parse_time(date) if date else row["start_time"]
    return GameState(st["state"], st.get("home_score"), st.get("away_score"), _iso(start),
                     bool(st.get("postponed")), bool(st.get("completed")) and st["state"] == "post")


@activity.defn
def fetch_summary(league: str, espn_id: str) -> None:
    """Fetch and store the ESPN game summary (Phase 2's refresh_summary); moves the game's state and score
    forward, never back. Raises on failure so Temporal retries."""
    row = _stored(league, espn_id)
    with db.connect() as conn:
        ok, err = games.refresh_summary(conn, row, base_url=ESPN_BASE)
    if not ok:
        raise RuntimeError(f"summary fetch failed: {err}")


@activity.defn
def grade_game(league: str, espn_id: str) -> list[int]:
    """Phase 2 grading plus upsert (writes only when something changed). Returns [home, away] as graded,
    or [] when the game isn't a completed final with a score."""
    row = _stored(league, espn_id)
    with db.connect() as conn:
        graded = games.grade_game(conn, row["id"])
        g = conn.execute("SELECT home_score, away_score FROM games WHERE id = %s", (row["id"],)).fetchone()
    if graded is None:
        return []
    return [g["home_score"], g["away_score"]]


@activity.defn
def fetch_news(league: str) -> int:
    """Fetch ESPN news and store new items. Returns how many were added."""
    items = espn.parse_news(espn.fetch_news(league, base_url=ESPN_BASE))
    with db.connect() as conn:
        added = db.insert_news(conn, league, items)
    activity.logger.info("%s news: %d fetched, %d new", league, len(items), added)
    return added


@activity.defn
def generate_preview(league: str, espn_id: str) -> None:
    """Game-morning preview. Phase 4 fills this in; Phase 3 only logs."""
    activity.logger.info("preview placeholder for %s %s (Phase 4)", league, espn_id)


ALL = [sync_schedule, save_line, fetch_game_state, fetch_summary, grade_game, fetch_news, generate_preview]
