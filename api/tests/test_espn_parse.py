"""Parser tests against an ESPN-shaped fixture. No network, no database."""
import json
from pathlib import Path

from app.espn import parse_scoreboard, _home_spread

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "nfl_scoreboard.json").read_text())


def _by_id(games):
    return {g.espn_id: g for g in games}


def test_parses_valid_games_and_reports_bad_one():
    games, errors = parse_scoreboard(FIXTURE, "nfl")
    assert len(games) == 4
    assert len(errors) == 1 and "401900099" in errors[0]


def test_season_and_week():
    games, _ = parse_scoreboard(FIXTURE, "nfl")
    assert all(g.season == 2026 and g.week == 4 for g in games)


def test_pre_game_home_favorite_classic_shape():
    g = _by_id(parse_scoreboard(FIXTURE, "nfl")[0])["401900001"]
    assert g.state == "pre" and g.home.abbr == "BUF" and g.away.abbr == "MIA"
    assert g.home_score is None and g.away_score is None  # no 0-0 on upcoming games
    assert g.odds.home_spread == -6.5 and g.odds.total == 48.5
    assert g.odds.home_ml == -285 and g.odds.away_ml == 230
    assert g.odds.provider == "DraftKings"


def test_pre_game_away_favorite_nested_moneyline():
    g = _by_id(parse_scoreboard(FIXTURE, "nfl")[0])["401900002"]
    assert g.odds.home_spread == 2.5          # SF -2.5 on the road -> SEA +2.5
    assert g.odds.home_ml == 120 and g.odds.away_ml == -142


def test_live_game():
    g = _by_id(parse_scoreboard(FIXTURE, "nfl")[0])["401900003"]
    assert g.state == "in" and g.home_score == 17 and g.away_score == 10
    assert g.period == 3 and g.clock == "10:21"


def test_final_without_odds():
    g = _by_id(parse_scoreboard(FIXTURE, "nfl")[0])["401900004"]
    assert g.state == "post" and g.odds is None
    assert (g.home_score, g.away_score) == (20, 27)


def test_spread_edge_cases():
    assert _home_spread({"details": "EVEN"}, "A", "B") == 0.0
    assert _home_spread({"details": "PK"}, "A", "B") == 0.0
    assert _home_spread({"details": "garbage", "spread": -1.5}, "A", "B") == -1.5
    assert _home_spread({}, "A", "B") is None


def test_empty_payload():
    assert parse_scoreboard({}, "nfl") == ([], [])


def test_soccer_ignores_odds():
    games, _ = parse_scoreboard(FIXTURE, "epl")
    assert all(g.odds is None for g in games)


# ---------- bug bash: season type, canceled games, flexed kickoffs, team leaders ----------

def _with_status(state, name, completed=None, detail="Final"):
    p = json.loads(json.dumps(FIXTURE))
    t = p["events"][3]["competitions"][0]["status"]["type"]
    t.update({"state": state, "name": name, "shortDetail": detail, "detail": detail})
    if completed is None:
        t.pop("completed", None)
    else:
        t["completed"] = completed
    return p


def test_season_type_is_parsed():
    games, _ = parse_scoreboard(FIXTURE, "nfl")
    assert {g.season_type for g in games} == {2}
    p = json.loads(json.dumps(FIXTURE))
    p["season"]["type"] = 3
    assert {g.season_type for g in parse_scoreboard(p, "nfl")[0]} == {3}


def test_canceled_game_is_not_completed():
    # ESPN, BUF @ CIN 2023: {"name": "STATUS_CANCELED", "state": "post", "completed": false}, 0-0
    g = _by_id(parse_scoreboard(_with_status("post", "STATUS_CANCELED", False, "Canceled"), "nfl")[0])["401900004"]
    assert g.state == "post" and g.completed is False


def test_completed_flag_and_fallback_when_missing():
    assert _by_id(parse_scoreboard(_with_status("post", "STATUS_FINAL", True), "nfl")[0])["401900004"].completed
    # If ESPN ever drops 'completed', fall back to the status name.
    assert _by_id(parse_scoreboard(_with_status("post", "STATUS_FINAL"), "nfl")[0])["401900004"].completed
    assert not _by_id(parse_scoreboard(_with_status("post", "STATUS_POSTPONED", None, "Postponed"), "nfl")[0])["401900004"].completed
    assert not _by_id(parse_scoreboard(FIXTURE, "nfl")[0])["401900001"].completed        # upcoming


def test_flexed_game_has_no_valid_time():
    p = json.loads(json.dumps(FIXTURE))
    p["events"][0]["competitions"][0]["timeValid"] = False   # ESPN week 18 before flex: 05:00Z placeholder
    games = _by_id(parse_scoreboard(p, "nfl")[0])
    assert games["401900001"].time_valid is False and games["401900002"].time_valid is True


