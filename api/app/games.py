"""Game detail (screens C1, C2, D): summary cache, state moves on pull, and bet grading.

Cache rules (handoff step 2.11): at most one ESPN summary call per game per 30 seconds; a final
game's summary is fetched once and then served from Postgres. Upcoming games refresh every
SUMMARY_PRE_SECONDS so the injury report doesn't go stale; from kickoff time on, the live rule applies.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from . import db, espn, grading, summary

log = logging.getLogger(__name__)

SUMMARY_LIVE_SECONDS = int(os.environ.get("SUMMARY_CACHE_SECONDS", "30"))
SUMMARY_PRE_SECONDS = int(os.environ.get("SUMMARY_PRE_SECONDS", "600"))
# Ranks depend on all 32 teams, so besides this age limit the cache is refetched whenever another game
# has finished since it was stored, and more often (TEAM_SEASON_RECENT_SECONDS) for a few hours after a
# final, since ESPN recomputes season stats and ranks some minutes after the game (see start_team_season).
TEAM_SEASON_SECONDS = int(os.environ.get("TEAM_SEASON_SECONDS", "3600"))
TEAM_SEASON_RECENT_SECONDS = int(os.environ.get("TEAM_SEASON_RECENT_SECONDS", "600"))
RECENT_FINAL_HOURS = 6

SCREENS = {"pre": "C1", "in": "C2", "post": "D"}
MARKET_LABELS = {"moneyline": "Moneyline", "spread": "Spread", "total": "Over/under"}
SOURCE_LABELS = {"pre_game": "line saved before kickoff", "espn_close": "ESPN closing line", "in_game": "in-game line"}
_ORDER = {"pre": 0, "in": 1, "post": 2}
PLACEHOLDERS = {
    "C1": ["Things you should know", "Edges", "Writers' picks"],
    "C2": [],
    "D": ["Recap", "Team summaries"],
}


# ---------- summary cache ----------

def needs_refresh(game: dict, stored: dict | None, now: datetime | None = None) -> bool:
    if stored is None:
        return True
    # Finals are fetched once and kept, but only a completed one: a postponed game is 'post' too, and
    # once it's rescheduled and played its stored summary must be replaced.
    if stored["game_state"] == "post" and game.get("completed") is not False:
        return False
    now = now or datetime.now(timezone.utc)
    age = (now - stored["fetched_at"]).total_seconds()
    if game["state"] == "post" and stored["game_state"] != "post":
        # Went final since we stored it: fetch the final, but still at most once per 30 s while ESPN's
        # summary lags its scoreboard (the page meanwhile says the box score is catching up).
        return age >= SUMMARY_LIVE_SECONDS
    # A flexed game's start_time is a placeholder until ESPN sets the kickoff; don't treat it as started.
    kicked_off = game.get("time_valid", True) and game["start_time"] <= now
    started = game["state"] == "in" or stored["game_state"] == "in" or kicked_off
    return age >= (SUMMARY_LIVE_SECONDS if started else SUMMARY_PRE_SECONDS)


def refresh_summary(conn, game: dict, base_url: str = espn.BASE) -> tuple[bool, str | None]:
    """Fetch, store, and move the game's state and score forward. Returns (ok, error)."""
    try:
        payload = espn.fetch_summary(game["league"], game["espn_id"], base_url=base_url)
        st = summary.status(payload)
        if st.get("event_id") and st["event_id"] != str(game["espn_id"]):
            raise espn.ESPNError(f"summary for {st['event_id']} returned for game {game['espn_id']}")
        state = st.get("state") or game["state"]
        db.save_summary(conn, game["id"], state, payload)
        if st.get("state"):
            db.update_game_status(conn, game["id"], st)
        conn.commit()
        return True, None
    except Exception as exc:  # noqa: BLE001 — ESPN down means stored data + banner, never an error page
        conn.rollback()
        log.warning("summary refresh failed for game %s: %r", game["id"], exc)
        return False, repr(exc)[:500]


# ---------- grading ----------

def grading_lines(conn, game_id: int, stored: dict | None) -> dict[str, grading.Line]:
    close = summary.closing_line(stored["payload"]) if stored else None
    return grading.choose_lines(db.snapshots_for_game(conn, game_id), close)


