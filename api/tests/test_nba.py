"""Phase 5b: NBA scores only. ESPN's NBA feed has no weeks, so the board is one day (`?date=YYYY-MM-DD`, none = today
as ESPN sees it), read back by the game's US Eastern date. No lines, no grading, no workflows; the game page is a box score (test_nba_box.py).

Fixtures are ESPN's real NBA preseason scoreboards captured 2026-10-15 (Oct 13: five finals, one in overtime, one at a
neutral site that ended after 10 PM Eastern; "today": two games not started, with a DraftKings line that must be ignored),
cut down to what the parser reads. Tests using the `client` fixture need TEST_DATABASE_URL; the rest need nothing."""
import copy
import json
import os
from datetime import date
from pathlib import Path

import pytest

from app import espn

FIX = Path(__file__).parent / "fixtures"
FINALS = json.loads((FIX / "nba_scoreboard_final.json").read_text())      # ESPN's Oct 13, asked with ?dates=
TODAY = json.loads((FIX / "nba_scoreboard_today.json").read_text())       # asked without a date: has a "day" block
TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")


# ---------------- parser ----------------

def test_nba_day_parses_without_a_week():
    games, errors = espn.parse_scoreboard(copy.deepcopy(FINALS), "nba")
    assert errors == [] and len(games) == 5
    assert {g.week for g in games} == {None}                       # ESPN sends no week for the NBA
    assert {(g.season, g.season_type) for g in games} == {(2027, 1)}   # read from each event: no top-level season
    assert all(g.state == "post" and g.completed for g in games)
    assert [g.status_detail for g in games].count("Final/OT") == 1


def test_nba_has_no_lines_even_when_espn_lists_one():
    # The 'today' capture carries a DraftKings line on both games; this phase is scores only.
    raw = [ev["competitions"][0].get("odds") for ev in TODAY["events"]]
    assert all(raw), "the fixture should carry a line for this test to mean anything"
    games, _ = espn.parse_scoreboard(copy.deepcopy(TODAY), "nba")
    assert len(games) == 2 and all(g.odds is None for g in games)
    assert all("no line" not in g.warnings for g in games)
    assert all(g.state == "pre" and g.home_score is None for g in games)


def test_football_keeps_its_lines():
    assert espn.BETTING_LEAGUES == {"nfl", "ncaaf"} and espn.DATE_LEAGUES == {"nba"}


def test_fetch_scoreboard_passes_the_date(monkeypatch):
    seen = {}
    monkeypatch.setattr(espn, "_get_json", lambda url, params, **kw: seen.update(url=url, params=params) or {})
    espn.fetch_scoreboard("nba", dates="20261013")
    assert seen["url"].endswith("/basketball/nba/scoreboard") and seen["params"] == {"dates": "20261013"}
    espn.fetch_scoreboard("nba")
    assert seen["params"] == {}                                      # no date: ESPN picks its current day


# ---------------- API (database) ----------------

@pytest.fixture()
def nba_client(client, monkeypatch):
    """The shared `client` with NBA enabled and ESPN answering per requested date."""
    from app import main
    monkeypatch.setattr(main, "ENABLED_LEAGUES", ["nfl", "ncaaf", "nba"])
    asked = []
    pages = {"20261013": FINALS}
    football = main.espn.fetch_scoreboard         # the shared fixture's stub still answers NFL and NCAAF

    def fake_fetch(league, week=None, dates=None, **kw):
        if league != "nba":
            return football(league, week=week, **kw)
        client.calls["n"] += 1
        asked.append(dates)
        if client.calls["fail"]:
            raise RuntimeError("ESPN down")
        if dates is None:
            return copy.deepcopy(TODAY)
        return copy.deepcopy(pages.get(dates, {"events": []}))      # a day ESPN has nothing for: no events

    monkeypatch.setattr(main.espn, "fetch_scoreboard", fake_fetch)
    client.asked, client.pages = asked, pages
    return client


def _q(sql, *args):
    import psycopg
    with psycopg.connect(TEST_DB) as conn:
        return conn.execute(sql, args).fetchall()


@needs_db
def test_day_board_for_a_date(nba_client):
    r = nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-10-13" and body["week"] is None and body["calendar"] == []
    assert body["stale"] is False and body["season"] == 2027 and body["season_type"] == 1
    assert nba_client.asked == ["20261013"]
    games = body["games"]
    assert len(games) == 5 and [g["start_time"] for g in games] == sorted(g["start_time"] for g in games)
    assert all(g["state"] == "post" and g["line"] is None and g["league"] == "nba" for g in games)
    assert sum(g["neutral_site"] for g in games) == 1                 # GS vs LAL is at a neutral site; stored, not shown
    assert any(g["status_detail"] == "Final/OT" for g in games)
    assert _q("select count(*) from odds_snapshots")[0][0] == 0


