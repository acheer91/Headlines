"""FastAPI app: NFL and NCAAF scoreboards (Screen B) and game pages (C1 pre-game, C2 live, D post-game).

Pull model: a scoreboard request refreshes from ESPN only if our last good fetch is older
than CACHE_SECONDS; otherwise it serves Postgres. If ESPN fails, it serves the last stored
data with stale=true, so the scoreboard never goes blank because a feed hiccupped.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, espn, favorites, games, migrate, ncaaf
from .ai import client as ai_client
from .ai import jobs as ai_jobs
from .ai import quota as ai_quota
from .ai import scope as ai_scope
from .ai import store as ai_store

CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "30"))
ENABLED_LEAGUES = [x.strip() for x in os.environ.get("ENABLED_LEAGUES", "nfl").split(",") if x.strip()]
ESPN_BASE = os.environ.get("ESPN_BASE", espn.BASE)  # override to test degraded mode
FAVORITES_FILE = Path(os.environ.get("FAVORITES_FILE", Path(__file__).resolve().parents[2] / "config" / "favorites.json"))
WEB_DIST = Path(os.environ.get("WEB_DIST", Path(__file__).resolve().parents[2] / "web" / "dist"))

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The only place migrations run: brings a new or existing database up to the current schema.
    try:
        applied = migrate.migrate(db.DATABASE_URL)
        if applied:
            log.warning("applied migrations: %s", ", ".join(applied))
    except Exception as exc:  # noqa: BLE001 — /api/health reports a down database; don't crash-loop
        log.error("migrations not applied: %r", exc)
    if os.environ.get("AI_QUOTA") == "db":
        ai_quota.install()        # share the free-tier quota with the worker through Postgres
    yield
    db.close_pools()


app = FastAPI(title="Scores API", version="0.2.0", lifespan=lifespan)
# Scoreboard and game JSON shrink ~5x, the app bundle ~3x: fewer bytes over the phone's connection.
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _favorites() -> dict[str, list[str]]:
    return favorites.load(FAVORITES_FILE)


def _age_seconds(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _refresh(conn, league: str, week: int | None, season_type: int | None = None) -> tuple[bool, str | None]:
    """Fetch from ESPN and store. Returns (ok, error)."""
    try:
        payload = espn.fetch_scoreboard(league, week=week, season_type=season_type, base_url=ESPN_BASE)
        games_, errors = espn.parse_scoreboard(payload, league)
        raw = len(payload.get("events") or [])
        if raw and not games_:
            # ESPN answered but we couldn't read any game (a format change): that's a failed refresh,
            # so the board shows the stale banner instead of old scores under a fresh "Updated" time.
            raise espn.ESPNError(f"ESPN answered but none of its {raw} games could be read: {'; '.join(errors[:3])}")
        if not raw:
            # No events at all. Fine for a week with no games; but for a week we already hold games for,
            # it means ESPN moved or dropped the list, and "fresh" would freeze those cards unnoticed.
            p_season = espn._int(espn._get(payload, "season", "year"))
            p_week = espn._int(espn._get(payload, "week", "number"))
            p_type = espn._int(espn._get(payload, "season", "type"))
            if p_season and p_week and db.games_for_week(conn, league, p_season, p_week, p_type):
                raise espn.ESPNError(f"ESPN listed no games for {p_season} week {p_week}, which has games")
        saved = db.save_games(conn, games_)
        season = games_[0].season if games_ else espn._int(espn._get(payload, "season", "year"))
        wk = games_[0].week if games_ else espn._int(espn._get(payload, "week", "number"))
        stype = games_[0].season_type if games_ else espn._int(espn._get(payload, "season", "type"))
        # Some games unreadable: the rest refreshed, and the board warns that some cards may be old.
        err = f"{len(errors)} of {raw} games unreadable: " + "; ".join(errors[:5]) if errors else None
        db.save_calendar(conn, league, season, espn.parse_calendar(payload))
        db.log_fetch(conn, league, "espn", True, requested_week=week, games=len(games_),
                     season=season, week=wk, season_type=stype, error=err,
                     requested_season_type=season_type)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        db.log_fetch(conn, league, "espn", False, requested_week=week, error=repr(exc)[:500],
                     requested_season_type=season_type)
        return False, repr(exc)
    # Regrade finals whose score changed (stat corrections). Grading problems never fail the pull.
    for game_id, state in saved:
        if state == "post":
            try:
                games.grade_game(conn, game_id)
            except Exception:  # noqa: BLE001
                conn.rollback()
                log.exception("grading game %s failed", game_id)
    return True, None


_card = games.card   # moved to games.py (Phase 4: the worker builds game pages too)


_STATE_ORDER = {"in": 0, "pre": 1, "post": 2}


def _sort_key(card: dict):
    # Favorites pinned first; then live, upcoming, final; then kickoff time.
    return (0 if card["favorite"] else 1, _STATE_ORDER.get(card["state"], 3), card["start_time"])


@app.get("/api/health")
def health():
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "db": "up", "leagues": ENABLED_LEAGUES}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"db down: {exc!r}")


@app.get("/api/scoreboard/{league}")
def scoreboard(league: str, week: int | None = Query(None, ge=1, le=999),
               season_type: int | None = Query(None, ge=1, le=3), force: bool = False):
    # week goes to 999: ESPN numbers the College Football Playoff week 999 (season type 3).
    if league not in ENABLED_LEAGUES:
        raise HTTPException(404, f"league '{league}' not enabled")
    if league not in ("nfl", "ncaaf"):
        # Week-based lookup only fits football. Phase 5 adds a date-window query for NBA/soccer.
        raise HTTPException(501, "only week-based leagues are wired in Phase 1")

    with db.connect() as conn:
        last = db.last_fetch(conn, league, week, requested_season_type=season_type)
        age = _age_seconds(last["fetched_at"]) if last else None
        stale, error = False, None

        if force or age is None or age >= CACHE_SECONDS:
            ok, error = _refresh(conn, league, week, season_type)
            if not ok:
                stale = True
            last = db.last_fetch(conn, league, week, requested_season_type=season_type)
            if last is None:
                raise HTTPException(502, f"ESPN unreachable and nothing stored yet: {error}")

        season, wk = last["season"], last["week"]
        rows = db.games_for_week(conn, league, season, wk, last.get("season_type")) if season and wk else []
        favs = set(_favorites().get(league, []))
        if league == "ncaaf":
            # Every FBS game is stored; the board shows the PRD's filtered set (ncaaf.py).
            rows = [r for r in rows if ncaaf.is_featured_row(r, favs)]
        cards = sorted((_card(r, favs) for r in rows), key=_sort_key)
        # ESPN's season stages (weeks, then Bowls / Playoffs), for Prev and Next across season types.
        calendar = [{"season_type": s["season_type"], "week": s["week"], "label": s["label"]}
                    for s in db.get_calendar(conn, league)]

    return {
        "league": league,
        "season": season,
        "week": wk,
        "season_type": last.get("season_type"),
        "calendar": calendar,
        "updated_at": last["fetched_at"].isoformat(),
        "stale": stale,
        "error": error if stale else None,
        # The refresh worked but ESPN sent some games we couldn't read; their cards may be out of date.
        "warning": "Some games couldn't be updated from ESPN; their scores may be out of date."
        if not stale and last.get("error") else None,
        "games": cards,
    }


@app.get("/api/games/{game_id}")
def game(game_id: int):
    """One game's screen: C1 (pre), C2 (live) or D (final), picked from the game's state.

    Opening the page is a pull: the ESPN summary is refreshed when the cache rule says so
    (see games.needs_refresh), and the game's state and score move forward from it. If ESPN
    fails, the stored summary is served with stale=true. Finals are graded here."""
    with db.connect() as conn:
        out = games.page(conn, game_id, base_url=ESPN_BASE, favorites=_favorites())
    if out is None:
        raise HTTPException(404, "game not found")
    return out


# ---------- Phase 4: AI text ----------
# Written ahead by the worker; a text nobody wrote ahead is written here, on open, within these limits. The api
# never waits for free-tier quota (client.no_wait): with no room it answers at once and the app shows fallback text.
AI_WAIT = {"preview": 20.0, "recap": 10.0}
_ai_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ai-open")


def _ai_out(kind: str | None, row: dict | None) -> dict:
    """The response for a stored row. While a preview is being refreshed its last good text is served as ready
    (the 8 AM refresh must not blank a good midweek preview; audit, 2026-09-29)."""
    if not row:
        return {"kind": kind, "status": "missing", "body": None, "sources": None, "updated_at": None,
                "written_at": None}
    shown = ai_store.showable(row)
    status = row["status"] if shown is None or row["status"] != "writing" else "ready"
    return {"kind": kind, "status": status, "body": row["body"] if shown else None,
            "sources": row["sources"] if shown else None, "updated_at": row["updated_at"].isoformat(),
            # When the shown text was written: a failed refresh keeps the last good preview (CTO, 2026-10-01).
            "written_at": row["written_at"].isoformat() if shown and row["written_at"] else None}


def _write_on_open(kind: str, game_id: int) -> dict:
    with ai_client.no_wait():
        return ai_jobs.write_for_game(kind, game_id, "open", base_url=ESPN_BASE)


@app.get("/api/games/{game_id}/ai")
def game_ai(game_id: int):
    """The AI text that fits the game now (handoff 2.6): preview (pre), recap (played final); none live (the
    one-liner is the box-score template) or in a league without AI text (AI_LEAGUES).
    status: ready | no_sources ("No fresh previews") | failed or writing (the app shows fallback text) | missing.
    Current text comes back at once, and so does a text that failed for the same inputs in the last 30 minutes
    (a pull shouldn't pay for the same failure again) or that was rejected twice for this game day or score
    (ai_store.REJECTION_CAP). Otherwise this request writes it (or waits for whoever is
    writing it) for up to AI_WAIT seconds; a write that runs longer finishes in the background."""
    with db.connect() as conn:
        game_row = db.game_by_id(conn, game_id)
        if not game_row:
            raise HTTPException(404, "game not found")
        kind = ai_jobs.kind_for(game_row)
        if kind is None or not ai_scope.ai_league(game_row["league"]):
            return _ai_out(None, None) | {"status": "none"}
        stored = ai_store.get(conn, game_id, kind)
    basis = ai_jobs.row_basis(kind, game_row)
    # The api doesn't recompute a preview's fingerprint (that fetches articles): the 8 AM refresh does.
    if (ai_store.current(stored, basis) or ai_store.failed_recently(stored, basis)
            or ai_store.rejected_out(stored, basis)):       # rejected twice for this game day or score: fallback
        return _ai_out(kind, stored)
    if ai_store.being_written(stored) and ai_store.showable(stored):
        return _ai_out(kind, stored)            # a preview mid-refresh: its last good text, at once
    job = _ai_pool.submit(_write_on_open, kind, game_id)
    deadline = time.monotonic() + AI_WAIT[kind]
    try:
        job.result(timeout=AI_WAIT[kind])
    except FuturesTimeout:
        pass
    except Exception:  # noqa: BLE001 — the page still shows fallback text
        log.exception("ai %s for game %s failed", kind, game_id)
    # Busy (another writer holds the row) or still writing: re-read once a second until the limit. Once this
    # request's own job has finished, a missing or settled row is the answer: no point waiting out the limit.
    while True:
        with db.connect() as conn:
            row = ai_store.get(conn, game_id, kind)
        settled = row is not None and (row["status"] != "writing" or ai_store.showable(row) is not None)
        if settled or (job.done() and row is None) or time.monotonic() >= deadline:
            return _ai_out(kind, row)
        time.sleep(1)


@app.get("/api/headlines")
def headlines():
    """Screen A: the newest headline set, or null before the first one is written."""
    with db.connect() as conn:
        row = ai_store.latest_headlines(conn)
    if not row:
        return None
    return {"items": row["body"]["items"], "updated_at": row["updated_at"].isoformat()}


# Serve the built web app from the same origin (one server, one HTTPS name, no CORS).
if WEB_DIST.is_dir():
    class _ImmutableAssets(StaticFiles):
        """Vite puts a content hash in every /assets file name, so a file never changes: let the
        phone cache it for a year instead of re-checking it."""

        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            if resp.status_code == 200:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp

    app.mount("/assets", _ImmutableAssets(directory=WEB_DIST / "assets"), name="assets")

    _DIST_ROOT = WEB_DIST.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "not found")
        target = (_DIST_ROOT / path).resolve()
        # Only files inside web/dist; "..%2F" paths must not reach the rest of the disk.
        if path and target.is_relative_to(_DIST_ROOT) and target.is_file():
            # The service worker and manifest must be re-checked on every load, or updates stall.
            headers = {"Cache-Control": "no-cache"} if target.suffix in (".js", ".webmanifest") else None
            return FileResponse(target, headers=headers)
        return FileResponse(_DIST_ROOT / "index.html", headers={"Cache-Control": "no-cache"})
