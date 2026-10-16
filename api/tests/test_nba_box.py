"""Phase 5b: the NBA box score. Real ESPN summaries captured 2026-10-15/16 (a final, a game in the 3rd quarter, a game
the next day before tip-off), cut down to the header, box score and venue. The parser tests need nothing; the API tests
need TEST_DATABASE_URL (the shared `client` fixture)."""
import copy
import json
import os
from pathlib import Path

import pytest

from app import nba_box, summary

FIX = Path(__file__).parent / "fixtures"
FINAL = json.loads((FIX / "nba_summary_final.json").read_text())      # GS 101 @ LAL 89, a neutral-site preseason game
LIVE = json.loads((FIX / "nba_summary_live.json").read_text())        # TOR 73 @ NY 68, 6:12 left in the 3rd
PRE = json.loads((FIX / "nba_summary_pre.json").read_text())          # MIA @ ORL, not started: no player table
TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")

COLUMNS = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]


# ---------------- parser ----------------

def test_final_box_has_both_teams_and_espns_columns():
    box = nba_box.players(FINAL)
    assert set(box) == {"home", "away"}
    assert box["away"]["columns"] == COLUMNS and box["home"]["columns"] == COLUMNS
    assert box["away"]["keys"][:2] == ["minutes", "points"]
    assert len(box["away"]["players"]) == 17 and len(box["home"]["players"]) == 17
    assert box["away"]["totals"][1] == "101" and box["home"]["totals"][1] == "89"       # GS 101 @ LAL 89


def test_starters_first_and_every_player_row_lines_up_with_the_columns():
    for side, t in nba_box.players(FINAL).items():
        starters = [p["starter"] for p in t["players"]]
        assert starters[:5] == [True] * 5 and not any(starters[5:])
        for p in t["players"]:
            assert p["name"] and (p["dnp"] is not None) == (p["stats"] == [])
            assert p["dnp"] or len(p["stats"]) == len(t["columns"])


def test_players_who_did_not_play_carry_espns_reason():
    box = nba_box.players(FINAL)
    dnp = [p for p in box["away"]["players"] if p["dnp"]]
    assert [p["name"] for p in dnp] == ["T. Burton", "A. Toohey", "M. Kelly"]
    assert all(p["dnp"] == "Coach's decision" and p["stats"] == [] and not p["on_court"] for p in dnp)
    santos = next(p for p in box["away"]["players"] if p["name"] == "G. Santos")
    assert santos["stats"][:3] == ["24", "10", "2-6"] and santos["pos"] == "F" and santos["starter"]


def test_live_game_marks_the_five_on_the_floor():
    box = nba_box.players(LIVE)
    for side in ("home", "away"):
        assert sum(p["on_court"] for p in box[side]["players"]) == 5
    assert box["away"]["totals"][1] == "73" and box["home"]["totals"][1] == "68"


def test_game_not_started_has_no_player_table():
    assert nba_box.players(PRE) is None
    assert nba_box.leaders(None) == []


def test_leaders_come_from_the_box_and_report_ties():
    box = nba_box.players(FINAL)
    rows = {r["key"]: r for r in nba_box.leaders(box)}
    assert [r["label"] for r in rows.values()] == ["Points", "Rebounds", "Assists"]
    for side, abbr in (("away", "GS"), ("home", "LAL")):
        block = next(b for b in FINAL["boxscore"]["players"] if b["team"]["abbreviation"] == abbr)
        for key, idx in (("points", 1), ("rebounds", 5), ("assists", 6)):      # an independent read of ESPN's table
            vals = [int(a["stats"][idx]) for a in block["statistics"][0]["athletes"] if a["stats"]]
            leader = rows[key][side]
            assert leader["value"].split()[0] == str(max(vals))
            assert leader["tied"] == vals.count(max(vals)) - 1


def test_a_leader_needs_something_to_lead():
    box = nba_box.players(LIVE)
    for t in box.values():
        for p in t["players"]:
            if p["stats"]:
                p["stats"] = ["0"] * len(p["stats"])
    assert nba_box.leaders(box) == []


