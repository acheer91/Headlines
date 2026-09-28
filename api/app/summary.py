"""ESPN game summary (/summary?event=<id>) -> the blocks on screens C1, C2 and D.

Like espn.py, this reads an unofficial feed, so every field is read defensively and every block
is parsed on its own: a block that fails to parse comes back empty (and is logged), never taking
the rest of the page down. Nothing here invents a value; a stat ESPN didn't send is left out.
Field paths are written up in docs/espn_summary_fields.md.
"""
from __future__ import annotations

import functools
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable

from .espn import _float, _get, _home_spread, _int, _ml, is_completed

log = logging.getLogger(__name__)


def _safe(default: Callable[[], Any]):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception:  # noqa: BLE001 — one bad block never kills the page
                log.exception("summary block %s failed to parse", fn.__name__)
                return default()
        return inner
    return wrap


def _comp(p: dict) -> dict:
    return _get(p, "header", "competitions", 0) or {}


def _competitor(p: dict, side: str) -> dict:
    return next((c for c in _comp(p).get("competitors") or [] if c.get("homeAway") == side), {})


def _record(c: dict, kind: str) -> str | None:
    rec = next((r for r in c.get("record") or [] if r.get("type") == kind), None)
    return rec and (rec.get("summary") or rec.get("displayValue"))


# ---------- header ----------

@_safe(dict)
def status(p: dict) -> dict:
    """State, clock and score as ESPN reports them in this payload."""
    comp = _comp(p)
    st = comp.get("status") or {}
    state = _get(st, "type", "state")
    return {
        "event_id": str(_get(p, "header", "id") or comp.get("id") or ""),
        "state": state if state in ("pre", "in", "post") else None,
        "completed": is_completed(st),
        "detail": _get(st, "type", "detail"),
        "short_detail": _get(st, "type", "shortDetail"),
        "period": _int(st.get("period")),
        "clock": st.get("displayClock"),
        "home_score": _int(_competitor(p, "home").get("score")) if state in ("in", "post") else None,
        "away_score": _int(_competitor(p, "away").get("score")) if state in ("in", "post") else None,
    }


@_safe(dict)
def teams(p: dict) -> dict:
    """Per side: abbr, name, logo, records, score by quarter, possession. Neutral-site games
    (London, Germany) get no home/road record, matching ESPN's own header."""
    out = {}
    neutral = bool(_comp(p).get("neutralSite"))
    for side in ("home", "away"):
        c = _competitor(p, side)
        t = c.get("team") or {}
        lines = [_int(ls.get("displayValue", ls.get("value"))) for ls in c.get("linescores") or []]
        out[side] = {
            "abbr": t.get("abbreviation"),
            "name": t.get("displayName"),
            "logo": _get(t, "logos", 0, "href") or t.get("logo"),
            "record": _record(c, "total"),
            "venue_record": None if neutral else _record(c, "home" if side == "home" else "road"),
            "linescores": lines,
            "possession": bool(c.get("possession")),
        }
    return out


@_safe(lambda: None)
def situation(p: dict) -> dict | None:
    """Possession and down-and-distance, live games only. ESPN puts it on the last play of
    drives.current; missing between drives, at the half, and after the final."""
    if _get(_comp(p), "status", "type", "state") != "in":
        return None
    possession = next((_get(c, "team", "abbreviation") for c in _comp(p).get("competitors") or []
                       if c.get("possession")), None)
    end = _get(p, "drives", "current", "plays", -1, "end") or {}
    text = end.get("downDistanceText")
    if not possession and not text:
        return None
    return {"possession": possession, "down_distance": text}


@_safe(lambda: None)
def venue(p: dict) -> str | None:
    return _get(p, "gameInfo", "venue", "fullName")


# ---------- team stats ----------

# C1: season averages ESPN attaches to upcoming games. League ranks aren't in the summary; they come from
# ESPN's team endpoints (games.start_team_season). Turnover margin isn't wanted (Adam).
PRE_STATS = [
    ("totalPointsPerGame", "Points / game"),
    ("totalPointsPerGameAllowed", "Points allowed / game"),
    ("yardsPerGame", "Yards / game"),
    ("yardsPerGameAllowed", "Yards allowed / game"),
]
# C2 and D: this game's box score.
GAME_STATS = [
    ("totalYards", "Total yards"),
    ("netPassingYards", "Passing"),
    ("rushingYards", "Rushing"),
    ("turnovers", "Turnovers"),
    ("thirdDownEff", "3rd down"),
    ("possessionTime", "Time of possession"),
    ("totalPenaltiesYards", "Penalties"),
]


