"""Live-score backup (backup.py): parsers, matching, and the scoreboard overlay when ESPN fails.

Samples captured 2026-09-29, no live calls: Yahoo NFL 2026-09-27 (14 finals), Yahoo NBA 2026-04-10 (15 finals),
NCAA.com 2026 FBS week 4 (71 finals, the same week as ncaaf_scoreboard.json), Premier League latest 20 results.
No sample caught a game in progress, so the live-status tests edit a sample the way the source shows live games.
Tests using `client` need TEST_DATABASE_URL; the rest need nothing."""
import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app import backup, espn
from app.backup import BackupScore

FIX = Path(__file__).parent / "fixtures"
TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _find(scores, home, away):
    return next(s for s in scores if s.home == home and s.away == away)


# ---------------- Parsers ----------------

def test_yahoo_nfl_sample():
    s = backup._yahoo_parse(_load("backup_yahoo_nfl.json"))
    assert len(s) == 14
    assert _find(s, "Buffalo Bills", "Los Angeles Chargers") == BackupScore("Buffalo Bills", "Los Angeles Chargers", 24, 16, "Final")


def test_yahoo_nba_sample():
    s = backup._yahoo_parse(_load("backup_yahoo_nba.json"))
    assert len(s) == 15
    assert _find(s, "Charlotte Hornets", "Detroit Pistons") == BackupScore("Charlotte Hornets", "Detroit Pistons", 100, 118, "Final")


def test_yahoo_live_status_is_the_display_text():
    p = _load("backup_yahoo_nfl.json")
    g = next(iter(p["service"]["scoreboard"]["games"].values()))
    g.update(status_type="status.type.in_progress", status_display_name="4:12 3rd",
             total_home_points="10", total_away_points="7")
    s = backup._yahoo_parse(p)[0]
    assert (s.home_score, s.away_score, s.status) == (10, 7, "4:12 3rd")


def test_ncaa_sample_agrees_with_espn_for_every_game():
    """Same week from both sources: each ESPN game finds exactly its backup game, with the same final score."""
    s = backup._ncaa_parse(_load("backup_ncaa.json"))
    assert len(s) == 71 and all(x.status == "Final" for x in s)
    espn_games, _ = espn.parse_scoreboard(_load("ncaaf_scoreboard.json"), "ncaaf")
    assert len(espn_games) == 71
    for g in espn_games:
        b = backup.match({g.home.name, g.home.short_name or ""}, {g.away.name, g.away.short_name or ""}, s)
        assert b is not None, (g.home.name, g.away.name)
        assert (b.home_score, b.away_score) == (g.home_score, g.away_score), (g.home.name, g.away.name)


def test_ncaa_live_status():
    p = _load("backup_ncaa.json")
    g = p["games"][0]["game"]
    g.update(gameState="live", currentPeriod="3rd", contestClock="4:12", finalMessage="")
    assert backup._ncaa_parse(p)[0].status == "3rd 4:12"
    g.update(currentPeriod="HALF", contestClock="0:00")
    assert backup._ncaa_parse(p)[0].status == "HALF"


def test_premier_league_sample():
    s = backup._premier_league_parse(_load("backup_premier_league.json"))
    assert len(s) == 20
    assert _find(s, "Fulham", "Manchester United") == BackupScore("Fulham", "Manchester United", 1, 1, "FT")
    assert _find(s, "Manchester City", "Sunderland") == BackupScore("Manchester City", "Sunderland", 5, 3, "FT")


def test_premier_league_live_status():
    p = _load("backup_premier_league.json")
    f = p["content"][0]
    f.update(status="L", phase="2", clock={"secs": 3780.0, "label": "63'00"})
    assert backup._premier_league_parse(p)[0].status == "63'"
    f.update(phase="H")
    assert backup._premier_league_parse(p)[0].status == "HT"


# ---------------- Matching ----------------

SCORES = [BackupScore("Kansas City Chiefs", "Denver Broncos", 24, 10, "4:12 3rd"),
          BackupScore("Buffalo Bills", "Miami Dolphins", 7, 0, "1st 9:00")]


