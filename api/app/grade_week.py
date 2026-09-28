"""Grade every final game in a week and print a table to hand-check (step 2.7).

    python -m app.grade_week --week 2
    python -m app.grade_week --week 1 --season-type 3     # playoffs: Wild Card round

Pulls the week's scoreboard, fetches each final game's summary (once; finals are kept), grades
ML, spread and O/U against the line the rules pick (per market: ESPN's close, else our saved
pre-game line, else the newest line), stores one result per market, and prints the
line, final score and results. The 'check' column shows the arithmetic so each row can be verified
by hand. Exit 1 if any final game has an ungraded market.
"""
import argparse
import sys
from decimal import Decimal

from . import db, espn, games, grading, summary

SRC = {"pre_game": "pre-game", "espn_close": "ESPN close", "in_game": "IN-GAME"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--season-type", type=int, choices=[1, 2, 3],
                    help="1 preseason, 2 regular season, 3 postseason (default: ESPN's current)")
    ap.add_argument("--league", default="nfl", choices=["nfl"])
    a = ap.parse_args()
    espn.disable_down_switch()

    payload = espn.fetch_scoreboard(a.league, week=a.week, season_type=a.season_type)
    parsed, errors = espn.parse_scoreboard(payload, a.league)
    for e in errors:
        print("PARSE ERROR", e)
    if not parsed:
        print(f"No games for week {a.week}")
        return 1
    season, week, season_type = parsed[0].season, parsed[0].week, parsed[0].season_type

    rows, ungraded, not_final, not_played = [], 0, 0, []
    with db.connect() as conn:
        db.save_games(conn, parsed)
        not_final = sum(1 for g in parsed if g.state != "post")
        for game_id in db.final_game_ids(conn, a.league, season, week, season_type):
            g = db.game_by_id(conn, game_id)
            stored = db.get_summary(conn, game_id)
            if games.needs_refresh(g, stored):
                ok, err = games.refresh_summary(conn, g)
                if not ok:
                    print(f"  summary fetch failed for {g['away_abbr']} @ {g['home_abbr']}: {err}")
                stored = db.get_summary(conn, game_id)
            graded = games.grade_game(conn, game_id)
            g = db.game_by_id(conn, game_id)          # the summary may have updated it
            if not g["completed"]:
                not_played.append(f"{g['away_abbr']} @ {g['home_abbr']} ({g['status_detail'] or 'not played'})")
                continue
            results, lines = graded if graded else ({}, {})
            ungraded += len(grading.MARKETS) - len(results)
            # For the hand check: flag when a line we saved before kickoff differs from ESPN's close
            # (the close is what's graded; a big gap means the line moved late).
            note = ""
            pre = [s for s in db.snapshots_for_game(conn, game_id) if s["game_state"] == "pre"]
            sp = lines.get("spread")
            if pre and sp is not None and sp.source == "espn_close" and pre[-1]["home_spread"] is not None \
                    and Decimal(pre[-1]["home_spread"]) != sp.home_spread:
                note = f"saved pre-game {grading.fmt_signed(pre[-1]['home_spread'])}"
            # The final should equal the box score's quarters; flag it if not.
            if stored:
                t = summary.teams(stored["payload"])
                for side, score in (("home", g["home_score"]), ("away", g["away_score"])):
                    qs = t.get(side, {}).get("linescores") or []
                    if qs and None not in qs and sum(qs) != score:
                        note += f" {side} quarters sum {sum(qs)} != {score}"
            rows.append((g, results, lines, note.strip()))

    home_w = max(len(r[0]["home_abbr"]) for r in rows) if rows else 3
    kind = {1: "preseason ", 3: "postseason "}.get(season_type, "")
    print(f"NFL {season} {kind}week {week}: {len(rows)} finals graded"
          + (f", {not_final} not final yet (skipped)" if not_final else "")
          + (f", not played (never graded): {', '.join(not_played)}" if not_played else ""))
    print()
    hdr = f"{'Game':<{9 + home_w}} {'Final':<9} {'Line (source)':<34} {'ML':<14} {'Spread':<24} {'Total':<20} Check"
    print(hdr)
    print("-" * len(hdr))
    for g, results, lines, note in rows:
        home, away = g["home_abbr"], g["away_abbr"]
        hs, as_ = g["home_score"], g["away_score"]
        game = f"{away:>4} @ {home:<{home_w}}"
        final = f"{as_}-{hs}"
        if not lines:
            print(f"{game:<{9 + home_w}} {final:<9} {'NO LINE':<34} {'Ungraded':<14} {'Ungraded':<24} {'Ungraded':<20}")
            continue
        sp, tot = lines.get("spread"), lines.get("total")
        sources = sorted({SRC[ln.source] for ln in lines.values()})
        line_txt = (f"{grading.spread_label(sp.home_spread, home, away) if sp else '–'} · "
                    f"O/U {grading.fmt_num(tot.total) if tot else '–'} ({'/'.join(sources)})")

        def cell(m):
            r = results.get(m)
            return grading.describe(r, lines[m], home, away) if r else "Ungraded"

        check = []
        if sp is not None:
            op = "-" if sp.home_spread < 0 else "+"
            check.append(f"{hs - as_:+d} {op} {grading.fmt_num(abs(sp.home_spread))} = "
                         f"{grading.fmt_signed(Decimal(hs - as_) + sp.home_spread)}")
        if tot is not None:
            check.append(f"{hs + as_} - {grading.fmt_num(tot.total)} = {grading.fmt_signed(Decimal(hs + as_) - tot.total)}")
        print(f"{game:<{9 + home_w}} {final:<9} {line_txt:<34} {cell('moneyline'):<14} {cell('spread'):<24} "
              f"{cell('total'):<20} {'  '.join(check)}{'  ' + note if note else ''}")

    print()
    print("Final score is away-home. Check: home margin + home spread (>0 home covers, <0 away covers, 0 push);"
          " total points - O/U (>0 over, <0 under, 0 push). Finals are also checked against the quarter scores.")
    if ungraded:
        print(f"FAIL: {ungraded} ungraded market(s)")
        return 1
    print(f"PASS: all {len(rows)} finals graded on every market")
    return 0


if __name__ == "__main__":
    sys.exit(main())