def _third_down(v: str | None) -> str | None:
    """'4-8' -> '4-8 (50%)'. The rate is arithmetic on ESPN's own numbers."""
    m = re.fullmatch(r"\s*(\d+)-(\d+)\s*", v or "")
    if not m:
        return v
    made, att = int(m.group(1)), int(m.group(2))
    return f"{made}-{att} ({round(100 * made / att)}%)" if att else f"{made}-{att}"


@_safe(list)
def team_stats(p: dict, kind: str) -> list[dict]:
    """kind 'season' (C1) or 'game' (C2, D). Rows with neither side reported are dropped."""
    wanted = PRE_STATS if kind == "season" else GAME_STATS
    by_side: dict[str, dict[str, str]] = {}
    for t in _get(p, "boxscore", "teams") or []:
        vals: dict[str, str] = {}
        for s in t.get("statistics") or []:
            if s.get("name") and s["name"] not in vals:   # ESPN repeats 'interceptions'; first wins
                vals[s["name"]] = (s.get("displayValue") or "").strip() or None
        if t.get("homeAway") in ("home", "away"):
            by_side[t["homeAway"]] = vals
    rows = []
    for key, label in wanted:
        home, away = by_side.get("home", {}).get(key), by_side.get("away", {}).get(key)
        if key == "thirdDownEff":
            home, away = _third_down(home), _third_down(away)
        if home is not None or away is not None:
            rows.append({"key": key, "label": label, "home": home, "away": away})
    return rows


# ---------- leaders ----------

# C1 shows Passing, Rushing, Receiving, Tackles and INTs. The summary has the first four; INTs come from
# ESPN's team season leaders (games.start_team_season), since the summary has no interceptions category.
SEASON_LEADERS = [("passingYards", "Passing"), ("rushingYards", "Rushing"), ("receivingYards", "Receiving"),
                  ("totalTackles", "Tackles")]
GAME_LEADERS = [("passingYards", "Passing"), ("rushingYards", "Rushing"), ("receivingYards", "Receiving")]


@_safe(list)
def leaders(p: dict, kind: str) -> list[dict]:
    """kind 'season' (C1: ESPN sends season leaders before kickoff) or 'game' (C2, D)."""
    sides = {t.get("abbr"): side for side, t in teams(p).items()}
    found: dict[str, dict[str, dict]] = {}
    for team in p.get("leaders") or []:
        side = sides.get(_get(team, "team", "abbreviation"))
        if not side:
            continue
        for cat in team.get("leaders") or []:
            top = _get(cat, "leaders", 0)
            if not top:
                continue
            a = top.get("athlete") or {}
            found.setdefault(cat.get("name"), {})[side] = {
                "name": a.get("displayName"),
                "last_name": a.get("lastName") or a.get("displayName"),
                "position": _get(a, "position", "abbreviation"),
                "value": top.get("displayValue"),
            }
    wanted = SEASON_LEADERS if kind == "season" else GAME_LEADERS
    return [{"key": k, "label": label, "home": found[k].get("home"), "away": found[k].get("away")}
            for k, label in wanted if k in found]


# ---------- injuries ----------

@_safe(dict)
def injuries(p: dict) -> dict:
    """Per side, in ESPN's order. An empty list means none reported: the screen says so.
    ESPN's summary lists at most 5 per team, the same list its game page shows. The status is
    ESPN's display status ("PUP-R", "IR", "Out"), which is finer than the top-level `status`
    (that one says "Out" for a PUP-R player)."""
    sides = {t.get("abbr"): side for side, t in teams(p).items()}
    out: dict[str, list] = {"home": [], "away": []}
    for team in p.get("injuries") or []:
        side = sides.get(_get(team, "team", "abbreviation"))
        if not side:
            continue
        for i in team.get("injuries") or []:
            a = i.get("athlete") or {}
            if not a.get("displayName"):
                continue
            out[side].append({
                "name": a["displayName"],
                "position": _get(a, "position", "abbreviation"),
                "status": _get(i, "details", "fantasyStatus", "displayDescription") or i.get("status")
                or _get(i, "type", "description"),
                "detail": _get(i, "details", "type"),
                "return_date": _get(i, "details", "returnDate"),   # "2026-10-04", ESPN's estimate
            })
    return out


