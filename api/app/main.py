"""FastAPI app: NFL scoreboard (Screen B) and game pages (C1 pre-game, C2 live, D post-game).

Pull model: a scoreboard request refreshes from ESPN only if our last good fetch is older
than CACHE_SECONDS; otherwise it serves Postgres. If ESPN fails, it serves the last stored
data with stale=true, so the scoreboard never goes blank because a feed hiccupped.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, espn, games, migrate

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
    yield
    db.close_pools()


app = FastAPI(title="Scores API", version="0.2.0", lifespan=lifespan)
# Scoreboard and game JSON shrink ~5x, the app bundle ~3x: fewer bytes over the phone's connection.
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _favorites() -> dict[str, list[str]]:
    try:
        data = json.loads(FAVORITES_FILE.read_text())
        return {k: [a.upper() for a in v] for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError):
        return {}


def _age_seconds(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _refresh(conn, league: str, week: int | None) -> tuple[bool, str | None]:
    """Fetch from ESPN and store. Returns (ok, error)."""
    try:
        payload = espn.fetch_scoreboard(league, week=week, base_url=ESPN_BASE)
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
        db.log_fetch(conn, league, "espn", True, requested_week=week, games=len(games_),
                     season=season, week=wk, season_type=stype, error=err)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        db.log_fetch(conn, league, "espn", False, requested_week=week, error=repr(exc)[:500])
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


def _card(row: dict, favorites: set[str]) -> dict:
    has_line = any(row[k] is not None for k in ("home_spread", "total", "home_ml", "away_ml"))
    return {
        "id": row["id"],
        "league": row["league"],
        "state": row["state"],  # pre -> C1, in -> C2, post -> D
        "start_time": row["start_time"].isoformat(),
        # False for a flexed game with no kickoff set: start_time is ESPN's placeholder, show the date only.
        "time_valid": row.get("time_valid", True) is not False,
        # False for a canceled or postponed game (state 'post' but never played).
        "completed": row.get("completed"),
        "status_detail": row["status_detail"],
        "period": row["period"],
        "clock": row["clock"],
        "broadcast": row["broadcast"],
        "venue": row["venue"],
        "favorite": row["home_abbr"] in favorites or row["away_abbr"] in favorites,
        "home": {"abbr": row["home_abbr"], "name": row["home_name"], "short": row["home_short"],
                 "logo": row["home_logo"], "color": row["home_color"], "score": row["home_score"]},
        "away": {"abbr": row["away_abbr"], "name": row["away_name"], "short": row["away_short"],
                 "logo": row["away_logo"], "color": row["away_color"], "score": row["away_score"]},
        "line": None if not has_line else {
            "provider": row["odds_provider"],
            "details": row["odds_details"],
            "home_spread": None if row["home_spread"] is None else float(row["home_spread"]),
            "total": None if row["total"] is None else float(row["total"]),
            "home_ml": row["home_ml"],
            "away_ml": row["away_ml"],
            # True when no pre-game line was ever captured and this is a later one.
            "captured_after_kickoff": row["state"] != "pre" and row["odds_state"] != "pre",
        },
    }


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
def scoreboard(league: str, week: int | None = Query(None, ge=1, le=25), force: bool = False):
    if league not in ENABLED_LEAGUES:
        raise HTTPException(404, f"league '{league}' not enabled")
    if league not in ("nfl", "ncaaf"):
        # Week-based lookup only fits football. Phase 5 adds a date-window query for NBA/soccer.
        raise HTTPException(501, "only week-based leagues are wired in Phase 1")

    with db.connect() as conn:
        last = db.last_fetch(conn, league, week)
        age = _age_seconds(last["fetched_at"]) if last else None
        stale, error = False, None

        if force or age is None or age >= CACHE_SECONDS:
            ok, error = _refresh(conn, league, week)
            if not ok:
                stale = True
            last = db.last_fetch(conn, league, week)
            if last is None:
                raise HTTPException(502, f"ESPN unreachable and nothing stored yet: {error}")

        season, wk = last["season"], last["week"]
        rows = db.games_for_week(conn, league, season, wk, last.get("season_type")) if season and wk else []
        favs = set(_favorites().get(league, []))
        cards = sorted((_card(r, favs) for r in rows), key=_sort_key)

    return {
        "league": league,
        "season": season,
        "week": wk,
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
        row = db.game_by_id(conn, game_id)
        if not row:
            raise HTTPException(404, "game not found")
        # C1's team season stats (ranks, INTs leader) are fetched in parallel with the summary.
        team_jobs = games.start_team_season(conn, row, base_url=ESPN_BASE) if row["state"] == "pre" else []
        stored = db.get_summary_view(conn, game_id)
        stale, error = False, None
        if games.needs_refresh(row, stored):
            ok, error = games.refresh_summary(conn, row, base_url=ESPN_BASE)
            stale = not ok
            stored = db.get_summary_view(conn, game_id)
            row = db.game_by_id(conn, game_id)
        team_season = games.finish_team_season(conn, row, team_jobs) if team_jobs else None
        if row["state"] == "post":
            try:
                games.grade_game(conn, game_id)
            except Exception:  # noqa: BLE001 — show the page even if grading hit a bug
                conn.rollback()
                log.exception("grading game %s failed", game_id)
        card = _card(row, set(_favorites().get(row["league"], [])))
        return games.detail(conn, card, stored, stale=stale, error=error,
                            team_season=team_season if row["state"] == "pre" else None)


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