def test_team_stats_rows_in_order_with_percent_signs():
    rows = nba_box.team_stats(FINAL)
    assert [r["label"] for r in rows][:6] == ["FG", "FG%", "3PT", "3P%", "FT", "FT%"]
    by = {r["key"]: r for r in rows}
    assert (by["fieldGoalsMade-fieldGoalsAttempted"]["away"], by["fieldGoalsMade-fieldGoalsAttempted"]["home"]) == ("33-84", "33-76")
    assert (by["fieldGoalPct"]["away"], by["fieldGoalPct"]["home"]) == ("39%", "43%")
    assert (by["pointsInPaint"]["away"], by["pointsInPaint"]["home"]) == ("34", "46")
    assert "teamTurnovers" not in by and "turnoverPoints" not in by      # the rows left out on purpose


def test_garbage_never_raises():
    for bad in ({}, {"boxscore": None}, {"boxscore": {"players": [{"team": {}, "statistics": [{}]}]}},
                {"boxscore": {"teams": [{"statistics": [None]}]}}):
        assert nba_box.players(bad) is None or isinstance(nba_box.players(bad), dict)
        assert isinstance(nba_box.team_stats(bad), list)
    assert summary.status({})["state"] is None


# ---------------- API (database) ----------------

def _retarget(src: dict, event_id: str, home: str, away: str) -> dict:
    """A real summary rewritten to describe another game of a scoreboard fixture (the id and the two teams)."""
    p = copy.deepcopy(src)
    old = {c["homeAway"]: c["team"]["abbreviation"] for c in p["header"]["competitions"][0]["competitors"]}
    new = {"home": home, "away": away}
    rename = {old[s]: new[s] for s in ("home", "away")}
    p["header"]["id"] = event_id
    for c in p["header"]["competitions"][0]["competitors"]:
        c["team"]["abbreviation"] = new[c["homeAway"]]
    for t in p["boxscore"]["teams"]:
        t["team"]["abbreviation"] = rename[t["team"]["abbreviation"]]
    for pl in p["boxscore"].get("players", []):
        pl["team"]["abbreviation"] = rename[pl["team"]["abbreviation"]]
    return p


def _q(sql, *args):
    import psycopg
    with psycopg.connect(TEST_DB) as conn:
        return conn.execute(sql, args).fetchall()


def _game_id(espn_id):
    return _q("select id from games where league = 'nba' and espn_id = %s", espn_id)[0][0]


FINAL_ID, LIVE_ID, TODAY_OTHER_ID = "401898718", "401908945", "401908622"      # GS VS LAL; TOR @ NY; OKC @ HOU


@needs_db
def test_final_game_page_is_a_box_score_with_no_bets(nba_client):
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    nba_client.calls["summaries"][FINAL_ID] = copy.deepcopy(FINAL)
    r = nba_client.get(f"/api/games/{_game_id(FINAL_ID)}")
    assert r.status_code == 200
    g = r.json()
    assert g["screen"] == "D" and g["state"] == "post" and g["stale"] is False and g["summary_behind"] is False
    assert g["header"]["away"]["linescores"] == [29, 21, 22, 29] and g["header"]["home"]["linescores"] == [29, 34, 13, 13]
    assert g["venue"] == "T-Mobile Arena" and g["header"]["home"]["record"] == "2-2"
    assert len(g["box"]["away"]["players"]) == 17 and g["box"]["home"]["totals"][1] == "89"
    assert [r["label"] for r in g["leaders"]["rows"]] == ["Points", "Rebounds", "Assists"]
    assert g["team_stats"]["rows"][0]["label"] == "FG"
    assert g["bets"] is None and g["line"] is None and g["injuries"] is None and g["one_liner"] is None
    assert g["placeholders"] == [] and g["situation"] is None and g["header"]["home"]["possession"] is False
    assert _q("select count(*) from bet_results")[0][0] == 0 and _q("select count(*) from odds_snapshots")[0][0] == 0
    assert nba_client.calls["leader_calls"] == 0            # no football season stats were asked for
    nba_client.get(f"/api/games/{_game_id(FINAL_ID)}")
    assert nba_client.calls["summary_calls"][FINAL_ID] == 1  # a final is fetched once and kept