def grade_game(conn, game_id: int) -> tuple[dict[str, grading.Result], dict[str, grading.Line]] | None:
    """Grade a final game and store one row per market. Writes only when something changed
    (first grade, a stat correction, or a better line), so repeat pulls are no-ops.
    Returns None when the game isn't a completed final. A canceled or postponed game ('post' but not
    completed, usually 0-0) is never graded, and any results it had are removed."""
    g = conn.execute("SELECT state, completed, home_score, away_score FROM games WHERE id = %s",
                     (game_id,)).fetchone()
    if not g or g["state"] != "post":
        return None
    if not g["completed"]:
        db.delete_bet_results(conn, game_id, list(grading.MARKETS))
        conn.commit()
        return None
    if g["home_score"] is None or g["away_score"] is None:
        return None
    lines = grading_lines(conn, game_id, db.get_summary_lines(conn, game_id))
    results = grading.grade(g["home_score"], g["away_score"], lines)
    existing = {r["market"]: r for r in db.bet_results_for_game(conn, game_id)}

    def dec(v):
        return None if v is None else Decimal(v)

    for market, r in results.items():
        line = lines[market]
        e = existing.get(market)
        new = (line.source, dec(line.home_spread), dec(line.total), line.home_ml, line.away_ml,
               g["home_score"], g["away_score"], r.outcome, r.margin)
        old = None if e is None else (e["line_source"], dec(e["home_spread"]), dec(e["total"]), e["home_ml"],
                                      e["away_ml"], e["home_score"], e["away_score"], e["outcome"], dec(e["margin"]))
        if new != old:
            db.upsert_bet_result(conn, game_id, market, line, g["home_score"], g["away_score"], r.outcome, r.margin)
    db.delete_bet_results(conn, game_id, [m for m in existing if m not in results])
    conn.commit()
    return results, lines


# ---------- screen payloads ----------

def _line_text(market: str, home_spread, total, home_ml, away_ml, home: str, away: str) -> str | None:
    if market == "moneyline":
        return None if home_ml is None and away_ml is None else \
            f"{away} {grading.fmt_ml(away_ml)} · {home} {grading.fmt_ml(home_ml)}"
    if market == "spread":
        return None if home_spread is None else grading.spread_label(home_spread, home, away)
    return None if total is None else f"O/U {grading.fmt_num(total)}"


def _bets_not_played(status: str | None) -> list[dict]:
    text = f"Not graded · {status}" if status else "Not graded · not played"
    return [{"market": m, "label": MARKET_LABELS[m], "status": "not_played", "text": text} for m in grading.MARKETS]


# ---------- C1: team season stats (league ranks + INTs leader), not in the game summary ----------
# Per team, 4 ESPN calls: core stats (offense ranks), site stats (allowed ranks), core leaders, and the
# INTs leader's athlete record. They run in parallel with each other and with the summary fetch, so a
# cold C1 open costs about two ESPN round trips. Worker threads never touch the database: the request's
# connection reads the cache before and writes it after.

_net = ThreadPoolExecutor(max_workers=16, thread_name_prefix="espn")


def _int_leader(league: str, team_id: str, season: int, season_type: int) -> dict | None:
    top = espn.parse_category_leader(espn.fetch_team_leaders(league, team_id, season, season_type), "interceptions")
    if top is None:
        return None                              # no interceptions yet this season
    athlete = espn.fetch_ref(top["athlete_ref"]) if top.get("athlete_ref") else {}
    return {
        "name": athlete.get("displayName"),
        "last_name": athlete.get("lastName") or athlete.get("displayName"),
        "position": espn._get(athlete, "position", "abbreviation"),
        "value": f"{top['display']} INT",
        "tied": top["tied"],
    }


@dataclass
class _TeamJob:
    side: str
    team_id: str
    stored: dict | None
    futures: dict[str, Future] | None     # None: cache is fresh, nothing to fetch
    finals: int = 0                        # completed games in the season when the fetch started


def start_team_season(conn, game: dict, now: datetime | None = None,
                      base_url: str = espn.BASE) -> list[_TeamJob]:
    """Read each team's cached season stats and start ESPN fetches for the stale ones (non-blocking).
    base_url is the site API (ESPN_BASE); the core API has its own host."""
    now = now or datetime.now(timezone.utc)
    season, season_type = game.get("season"), game.get("season_type") or 2
    jobs: list[_TeamJob] = []
    if not season:
        return jobs
    finals, last_final_start = db.completed_game_stats(conn, game["league"], season, season_type)
    recent = last_final_start is not None and (now - last_final_start).total_seconds() < RECENT_FINAL_HOURS * 3600
    ttl = TEAM_SEASON_RECENT_SECONDS if recent else TEAM_SEASON_SECONDS
    for side in ("home", "away"):
        team_id = game.get(f"{side}_espn_id")
        if not team_id:
            continue
        stored = db.get_team_season(conn, game["league"], team_id, season, season_type)
        futures = None
        if (stored is None or (now - stored["fetched_at"]).total_seconds() >= ttl
                or (stored["data"] or {}).get("completed_games") != finals):
            lg = game["league"]
            futures = {
                "own": _net.submit(espn.fetch_team_stats, lg, team_id, season, season_type),
                "opponent": _net.submit(espn.fetch_team_opponent_stats, lg, team_id, season, season_type,
                                        base_url=base_url),
                "interceptions": _net.submit(_int_leader, lg, team_id, season, season_type),
            }
        jobs.append(_TeamJob(side, team_id, stored["data"] if stored else None, futures, finals))
    return jobs