@needs_db
def test_the_late_game_is_on_its_eastern_day(nba_client):
    # GS vs LAL tipped at 02:00 UTC on the 14th = 10 PM Eastern on the 13th: ESPN lists it under the 13th, so do we.
    body = nba_client.get("/api/scoreboard/nba?date=2026-10-13").json()
    assert any(g["start_time"].startswith("2026-10-14T02:00") for g in body["games"])
    assert nba_client.get("/api/scoreboard/nba?date=2026-10-14").json()["games"] == []


@needs_db
def test_no_date_means_the_day_espn_answers_for(nba_client):
    body = nba_client.get("/api/scoreboard/nba").json()
    assert nba_client.asked == [None]
    assert body["date"] == "2026-10-15" and len(body["games"]) == 2
    assert all(g["state"] == "pre" and g["home"]["score"] is None and g["line"] is None for g in body["games"])
    # An explicit ask for the same day is its own request: cached separately from "today".
    nba_client.get("/api/scoreboard/nba?date=2026-10-15")
    assert nba_client.asked == [None, "20261015"]


@needs_db
def test_each_day_is_cached_for_the_cache_window(nba_client):
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    assert nba_client.asked == ["20261013"]                           # second pull served from Postgres
    nba_client.get("/api/scoreboard/nba?date=2026-10-13&force=true")
    nba_client.get("/api/scoreboard/nba?date=2026-10-12")             # another day: its own fetch
    assert nba_client.asked == ["20261013", "20261013", "20261012"]


@needs_db
def test_espn_down_serves_the_stored_day_as_stale(nba_client):
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    nba_client.calls["fail"] = True
    body = nba_client.get("/api/scoreboard/nba?date=2026-10-13&force=true").json()
    assert body["stale"] is True and body["error"] and len(body["games"]) == 5
    nba_client.calls["fail"] = False
    assert nba_client.get("/api/scoreboard/nba?date=2026-10-13&force=true").json()["stale"] is False


@needs_db
def test_espn_down_with_nothing_stored_is_a_502(nba_client):
    nba_client.calls["fail"] = True
    assert nba_client.get("/api/scoreboard/nba?date=2026-10-13").status_code == 502


@needs_db
def test_an_emptied_day_is_a_failed_refresh_not_a_fresh_one(nba_client):
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    nba_client.pages["20261013"] = {"events": []}                      # ESPN drops the list for a day we hold
    body = nba_client.get("/api/scoreboard/nba?date=2026-10-13&force=true").json()
    assert body["stale"] is True and len(body["games"]) == 5
    # A day we hold nothing for may simply have no games.
    empty = nba_client.get("/api/scoreboard/nba?date=2026-07-01").json()
    assert empty["games"] == [] and empty["stale"] is False


@needs_db
def test_unreadable_games_are_a_failed_refresh_and_some_are_a_warning(nba_client):
    bad = copy.deepcopy(FINALS)
    for ev in bad["events"]:
        ev["competitions"][0]["competitors"] = []
    nba_client.pages["20261013"] = bad
    assert nba_client.get("/api/scoreboard/nba?date=2026-10-13").status_code == 502
    some = copy.deepcopy(FINALS)
    some["events"][0]["competitions"][0]["competitors"] = []
    nba_client.pages["20261013"] = some
    body = nba_client.get("/api/scoreboard/nba?date=2026-10-13&force=true").json()
    assert len(body["games"]) == 4 and body["warning"] and body["stale"] is False


@needs_db
def test_bad_dates_are_rejected(nba_client):
    for bad in ("tomorrow", "2026-13-45", "20261013", "2026-10-1"):
        assert nba_client.get(f"/api/scoreboard/nba?date={bad}").status_code == 422, bad
    assert nba_client.asked == []


@needs_db
def test_nba_off_unless_enabled(client):
    assert client.get("/api/scoreboard/nba").status_code == 404


@needs_db
def test_nfl_and_ncaaf_boards_ignore_a_date(nba_client):
    body = nba_client.get("/api/scoreboard/nfl?date=2026-10-13").json()
    assert body["week"] == 4 and "date" not in body


@needs_db
def test_only_nba_games_are_read_by_day(nba_client):
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    nba_client.get("/api/scoreboard/nfl")
    from app import db
    with db.connect() as conn:
        assert {r["league"] for r in db.games_for_day(conn, "nba", date(2026, 10, 13))} == {"nba"}
        assert db.games_for_day(conn, "nba", date(2026, 10, 12)) == []


# ---------------- backup scores, workflows ----------------

def test_nba_live_backup_is_wired():
    # The Yahoo source takes a day, not a week, so it already fits a day board.
    from app import backup
    assert "nba" in backup.SOURCES


def test_schedules_leave_the_day_leagues_out(monkeypatch):
    import importlib

    from app.temporal import schedules
    monkeypatch.setenv("ENABLED_LEAGUES", "nfl,ncaaf,nba")
    try:
        importlib.reload(schedules)
        assert schedules.LEAGUES == ["nfl", "ncaaf"]
        assert all("nba" not in leagues for _wf, leagues, *_ in schedules.SCHEDULES.values())
    finally:
        monkeypatch.undo()
        importlib.reload(schedules)


from test_api import client  # noqa: E402,F401  (the shared fixture: migrated database, stubbed ESPN)