def test_match_by_full_or_short_name():
    assert backup.match({"Kansas City Chiefs", "Chiefs"}, {"Denver Broncos", "Broncos"}, SCORES) is SCORES[0]
    assert backup.match({"KC Chiefs", "kansas city chiefs"}, {"Broncos-X", "DENVER BRONCOS"}, SCORES) is SCORES[0]
    assert backup.match({"Coastal Carolina Chanticleers", "Coastal"}, {"Liberty Flames", "Liberty"},
                        [BackupScore("Coastal Carolina", "Liberty", 17, 34, "Final")]).home_score == 17


def test_no_match_when_only_one_team_matches():
    assert backup.match({"Kansas City Chiefs"}, {"Miami Dolphins"}, SCORES) is None
    assert backup.match({"Denver Broncos"}, {"Kansas City Chiefs"}, SCORES) is None      # home/away swapped
    assert backup.match({"Kansas City Chiefs", ""}, {"", None}, SCORES) is None


# ---------------- fetch ----------------

LIVE_CARD = {"state": "in", "time_valid": True, "start_time": "2026-09-28T17:00:00+00:00"}


def test_fetch_is_one_call_with_the_short_timeout(monkeypatch):
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append((url, params, timeout))
        return httpx.Response(200, json=_load("backup_yahoo_nfl.json"), request=httpx.Request("GET", url))

    monkeypatch.setattr(backup.httpx, "get", fake_get)
    # A late game still live after midnight ET is listed under its start date (Sunday), not Monday.
    late = dict(LIVE_CARD, start_time="2026-09-28T03:30:00+00:00")          # 11:30 PM ET Sunday
    stuck = dict(LIVE_CARD, start_time="2026-09-25T00:15:00+00:00")         # Thursday's game, never refreshed
    assert len(backup.fetch("nfl", [stuck, late])) == 14
    assert seen == [(backup.YAHOO_URL, {"leagues": "nfl", "date": "2026-09-27"}, backup.TIMEOUT)]


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), ValueError("not json")])
def test_fetch_never_raises(monkeypatch, exc):
    def fake_get(*a, **kw):
        raise exc
    monkeypatch.setattr(backup.httpx, "get", fake_get)
    assert backup.fetch("epl", [LIVE_CARD]) == []


def test_fetch_drops_games_without_a_score(monkeypatch):
    monkeypatch.setitem(backup.SOURCES, "nfl", lambda **kw: [BackupScore("A", "B", None, None, "8:00 pm ET"), SCORES[0]])
    assert backup.fetch("nfl", [LIVE_CARD]) == [SCORES[0]]


def test_ncaa_backup_only_for_regular_season_weeks(monkeypatch):
    urls = []
    monkeypatch.setattr(backup, "_get", lambda url, params=None: urls.append(url) or _load("backup_ncaa.json"))
    assert len(backup.fetch("ncaaf", [LIVE_CARD], 2026, 4, 2)) == 71
    assert urls == ["https://ncaa-api.henrygd.me/scoreboard/football/fbs/2026/04/all-conf"]
    assert backup.fetch("ncaaf", [LIVE_CARD], 2026, 1, 3) == []                   # bowls: no backup
    assert len(urls) == 1


def test_no_backup_for_mls(monkeypatch):
    def fake_get(*a, **kw):
        raise AssertionError("no MLS source may be called")
    monkeypatch.setattr(backup.httpx, "get", fake_get)
    assert "mls" not in backup.SOURCES
    assert backup.fetch("mls", [LIVE_CARD]) == []


def test_is_live():
    now = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
    assert backup.is_live(LIVE_CARD, now)
    started = {"state": "pre", "time_valid": True, "start_time": "2026-09-28T10:00:00-07:00"}   # 17:00 UTC
    assert backup.is_live(started, now)
    assert not backup.is_live(dict(started, start_time="2026-09-28T12:00:00-07:00"), now)   # 19:00 UTC
    assert not backup.is_live(dict(started, time_valid=False), now)                        # flexed, time TBD
    assert not backup.is_live(dict(LIVE_CARD, state="post"), now)


# ---------------- Scoreboard overlay (database) ----------------

from test_api import client  # noqa: E402,F401


@pytest.fixture()
def fake_backup(monkeypatch):
    """Replaces the NFL source: records each call; `result` is the list to return or an exception to raise."""
    state = {"calls": [], "result": [BackupScore("Kansas City Chiefs", "Denver Broncos", 24, 10, "4:12 3rd")]}

    def src(**kw):
        state["calls"].append(kw)
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setitem(backup.SOURCES, "nfl", src)
    return state