def test_team_interceptions_leader():
    from app.espn import parse_category_leader
    payload = json.loads((Path(__file__).parent / "fixtures" / "team_leaders_chi.json").read_text())
    top = parse_category_leader(payload, "interceptions")
    assert top["value"] == 1.0 and top["display"] == "1" and top["tied"] == 0
    assert top["athlete_ref"].endswith("/athletes/4870953?lang=en&region=us")      # Malik Muhammad II
    assert parse_category_leader(payload, "passingYards") is None                  # category not in payload
    assert parse_category_leader({"categories": [{"name": "interceptions", "leaders": []}]}, "interceptions") is None
    tied = {"categories": [{"name": "interceptions", "leaders": [
        {"value": 2.0, "displayValue": "2", "athlete": {"$ref": "a"}},
        {"value": 2.0, "displayValue": "2", "athlete": {"$ref": "b"}},
        {"value": 1.0, "displayValue": "1", "athlete": {"$ref": "c"}}]}]}
    assert parse_category_leader(tied, "interceptions")["tied"] == 1


# ---------- league ranks (C1) ----------

def test_parse_ranks_from_core_and_site_payloads():
    from app.espn import parse_ranks
    fx = Path(__file__).parent / "fixtures"
    core = json.loads((fx / "team_stats_chi_core.json").read_text())
    site = json.loads((fx / "team_stats_chi_site.json").read_text())
    assert parse_ranks(core, site) == {
        "totalPointsPerGame": {"value": 31.0, "rank": "Tied-4th"},
        "yardsPerGame": {"value": 443.0, "rank": "2nd"},
        "totalPointsPerGameAllowed": {"value": 23.0, "rank": "16th"},   # 1st = allowed the least
        "yardsPerGameAllowed": {"value": 374.0, "rank": "26th"},
    }
    assert parse_ranks(core, None) == {k: v for k, v in parse_ranks(core, site).items() if "Allowed" not in k}
    assert parse_ranks({}, {"results": "junk"}) == {}


# ---------- ESPN client: fast retries and the down switch ----------

def _mock_client(handler):
    import httpx
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=2)


def test_down_switch_fails_fast_then_recovers(monkeypatch):
    import time as _time
    from app import espn
    calls = {"n": 0, "ok": False}

    def handler(request):
        calls["n"] += 1
        return __import__("httpx").Response(200, json={"events": []}) if calls["ok"] else \
            __import__("httpx").Response(503)

    monkeypatch.setattr(espn, "_client", _mock_client(handler))
    espn.reset_down()
    t = _time.perf_counter()
    try:
        espn.fetch_scoreboard("nfl")
        raise AssertionError("expected ESPNError")
    except espn.ESPNError:
        pass
    assert calls["n"] == 2 and _time.perf_counter() - t < 1.0          # 2 attempts, 0.25 s apart
    try:
        espn.fetch_summary("nfl", "1")                                   # same host: skipped at once
        raise AssertionError("expected ESPNError")
    except espn.ESPNError as exc:
        assert "skipped" in str(exc)
    assert calls["n"] == 2
    calls["ok"] = True
    monkeypatch.setattr(espn, "_down_until", {})                         # window passed
    assert espn.fetch_scoreboard("nfl") == {"events": []}
    assert not espn.host_down(espn.BASE)
    espn.reset_down()


def test_explicit_client_bypasses_the_down_switch(monkeypatch):
    from app import espn
    espn.reset_down()
    espn._mark(espn.BASE + "/x", False)
    c = _mock_client(lambda r: __import__("httpx").Response(200, json={"ok": 1}))
    assert espn.fetch_scoreboard("nfl", client=c) == {"ok": 1}
    espn.reset_down()


# ---------- review fixes: what trips the down switch ----------

def _status_client(code, counter):
    import httpx

    def handler(request):
        counter["n"] += 1
        return httpx.Response(code, json={})
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=2)


def test_a_404_is_one_bad_request_not_an_outage(monkeypatch):
    from app import espn
    espn.reset_down()
    n = {"n": 0}
    monkeypatch.setattr(espn, "_client", _status_client(404, n))
    try:
        espn.fetch_summary("nfl", "999")
        raise AssertionError("expected ESPNError")
    except espn.ESPNError:
        pass
    assert n["n"] == 1                       # not retried: asking again won't change a 404
    assert not espn.host_down(espn.BASE)     # and the scoreboard isn't taken down with it
    espn.reset_down()


def test_403_block_trips_the_switch(monkeypatch):
    from app import espn
    espn.reset_down()
    n = {"n": 0}
    monkeypatch.setattr(espn, "_client", _status_client(403, n))
    try:
        espn.fetch_scoreboard("nfl")
    except espn.ESPNError:
        pass
    assert n["n"] == 2 and espn.host_down(espn.BASE)
    espn.reset_down()


def test_batch_commands_disable_the_switch(monkeypatch):
    from app import espn
    monkeypatch.setattr(espn, "DOWN_SECONDS", 30.0)
    espn.reset_down()
    n = {"n": 0}
    monkeypatch.setattr(espn, "_client", _status_client(503, n))
    espn.disable_down_switch()
    for _ in range(3):
        try:
            espn.fetch_scoreboard("nfl")
        except espn.ESPNError:
            pass
    assert n["n"] == 6                       # every call retried on its own, none skipped
    assert not espn.host_down(espn.BASE)
