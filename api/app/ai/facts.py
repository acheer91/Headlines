"""Fact sheets built by code from the game payload (/api/games/{id}), so the model never has to read raw data.

Every error in the Sep 29 samples came from the model misreading raw data: "Turnovers: BAL 0, DAL 1" became
"the Ravens forced no turnovers"; a quarter's score landed in the wrong quarter. Here each fact is one plain,
labelled sentence with team names attached, and anything that needs arithmetic (margins, halftime, who led,
takeaways) is computed in code.

A recap's facts come only from here (no model extract step). A preview's game facts come from here; the model
extracts only from the articles.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _names(game: dict) -> dict:
    """Team names as the writer may use them: full, short, and the place and nickname parts."""
    out = {}
    for side in ("home", "away"):
        t = game[side]
        words = (t.get("name") or "").split()
        out[side] = {"name": t.get("name"), "short": t.get("short"), "abbr": t.get("abbr"),
                     "nickname": words[-1] if words else None,
                     "place": " ".join(words[:-1]) if len(words) > 1 else None,
                     "rank": t.get("rank"), "side": side}
    return out


def _label(team: dict) -> str:
    return f"No. {team['rank']} {team['name']}" if team.get("rank") else team["name"]


def _period(i: int) -> str:
    return f"Q{i + 1}" if i < 4 else ("OT" if i == 4 else f"OT{i - 3}")


def _break(i: int) -> str:
    """The quarter break after period i: 'end of Q1', 'halftime', 'end of Q3'..."""
    return "halftime" if i == 1 else f"end of {_period(i)}"


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _stat_lines(game: dict, short: dict) -> list[str]:
    ts = game.get("team_stats") or {}
    season = ts.get("kind") == "season"
    lines = []
    for r in ts.get("rows") or []:
        h, a = r.get("home"), r.get("away")
        if h is None and a is None:
            continue
        # Ranks for "allowed" stats count from the fewest (CLAUDE.md: 1st = allowed the least).
        scope = "nationally" if game.get("league") == "ncaaf" else "in the league"
        order = " fewest" if "allowed" in (r.get("key") or "").lower() else ""
        rank = lambda side: f" ({r[side + '_rank']}{order} {scope})" if r.get(side + "_rank") else ""
        if r.get("key") == "turnovers" and not season:
            ha, aa = _int(h), _int(a)
            if ha is not None and aa is not None:
                # The line the model got wrong ("forced no turnovers"): giveaways and takeaways are the same two
                # numbers seen from each side, so both are spelled out instead of the bare "Turnovers" row.
                lines.append(f"Giveaways (turnovers each team committed): {short['away']} {aa}, {short['home']} {ha}. "
                             f"Takeaways (turnovers each team forced): {short['away']} {ha}, {short['home']} {aa}.")
                continue
        prefix = "Season " if season else ""
        lines.append(f"{prefix}{r['label'].lower()}: {short['away']} {a}{rank('away')}, "
                     f"{short['home']} {h}{rank('home')}.")
    return lines


def _leader_lines(game: dict, short: dict) -> list[str]:
    ld = game.get("leaders") or {}
    season = ld.get("kind") == "season"
    out = []
    for r in ld.get("rows") or []:
        for side in ("away", "home"):
            p = r.get(side)
            if p and p.get("name"):
                what = "season" if season else "this game"
                out.append(f"{short[side]} {r['label']} leader ({what}): {p['name']}"
                           f"{', ' + p['position'] if p.get('position') else ''}: {p.get('value')}.")
    return out


def _linescores(game: dict) -> tuple[list, list]:
    hdr = game.get("header") or {}
    hl = (hdr.get("home") or {}).get("linescores") or []
    al = (hdr.get("away") or {}).get("linescores") or []
    return (al, hl) if hl and len(hl) == len(al) else ([], [])


def _breaks(al: list, hl: list) -> list[tuple[int, int, int, str | None, bool]]:
    """Per quarter break: (period, away total, home total, side leading or None, whether the lead changed hands)."""
    out = []
    at = ht = 0
    before = None
    for i, (a, h) in enumerate(zip(al, hl)):
        at, ht = at + a, ht + h
        lead = None if at == ht else ("away" if at > ht else "home")
        out.append((i, at, ht, lead, bool(lead and before and lead != before)))
        before = lead or before
    return out


def lead_changes(game: dict) -> int:
    return sum(changed for *_, changed in _breaks(*_linescores(game)))


def _scoring(game: dict, short: dict) -> list[str]:
    al, hl = _linescores(game)
    if not hl:
        return []
    out = [f"Points scored in {_period(i)}: {short['away']} {a}, {short['home']} {h}."
           for i, (a, h) in enumerate(zip(al, hl))]
    moves = []
    for i, at, ht, lead, changed in _breaks(al, hl):
        where = "Halftime" if i == 1 else f"End of {_period(i)}"
        if lead is None:
            out.append(f"{where} score: tied {at}-{ht}.")
        else:
            out.append(f"{where} score: {short['away']} {at}, {short['home']} {ht} "
                       f"({short[lead]} led by {abs(at - ht)}).")
        if changed:
            # Spelled out: the writer called the wrong break "the only lead change" (2026-09-30).
            moves.append(f"Lead change: {short[lead]} took the lead between the {_break(i - 1)} and "
                         f"{_break(i)} scores.")
    out.append(f"Times the lead changed hands between quarter breaks: {len(moves)}.")
    return out + moves


def recap_facts(game: dict) -> dict:
    """The full fact sheet for a recap and team summaries."""
    n = _names(game)
    short = {s: n[s]["short"] or n[s]["name"] for s in ("home", "away")}
    h, a = game["home"].get("score"), game["away"].get("score")
    hdr = game.get("header") or {}
    facts = [f"Final score: {n['away']['name']} {a}, {n['home']['name']} {h}"
             f"{' (neutral site)' if game.get('neutral_site') else ''}"
             f"{', at ' + game['venue'] if game.get('venue') else ''}."]
    if h is not None and a is not None and h != a:
        win, lose = ("home", "away") if h > a else ("away", "home")
        facts.append(f"{n[win]['name']} won by {abs(h - a)} points; {h + a} points were scored in all.")
    for side in ("away", "home"):
        rec = (hdr.get(side) or {}).get("record")
        if rec:
            facts.append(f"{n[side]['name']} record, as ESPN lists it after this game: {rec}.")
    if not game.get("neutral_site"):
        # Stated outright: a recap had the home team "scoring 16 points on the road" (2026-09-30).
        facts.append(f"Home team: {n['home']['name']}. Visiting team: {n['away']['name']}.")
    facts += _scoring(game, short)
    facts += _stat_lines(game, short)
    facts += _edge_lines(game, short)
    facts += _leader_lines(game, short)
    return {"teams": n, "facts": facts, "players": players(game), "stats": stats(game)}


def stats(game: dict) -> dict[str, dict]:
    """Team stat rows by ESPN key: {"possessionTime": {"home": "36:53", "away": "23:07"}, ...}."""
    return {r["key"]: {"home": r.get("home"), "away": r.get("away")}
            for r in (game.get("team_stats") or {}).get("rows") or [] if r.get("key")}


def players(game: dict) -> list[dict]:
    """Each stat leader with the numbers on their line, for the writer's code check."""
    out = []
    for r in (game.get("leaders") or {}).get("rows") or []:
        for side in ("away", "home"):
            p = r.get(side)
            if p and p.get("name"):
                out.append({"name": p["name"], "last_name": p.get("last_name"), "side": side,
                            "value": str(p.get("value") or "")})
    return out