# ---------- lines ----------

def _pick(p: dict) -> dict:
    pcs = [x for x in p.get("pickcenter") or [] if isinstance(x, dict)]
    return min(pcs, key=lambda x: _int(_get(x, "provider", "priority")) or 99) if pcs else {}


def _num(s: Any) -> float | None:
    """'-7' -> -7.0, 'o50.5' -> 50.5, '+4.5' -> 4.5; 'OFF' or missing -> None."""
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else None


@_safe(lambda: None)
def closing_line(p: dict) -> dict | None:
    """ESPN's closing line: the book's line locked at kickoff (pickcenter .close). Only a
    grading line once the game has started; before kickoff 'close' is just the current line."""
    if _get(_comp(p), "status", "type", "state") not in ("in", "post"):
        return None
    pc = _pick(p)
    if not pc:
        return None
    t = teams(p)
    home_spread = _num(_get(pc, "pointSpread", "home", "close", "line"))
    if home_spread is None:
        home_spread = _home_spread(pc, t["home"]["abbr"] or "", t["away"]["abbr"] or "")
    total = _num(_get(pc, "total", "over", "close", "line"))
    if total is None:
        total = _float(pc.get("overUnder"))
    home_ml = _ml(_get(pc, "moneyline", "home", "close", "odds"))
    away_ml = _ml(_get(pc, "moneyline", "away", "close", "odds"))
    if home_ml is None:
        home_ml = _ml(_get(pc, "homeTeamOdds", "moneyLine"))
    if away_ml is None:
        away_ml = _ml(_get(pc, "awayTeamOdds", "moneyLine"))
    line = {"provider": _get(pc, "provider", "name"), "home_spread": home_spread, "total": total,
            "home_ml": home_ml, "away_ml": away_ml}
    return line if any(line[k] is not None for k in ("home_spread", "total", "home_ml", "away_ml")) else None


@_safe(lambda: None)
def current_line(p: dict) -> dict | None:
    """The book's line right now (pickcenter top level). C1 falls back to it when the scoreboard
    never gave us a line for this game."""
    pc = _pick(p)
    if not pc:
        return None
    t = teams(p)
    line = {
        "provider": _get(pc, "provider", "name"),
        "details": pc.get("details"),
        "home_spread": _home_spread(pc, t["home"]["abbr"] or "", t["away"]["abbr"] or ""),
        "total": _float(pc.get("overUnder")),
        "home_ml": _ml(_get(pc, "homeTeamOdds", "moneyLine")),
        "away_ml": _ml(_get(pc, "awayTeamOdds", "moneyLine")),
    }
    return line if any(line[k] is not None for k in ("home_spread", "total", "home_ml", "away_ml")) else None


# ---------- top fantasy performer per team (C2 so far, D final) ----------

# Half-PPR (Adam, 2026-09-27), from ESPN's player box score. Offense only: kicker scoring needs
# field-goal distances the box score doesn't have, and two-point conversions aren't in it either,
# so neither is counted (never fake a field). Weights are per unit of each box-score stat.
HALF_PPR: dict[tuple[str, str], Decimal] = {
    ("passing", "passingYards"): Decimal("0.04"),          # 1 point per 25 yards
    ("passing", "passingTouchdowns"): Decimal(4),
    ("passing", "interceptions"): Decimal(-2),
    ("rushing", "rushingYards"): Decimal("0.1"),
    ("rushing", "rushingTouchdowns"): Decimal(6),
    ("receiving", "receptions"): Decimal("0.5"),
    ("receiving", "receivingYards"): Decimal("0.1"),
    ("receiving", "receivingTouchdowns"): Decimal(6),
    ("fumbles", "fumblesLost"): Decimal(-2),
    ("kickReturns", "kickReturnTouchdowns"): Decimal(6),
    ("puntReturns", "puntReturnTouchdowns"): Decimal(6),
}


def _dec(v: Any) -> Decimal:
    """A box-score number; anything else ('--', '', 'NaN', None) counts as 0."""
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return d if d.is_finite() else Decimal(0)


def _yds(v: Any) -> str:
    return f"{v} yd" if _dec(v) in (1, -1) else f"{v} yds"