def finish_team_season(conn, game: dict, jobs: list[_TeamJob]) -> dict[str, dict]:
    """Wait for the fetches, cache complete results, and return {side: data}. A failed part falls back to
    the cached value and isn't saved, so it's retried on the next open."""
    out: dict[str, dict] = {}
    for job in jobs:
        data = dict(job.stored or {})
        if job.futures:
            got, complete = {}, True
            for part, fut in job.futures.items():
                try:
                    got[part] = fut.result(timeout=10)
                except Exception as exc:  # noqa: BLE001 — keep what we have
                    complete = False
                    log.warning("team %s %s fetch failed: %r", job.team_id, part, exc)
            if "own" in got and "opponent" in got:
                data["ranks"] = espn.parse_ranks(got["own"], got["opponent"])
            if "interceptions" in got:
                data["interceptions"] = got["interceptions"]
            if complete:
                data["completed_games"] = job.finals
                db.save_team_season(conn, game["league"], job.team_id, game["season"],
                                    game.get("season_type") or 2, data)
                conn.commit()
        if data:
            out[job.side] = data
    return out


def _attach_ranks(rows: list[dict], team_season: dict[str, dict]) -> None:
    """Add ESPN's league rank to each C1 stat row, but only when ESPN's ranked value is the value on
    the row: season stats and ranks come from different endpoints and may update at different times,
    and a rank beside a number it doesn't belong to would be wrong."""
    for row in rows:
        for side in ("home", "away"):
            r = ((team_season.get(side) or {}).get("ranks") or {}).get(row["key"])
            shown = espn._float(row.get(side))
            if r and r.get("value") is not None and shown is not None and abs(r["value"] - shown) < 0.05:
                row[f"{side}_rank"] = r["rank"]


def _bets_final(results: list[dict], home: str, away: str) -> list[dict]:
    by_market = {r["market"]: r for r in results}
    out = []
    for market in grading.MARKETS:
        r = by_market.get(market)
        if r is None:
            out.append({"market": market, "label": MARKET_LABELS[market], "status": "ungraded", "text": "Ungraded"})
            continue
        line = grading.Line(r["line_source"], r["provider"], r["home_spread"], r["total"], r["home_ml"], r["away_ml"])
        res = grading.Result(market, r["outcome"], Decimal(r["margin"]))
        out.append({
            "market": market, "label": MARKET_LABELS[market], "status": "graded",
            "outcome": r["outcome"], "margin": float(r["margin"]),
            "text": grading.describe(res, line, home, away),
            "line": _line_text(market, r["home_spread"], r["total"], r["home_ml"], r["away_ml"], home, away),
            "line_source": r["line_source"], "line_source_label": SOURCE_LABELS[r["line_source"]],
            "provider": r["provider"], "graded_score": f"{away} {r['away_score']} – {home} {r['home_score']}",
        })
    return out


def _bets_live(home_score, away_score, lines: dict[str, grading.Line], home: str, away: str) -> list[dict]:
    status = {s["market"]: s for s in grading.live_status(home_score, away_score, lines, home, away)} \
        if home_score is not None and away_score is not None else {}
    out = []
    for market in grading.MARKETS:
        s = status.get(market)
        line = lines.get(market)
        if s is None or line is None:
            out.append({"market": market, "label": MARKET_LABELS[market], "status": "ungraded", "text": "No line"})
            continue
        out.append({
            "market": market, "label": MARKET_LABELS[market], "status": "so_far",
            "outcome": s["outcome"], "margin": s["margin"], "text": s["text"],
            "line": _line_text(market, line.home_spread, line.total, line.home_ml, line.away_ml, home, away),
            "line_source": line.source, "line_source_label": SOURCE_LABELS[line.source], "provider": line.provider,
        })
    return out