def clock_seconds(v) -> int | None:
    """'36:53' -> 2213; anything else -> None."""
    m, _, s = str(v or "").partition(":")
    return int(m) * 60 + int(s) if m.isdigit() and s.isdigit() else None


def _edge_lines(game: dict, short: dict) -> list[str]:
    """Who had more of the ball and the yards, worked out in code: a recap said possession "favored Green Bay
    22:55 to 37:05" and another that the Saints "held the ball for over five minutes longer" (4:56; 2026-09-30)."""
    st = stats(game)
    out = []
    top = st.get("possessionTime") or {}
    h, a = clock_seconds(top.get("home")), clock_seconds(top.get("away"))
    if h is not None and a is not None and h != a:
        d = abs(h - a)
        out.append(f"Time of possession edge: {short['home' if h > a else 'away']}, by {d // 60}:{d % 60:02d}.")
    yd = st.get("totalYards") or {}
    h, a = _int(yd.get("home")), _int(yd.get("away"))
    if h is not None and a is not None and h != a:
        out.append(f"Total yards edge: {short['home' if h > a else 'away']}, by {abs(h - a)}.")
    return out


# ---------------------------------------------------------------- recap outline (M3, 2026-10-02)
# Not in production: only T3's outline arms add it to a prompt. Code picks the recap's lead angle and the FACTS lines
# that tell it, so the writer no longer has to find the story or work out which way each comparison points. The
# outline is the angle's id, a frame sentence (team names and FACTS' quarter labels: no score, count or cause) and 4-6
# lines copied exactly from recap_facts, in the order the recap should take them. Nothing in it is new, so it can't
# add a wrong fact; the risk is an angle that is true but secondary, which no checker sees (FACT_CHECK lets framing
# pass). Gated on Z5 (a person picks the lead blind: python -m app.ai.angle_eval) and T3 (python -m
# app.ai.outline_eval, which refuses to run until Z5 passes). The first Z5 run on the 16 week-4 fixture finals
# matched 11/16 (Oct 2, FAIL): in 4 of the 5 misses the labeller led with the final margin where code led with yards
# or giveaways, and Adam ruled NE @ JAX "absolutely a blowout" (Oct 2). So the margin is checked first (blowout and
# close_finish before outgained_but_lost and turnovers) and the "any yardage edge in a loss by BLOWOUT_POINTS" branch
# is gone. That scores 15/16 on the same labels, which is in-sample and proves nothing: this order is pre-registered
# for a blind label of week 5's finals (python -m app.ai.angle_fixtures, then angle_eval --fixtures). Don't retune the
# angle rules on labels already seen (lac_buf's comeback-vs-late_lead_change miss stays): label a fresh set.
#
# The angle is the first of these that holds (W the winner, L the loser, a break the end of a quarter; a tie has
# neither, so only overtime, seesaw or close_finish fits it):
#   overtime            the linescore has more than 4 periods
#   comeback            W trailed by COMEBACK_POINTS or more at a break before the last
#   late_lead_change    W didn't lead at the break before the last (trailing or tied after Q3)
#   seesaw              the lead changed hands at 2 or more breaks (lead_changes)
#   blowout             W won by BLOWOUT_POINTS or more
#   close_finish        decided by CLOSE_POINTS or less, or a tie
#   outgained_but_lost  L had OUTGAINED_YARDS more total yards
#   turnovers           one team had TURNOVER_GAP more giveaways than the other ("three giveaways against none")
#   front_runner        W led at every break
#   routine_win         none of the above
# Not angles: a defensive or special-teams score (the box score has no scoring plays), an upset (the recap's sheet
# has no line; bets are written by code) and streaks or records going in (FACTS has only the record after the game).
COMEBACK_POINTS = 10        # the comeback frame says "double digits": change both together
OUTGAINED_YARDS = 75        # the 16 week-4 finals: losers' edges of 2 to 225
BLOWOUT_POINTS = 17         # three scores
TURNOVER_GAP = 3
CLOSE_POINTS = 3
OUTLINE_MIN, OUTLINE_MAX = 4, 6
# Each angle and what it means, in the order above. Z5's labeller gets these meanings, never the rules.
ANGLES = {
    "overtime": "The game went to overtime.",
    "comeback": "The winner came back from a big deficit.",
    "late_lead_change": "The winner was behind or tied going into the fourth quarter.",
    "seesaw": "The lead went back and forth.",
    "blowout": "A lopsided win.",
    "close_finish": "A close game, decided by a few points.",
    "outgained_but_lost": "The losing team gained more yards.",
    "turnovers": "The giveaway count is the story.",
    "front_runner": "The winner led from the first quarter on.",
    "routine_win": "Nothing stands out: a plain result.",
}
LEADER_TD = re.compile(r"(\d+) TD\b")
LEADER_YDS = re.compile(r"(\d+) YDS\b")


