"""Validation helper (step 2.1): hit live ESPN summaries for one upcoming, one live and one final
game and print which blocks came back. No database needed.

    python -m app.check_summary                 # picks games from this week (and last week for a final)
    python -m app.check_summary --event 401872953
"""
import argparse
import sys

from . import espn, summary

# Blocks each screen needs. 'required' ones fail the check; 'optional' ones are reported only
# (a line can be missing a week out; possession is missing between drives).
REQUIRED = {
    "pre": ["teams", "records", "season stats", "season leaders", "injuries"],
    "in": ["teams", "records", "score by quarter", "game stats", "game leaders", "injuries"],
    "post": ["teams", "records", "score by quarter", "game stats", "game leaders", "injuries"],
}
OPTIONAL = {"pre": ["current line"], "in": ["closing line", "situation"], "post": ["closing line"]}


def blocks(p: dict) -> dict[str, str | None]:
    """Block name -> short description, or None when ESPN didn't send it."""
    t = summary.teams(p)
    sides = [t.get("away") or {}, t.get("home") or {}]
    ss, gs = summary.team_stats(p, "season"), summary.team_stats(p, "game")
    sl, gl = summary.leaders(p, "season"), summary.leaders(p, "game")
    inj = p.get("injuries")
    close, cur, sit = summary.closing_line(p), summary.current_line(p), summary.situation(p)

    def line_text(ln):
        return ln and f"{ln.get('provider')}: home {ln.get('home_spread')} / O/U {ln.get('total')} / ML {ln.get('home_ml')} {ln.get('away_ml')}"

    return {
        "teams": " @ ".join(s.get("abbr") or "?" for s in sides) if all(s.get("abbr") for s in sides) else None,
        "records": " / ".join(s.get("record") or "?" for s in sides) if all(s.get("record") for s in sides) else None,
        "score by quarter": " / ".join(",".join(map(str, s.get("linescores") or [])) for s in sides)
        if all(s.get("linescores") for s in sides) else None,
        "season stats": f"{len(ss)} rows" if ss else None,
        "game stats": f"{len(gs)} rows" if len(gs) >= 5 else None,
        "season leaders": ", ".join(r["label"] for r in sl) if len(sl) >= 3 else None,
        "game leaders": ", ".join(r["label"] for r in gl) if len(gl) >= 3 else None,
        "injuries": (f"{sum(len(v) for v in summary.injuries(p).values())} listed" if isinstance(inj, list) else None),
        "current line": line_text(cur),
        "closing line": line_text(close),
        "situation": sit and f"{sit.get('possession')} ball, {sit.get('down_distance')}",
    }


def check(event_id: str, league: str = "nfl") -> bool:
    p = espn.fetch_summary(league, event_id)
    st = summary.status(p)
    state = st.get("state") or "?"
    b = blocks(p)
    print(f"\n{state.upper():<4} event {event_id}  {b['teams']}  {st.get('short_detail') or ''}")
    ok = state in REQUIRED
    for name in REQUIRED.get(state, []) + OPTIONAL.get(state, []):
        req = name in REQUIRED.get(state, [])
        val = b[name]
        mark = "ok  " if val else ("MISS" if req else "none")
        print(f"  {mark} {name:<16} {val or ''}")
        ok = ok and (bool(val) or not req)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="nfl", choices=sorted(espn.LEAGUES))
    ap.add_argument("--event", action="append", help="ESPN event id (repeatable)")
    a = ap.parse_args()
    espn.disable_down_switch()

    events: dict[str, str] = {}
    if a.event:
        events = {f"arg{i}": e for i, e in enumerate(a.event)}
    else:
        sb = espn.fetch_scoreboard(a.league)
        games, _ = espn.parse_scoreboard(sb, a.league)
        for g in games:
            events.setdefault(g.state, g.espn_id)
        if "post" not in events and games and games[0].week and games[0].week > 1:
            prev, _ = espn.parse_scoreboard(espn.fetch_scoreboard(a.league, week=games[0].week - 1), a.league)
            post = next((g for g in prev if g.state == "post"), None)
            if post:
                events["post"] = post.espn_id

    results = {k: check(e, a.league) for k, e in events.items()}
    missing = [s for s in ("pre", "in", "post") if s not in events] if not a.event else []
    for s in missing:
        print(f"\nSKIP {s}: no {s} game on ESPN right now")
    ok = bool(results) and all(results.values())
    print(f"\n{'PASS' if ok else 'FAIL'}: {sum(results.values())}/{len(results)} games returned every required block"
          + (f" (no {', '.join(missing)} game to check right now)" if missing else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
