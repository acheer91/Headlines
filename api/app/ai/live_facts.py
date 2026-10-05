"""The live NFL one-liner's fact sheet (Adam, Oct 4): the score, the phase and shape of the game, the freshest plays and the
strongest few hooks the game offers, picked by code from the box score and the play-by-play (summary.live_context).

This replaces facts.live_facts (the M4 sheet, still there). It lives in its own module because facts.py is pinned by
sha256 for the Z5 recap-angle test (docs/z5-preregistration.txt): any edit to facts.py voids the pre-registered week.
"""
from __future__ import annotations

import re

from .facts import (LIVE_LEADERS, LIVE_STATS, _int, _line, _names, _only, _period, _stat_lines, clock_seconds,
                    players, stats)

LIVE_HOOKS = 4              # the strongest hook lines on the sheet (code picks them: M4 trimmed by line, this by salience)
LIVE_LAST_PLAYS = 2         # the freshest plays, always: the "right now"
UNANSWERED_MIN = 10         # "N unanswered points" is a hook from here
COMEBACK_LEAD = 10          # a lead this big, now gone or nearly: the comeback hook
COMEBACK_MARGIN = 3
PUNT_STREAK_MIN = 2
LIVE_TURNOVER_GAP, LIVE_TURNOVERS_TOTAL = 2, 3     # a gap between the teams, or this many between them (a sloppy game)
YARDS_GAP = 75              # total yards
PASS_GAP, RUSH_GAP = 100, 60
THIRD_DOWN_TRIES, THIRD_DOWN_RATE = 5, 0.25
PICKS_MIN, BIG_PASSING_YARDS, PASSING_TDS = 2, 250, 3
WIN_SWING, WIN_EXTREME = 15, 92     # points of ESPN's win probability over its look-back; a near-settled game
RUN_CALLS_MIN, RUN_SHARE_MAX = 12, 0.25
QUARTER_RUN = 14            # one team's points in a quarter while the other scored none
ONE_SCORE, TWO_SCORES = 8, 16


def _score_run(scores: list[dict]) -> tuple[list[str | None], int, str | None, int]:
    """From the running scores: who led after each scoring play (None: tied), how many times the lead changed
    hands, and the team that has scored the last points with how many (the other side has not scored since)."""
    leaders, flips, prev_a, prev_h, last = [], 0, 0, 0, None
    unanswered = {"home": 0, "away": 0}
    for s in scores:
        a, h = s.get("away"), s.get("home")
        if a is None or h is None:
            continue
        side = "home" if h > prev_h else "away" if a > prev_a else None
        if side:
            other = "away" if side == "home" else "home"
            unanswered[other] = 0
            unanswered[side] += (h - prev_h) + (a - prev_a)
        lead = "home" if h > a else "away" if a > h else None
        prior = next((x for x in reversed(leaders) if x), None)
        if lead and prior and lead != prior:
            flips += 1
        leaders.append(lead)
        prev_a, prev_h, last = a, h, side
    return leaders, flips, last, unanswered[last] if last else 0


EARLY_SHARE = 0.15          # the prompt's "first ~15%": light, provisional takes
LATE_SHARE = 0.75           # the fourth quarter on


def game_phase(period, clock) -> str | None:
    """'early' (the first 15% of regulation), 'late' (the fourth quarter), 'overtime', else 'middle'; None when the
    period or clock isn't known. Football only: four 15-minute quarters."""
    left = clock_seconds(clock)
    if not isinstance(period, int) or period < 1 or left is None:
        return None
    if period > 4:
        return "overtime"
    share = ((period - 1) * 900 + max(0, 900 - left)) / 3600
    return "early" if share < EARLY_SHARE else "late" if share >= LATE_SHARE else "middle"


def _shape(margin: int, phase: str | None) -> str:
    """The game's shape from its margin; a blowout only late (17-0 in the second quarter is a three-score game)."""
    return ("tied" if margin == 0 else "a one-score game" if margin <= ONE_SCORE else
            "a two-score game" if margin <= TWO_SCORES else
            "a blowout" if phase in ("late", "overtime") else "a three-score game")


def _num(v) -> tuple[int, int] | None:
    """'4-8' or '4-8 (50%)' -> (4, 8)."""
    m = re.match(r"\s*(\d+)-(\d+)", str(v or ""))
    return (int(m[1]), int(m[2])) if m else None


def _count(value: str, word: str) -> int:
    """'3/9, 29 YDS, 1 TD, 2 INT' -> the number before the word (0 when absent)."""
    m = re.search(rf"(\d+) {word}\b", value)
    return int(m[1]) if m else 0