def _first(lines: list[str], start: str) -> str | None:
    return next((f for f in lines if f.startswith(start)), None)


def _break_line(lines: list[str], i: int | None) -> str | None:
    """The sheet's score line for the break after period i ('Halftime score: ...')."""
    if i is None or i < 0:
        return None
    return _first(lines, f"{'Halftime' if i == 1 else 'End of ' + _period(i)} score:")


def _points_line(lines: list[str], i: int | None) -> str | None:
    return None if i is None or i < 0 else _first(lines, f"Points scored in {_period(i)}:")


def _top_leader(lines: list[str], short: str) -> str | None:
    """A team's leader line with the most touchdowns, then yards; the sheet's first on a tie."""
    mine = [f for f in lines if f.startswith(short + " ") and " leader (this game): " in f]
    count = lambda f: tuple(int(m[1]) if m else 0 for m in (LEADER_TD.search(f), LEADER_YDS.search(f)))
    return max(mine, key=count, default=None)


def recap_outline(game: dict, sheet: dict | None = None) -> dict | None:
    """{"angle", "frame", "lines"} for a final: the first angle above that holds, and 4-6 lines copied exactly from
    recap_facts (the angle's own lines first, then the result, each team's top leader, the giveaways when they
    differ, the yards and possession edges, the halftime score). None when the sheet can't give 4 lines."""
    sheet = sheet or recap_facts(game)
    lines = sheet["facts"]
    h, a = game["home"].get("score"), game["away"].get("score")
    if h is None or a is None:
        return None
    n = sheet["teams"]
    short = {s: n[s]["short"] or n[s]["name"] for s in ("home", "away")}
    w = None if h == a else ("home" if h > a else "away")
    lo = {"home": "away", "away": "home"}.get(w)
    margin = abs(h - a)
    al, hl = _linescores(game)
    pts = {"away": al, "home": hl}
    brks = _breaks(al, hl)
    last = len(al) - 1
    st = stats(game)
    yards = {s: _int((st.get("totalYards") or {}).get(s)) for s in ("home", "away")}
    gives = {s: _int((st.get("turnovers") or {}).get(s)) for s in ("home", "away")}
    won_by = _first(lines, f"{n[w]['name']} won by ") if w else None
    took = [f for f in lines if w and f.startswith(f"Lead change: {short[w]} ")]

    def swing(start: int) -> int | None:
        """The period from `start` on that W won by the most (the first on a tie)."""
        best = None
        for i in range(start, len(al)):
            if best is None or pts[w][i] - pts[lo][i] > pts[w][best] - pts[lo][best]:
                best = i
        return best

    # W's deficit at each break before the last: (points behind, break).
    behind = [((at - ht) if w == "home" else (ht - at), i) for i, at, ht, _, _ in brks[:-1]] if w else []
    worst = max(behind, key=lambda d: (d[0], -d[1]), default=(0, None))
    if len(al) > 4:
        pick = ("overtime", "The game went to overtime.",
                [_break_line(lines, 3)] + [_points_line(lines, i) for i in range(4, len(al))] + [won_by])
    elif worst[0] >= COMEBACK_POINTS:
        pick = ("comeback", f"{short[w]} trailed by double digits at a quarter break and won.",
                [_break_line(lines, worst[1]), _points_line(lines, swing(worst[1] + 1))] + took + [won_by])
    elif w and len(brks) >= 2 and brks[-2][3] != w:
        pick = ("late_lead_change", f"{short[w]} did not lead at the {_break(last - 1)} and won.",
                [_break_line(lines, last - 1), _points_line(lines, last)]
                + [f for f in took if f.endswith(f"and {_break(last)} scores.")] + [won_by])
    elif lead_changes(game) >= 2:
        pick = ("seesaw", "The lead changed hands more than once between quarter breaks.",
                [_first(lines, "Times the lead changed hands")] + [f for f in lines if f.startswith("Lead change: ")]
                + [won_by])
    elif w and margin >= BLOWOUT_POINTS:
        pick = ("blowout", f"{short[w]} won by a lopsided margin.",
                [won_by, _break_line(lines, 1), _points_line(lines, swing(0))])
    elif margin <= CLOSE_POINTS:
        pick = ("close_finish", f"{short[w]} won a close game." if w else "The game ended in a tie.",
                [won_by or _first(lines, "Final score:"), _break_line(lines, last - 1), _points_line(lines, last)])
    elif w and None not in yards.values() and yards[lo] - yards[w] >= OUTGAINED_YARDS:
        pick = ("outgained_but_lost", f"{short[lo]} gained more total yards and lost.",
                [_first(lines, "Total yards edge:"), _first(lines, "total yards:"), won_by])
    elif w and None not in gives.values() and abs(gives["home"] - gives["away"]) >= TURNOVER_GAP:
        t = "home" if gives["home"] > gives["away"] else "away"
        pick = ("turnovers", f"{short[t]} had more giveaways and {'still won' if t == w else 'lost'}.",
                [_first(lines, "Giveaways "), won_by])
    elif brks and all(lead == w for *_, lead, _ in brks):
        pick = ("front_runner", f"{short[w]} led at every quarter break.",
                [_break_line(lines, i) for i in range(last)] + [won_by])
    else:
        pick = ("routine_win", f"{short[w]} won, and no swing stands out: open with the result.",
                [won_by, _break_line(lines, 1)])
    angle, frame, own = pick
    sides = (w, lo) if w else ("away", "home")
    fill = [won_by] + [_top_leader(lines, short[s]) for s in sides] + [
        _first(lines, "Giveaways ") if None not in gives.values() and gives["home"] != gives["away"] else None,
        _first(lines, "Total yards edge:"), _first(lines, "Time of possession edge:"), _break_line(lines, 1)]
    out = []
    for f in own + fill:
        if f and f not in out and len(out) < OUTLINE_MAX:
            out.append(f)
    return {"angle": angle, "frame": frame, "lines": out} if len(out) >= OUTLINE_MIN else None


