"""Summary parser against real ESPN summaries captured 2026-09-27 (trimmed to the blocks we read).
pre = PHI @ CHI (upcoming), in = LAR @ DEN (2nd quarter), post = LAC @ BUF (final). No network."""
import json
from pathlib import Path

import pytest

from app import summary

FIX = Path(__file__).parent / "fixtures"
PRE, LIVE, POST = (json.loads((FIX / f"summary_{s}.json").read_text()) for s in ("pre", "in", "post"))


def test_status():
    assert summary.status(PRE)["state"] == "pre" and summary.status(PRE)["home_score"] is None
    s = summary.status(LIVE)
    assert (s["state"], s["period"], s["clock"], s["away_score"], s["home_score"]) == ("in", 2, "3:07", 13, 0)
    s = summary.status(POST)
    assert (s["state"], s["short_detail"], s["away_score"], s["home_score"], s["event_id"]) == ("post", "Final", 16, 24, "401872953")


def test_teams_records_and_quarters():
    t = summary.teams(POST)
    assert (t["home"]["abbr"], t["home"]["record"], t["home"]["venue_record"]) == ("BUF", "3-0", "2-0")
    assert t["home"]["linescores"] == [0, 10, 0, 14] and sum(t["home"]["linescores"]) == 24
    assert t["away"]["linescores"] == [10, 0, 3, 3] and sum(t["away"]["linescores"]) == 16
    assert summary.teams(PRE)["away"]["record"] == "2-0" and summary.teams(PRE)["home"]["linescores"] == []


def test_neutral_site_has_no_home_or_road_record():
    p = json.loads(json.dumps(PRE))
    p["header"]["competitions"][0]["neutralSite"] = True
    t = summary.teams(p)
    assert t["home"]["record"] == "1-1" and t["home"]["venue_record"] is None and t["away"]["venue_record"] is None


def test_situation_only_live():
    assert summary.situation(LIVE) == {"possession": "DEN", "down_distance": "3rd & 8 at DEN 40"}
    assert summary.situation(PRE) is None and summary.situation(POST) is None


def test_season_stats_pre():
    rows = {r["key"]: r for r in summary.team_stats(PRE, "season")}
    assert list(rows) == ["totalPointsPerGame", "totalPointsPerGameAllowed", "yardsPerGame", "yardsPerGameAllowed"]
    assert (rows["totalPointsPerGame"]["away"], rows["totalPointsPerGame"]["home"]) == ("24.0", "31.0")


def test_game_stats():
    rows = {r["key"]: r for r in summary.team_stats(POST, "game")}
    assert (rows["totalYards"]["away"], rows["totalYards"]["home"]) == ("348", "350")
    assert rows["turnovers"]["home"] == "5"
    assert rows["thirdDownEff"]["away"] == "6-15 (40%)"
    assert rows["possessionTime"]["home"] == "28:38"
    assert rows["totalPenaltiesYards"]["home"] == "10-88"


def test_leaders():
    pre = {r["key"]: r for r in summary.leaders(PRE, "season")}
    # C1 is Passing, Rushing, Receiving, Tackles + INTs; INTs come from the team leaders endpoint.
    assert list(pre) == ["passingYards", "rushingYards", "receivingYards", "totalTackles"]
    assert pre["passingYards"]["away"]["name"] == "Jalen Hurts"
    post = {r["key"]: r for r in summary.leaders(POST, "game")}
    assert list(post) == ["passingYards", "rushingYards", "receivingYards"]
    assert post["rushingYards"]["home"]["name"] == "James Cook III"
    assert post["rushingYards"]["home"]["value"] == "24 CAR, 154 YDS, 1 TD"


def test_injuries_match_espn_game_page():
    # Checked against espn.com/nfl/game/_/gameId/401872963 on 2026-09-27: same players, order and statuses.
    inj = summary.injuries(PRE)
    assert [(i["name"], i["status"]) for i in inj["home"]] == [
        ("Anthony Johnson Jr.", "IR"), ("Caleb Williams", "Out"), ("Shemar Turner", "PUP-R"),
        ("Noah Sewell", "PUP-R"), ("Kyler Gordon", "PUP-R")]
    assert [i["status"] for i in inj["away"]] == ["Out", "Questionable", "Out", "Out", "IR"]
    assert inj["away"][1] == {"name": "Jonathan Greenard", "position": "LB", "status": "Questionable",
                              "detail": "Pectoral", "return_date": "2026-09-28"}


def test_injury_status_falls_back_to_plain_status():
    p = json.loads(json.dumps(PRE))
    del p["injuries"][0]["injuries"][0]["details"]["fantasyStatus"]
    assert summary.injuries(p)["home"][0]["status"] == "Injured Reserve"


def test_closing_line_only_after_kickoff():
    assert summary.closing_line(PRE) is None
    assert summary.closing_line(POST) == {"provider": "Draft Kings", "home_spread": -7.0, "total": 50.5,
                                          "home_ml": -345, "away_ml": 275}
    assert summary.closing_line(LIVE)["home_spread"] == -1.5


def test_current_line_pre():
    line = summary.current_line(PRE)
    assert (line["home_spread"], line["total"], line["home_ml"], line["away_ml"]) == (4.5, 41.5, 170, -205)


def test_one_liner():
    assert summary.one_liner(LIVE) == "LAR lead 13–0 · Stafford 143 yds, 1 TD"
    assert summary.one_liner(PRE) is None


@pytest.mark.parametrize("payload", [{}, {"header": {}}, {"header": {"competitions": [{}]}}, {"boxscore": None},
                                     {"leaders": "junk", "injuries": [{"team": None}]}, {"pickcenter": [None]}])
def test_missing_or_broken_blocks_never_raise(payload):
    assert summary.team_stats(payload, "game") == []
    assert summary.leaders(payload, "season") == []
    assert summary.injuries(payload) == {"home": [], "away": []}
    assert summary.closing_line(payload) is None
    assert summary.situation(payload) is None
    assert summary.one_liner(payload) is None
    summary.teams(payload)
    summary.status(payload)


def test_closing_line_parses_odd_strings():
    p = json.loads(json.dumps(POST))
    pc = p["pickcenter"][0]
    pc["pointSpread"]["home"]["close"]["line"] = "OFF"          # no number: fall back to details
    pc["total"]["over"]["close"]["line"] = "o47"
    pc["moneyline"]["home"]["close"]["odds"] = "EVEN"
    line = summary.closing_line(p)
    assert (line["home_spread"], line["total"], line["home_ml"]) == (-7.0, 47.0, 100)


def test_one_liner_keeps_negative_yards():
    p = json.loads(json.dumps(LIVE))
    for team in p["leaders"]:
        for cat in team["leaders"]:
            if cat["name"] == "passingYards":
                cat["leaders"] = []              # no passer: the one-liner falls back to the rusher
            if cat["name"] == "rushingYards":
                cat["leaders"][0]["displayValue"] = "3 CAR, -4 YDS"
    assert summary.one_liner(p).endswith("-4 yds")