def _stat_hooks(game: dict, short: dict) -> list[tuple[int, str]]:
    """Hooks from the box score: a turnover gap, a big yardage gap, a team stuck on third down, a passer's day."""
    out = []
    for r in (game.get("team_stats") or {}).get("rows") or []:
        h, a = _int(r.get("home")), _int(r.get("away"))
        key, sal = r.get("key"), 0
        if key == "turnovers" and h is not None and a is not None and (abs(h - a) >= LIVE_TURNOVER_GAP or h + a >= LIVE_TURNOVERS_TOTAL):
            sal = 6 + abs(h - a) if abs(h - a) >= LIVE_TURNOVER_GAP else 4          # a gap, or a sloppy game
        elif key == "totalYards" and h is not None and a is not None and abs(h - a) >= YARDS_GAP:
            sal = 4 + min(3, abs(h - a) // 50)
        elif key == "netPassingYards" and h is not None and a is not None and abs(h - a) >= PASS_GAP:
            sal = 3
        elif key == "rushingYards" and h is not None and a is not None and abs(h - a) >= RUSH_GAP:
            sal = 3
        elif key == "thirdDownEff":
            tries = [_num(r.get(s)) for s in ("home", "away")]
            if any(t and t[1] >= THIRD_DOWN_TRIES and t[0] / t[1] <= THIRD_DOWN_RATE for t in tries):
                sal = 3
        if sal:
            line = _stat_lines(dict(game, team_stats={"kind": "game", "rows": [r]}), short)
            out += [(sal, f"So far, {x[0].lower()}{x[1:]}") for x in line]
    for r in (game.get("leaders") or {}).get("rows") or []:
        for side in ("away", "home"):
            p = r.get(side)
            val = str((p or {}).get("value") or "")
            ints, yds, tds = (_count(val, w) for w in ("INT", "YDS", "TD"))
            sal = (6 + ints if ints >= PICKS_MIN else 5 if tds >= PASSING_TDS else 4 if yds >= BIG_PASSING_YARDS else 0)
            if p and p.get("name") and sal:
                out.append((sal, f"{short[side]} {r['label']} leader (so far): {p['name']}"
                                 f"{', ' + p['position'] if p.get('position') else ''}: {val}."))
    return out


def _live_hooks(game: dict, n: dict, short: dict) -> list[tuple[int, str]]:
    """Every hook the game has right now as (salience, one labelled FACTS line): what the one-liner's angle can be.
    Code ranks them and keeps the strongest LIVE_HOOKS (live_facts); the model sees those, not the whole box score and
    play-by-play (588 facts tokens became about a third; Oct 4)."""
    live = game.get("live") or {}
    out = _stat_hooks(game, short)
    h, a = game["home"].get("score"), game["away"].get("score")
    scores = live.get("scores") or []
    leaders, flips, last, run = _score_run(scores)

    sp = (game.get("line") or {}).get("home_spread")
    if sp is not None and h is not None and a is not None and float(sp) != 0 and h != a:
        fav = "home" if float(sp) < 0 else "away"
        if (h < a) if fav == "home" else (a < h):
            out.append((8, f"The pregame favorite, {n[fav]['name']}, is trailing."))
    if flips >= 2:
        out.append((4 + flips, f"The lead has changed hands {flips} times."))
    if last and run >= UNANSWERED_MIN:
        out.append((5 + run // 10, f"Unanswered points, latest run: {short[last]} {run}."))
    seen = {x for x in leaders if x}
    if len(seen) == 1 and leaders and leaders[-1] == next(iter(seen)):
        out.append((2, f"Not trailed at any point in this game: {short[next(iter(seen))]}."))
    margins = [(abs(s["home"] - s["away"]), "home" if s["home"] > s["away"] else "away") for s in scores
               if s.get("home") is not None and s.get("away") is not None and s["home"] != s["away"]]
    if margins and h is not None and a is not None:
        big, side = max(margins)
        trailing = (h < a) if side == "home" else (a < h)
        if big >= COMEBACK_LEAD and (trailing or abs(h - a) <= COMEBACK_MARGIN):
            out.append((9, f"{short[side]} led by {big} earlier in this game."))

    for side in ("away", "home"):
        results = [d for d in live.get("drives") or [] if d.get("side") == side and d.get("result")]
        streak = 0
        for d in reversed(results):
            if d["result"] != "Punt":
                break
            streak += 1
        if streak >= PUNT_STREAK_MIN:
            poss = short[side] + ("'" if short[side].endswith("s") else "'s")
            out.append((4 + streak, f"{poss} last {streak} drives all ended in a punt."))

    win = live.get("win_prob")
    if win:
        swing = abs(win["home"] - win["home_before"]) if win.get("home_before") is not None else 0
        line = f"ESPN's live win probability: {short['home']} {win['home']}%, {short['away']} {100 - win['home']}%"
        if win.get("home_before") is not None:
            line += f" (about {win['plays_back']} plays ago: {short['home']} {win['home_before']}%)"
        sal = max(5 + swing // 10 if swing >= WIN_SWING else 0, 5 if max(win["home"], 100 - win["home"]) >= WIN_EXTREME else 0)
        if sal:
            out.append((sal, line + "."))

    calls = live.get("calls") or {}
    for side in ("away", "home"):
        c = calls.get(side) or {}
        tot = c.get("run", 0) + c.get("pass", 0)
        mine, theirs = (h, a) if side == "home" else (a, h)
        if (tot >= RUN_CALLS_MIN and c["run"] / tot <= RUN_SHARE_MAX and mine is not None and theirs is not None
                and mine < theirs):
            out.append((3, "Play calls so far (sacks count as passes): " + "; ".join(
                f"{short[s]} {calls[s]['run']} runs, {calls[s]['pass']} passes" for s in ("away", "home")) + "."))
            break

    hdr = game.get("header") or {}
    hl, al = (hdr.get("home") or {}).get("linescores") or [], (hdr.get("away") or {}).get("linescores") or []
    if hl and len(hl) == len(al):
        for i, (x, y) in enumerate(zip(al, hl[: len(al)])):
            if (i < len(hl) - 1 and isinstance(x, int) and isinstance(y, int)
                    and ((x >= QUARTER_RUN and y == 0) or (y >= QUARTER_RUN and x == 0))):
                out.append((4, f"Points in {_period(i)}: {short['away']} {x}, {short['home']} {y}."))
    return out


def _live_lines(game: dict, n: dict, short: dict) -> list[str]:
    """The live game's context lines the sheet always carries: the phase and the shape of the game, the ball, the
    freshest plays and the latest score. Hooks (_live_hooks) come after, strongest first."""
    live = game.get("live") or {}
    side_of = {n[s]["abbr"]: s for s in ("home", "away") if n[s].get("abbr")}

    def team(entry: dict) -> str:
        """The team a play or score belongs to: the side summary.live_context matched by team id, else by abbreviation."""
        side = entry.get("side") or side_of.get(entry.get("team"))
        return short[side] if side in short else str(entry.get("team") or "A team")

    def when(period, clock) -> str:
        return f"{_period(period - 1)} {clock}" if period and clock else "earlier"

    out = []
    h, a = game["home"].get("score"), game["away"].get("score")
    phase = game_phase(live.get("period"), live.get("clock"))
    if phase or (h is not None and a is not None):
        shape = _shape(abs(h - a), phase) if h is not None and a is not None else None
        out.append("Game phase: " + ", ".join(x for x in (phase, shape) if x) + ".")
    sit = game.get("situation") or {}
    if sit.get("possession") in side_of:
        out.append(f"Ball: {team({'team': sit['possession']})}" + (f", {sit['down_distance']}." if sit.get("down_distance") else "."))
    for pl in (live.get("recent") or [])[-LIVE_LAST_PLAYS:]:
        out.append(f"Recent play ({when(pl['period'], pl['clock'])}, {team(pl)} on offense): {pl['text']}")
    for s in [x for x in live.get("scores") or [] if x.get("away") is not None and x.get("home") is not None][-1:]:
        out.append(f"Latest score, {when(s['period'], s['clock'])}: {team(s)} {s['text']}. "
                   f"{short['away']} {s['away']}, {short['home']} {s['home']}.")
    return out


def live_facts(game: dict, hooks: int | None = LIVE_HOOKS) -> dict:
    """The fact sheet for the live one-liner: the game so far, nothing about how it will end. The score, who was
    favored, who leads, the phase and the freshest plays, then the strongest `hooks` lines the game offers (_live_hooks;
    None: every hook, for tests and the eval). The writer and the fact-checker get this same sheet."""
    game = dict(game, team_stats=_only(game.get("team_stats"), LIVE_STATS),
                leaders=_only(game.get("leaders"), LIVE_LEADERS))
    n = _names(game)
    short = {s: n[s]["short"] or n[s]["name"] for s in ("home", "away")}
    h, a = game["home"].get("score"), game["away"].get("score")
    facts = [f"Live, {game.get('status_detail') or 'in progress'}: {n['away']['name']} {a}, {n['home']['name']} {h}.",
             f"Home team: {n['home']['name']}. Visiting team: {n['away']['name']}."]
    facts += [f"Before kickoff, {line[0].lower()}{line[1:]}" for line in _line(game, n, total=False)]
    if h is not None and a is not None:
        facts.append("Tied." if h == a else
                     f"{n['home' if h > a else 'away']['name']} leads by {abs(h - a)} right now.")
    facts += _live_lines(game, n, short)
    ranked = sorted(enumerate(_live_hooks(game, n, short)), key=lambda x: (-x[1][0], x[0]))
    facts += [line for _, (_, line) in (ranked if hooks is None else ranked[:hooks])]
    return {"teams": n, "facts": facts, "players": players(game), "stats": stats(game)}