# The live one-liner's sheet holds only what its prompt (prompts.ONE_LINER) and Adam's examples can use (M4,
# 2026-10-02): the score, who was favored, the lead, points per quarter, the team stats the prompt names, and the
# passers' lines (the only place a live box score counts interceptions: "Two picks already"). Left out: time of
# possession, penalties, the rushing and receiving leaders, the over/under (betting words are banned in the line) and
# who has the ball. The writer and the fact-checker get this same sheet; claims_ok also gets every leader on the box
# score (writer.write_one_liner), so a player handed a team total is still refused in code.
LIVE_STATS = ("totalYards", "netPassingYards", "rushingYards", "turnovers", "thirdDownEff")
LIVE_LEADERS = ("passingYards",)


def _only(block: dict | None, keys: tuple[str, ...]) -> dict:
    """A team-stats or leaders block with only the rows whose key is in `keys`."""
    block = block or {}
    return dict(block, rows=[r for r in block.get("rows") or [] if r.get("key") in keys])


def live_facts(game: dict) -> dict:
    """The fact sheet for the live one-liner: the game so far, nothing about how it will end, trimmed to the hooks
    its prompt uses (LIVE_STATS, LIVE_LEADERS)."""
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
    hdr = game.get("header") or {}
    hl = (hdr.get("home") or {}).get("linescores") or []
    al = (hdr.get("away") or {}).get("linescores") or []
    if hl and len(hl) == len(al):
        # ESPN's last linescore is the quarter being played, so it is labelled as unfinished.
        for i, (x, y) in enumerate(zip(al, hl)):
            done = "" if i < len(hl) - 1 else " (in progress)"
            facts.append(f"Points in {_period(i)}{done}: {short['away']} {x}, {short['home']} {y}.")
    facts += [f"So far, {line[0].lower()}{line[1:]}" for line in _stat_lines(game, short)]
    facts += [line.replace("(this game)", "(so far)") for line in _leader_lines(game, short)]
    return {"teams": n, "facts": facts, "players": players(game), "stats": stats(game)}


