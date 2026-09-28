"""Validation helper: hit live ESPN, parse, print what we got. No database needed.

    python -m app.check_espn            # NFL, current week
    python -m app.check_espn --week 3   # a past week (finals)
"""
import argparse
import sys

from . import espn


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--league", default="nfl", choices=sorted(espn.LEAGUES))
    p.add_argument("--week", type=int)
    a = p.parse_args()

    payload = espn.fetch_scoreboard(a.league, week=a.week)
    games, errors = espn.parse_scoreboard(payload, a.league)
    raw = len(payload.get("events") or [])
    print(f"{a.league.upper()} season {games[0].season if games else '?'} week {games[0].week if games else '?'}: "
          f"{raw} events from ESPN, {len(games)} parsed, {len(errors)} errors\n")
    for g in sorted(games, key=lambda g: g.start_time):
        score = "" if g.state == "pre" else f"{g.away_score}-{g.home_score}"
        o = g.odds
        line = "no line" if not o else (
            f"{o.provider or '?'}: home {o.home_spread:+} / O/U {o.total} / ML {o.home_ml} {o.away_ml}"
            if o.home_spread is not None else f"{o.provider or '?'}: {o.details} / O/U {o.total}")
        print(f"{g.start_time:%a %m/%d %H:%MZ}  {g.away.abbr:>4} @ {g.home.abbr:<4} {g.state:<4} {score:<7} {g.status_detail or '':<24} {line}")
    for e in errors:
        print("ERROR", e)
    ok = raw > 0 and len(games) == raw
    print("\nPASS" if ok else "\nFAIL: not every ESPN event parsed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
