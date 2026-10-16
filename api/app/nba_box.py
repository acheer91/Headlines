"""NBA box score blocks from ESPN's game summary (/summary?event=<id>): the team stats, the player box score and the
game leaders. The NBA has no game screens of its own beyond this, so there is no AI text, no line and no grading.

Like summary.py, every block is read defensively and parsed on its own: one that fails comes back empty and is logged,
never taking the page down. Nothing here invents a value; a stat ESPN didn't send is left out. A live game's summary
carries the same shape as a final's, so the same code serves the live screen and the post-game one.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from .espn import _get, _int
from .summary import teams

log = logging.getLogger(__name__)


def _safe(default: Callable[[], Any]):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception:  # noqa: BLE001 — one bad block never kills the page
                log.exception("nba box block %s failed to parse", fn.__name__)
                return default()
        return inner
    return wrap


# Team stat rows in screen order: ESPN's name, our label. Percentages arrive as "39" and get a "%".
# ESPN also sends technicals, team turnovers, points conceded off turnovers and lead changes; they are left out
# (a row nobody asked for, or one whose wording reads two ways).
TEAM_STATS = [
    ("fieldGoalsMade-fieldGoalsAttempted", "FG"), ("fieldGoalPct", "FG%"),
    ("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "3PT"), ("threePointFieldGoalPct", "3P%"),
    ("freeThrowsMade-freeThrowsAttempted", "FT"), ("freeThrowPct", "FT%"),
    ("totalRebounds", "Rebounds"), ("offensiveRebounds", "Offensive rebounds"),
    ("assists", "Assists"), ("steals", "Steals"), ("blocks", "Blocks"), ("turnovers", "Turnovers"),
    ("pointsInPaint", "Points in the paint"), ("fastBreakPoints", "Fast break points"),
    ("largestLead", "Largest lead"),
]
_PERCENT = {"fieldGoalPct", "threePointFieldGoalPct", "freeThrowPct"}


def _side_of(p: dict) -> dict[str, str]:
    """Team abbreviation -> 'home' or 'away', from the header."""
    return {t["abbr"]: side for side, t in teams(p).items() if t.get("abbr")}


@_safe(list)
def team_stats(p: dict) -> list[dict]:
    """One row per stat, both sides, in TEAM_STATS order. A row neither side reports is dropped."""
    sides = _side_of(p)
    by_side: dict[str, dict[str, str]] = {}
    for t in _get(p, "boxscore", "teams") or []:
        side = t.get("homeAway") if t.get("homeAway") in ("home", "away") else sides.get(_get(t, "team", "abbreviation"))
        if side not in ("home", "away"):
            continue
        vals: dict[str, str] = {}
        for s in t.get("statistics") or []:
            if s.get("name") and s["name"] not in vals:
                v = (s.get("displayValue") or "").strip()
                vals[s["name"]] = (v + "%" if v and s["name"] in _PERCENT else v) or None
        by_side[side] = vals
    rows = []
    for key, label in TEAM_STATS:
        home, away = by_side.get("home", {}).get(key), by_side.get("away", {}).get(key)
        if home is not None or away is not None:
            rows.append({"key": key, "label": label, "home": home, "away": away})
    return rows


@_safe(lambda: None)
def players(p: dict) -> dict | None:
    """The player box score per side: ESPN's own columns (MIN PTS FG 3PT FT REB AST TO STL BLK OREB DREB PF +/-) and
    one row per player in ESPN's order (starters first). A player who did not play has `dnp` (ESPN's reason) and no
    stats. `on_court` is true for a player on the floor right now (live games). None when ESPN sent no player table
    (a game that has not started)."""
    sides = _side_of(p)
    out: dict[str, dict] = {}
    for block in _get(p, "boxscore", "players") or []:
        side = sides.get(_get(block, "team", "abbreviation"))
        table = _get(block, "statistics", 0) or {}
        if side is None or not table.get("athletes"):
            continue
        columns = list(table.get("labels") or table.get("names") or [])
        rows = []
        for a in table["athletes"]:
            ath = a.get("athlete") or {}
            stats = [str(x) for x in a.get("stats") or []]
            dnp = bool(a.get("didNotPlay")) or not stats
            rows.append({
                "id": str(ath.get("id")) if ath.get("id") is not None else None,
                "name": ath.get("shortName") or ath.get("displayName"),
                "full_name": ath.get("displayName"),
                "pos": _get(ath, "position", "abbreviation"),
                "jersey": ath.get("jersey"),
                "starter": bool(a.get("starter")),
                "on_court": bool(a.get("active")) and not dnp,
                "dnp": ((a.get("reason") or "Did not play").strip().capitalize()) if dnp else None,
                "stats": [] if dnp else stats,
            })
        out[side] = {"columns": columns, "keys": list(table.get("keys") or []), "players": rows,
                     "totals": [str(x) for x in table.get("totals") or []]}
    return out or None


# The game leaders: the top scorer, rebounder and passer of each team, from the box score (so they can never
# disagree with it). Ties are reported, not broken: "+1 tied".
LEADER_STATS = [("points", "Points", "pts"), ("rebounds", "Rebounds", "reb"), ("assists", "Assists", "ast")]


@_safe(list)
def leaders(box: dict | None) -> list[dict]:
    if not box:
        return []
    rows = []
    for key, label, unit in LEADER_STATS:
        row = {"key": key, "label": label}
        for side in ("home", "away"):
            table = box.get(side) or {}
            idx = (table.get("keys") or []).index(key) if key in (table.get("keys") or []) else None
            scored = []
            for pl in table.get("players") or []:
                if idx is not None and not pl["dnp"] and idx < len(pl["stats"]) and _int(pl["stats"][idx]) is not None:
                    scored.append((_int(pl["stats"][idx]), pl))
            if not scored:
                row[side] = None
                continue
            best = max(v for v, _ in scored)
            if best <= 0:                     # nobody has scored, rebounded or passed yet: no leader to show
                row[side] = None
                continue
            top = [pl for v, pl in scored if v == best]
            row[side] = {"name": top[0]["full_name"] or top[0]["name"], "position": top[0]["pos"],
                         "value": f"{best} {unit}", "tied": len(top) - 1}
        if row["home"] or row["away"]:
            rows.append(row)
    return rows
