"""Phase 5a: NCAAF board filter, parser fields (conference, rank, neutral site), the filtered board in the API,
workflows only for board games, and browsing across season types (ESPN's calendar, handoff step 11b).

The fixture is ESPN's real 2026 week 4 FBS scoreboard (71 finals) (captured 2026-09-29, groups=80, limit=300), with blocks the
parser never reads (leaders, records, tickets, weather, links) removed to keep it small. Tests marked with the
`client` / `acts` fixtures need TEST_DATABASE_URL; the rest need nothing."""
import copy
import json
import os
from pathlib import Path

import pytest

from app import espn, ncaaf

FIX = Path(__file__).parent / "fixtures"
NCAAF = json.loads((FIX / "ncaaf_scoreboard.json").read_text())
NFL = json.loads((FIX / "nfl_scoreboard.json").read_text())
TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")

SEC, BIG_TEN, ACC, MAC, SUN_BELT, INDEP, FCS = 8, 5, 1, 15, 37, 18, 48


def _parsed():
    games, errors = espn.parse_scoreboard(copy.deepcopy(NCAAF), "ncaaf")
    return games, errors


def _as_pregame(payload: dict) -> dict:
    """The fixture's week, before kickoff: every game 'pre' and not completed."""
    p = copy.deepcopy(payload)
    for ev in p["events"]:
        for status in (ev.get("status"), ev["competitions"][0].get("status")):
            if status:
                status["type"].update(state="pre", completed=False, name="STATUS_SCHEDULED")
    return p


RANKED_MID = "401862779"   # TEM (American) vs ARMY (American), both unranked in the fixture


def _with_mid_major_rank(payload: dict, rank: int) -> dict:
    """The payload with TEM (home, 401862779) given ESPN's curatedRank.current = rank (99 = unranked)."""
    p = copy.deepcopy(payload)
    ev = next(e for e in p["events"] if e["id"] == RANKED_MID)
    home = next(c for c in ev["competitions"][0]["competitors"] if c["homeAway"] == "home")
    home["curatedRank"] = {"current": rank}
    return p


def _hidden_game(games):
    """A game the rules leave off the board (an FCS opponent), for the favorites tests."""
    return next(g for g in games if not ncaaf.is_featured_game(g, set())
                and g.home.abbr != g.away.abbr)


# ---------------- the rule (pure) ----------------

@pytest.mark.parametrize("home_id,away_id,home_abbr,away_abbr,home_conf,away_conf,favs,shows", [
    ("1", "2", "UGA", "ALA", SEC, SEC, set(), True),            # SEC vs SEC
    ("1", "2", "MICH", "TOL", BIG_TEN, MAC, set(), True),       # Big Ten vs MAC (mid-major at a major: Adam, Sep 29)
    ("1", "2", "UNC", "ALB", ACC, FCS, set(), False),           # ACC vs FCS
    ("1", "2", "EMU", "GAST", MAC, SUN_BELT, set(), False),     # two mid-majors
    ("87", "2", "ND", "EMU", INDEP, MAC, set(), True),          # Notre Dame vs MAC
    ("2", "87", "EMU", "ND", MAC, INDEP, set(), True),          # ... either side
    ("1", "2", "CONN", "EMU", INDEP, MAC, set(), False),        # another independent
    ("87", "2", "ND", "ALB", INDEP, FCS, set(), False),         # Notre Dame vs FCS
    ("1", "2", "TEX", "ALB", SEC, FCS, {"TEX"}, True),          # a favorite overrides the FCS rule
    ("1", "2", "EMU", "ALB", MAC, FCS, {"ALB"}, True),          # ... and every other rule
    ("1", "2", "UGA", "ALA", None, SEC, set(), False),          # a missing conference hides
    ("1", "2", "UGA", "ALA", SEC, None, set(), False),
])
def test_is_featured(home_id, away_id, home_abbr, away_abbr, home_conf, away_conf, favs, shows):
    assert ncaaf.is_featured(home_id, away_id, home_abbr, away_abbr, home_conf, away_conf, favs) is shows


AMERICAN, MOUNTAIN_WEST = 151, 17