def _kickoff(game: dict) -> str | None:
    try:
        t = datetime.fromisoformat(game["start_time"]).astimezone(ET)
    except (KeyError, TypeError, ValueError):
        return None
    if game.get("time_valid") is False:
        return f"{t:%A, %B} {t.day}, time to be announced"
    return f"{t:%A, %B} {t.day}, {t:%I:%M %p}".replace(" 0", " ") + " ET"


def _line(game: dict, n: dict, total: bool = True) -> list[str]:
    """The point spread, and the over/under unless total is False (the live one-liner can't use it)."""
    ln = game.get("line") or {}
    out = []
    sp = ln.get("home_spread")
    if sp is not None:
        sp = float(sp)
        if sp == 0:
            out.append("Point spread: pick'em (neither team favored).")
        else:
            fav = "home" if sp < 0 else "away"
            out.append(f"Point spread: {n[fav]['name']} favored by {abs(sp):g} points"
                       f"{' (' + ln['provider'] + ')' if ln.get('provider') else ''}.")
    if total and ln.get("total") is not None:
        out.append(f"Over/under total: {float(ln['total']):g} points.")
    return out


def _injury_lines(game: dict, short: dict) -> list[str]:
    inj = game.get("injuries") or {}
    out = []
    for side in ("away", "home"):
        for p in inj.get(side) or []:
            if p.get("name") and p.get("status"):
                why = f" ({p['detail']})" if p.get("detail") else ""
                out.append(f"{short[side]} injury: {p['name']}"
                           f"{', ' + p['position'] if p.get('position') else ''}, {p['status']}{why}.")
    return out


def preview_facts(game: dict) -> dict:
    """Our own data for an upcoming game. Articles are extracted separately."""
    n = _names(game)
    short = {s: n[s]["short"] or n[s]["name"] for s in ("home", "away")}
    hdr = game.get("header") or {}
    where = " (neutral site)" if game.get("neutral_site") else ""
    facts = [f"Matchup: {_label(n['away'])} at {_label(n['home'])}{where}"
             f"{', ' + game['venue'] if game.get('venue') else ''}."]
    k = _kickoff(game)
    if k:
        facts.append(f"Kickoff: {k}{', on ' + game['broadcast'] if game.get('broadcast') else ''}.")
    for side in ("away", "home"):
        rec = (hdr.get(side) or {}).get("record")
        if rec:
            facts.append(f"{n[side]['name']} record: {rec}.")
    facts += _line(game, n)
    facts += _stat_lines(game, short)
    facts += _leader_lines(game, short)
    facts += _injury_lines(game, short)
    return {"teams": n, "facts": facts}
