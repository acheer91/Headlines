"""Z5's fresh set (Oct 2): one NFL week's regular-season finals as recap fixtures, plus a blind label sheet. Zero
tokens and no database: ESPN's public scoreboard and game summaries, read and parsed by the app's own code.

    python -m app.ai.angle_fixtures --season 2026 --week 4 --out <dir> [--labels-sheet <file>]
    python -m app.ai.angle_eval --fixtures <dir> --labels <labels.json>        # once the blind labeller replies

--week is ESPN's regular-season week (season type 2). The 16 finals in tests/fixtures/ai (Sep 24-28) are ESPN's
week 3, though the Oct 2 notes call them "week 4"; the weekend of Oct 1-5 (the notes' "week 5") is ESPN's week 4.

Writes into --out, which may not be under tests/ (the angle rules were tuned on the finals there):
  final_<away>_<home>.json  one per final (lowercase ESPN abbreviations) in the shape of tests/fixtures/ai, the
                            /api/games/{id} payload: games.card and games.detail on the game's ESPN summary, the game
                            moved forward by that summary and graded on ESPN's closing line, as a page pull does.
                            "id" is None: no database row is read.
  manifest.json             season, week, when, facts.py's sha256, each final's ESPN event id and file sha256, and
                            the games skipped.
  labels_sheet.md           (or --labels-sheet <file>) the blind sheet: the allowed angle ids with their meanings
                            (angle_eval.angles_file) and each final's recap fact sheet (facts.recap_facts). Never
                            code's angle, its outline or the rule order.
Games that aren't final (upcoming, live, canceled, postponed, or a summary that hasn't caught up) are skipped and
listed: run it after Monday night's game. A scoreboard event that doesn't parse, a summary ESPN won't send, or an
--out already holding final_*.json for other games stops the run before anything is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .. import espn, favorites, games, grading, summary
from . import angle_eval, facts

LEAGUE = "nfl"
REGULAR_SEASON = 2
TESTS = Path(__file__).resolve().parents[2] / "tests"
SHEET = "labels_sheet.md"
MANIFEST = "manifest.json"


class _BetResults:
    """Stands in for the database in games.detail, whose one read for a completed final is
    db.bet_results_for_game: it answers that with the results graded here and refuses any other query, so a new
    read in detail() fails loudly instead of being handed bet rows."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def execute(self, sql: str, params=None):
        if "FROM bet_results" not in sql:
            raise RuntimeError(f"angle_fixtures reads no database: {' '.join(sql.split())[:80]}")
        return self

    def fetchall(self) -> list[dict]:
        return self.rows


def _row(g: espn.Game) -> dict:
    """The game as db.game_by_id returns it after a scoreboard pull (db._GAMES_SQL's columns), with no database id.
    A line on the scoreboard is the newest odds snapshot that pull would have saved."""
    o = g.odds
    row = {"id": None, "league": g.league, "espn_id": g.espn_id, "start_time": g.start_time, "state": g.state,
           "status_detail": g.status_detail, "period": g.period, "clock": g.clock, "home_score": g.home_score,
           "away_score": g.away_score, "venue": g.venue, "broadcast": g.broadcast, "season": g.season,
           "week": g.week, "season_type": g.season_type, "completed": g.completed, "time_valid": g.time_valid,
           "home_conf": g.home_conf, "away_conf": g.away_conf, "home_rank": g.home_rank, "away_rank": g.away_rank,
           "neutral_site": g.neutral_site, "odds_state": g.state if o else None}
    for k in ("provider", "details"):
        row[f"odds_{k}"] = getattr(o, k) if o else None
    for k in ("home_spread", "total", "home_ml", "away_ml"):
        row[k] = getattr(o, k) if o else None
    for side, t in (("home", g.home), ("away", g.away)):
        row |= {f"{side}_espn_id": t.espn_id, f"{side}_abbr": t.abbr, f"{side}_name": t.name,
                f"{side}_short": t.short_name, f"{side}_logo": t.logo_url, f"{side}_color": t.color}
    return row