@needs_db
def test_opening_a_game_that_tipped_off_moves_it_forward_and_shows_the_live_box(nba_client):
    nba_client.get("/api/scoreboard/nba")                    # the board still says: not started
    gid = _game_id(LIVE_ID)
    assert _q("select state from games where id = %s", gid)[0][0] == "pre"
    nba_client.calls["summaries"][LIVE_ID] = copy.deepcopy(LIVE)
    g = nba_client.get(f"/api/games/{gid}").json()
    assert g["screen"] == "C2" and g["state"] == "in" and g["status_detail"] == "6:12 - 3rd"
    assert (g["away"]["score"], g["home"]["score"]) == (73, 68)
    assert _q("select state, home_score, away_score from games where id = %s", gid) == [("in", 68, 73)]
    assert sum(p["on_court"] for p in g["box"]["away"]["players"]) == 5
    from test_api import _age_summary
    nba_client.get(f"/api/games/{gid}")
    assert nba_client.calls["summary_calls"][LIVE_ID] == 1  # inside 30 seconds: from Postgres
    _age_summary(gid, 31)
    nba_client.get(f"/api/games/{gid}")
    assert nba_client.calls["summary_calls"][LIVE_ID] == 2


@needs_db
def test_game_page_before_tip_off_is_the_matchup_only(nba_client):
    nba_client.get("/api/scoreboard/nba")
    gid = _game_id(TODAY_OTHER_ID)
    nba_client.calls["summaries"][TODAY_OTHER_ID] = _retarget(PRE, TODAY_OTHER_ID, "HOU", "OKC")
    g = nba_client.get(f"/api/games/{gid}").json()
    assert g["screen"] == "C1" and g["box"] is None
    assert g["team_stats"]["rows"] == [] and g["leaders"]["rows"] == []    # ESPN's pre-game stats are not a box score
    assert g["header"]["home"]["record"] == "2-1" and g["header"]["home"]["linescores"] == []
    assert nba_client.calls["leader_calls"] == 0            # no INTs leader, no season ranks


@needs_db
def test_a_summary_that_trails_the_scoreboard_withholds_the_box(nba_client):
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")   # GS VS LAL is final on the board
    nba_client.calls["summaries"][FINAL_ID] = _retarget(LIVE, FINAL_ID, "LAL", "GS")     # ESPN's summary still says live
    g = nba_client.get(f"/api/games/{_game_id(FINAL_ID)}").json()
    assert g["state"] == "post" and g["summary_behind"] is True
    assert g["box"] is None and g["team_stats"]["rows"] == [] and g["header"]["home"]["linescores"] == []


@needs_db
def test_espn_down_serves_the_saved_box_score(nba_client):
    from test_api import _age_summary
    nba_client.get("/api/scoreboard/nba")
    nba_client.calls["summaries"][LIVE_ID] = copy.deepcopy(LIVE)
    gid = _game_id(LIVE_ID)
    nba_client.get(f"/api/games/{gid}")
    _age_summary(gid, 31)                                   # a live game is due for a refresh...
    nba_client.calls["fail"] = True                         # ...and ESPN does not answer
    g = nba_client.get(f"/api/games/{gid}").json()
    assert g["stale"] is True and g["error"] and g["summary_updated_at"]
    assert len(g["box"]["away"]["players"]) == 23 and (g["away"]["score"], g["home"]["score"]) == (73, 68)


@needs_db
def test_nba_game_has_no_ai_text(nba_client):
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    body = nba_client.get(f"/api/games/{_game_id(FINAL_ID)}/ai").json()
    assert body["status"] == "none"


@needs_db
def test_football_summary_view_still_leaves_the_player_table_out(nba_client):
    from app import db
    nba_client.get("/api/scoreboard/nba?date=2026-10-13")
    nba_client.calls["summaries"][FINAL_ID] = copy.deepcopy(FINAL)
    gid = _game_id(FINAL_ID)
    nba_client.get(f"/api/games/{gid}")
    with db.connect() as conn:
        assert "players" not in db.get_summary_view(conn, gid)["payload"]["boxscore"]
        assert db.get_summary_view(conn, gid, players=True)["payload"]["boxscore"]["players"]


from test_api import client  # noqa: E402,F401  (the shared fixture: migrated database, stubbed ESPN)
from test_nba import nba_client  # noqa: E402,F401