@pytest.mark.parametrize("home_conf,away_conf,home_rank,away_rank,shows", [
    (AMERICAN, SUN_BELT, 18, None, True),          # a ranked mid-major shows (PRD, Sep 29)
    (MAC, MOUNTAIN_WEST, None, 24, True),          # ... either side
    (AMERICAN, FCS, 18, None, False),              # a ranked team vs FCS still hides
    (MAC, SUN_BELT, None, None, False),            # two unranked mid-majors
    (None, SEC, 10, None, False),                  # a missing conference hides, ranked or not
])
def test_is_featured_ranked(home_conf, away_conf, home_rank, away_rank, shows):
    assert ncaaf.is_featured("1", "2", "HOME", "AWAY", home_conf, away_conf, set(),
                             home_rank=home_rank, away_rank=away_rank) is shows


# ---------------- parser on real ESPN data ----------------

def test_parser_reads_conference_and_rank_on_real_data():
    games, errors = _parsed()
    assert errors == [] and len(games) == len(NCAAF["events"]) == 71
    assert all(g.home_conf is not None and g.away_conf is not None for g in games)
    assert all("no conference" not in g.warnings for g in games)
    ranks = [r for g in games for r in (g.home_rank, g.away_rank)]
    assert all(r is None or 1 <= r <= 25 for r in ranks)          # ESPN's 99 (unranked) never survives
    assert sum(r is not None for r in ranks) >= 15                 # most of the Top 25 play in a week
    assert any(ncaaf.is_featured_game(g, set()) for g in games)
    assert any(not ncaaf.is_featured_game(g, set()) for g in games)
    nd = next(g for g in games if ncaaf.NOTRE_DAME in (g.home.espn_id, g.away.espn_id))
    assert ncaaf.is_featured_game(nd, set())
    assert any("TEX" in (g.home.abbr, g.away.abbr) for g in games)          # Adam's favorite's abbreviation


def test_missing_conference_warns_and_hides():
    p = copy.deepcopy(NCAAF)
    p["events"] = p["events"][:1]
    p["events"][0]["competitions"][0]["competitors"][0]["team"].pop("conferenceId", None)
    games, errors = espn.parse_scoreboard(p, "ncaaf")
    assert errors == [] and "no conference" in games[0].warnings
    assert not ncaaf.is_featured_game(games[0], set())


def test_nfl_never_warns_about_conference():
    games, _ = espn.parse_scoreboard(copy.deepcopy(NFL), "nfl")
    assert games and all("no conference" not in g.warnings for g in games)


def test_neutral_site_and_rank_read_from_the_competition():
    p = copy.deepcopy(NCAAF)
    p["events"] = p["events"][:1]
    comp = p["events"][0]["competitions"][0]
    comp["neutralSite"] = True
    comp["competitors"][0]["curatedRank"] = {"current": 6}
    comp["competitors"][1]["curatedRank"] = {"current": 99}
    g = espn.parse_scoreboard(p, "ncaaf")[0][0]
    home_is_first = comp["competitors"][0]["homeAway"] == "home"
    assert g.neutral_site is True
    assert (g.home_rank, g.away_rank) == ((6, None) if home_is_first else (None, 6))


# ---------------- ESPN's season calendar (step 11b) ----------------

def test_parse_calendar_on_real_data():
    stages = espn.parse_calendar(NCAAF)
    keys = [(s["season_type"], s["week"]) for s in stages]
    assert keys == [(2, w) for w in range(1, 16)] + [(3, 1), (3, 999)]    # no off-season (type 4) entries
    assert [s["label"] for s in stages[-2:]] == ["Bowls", "CFP"]
    assert stages[0]["label"] == "Week 1" and stages[0]["start"]


def test_parse_calendar_of_plain_dates_is_empty():
    # NBA and soccer send a list of date strings; NFL test fixture sends none.
    assert espn.parse_calendar({"leagues": [{"calendar": ["2026-10-01T07:00Z", "2026-10-02T07:00Z"]}]}) == []
    assert espn.parse_calendar(NFL) == []
    assert espn.parse_calendar({}) == []


# ---------------- API (database) ----------------