def _moved_forward(row: dict, st: dict) -> dict:
    """What games.refresh_summary's db.update_game_status does with the summary's status: never backwards (a
    completed final stays post, a live game never goes back to pre), and a missing score never blanks one."""
    if not st.get("state") or (row["completed"] and st["state"] != "post") or (
            row["state"] == "in" and st["state"] == "pre"):
        return row
    return dict(row, state=st["state"], status_detail=st.get("short_detail") or st.get("detail"),
                period=st.get("period"), clock=st.get("clock"), completed=bool(st.get("completed")),
                home_score=row["home_score"] if st.get("home_score") is None else st["home_score"],
                away_score=row["away_score"] if st.get("away_score") is None else st["away_score"])


def _json(v):
    """As the api (FastAPI) encodes what is left; games.detail already turns its times and margins into strings and
    floats."""
    if isinstance(v, Decimal):
        return int(v) if v.as_tuple().exponent >= 0 else float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    raise TypeError(f"not JSON: {type(v).__name__}")


def payload(g: espn.Game, summ: dict, favs: set[str], now: datetime) -> dict:
    """The game page's payload for a scoreboard game and its ESPN summary, as GET /api/games/{id} returns it."""
    st = summary.status(summ)
    if st.get("event_id") and st["event_id"] != str(g.espn_id):
        raise espn.ESPNError(f"summary for {st['event_id']} returned for game {g.espn_id}")
    row = _moved_forward(_row(g), st)
    o = g.odds
    snaps = [] if o is None else [{"id": 1, "captured_at": now, "game_state": g.state, "provider": o.provider,
                                   "home_spread": o.home_spread, "total": o.total, "home_ml": o.home_ml,
                                   "away_ml": o.away_ml}]
    bets = []
    if row["state"] == "post" and row["completed"] and None not in (row["home_score"], row["away_score"]):
        lines = grading.choose_lines(snaps, summary.closing_line(summ))
        for market, r in grading.grade(row["home_score"], row["away_score"], lines).items():   # games.grade_game
            ln = lines[market]
            bets.append({"market": market, "line_source": ln.source, "provider": ln.provider,
                         "home_spread": ln.home_spread, "total": ln.total, "home_ml": ln.home_ml,
                         "away_ml": ln.away_ml, "home_score": row["home_score"], "away_score": row["away_score"],
                         "outcome": r.outcome, "margin": r.margin})
    stored = {"payload": summ, "fetched_at": now, "game_state": st.get("state") or row["state"]}
    out = games.detail(_BetResults(sorted(bets, key=lambda b: b["market"])), games.card(row, favs), stored,
                       stale=False, error=None)
    return json.loads(json.dumps(out, default=_json))


def fixture_name(g: espn.Game) -> str:
    return f"final_{g.away.abbr.lower()}_{g.home.abbr.lower()}"


def week_finals(season: int, week: int, now: datetime,
                base_url: str = espn.BASE) -> tuple[dict[str, tuple[str, dict]], list[dict]]:
    """({fixture name: (ESPN event id, payload)} for the week's finals, [{game, espn_id, why}] for the games
    skipped). Raises ValueError or espn.ESPNError when the week can't be read whole."""
    board = espn.fetch_scoreboard(LEAGUE, week=week, season_type=REGULAR_SEASON, dates=str(season),
                                  base_url=base_url)
    parsed, errors = espn.parse_scoreboard(board, LEAGUE)
    if errors:
        raise ValueError(f"scoreboard events that don't parse (a final could be missing): {errors}")
    if not parsed:
        raise ValueError(f"ESPN lists no games for {season} week {week}")
    seen = sorted({(g.season, g.week, g.season_type) for g in parsed} - {(season, week, REGULAR_SEASON)},
                  key=str)
    if seen:
        raise ValueError(f"ESPN sent games of (season, week, season type) {seen}, not ({season}, {week}, "
                         f"{REGULAR_SEASON})")
    favs = set(favorites.load().get(LEAGUE, []))
    made, skipped = {}, []
    for g in sorted(parsed, key=lambda g: (g.start_time, g.espn_id)):
        game = f"{g.away.abbr} @ {g.home.abbr}"
        if g.state != "post" or not g.completed:
            why = "not played" if g.state == "post" else "not final"         # post but not completed: canceled
            skipped.append({"game": game, "espn_id": g.espn_id, "why": f"{why} ({g.status_detail or g.state})"})
            continue
        p = payload(g, espn.fetch_summary(LEAGUE, g.espn_id, base_url=base_url), favs, now)
        if p["state"] != "post" or not p["completed"] or p["summary_behind"]:
            skipped.append({"game": game, "espn_id": g.espn_id, "why": "ESPN's summary isn't final yet: run again"})
            continue
        name = fixture_name(g)
        if name in made:
            raise ValueError(f"two finals would both be {name}.json")
        made[name] = (g.espn_id, p)
    return made, skipped


