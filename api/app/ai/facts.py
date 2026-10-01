"""Fact sheets built by code from the game payload (/api/games/{id}), so the model never has to read raw data.

Every error in the Sep 29 samples came from the model misreading raw data: "Turnovers: BAL 0, DAL 1" became
"the Ravens forced no turnovers"; a quarter's score landed in the wrong quarter. Here each fact is one plain,
labelled sentence with team names attached, and anything that needs arithmetic (margins, halftime, who led,
takeaways) is computed in code.

A recap's facts come only from here (no model extract step). A preview's game facts come from here; the model
extracts only from the articles.
"""
from __future__ import annotations

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


def _scoring(game: dict, short: dict) -> list[str]:
    hdr = game.get("header") or {}
    hl = (hdr.get("home") or {}).get("linescores") or []
    al = (hdr.get("away") or {}).get("linescores") or []
    if not hl or len(hl) != len(al):
        return []
    out = [f"Points scored in {_period(i)}: {short['away']} {a}, {short['home']} {h}."
           for i, (a, h) in enumerate(zip(al, hl))]
    ht = hs = 0
    leader_before = None
    changes = 0
    moves = []
    for i, (a, h) in enumerate(zip(al, hl)):
        ht, hs = ht + a, hs + h
        where = "Halftime" if i == 1 else f"End of {_period(i)}"
        if ht == hs:
            out.append(f"{where} score: tied {ht}-{hs}.")
            lead = None
        else:
            lead = "away" if ht > hs else "home"
            out.append(f"{where} score: {short['away']} {ht}, {short['home']} {hs} "
                       f"({short[lead]} led by {abs(ht - hs)}).")
        if lead and leader_before and lead != leader_before:
            changes += 1
            # Spelled out: the writer called the wrong break "the only lead change" (2026-09-30).
            moves.append(f"Lead change: {short[lead]} took the lead between the {_break(i - 1)} and "
                         f"{_break(i)} scores.")
        leader_before = lead or leader_before
    out.append(f"Times the lead changed hands between quarter breaks: {changes}.")
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


def live_facts(game: dict) -> dict:
    """The fact sheet for the live one-liner: the game so far, nothing about how it will end."""
    n = _names(game)
    short = {s: n[s]["short"] or n[s]["name"] for s in ("home", "away")}
    h, a = game["home"].get("score"), game["away"].get("score")
    facts = [f"Live, {game.get('status_detail') or 'in progress'}: {n['away']['name']} {a}, {n['home']['name']} {h}."]
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
    sit = game.get("situation") or {}
    if sit.get("possession"):
        side = next((s for s in ("home", "away") if n[s]["abbr"] == sit["possession"]), None)
        who = n[side]["name"] if side else sit["possession"]
        facts.append(f"{who} has the ball{', ' + sit['down_distance'] if sit.get('down_distance') else ''}.")
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


def _line(game: dict, n: dict) -> list[str]:
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
    if ln.get("total") is not None:
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