def _statline(s: dict[str, dict[str, str]]) -> str:
    """'20/34, 226 yds, 1 TD, 1 INT · 4 car, 17 yds' from the player's box-score rows."""
    parts = []
    p = s.get("passing")
    if p:
        bits = [p.get("completions/passingAttempts", ""), _yds(p.get('passingYards', '0'))]
        if _dec(p.get("passingTouchdowns")):
            bits.append(f"{p['passingTouchdowns']} TD")
        if _dec(p.get("interceptions")):
            bits.append(f"{p['interceptions']} INT")
        parts.append(", ".join(b for b in bits if b))
    r = s.get("rushing")
    if r and (_dec(r.get("rushingAttempts")) or _dec(r.get("rushingYards"))):
        bits = [f"{r.get('rushingAttempts', '0')} car", _yds(r.get('rushingYards', '0'))]
        if _dec(r.get("rushingTouchdowns")):
            bits.append(f"{r['rushingTouchdowns']} TD")
        parts.append(", ".join(bits))
    c = s.get("receiving")
    if c and (_dec(c.get("receptions")) or _dec(c.get("receivingYards"))):
        bits = [f"{c.get('receptions', '0')} rec", _yds(c.get('receivingYards', '0'))]
        if _dec(c.get("receivingTouchdowns")):
            bits.append(f"{c['receivingTouchdowns']} TD")
        parts.append(", ".join(bits))
    return " · ".join(parts)


@_safe(dict)
def fantasy_top(p: dict) -> dict:
    """Per side, the player with the most half-PPR points in this game's box score:
    {name, position, points (Decimal, exact), points_text ('19.4'), statline}. A side is missing
    when ESPN sent no player box score for it. Position comes from the leaders block when the same
    player is listed there; otherwise it's left out rather than guessed."""
    sides = {t.get("abbr"): side for side, t in teams(p).items()}
    positions = {}
    for team in p.get("leaders") or []:
        for cat in team.get("leaders") or []:
            for ld in cat.get("leaders") or []:
                a = ld.get("athlete") or {}
                if a.get("id") and _get(a, "position", "abbreviation"):
                    positions[str(a["id"])] = a["position"]["abbreviation"]
    out = {}
    for team in _get(p, "boxscore", "players") or []:
        side = sides.get(_get(team, "team", "abbreviation"))
        if not side:
            continue
        players: dict[str, dict] = {}
        for cat in team.get("statistics") or []:
            keys = cat.get("keys") or []
            for row in cat.get("athletes") or []:
                a = row.get("athlete") or {}
                pid = str(a.get("id") or a.get("displayName") or "")
                if not pid:
                    continue
                pl = players.setdefault(pid, {"name": a.get("displayName"), "points": Decimal(0), "stats": {}})
                vals = dict(zip(keys, row.get("stats") or []))
                pl["stats"][cat.get("name")] = vals
                for (cname, key), w in HALF_PPR.items():
                    if cname == cat.get("name") and key in vals:
                        pl["points"] += _dec(vals[key]) * w
        if not players:
            continue
        # Highest points; ties go to the player listed first by ESPN (dicts keep insertion order).
        pid, top = max(players.items(), key=lambda kv: kv[1]["points"])
        out[side] = {
            "name": top["name"],
            "position": positions.get(pid),
            "points": top["points"],
            "points_text": f"{top['points'].quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):f}",
            "statline": _statline(top["stats"]),
        }
    return out


# ---------- live one-liner (template; Phase 4 replaces it with AI text) ----------

@_safe(lambda: None)
def one_liner(p: dict) -> str | None:
    """'LAR lead 13–0 · Stafford 143 yds, 1 TD'. Built only from the box score."""
    st, t = status(p), teams(p)
    hs, as_ = st.get("home_score"), st.get("away_score")
    if hs is None or as_ is None:
        return None
    if hs == as_:
        text, side = f"Tied {hs}–{as_}", "home"
    else:
        side = "home" if hs > as_ else "away"
        text = f"{t[side]['abbr']} lead {max(hs, as_)}–{min(hs, as_)}"
    ld = {row["key"]: row for row in leaders(p, "game")}
    for key in ("passingYards", "rushingYards"):
        who = (ld.get(key) or {}).get(side)
        if who and who.get("value"):
            yds = re.search(r"(-?\d+) YDS", who["value"])   # rushing can be negative
            td = re.search(r"(\d+) TD", who["value"])
            stat = f"{yds.group(1)} yds" if yds else who["value"]
            if td:
                stat += f", {td.group(1)} TD"
            return f"{text} · {who['last_name']} {stat}"
    return text