def sheet_text(season: int, week: int, fixtures: Path) -> str:
    """The blind label sheet for the finals in `fixtures`: the allowed ids and meanings, the reply format, and each
    final's recap fact sheet, read back from the files angle_eval will score."""
    sent = angle_eval.angles_file(fixtures)
    ids = [a["id"] for a in sent["angles"]]
    reply = json.dumps({"angles": ids} | {name: "<one id>" for name in sent["fixtures"]}, indent=1)
    md = [f"# Z5 blind label sheet: NFL {season}, regular-season week {week}", "",
          "For each final below, pick the ONE angle that is its lead story: what a recap of the game should open "
          "with. Judge from its fact sheet alone. Don't look the game up, and don't open the app's code or any "
          "other file.", "", "## Angles (alphabetical)", ""]
    md += [f"- `{a['id']}`: {a['meaning']}" for a in sent["angles"]]
    md += ["", "## Reply", "", "A JSON file with the allowed ids and one id per final:", "", "```json", reply, "```"]
    for name, game in angle_eval.finals(fixtures).items():
        where = "vs" if game.get("neutral_site") else "at"
        md += ["", f"## {name}: {game['away']['name']} {where} {game['home']['name']}", ""]
        md += [f"- {line}" for line in facts.recap_facts(game)["facts"]]
    return "\n".join(md) + "\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(season: int, week: int, out: Path, sheet: Path | None = None, now: datetime | None = None,
          base_url: str = espn.BASE) -> dict:
    """Fetch the week, write the fixtures, manifest and sheet; returns the manifest. Raises ValueError (nothing
    written) for an --out under tests/ or holding other games' fixtures, or a week that can't be read whole."""
    now = now or datetime.now(timezone.utc)
    if out.resolve().is_relative_to(TESTS.resolve()):
        raise ValueError(f"{out} is under tests/: fresh fixtures stay out of the set the rules were tuned on")
    made, skipped = week_finals(season, week, now, base_url)
    strays = sorted(p.name for p in out.glob("final_*.json") if p.stem not in made) if out.exists() else []
    if strays:
        raise ValueError(f"{out} already holds other games' fixtures ({', '.join(strays)}): use an empty folder")
    out.mkdir(parents=True, exist_ok=True)
    finals = []
    for name, (espn_id, p) in made.items():
        path = out / f"{name}.json"
        path.write_text(json.dumps(p, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        finals.append({"fixture": name, "espn_id": espn_id, "game": f"{p['away']['abbr']} @ {p['home']['abbr']}",
                       "sha256": sha256(path)})
    sheet = sheet or out / SHEET
    if made:
        sheet.write_text(sheet_text(season, week, out), encoding="utf-8")
    manifest = {"season": season, "week": week, "season_type": REGULAR_SEASON, "built_at": now.isoformat(),
                "facts_py_sha256": sha256(Path(facts.__file__)), "sheet": str(sheet) if made else None,
                "finals": finals, "skipped": skipped}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--season", type=int, required=True, help="e.g. 2026")
    ap.add_argument("--week", type=int, required=True, help="ESPN's regular-season week")
    ap.add_argument("--out", type=Path, required=True, help="folder for the fixtures (not under tests/)")
    ap.add_argument("--labels-sheet", type=Path, help=f"where the blind sheet goes (default <out>/{SHEET})")
    args = ap.parse_args(argv)
    espn.disable_down_switch()              # a batch: an ESPN hiccup retries instead of skipping the host
    try:
        m = build(args.season, args.week, args.out, args.labels_sheet)
    except (OSError, ValueError, espn.ESPNError) as exc:
        print(f"angle_fixtures: {exc}", file=sys.stderr)
        return 2
    print(f"NFL {m['season']} week {m['week']}: {len(m['finals'])} finals written to {args.out}")
    for f in m["finals"]:
        print(f"  {f['fixture']}.json  {f['game']}  (ESPN {f['espn_id']})")
    for s in m["skipped"]:
        print(f"  skipped {s['game']} (ESPN {s['espn_id']}): {s['why']}")
    if not m["finals"]:
        print("no finals yet: nothing to label")
        return 1
    print(f"blind label sheet: {m['sheet']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