@pytest.fixture()
def ncaaf_client(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ENABLED_LEAGUES", ["nfl", "ncaaf"])
    client.calls["payload"] = copy.deepcopy(NCAAF)
    client.favorites = lambda d: main.FAVORITES_FILE.write_text(json.dumps(d))
    return client


def _q(sql, *args):
    import psycopg
    with psycopg.connect(TEST_DB) as conn:
        return conn.execute(sql, args).fetchall()


@needs_db
def test_board_shows_only_featured_games_but_stores_all(ncaaf_client):
    body = ncaaf_client.get("/api/scoreboard/ncaaf").json()
    games, _ = _parsed()
    featured = {g.espn_id for g in games if ncaaf.is_featured_game(g, set())}
    assert 0 < len(body["games"]) == len(featured) < len(games)
    assert _q("select count(*) from games where league = 'ncaaf'")[0][0] == len(games)
    ranked = [s["rank"] for c in body["games"] for s in (c["home"], c["away"]) if s["rank"] is not None]
    assert ranked and all(1 <= r <= 25 for r in ranked)
    assert all(c["neutral_site"] is False for c in body["games"])
    assert body["season_type"] == 2 and body["week"] == 4


@needs_db
def test_favorite_puts_a_hidden_game_on_the_board_pinned_first(ncaaf_client):
    hidden = _hidden_game(_parsed()[0])
    before = ncaaf_client.get("/api/scoreboard/ncaaf").json()
    assert hidden.home.abbr not in {c["home"]["abbr"] for c in before["games"]}
    ncaaf_client.favorites({"nfl": ["SEA"], "ncaaf": [hidden.home.abbr]})
    after = ncaaf_client.get("/api/scoreboard/ncaaf").json()             # cached: no refetch needed
    assert len(after["games"]) == len(before["games"]) + 1
    assert after["games"][0]["home"]["abbr"] == hidden.home.abbr and after["games"][0]["favorite"]


@needs_db
def test_hidden_game_still_opens_by_id(ncaaf_client):
    ncaaf_client.get("/api/scoreboard/ncaaf")
    hidden = _hidden_game(_parsed()[0])
    gid = _q("select id from games where league = 'ncaaf' and espn_id = %s", hidden.espn_id)[0][0]
    ncaaf_client.calls["summaries"][hidden.espn_id] = ncaaf_client.calls["summaries"]["401900004"]
    assert ncaaf_client.get(f"/api/games/{gid}").status_code == 200


@needs_db
def test_rank_frozen_once_a_game_is_final(ncaaf_client):
    ncaaf_client.get("/api/scoreboard/ncaaf")
    p = copy.deepcopy(NCAAF)
    for ev in p["events"]:
        for c in ev["competitions"][0]["competitors"]:
            c["curatedRank"] = {"current": 1}                              # next week's poll
    ncaaf_client.calls["payload"] = p
    body = ncaaf_client.get("/api/scoreboard/ncaaf?force=true").json()
    ranks = {s["rank"] for c in body["games"] for s in (c["home"], c["away"])}
    assert ranks != {1}                                                   # the week-4 finals keep week-4 ranks


@needs_db
def test_ranked_mid_major_on_the_board_unranked_hidden(ncaaf_client):
    # Pre-game, so the rank isn't frozen and the forced refresh can take it away again.
    ncaaf_client.calls["payload"] = _with_mid_major_rank(_as_pregame(NCAAF), 18)
    body = ncaaf_client.get("/api/scoreboard/ncaaf").json()
    tem = [c for c in body["games"] if c["home"]["abbr"] == "TEM"]
    assert len(tem) == 1 and tem[0]["home"]["rank"] == 18 and tem[0]["away"]["rank"] is None
    ncaaf_client.calls["payload"] = _with_mid_major_rank(_as_pregame(NCAAF), 99)
    body = ncaaf_client.get("/api/scoreboard/ncaaf?force=true").json()
    assert "TEM" not in {c["home"]["abbr"] for c in body["games"]}


@needs_db
def test_season_type_is_part_of_the_cache_key(ncaaf_client):
    ncaaf_client.calls["payload"] = copy.deepcopy(NFL)
    assert ncaaf_client.get("/api/scoreboard/nfl?season_type=1&week=2").status_code == 200
    assert ncaaf_client.get("/api/scoreboard/nfl?week=2").status_code == 200
    assert ncaaf_client.calls["n"] == 2
    rows = _q("select requested_week, requested_season_type from fetch_log where league = 'nfl' order by id")
    assert rows == [(2, 1), (2, None)]
    ncaaf_client.get("/api/scoreboard/nfl?season_type=1&week=2")         # cached now
    assert ncaaf_client.calls["n"] == 2


@needs_db
def test_playoff_week_999_is_accepted(ncaaf_client):
    r = ncaaf_client.get("/api/scoreboard/ncaaf?week=999&season_type=3")
    assert r.status_code == 200
    assert ncaaf_client.get("/api/scoreboard/ncaaf?week=1000").status_code == 422
    assert ncaaf_client.get("/api/scoreboard/ncaaf?season_type=4").status_code == 422


@needs_db
def test_calendar_in_response_and_never_emptied(ncaaf_client):
    body = ncaaf_client.get("/api/scoreboard/ncaaf").json()
    assert len(body["calendar"]) == 17 and body["calendar"][-1] == {"season_type": 3, "week": 999, "label": "CFP"}
    p = copy.deepcopy(NCAAF)
    p["leagues"][0].pop("calendar")
    ncaaf_client.calls["payload"] = p
    assert len(ncaaf_client.get("/api/scoreboard/ncaaf?force=true").json()["calendar"]) == 17


# ---------------- worker (database) ----------------

@pytest.fixture()
def ncaaf_acts(acts, monkeypatch, tmp_path):
    fav = tmp_path / "favorites.json"
    fav.write_text(json.dumps({"nfl": ["SEA"]}))
    monkeypatch.setenv("FAVORITES_FILE", str(fav))
    acts.favorites = lambda d: fav.write_text(json.dumps(d))
    acts.calls["boards_map"][(None, None)] = _as_pregame(NCAAF)
    acts.calls["boards_map"][(5, 2)] = {**copy.deepcopy(NCAAF), "events": []}   # next week: nothing listed yet
    return acts


@needs_db
def test_sync_schedule_starts_workflows_only_for_board_games(ncaaf_acts):
    from app.temporal import activities as a
    games, _ = _parsed()
    featured = {g.espn_id for g in games if ncaaf.is_featured_game(g, set())}
    refs = ncaaf_acts(a.sync_schedule, "ncaaf")
    assert {r.espn_id for r in refs} == featured
    assert _q("select count(*) from games where league = 'ncaaf'")[0][0] == len(games)   # all saved
    assert ncaaf_acts.calls["boards"] == [(None, None), (5, 2)]                         # next stage from the calendar

    hidden = _hidden_game(games)
    ncaaf_acts.favorites({"ncaaf": [hidden.away.abbr]})
    refs = ncaaf_acts(a.sync_schedule, "ncaaf")
    assert {r.espn_id for r in refs} == featured | {hidden.espn_id}


@needs_db
def test_sync_schedule_includes_a_ranked_mid_major(ncaaf_acts):
    from app.temporal import activities as a
    assert RANKED_MID not in {r.espn_id for r in ncaaf_acts(a.sync_schedule, "ncaaf")}
    ncaaf_acts.calls["boards_map"][(None, None)] = _with_mid_major_rank(_as_pregame(NCAAF), 18)
    assert RANKED_MID in {r.espn_id for r in ncaaf_acts(a.sync_schedule, "ncaaf")}


@needs_db
def test_sync_schedule_skips_finals(ncaaf_acts):
    from app.temporal import activities as a
    ncaaf_acts.calls["boards_map"][(None, None)] = copy.deepcopy(NCAAF)      # week 4 as played: all final
    assert ncaaf_acts(a.sync_schedule, "ncaaf") == []


@needs_db
def test_sync_schedule_walks_the_calendar_into_bowls_and_the_playoff(ncaaf_acts):
    from app.temporal import activities as a
    wk15 = _as_pregame(NCAAF)
    wk15["week"]["number"] = 15
    ncaaf_acts.calls["boards_map"][(None, None)] = wk15
    ncaaf_acts(a.sync_schedule, "ncaaf")
    assert ncaaf_acts.calls["boards"][-1] == (1, 3)                                     # Bowls

    bowls = _as_pregame(NCAAF)
    bowls["season"]["type"], bowls["week"]["number"] = 3, 1
    ncaaf_acts.calls["boards_map"][(None, None)] = bowls
    ncaaf_acts(a.sync_schedule, "ncaaf")
    assert ncaaf_acts.calls["boards"][-1] == (999, 3)                                   # CFP


@needs_db
def test_sync_schedule_nfl_ignores_the_ncaaf_filter(acts):
    from app.temporal import activities as a
    assert [r.espn_id for r in acts(a.sync_schedule, "nfl")] == ["401900001", "401900003", "401900002"]


# Fixtures shared with the Phase 1-3 tests.
from test_activities import acts  # noqa: E402,F401
from test_api import client  # noqa: E402,F401