def _stale_board(client):
    client.get("/api/scoreboard/nfl")                     # ESPN works once: KC-DEN stored live at 17-10
    client.calls["fail"] = True
    return client.get("/api/scoreboard/nfl?force=true").json()


def _card(body, home):
    return next(g for g in body["games"] if g["home"]["abbr"] == home)


@needs_db
def test_espn_down_live_card_shows_backup_score(client, fake_backup):
    body = _stale_board(client)
    assert body["stale"] is True
    kc = _card(body, "KC")
    assert (kc["home"]["score"], kc["away"]["score"], kc["status_detail"]) == (24, 10, "4:12 3rd")
    assert kc["backup"] is True and kc["state"] == "in"
    assert "backup" not in _card(body, "DAL")                          # final: untouched
    assert len(fake_backup["calls"]) == 1
    assert fake_backup["calls"][0]["week"] == 4 and fake_backup["calls"][0]["season"] == body["season"]


@needs_db
def test_backup_final_does_not_change_state(client, fake_backup):
    fake_backup["result"] = [BackupScore("Kansas City Chiefs", "Denver Broncos", 31, 20, "Final")]
    kc = _card(_stale_board(client), "KC")
    assert kc["state"] == "in" and kc["status_detail"] == "Final" and kc["backup"] is True


@needs_db
def test_backup_is_display_only(client, fake_backup):
    from test_api import _sql
    kc_id = _card(client.get("/api/scoreboard/nfl").json(), "KC")["id"]
    tables = ("games", "odds_snapshots", "bet_results", "game_summaries")
    before = {t: _sql(f"SELECT count(*) FROM {t}")[0][0] for t in tables}
    game = "SELECT home_score, away_score, state, status_detail, fetched_at FROM games WHERE id = %s"
    stored = _sql(game, kc_id)
    assert stored[0][:3] == (17, 10, "in")
    client.calls["fail"] = True
    assert _card(client.get("/api/scoreboard/nfl?force=true").json(), "KC")["backup"] is True
    assert {t: _sql(f"SELECT count(*) FROM {t}")[0][0] for t in tables} == before
    assert _sql(game, kc_id) == stored
    # The next good ESPN refresh shows ESPN's score again, with no tag.
    client.calls["fail"] = False
    kc = _card(client.get("/api/scoreboard/nfl?force=true").json(), "KC")
    assert (kc["home"]["score"], kc["away"]["score"]) == (17, 10) and "backup" not in kc


@needs_db
@pytest.mark.parametrize("result", [RuntimeError("backup down"), httpx.ReadTimeout("slow"), [],
                                    [BackupScore("Kansas City Chiefs", "Miami Dolphins", 1, 2, "Q1")]])
def test_backup_failure_is_todays_stale_response(client, fake_backup, monkeypatch, result):
    fake_backup["result"] = result
    with_backup = _stale_board(client)
    assert len(fake_backup["calls"]) == 1
    monkeypatch.delitem(backup.SOURCES, "nfl")                     # today's code path: no backup at all
    today = client.get("/api/scoreboard/nfl?force=true").json()
    assert with_backup == today


@needs_db
def test_espn_ok_never_calls_backup(client, fake_backup):
    client.get("/api/scoreboard/nfl")
    body = client.get("/api/scoreboard/nfl?force=true").json()
    assert body["stale"] is False and fake_backup["calls"] == []
    assert not any("backup" in g for g in body["games"])


@needs_db
def test_no_live_game_never_calls_backup(client, fake_backup):
    from test_api import FIXTURE
    p = copy.deepcopy(FIXTURE)
    for ev in p["events"]:
        status = ev["competitions"][0].get("status")          # the fixture's last event is unreadable on purpose
        if status and status["type"]["state"] == "in":
            status["type"].update(state="post", completed=True, name="STATUS_FINAL")
        if status and status["type"]["state"] == "pre":
            ev["date"] = "2099-09-28T17:00Z"                    # upcoming, not overdue
    client.calls["payload"] = p
    body = _stale_board(client)
    assert body["stale"] is True and fake_backup["calls"] == []