def detail(conn, card: dict, stored: dict | None, *, stale: bool, error: str | None,
           team_season: dict[str, dict] | None = None) -> dict:
    """One response per screen, parsed from the stored summary on read."""
    screen = SCREENS.get(card["state"], "C1")
    p = stored["payload"] if stored else {}
    home, away = card["home"]["abbr"], card["away"]["abbr"]
    t = summary.teams(p) if stored else {}
    kind = "season" if screen == "C1" else "game"
    # ESPN's summary can trail the scoreboard (a pre-game summary for a game that just kicked off, a live
    # one for a game that just ended). Its box-score blocks would then describe the wrong moment, e.g.
    # season leaders under "Game leaders": withhold them until the summary catches up.
    behind = stored is not None and _ORDER.get(stored["game_state"], 0) < _ORDER.get(card["state"], 0)
    game_p = {} if behind else p
    out = dict(card)
    out.update({
        "screen": screen,
        "stale": stale,
        "error": error if stale else None,
        "summary_updated_at": stored["fetched_at"].isoformat() if stored else None,
        "venue": summary.venue(p) or card.get("venue"),
        "header": {
            side: {k: (t.get(side) or {}).get(k) for k in ("record", "venue_record", "linescores", "possession")}
            for side in ("home", "away")
        },
        "situation": summary.situation(game_p) if screen == "C2" else None,
        "team_stats": {"kind": kind, "rows": summary.team_stats(game_p, kind) if stored else []},
        "leaders": {"kind": kind, "rows": summary.leaders(game_p, kind) if stored else []},
        "time_valid": card.get("time_valid", True),
        "injuries": (summary.injuries(p) if stored else None) if screen == "C1" else None,
        "one_liner": summary.one_liner(game_p) if screen == "C2" else None,
        "bets": None,
        "placeholders": PLACEHOLDERS[screen],
        "summary_available": stored is not None,
        "summary_behind": behind,
    })
    for side in ("home", "away"):
        out["header"][side]["linescores"] = (out["header"][side]["linescores"] or []) if not behind else []
    if screen == "C1" and out.get("line") is None and stored:
        cur = summary.current_line(p)
        if cur:
            out["line"] = {**{k: cur[k] for k in ("provider", "details", "home_spread", "total", "home_ml", "away_ml")},
                           "captured_after_kickoff": False}
    if screen == "C1" and team_season is not None:
        _attach_ranks(out["team_stats"]["rows"], team_season)

        def int_cell(side):
            data = team_season.get(side)
            if data is None or "interceptions" not in data:
                return None                       # couldn't load it: the screen shows "–"
            return data["interceptions"] or {"name": None, "value": "None this season"}

        if out["leaders"]["rows"] or team_season:
            out["leaders"]["rows"].append({"key": "interceptions", "label": "INTs",
                                           "home": int_cell("home"), "away": int_cell("away")})
    if screen == "C2":
        lines = grading_lines(conn, card["id"], stored)
        out["bets"] = _bets_live(card["home"]["score"], card["away"]["score"], lines, home, away)
    elif screen == "D":
        if card.get("completed") is False:
            out["bets"] = _bets_not_played(card.get("status_detail"))
        else:
            out["bets"] = _bets_final(db.bet_results_for_game(conn, card["id"]), home, away)
    return out


def card(row: dict, favorites: set[str]) -> dict:
    """A game's card from its games row, as the scoreboard and the game page show it."""
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
        "neutral_site": bool(row.get("neutral_site")),
        "favorite": row["home_abbr"] in favorites or row["away_abbr"] in favorites,
        "home": {"abbr": row["home_abbr"], "name": row["home_name"], "short": row["home_short"],
                 "logo": row["home_logo"], "color": row["home_color"], "score": row["home_score"],
                 "rank": row.get("home_rank")},
        "away": {"abbr": row["away_abbr"], "name": row["away_name"], "short": row["away_short"],
                 "logo": row["away_logo"], "color": row["away_color"], "score": row["away_score"],
                 "rank": row.get("away_rank")},
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


def page(conn, game_id: int, *, base_url: str = espn.BASE, favorites: dict[str, list[str]] | None = None) -> dict | None:
    """One game's screen payload, exactly as GET /api/games/{id} returns it (None: no such game). A pull: the
    summary is refreshed when the cache rule says so and finals are graded. Shared by the api and, in Phase 4,
    the worker, which writes AI text from the same payload the page shows."""
    favorites = favorites or {}
    row = db.game_by_id(conn, game_id)
    if not row:
        return None
    # C1's team season stats (ranks, INTs leader) are fetched in parallel with the summary.
    team_jobs = start_team_season(conn, row, base_url=base_url) if row["state"] == "pre" else []
    stored = db.get_summary_view(conn, game_id)
    stale, error = False, None
    if needs_refresh(row, stored):
        ok, error = refresh_summary(conn, row, base_url=base_url)
        stale = not ok
        stored = db.get_summary_view(conn, game_id)
        row = db.game_by_id(conn, game_id)
    team_season = finish_team_season(conn, row, team_jobs) if team_jobs else None
    if row["state"] == "post":
        try:
            grade_game(conn, game_id)
        except Exception:  # noqa: BLE001 — show the page even if grading hit a bug
            conn.rollback()
            log.exception("grading game %s failed", game_id)
    c = card(row, set(favorites.get(row["league"], [])))
    return detail(conn, c, stored, stale=stale, error=error,
                  team_season=team_season if row["state"] == "pre" else None)
