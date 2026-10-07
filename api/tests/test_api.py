"""API tests against a real Postgres (set TEST_DATABASE_URL). ESPN is stubbed with fixtures,
so this checks the write path, caching, favorites pinning, line selection, degraded mode, and
(Phase 2) the game screens, summary cache and bet grading. Skipped when no test database is configured."""
import copy
import json
import os
from pathlib import Path

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")

FIX = Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIX / "nfl_scoreboard.json").read_text())
SUMMARY = {s: json.loads((FIX / f"summary_{s}.json").read_text()) for s in ("pre", "in", "post")}
TEAM_LEADERS = json.loads((FIX / "team_leaders_chi.json").read_text())
TEAM_STATS_CORE = json.loads((FIX / "team_stats_chi_core.json").read_text())   # CHI: 31.0 PPG T-4th, 443.0 YPG 2nd
TEAM_STATS_SITE = json.loads((FIX / "team_stats_chi_site.json").read_text())   # CHI allowed: 23.0 16th, 374.0 26th


def make_summary(event_id, state, home, away, home_score=None, away_score=None, close=None, injuries=True):
    """A real captured ESPN summary rewritten to describe one of the scoreboard fixture's games.
    close = (home_spread, total, home_ml, away_ml) for pickcenter, or None for no line at all."""
    p = copy.deepcopy(SUMMARY[state])
    comp = p["header"]["competitions"][0]
    p["header"]["id"] = event_id
    rename = {}
    for c in comp["competitors"]:
        new = home if c["homeAway"] == "home" else away
        rename[c["team"]["abbreviation"]] = new
        c["team"]["abbreviation"] = new
        c["score"] = str(home_score if c["homeAway"] == "home" else away_score) if state != "pre" else None
    for block in ("leaders", "injuries"):
        for t in p.get(block) or []:
            t["team"]["abbreviation"] = rename[t["team"]["abbreviation"]]
    if not injuries:
        for t in p["injuries"]:
            t["injuries"] = []
    if close is None:
        p["pickcenter"] = []
    else:
        hs, total, hml, aml = close
        pc = p["pickcenter"][0]
        pc["details"] = None
        pc["pointSpread"]["home"]["close"]["line"] = f"{hs:+}"
        pc["total"]["over"]["close"]["line"] = f"o{total}"
        pc["moneyline"]["home"]["close"]["odds"] = f"{hml:+d}"
        pc["moneyline"]["away"]["close"]["odds"] = f"{aml:+d}"
        pc["spread"], pc["overUnder"] = hs, total
        pc["homeTeamOdds"]["moneyLine"], pc["awayTeamOdds"]["moneyLine"] = hml, aml
    return p


def default_summaries():
    return {
        "401900001": make_summary("401900001", "pre", "BUF", "MIA", close=(-6.5, 48.5, -285, 230)),
        "401900002": make_summary("401900002", "pre", "SEA", "SF", close=(2.5, 44.5, 120, -142)),
        "401900003": make_summary("401900003", "in", "KC", "DEN", 17, 10, close=(-3, 43.5, -160, 135)),
        "401900004": make_summary("401900004", "post", "DAL", "PHI", 20, 27, close=(-3.5, 44.5, -170, 145)),
    }


@pytest.fixture()
def client(monkeypatch, tmp_path):
    os.environ["DATABASE_URL"] = TEST_DB
    import psycopg
    from app import migrate
    with psycopg.connect(TEST_DB) as conn:
        conn.execute("DROP TABLE IF EXISTS ai_calls, ai_cooling, ai_texts, league_calendar, news_items, team_season_stats, team_season_leaders, bet_results, game_summaries, fetch_log, odds_snapshots,"
                     " games, teams,"
                     " schema_migrations CASCADE")
        conn.commit()
    migrate.migrate(TEST_DB)

    fav = tmp_path / "favorites.json"
    fav.write_text(json.dumps({"nfl": ["SEA"]}))

    from app import db, main
    monkeypatch.setattr(db, "DATABASE_URL", TEST_DB)
    monkeypatch.setattr(main, "FAVORITES_FILE", fav)
    calls = {"n": 0, "fail": False, "payload": FIXTURE, "summary_calls": {}, "summaries": default_summaries()}

    def fake_fetch(league, week=None, base_url=None, **kw):
        calls["n"] += 1
        if calls["fail"]:
            raise RuntimeError("ESPN down")
        return calls["payload"]

    def fake_summary(league, event_id, base_url=None, **kw):
        calls["summary_calls"][event_id] = calls["summary_calls"].get(event_id, 0) + 1
        if calls["fail"]:
            raise RuntimeError("ESPN down")
        return copy.deepcopy(calls["summaries"][event_id])

    calls["leader_calls"] = 0
    calls["team_leaders"] = {}                      # team espn id -> payload; default: CHI's real one

    def fake_team_leaders(league, team_id, season, season_type, **kw):
        calls["leader_calls"] += 1
        if calls["fail"]:
            raise RuntimeError("ESPN down")
        return copy.deepcopy(calls["team_leaders"].get(team_id, TEAM_LEADERS))

    def fake_ref(ref, **kw):
        if calls["fail"]:
            raise RuntimeError("ESPN down")
        return {"displayName": "Malik Muhammad II", "lastName": "Muhammad II", "position": {"abbreviation": "CB"}}

    monkeypatch.setattr(main.espn, "fetch_scoreboard", fake_fetch)
    monkeypatch.setattr(main.espn, "fetch_summary", fake_summary)
    def fake_team_stats(league, team_id, season, season_type, **kw):
        calls["leader_calls"] += 1
        if calls["fail"]:
            raise RuntimeError("ESPN down")
        return copy.deepcopy(TEAM_STATS_CORE)

    def fake_opponent_stats(league, team_id, season, season_type, **kw):
        calls["leader_calls"] += 1
        if calls["fail"]:
            raise RuntimeError("ESPN down")
        return copy.deepcopy(TEAM_STATS_SITE)

    monkeypatch.setattr(main.espn, "fetch_team_leaders", fake_team_leaders)
    monkeypatch.setattr(main.espn, "fetch_ref", fake_ref)
    monkeypatch.setattr(main.espn, "fetch_team_stats", fake_team_stats)
    monkeypatch.setattr(main.espn, "fetch_team_opponent_stats", fake_opponent_stats)
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    c.calls = calls
    return c


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_scoreboard_writes_and_orders(client):
    body = client.get("/api/scoreboard/nfl").json()
    assert body["week"] == 4 and body["stale"] is False
    ids = [g["home"]["abbr"] for g in body["games"]]
    # SEA (favorite) pinned first, then live KC, then upcoming BUF, then final DAL
    assert ids == ["SEA", "KC", "BUF", "DAL"]
    sea = body["games"][0]
    assert sea["favorite"] and sea["line"]["home_spread"] == 2.5


def test_cache_prevents_refetch(client):
    client.get("/api/scoreboard/nfl")
    client.get("/api/scoreboard/nfl")
    assert client.calls["n"] == 1
    client.get("/api/scoreboard/nfl?force=true")
    assert client.calls["n"] == 2


def test_live_game_without_pregame_line_falls_back_and_flags(client):
    body = client.get("/api/scoreboard/nfl").json()
    kc = next(g for g in body["games"] if g["home"]["abbr"] == "KC")
    # Line was only ever seen after kickoff: show it, but flag it.
    assert kc["line"]["home_spread"] == -3.0
    assert kc["line"]["captured_after_kickoff"] is True


def test_pregame_line_survives_kickoff(client):
    client.get("/api/scoreboard/nfl")
    # BUF game kicks off; ESPN now shows a live line
    payload = json.loads(json.dumps(FIXTURE))
    comp = payload["events"][0]["competitions"][0]
    comp["status"]["type"]["state"] = "in"
    comp["odds"][0]["details"] = "BUF -10.5"
    comp["odds"][0]["spread"] = -10.5
    client.calls["payload"] = payload
    body = client.get("/api/scoreboard/nfl?force=true").json()
    buf = next(g for g in body["games"] if g["home"]["abbr"] == "BUF")
    assert buf["state"] == "in" and buf["line"]["home_spread"] == -6.5
    assert buf["line"]["captured_after_kickoff"] is False


def test_degraded_mode_serves_stale(client):
    client.get("/api/scoreboard/nfl")
    client.calls["fail"] = True
    body = client.get("/api/scoreboard/nfl?force=true").json()
    assert body["stale"] is True and len(body["games"]) == 4


def test_nothing_stored_and_espn_down(client):
    client.calls["fail"] = True
    assert client.get("/api/scoreboard/nfl").status_code == 502


def test_browsing_a_week_does_not_hijack_current(client):
    client.get("/api/scoreboard/nfl")                 # current = week 4
    past = json.loads(json.dumps(FIXTURE))
    past["week"]["number"] = 3
    for ev in past["events"]:
        ev["id"] = ev["id"] + "3"
    client.calls["payload"] = past
    assert client.get("/api/scoreboard/nfl?week=3").json()["week"] == 3
    # Current-week request is still cached as week 4, not the week we browsed.
    assert client.get("/api/scoreboard/nfl").json()["week"] == 4


def test_game_stub_routes_by_state(client):
    body = client.get("/api/scoreboard/nfl").json()
    screens = {g["home"]["abbr"]: client.get(f"/api/games/{g['id']}").json()["screen"] for g in body["games"]}
    assert screens == {"SEA": "C1", "KC": "C2", "BUF": "C1", "DAL": "D"}


# ---------------- Phase 2: game screens, summary cache, grading ----------------

def _sql(sql, *args):
    import psycopg
    with psycopg.connect(TEST_DB) as conn:
        cur = conn.execute(sql, args)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows


def _ids(client):
    body = client.get("/api/scoreboard/nfl").json()
    return {g["home"]["abbr"]: g["id"] for g in body["games"]}


def _age_summary(game_id, seconds):
    _sql("UPDATE game_summaries SET fetched_at = now() - make_interval(secs => %s) WHERE game_id = %s",
         seconds, game_id)


def test_migrations_applied_and_idempotent(client):
    from app import migrate
    names = [r[0] for r in _sql("SELECT name FROM schema_migrations ORDER BY name")]
    assert names == sorted(p.name for p in migrate.MIGRATIONS_DIR.glob("*.sql"))          # every file, once
    assert names[:3] == ["001_init.sql", "002_game_details.sql", "003_season_type_and_status.sql"]
    assert names[-1] == "010_ai_call_cache.sql"
    assert _sql("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'ai_texts' AND column_name = 'rejections'""") == [(1,)]
    assert _sql("""SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_name = 'ai_calls' AND column_name IN ('kind', 'cached_tokens', 'remaining_tokens',
                                                                     'reset_tokens_secs')
                   ORDER BY column_name""") == [("cached_tokens", "integer"), ("kind", "text"),
                                                ("remaining_tokens", "integer"), ("reset_tokens_secs", "real")]
    assert migrate.migrate(TEST_DB) == []                    # second run applies nothing
    tables = {r[0] for r in _sql("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    assert {"game_summaries", "bet_results", "ai_texts", "ai_calls", "ai_cooling"} <= tables


def test_c1_pregame(client):
    g = client.get(f"/api/games/{_ids(client)['SEA']}").json()
    assert g["screen"] == "C1" and g["stale"] is False and g["summary_available"] is True
    assert g["header"]["away"]["record"] and g["header"]["home"]["record"]
    assert [r["key"] for r in g["team_stats"]["rows"]][:2] == ["totalPointsPerGame", "totalPointsPerGameAllowed"]
    assert g["team_stats"]["kind"] == "season" and len(g["leaders"]["rows"]) == 5
    assert len(g["injuries"]["home"]) == 5 and len(g["injuries"]["away"]) == 5
    assert g["line"]["home_spread"] == 2.5                   # the scoreboard's saved line
    assert g["bets"] is None and g["placeholders"] == ["Things you should know", "Edges", "Writers' picks"]


def test_c1_no_injuries_is_an_empty_list(client):
    client.calls["summaries"]["401900002"] = make_summary("401900002", "pre", "SEA", "SF", injuries=False,
                                                          close=(2.5, 44.5, 120, -142))
    g = client.get(f"/api/games/{_ids(client)['SEA']}").json()
    assert g["injuries"] == {"home": [], "away": []}         # the page says "None reported"


def test_c2_live_bet_status_so_far(client):
    g = client.get(f"/api/games/{_ids(client)['KC']}").json()
    assert g["screen"] == "C2" and g["situation"]["down_distance"]
    bets = {b["market"]: b for b in g["bets"]}
    # KC was already live when first seen, so there is no pre-game snapshot: ESPN's close is used.
    assert bets["spread"]["text"] == "KC -3 covering by 4" and bets["spread"]["line_source"] == "espn_close"
    assert bets["total"]["text"] == "27 pts so far vs O/U 43.5 · 17 more to go over"
    assert bets["moneyline"]["text"] == "KC leads by 7" and bets["moneyline"]["status"] == "so_far"
    assert g["one_liner"].startswith("KC lead 17–10")
    assert _sql("SELECT count(*) FROM bet_results")[0][0] == 0   # live status is never stored
    assert [r["key"] for r in g["team_stats"]["rows"]] == [
        "totalYards", "netPassingYards", "rushingYards", "turnovers", "thirdDownEff", "possessionTime",
        "totalPenaltiesYards"]


def test_d_final_graded_once(client):
    dal = _ids(client)["DAL"]
    g = client.get(f"/api/games/{dal}").json()
    assert g["screen"] == "D" and g["header"]["home"]["linescores"]
    bets = {b["market"]: b for b in g["bets"]}
    # PHI 27 @ DAL 20, DAL -3.5, O/U 44.5 (ESPN close; no line was seen before kickoff)
    assert bets["moneyline"]["text"] == "PHI won by 7"
    assert bets["spread"]["text"] == "PHI +3.5 covered by 10.5"
    assert bets["total"]["text"] == "Over 44.5 by 2.5"
    assert all(b["line_source"] == "espn_close" for b in bets.values())
    first = _sql("SELECT market, graded_at FROM bet_results WHERE game_id = %s ORDER BY market", dal)
    assert len(first) == 3
    client.get(f"/api/games/{dal}")
    assert _sql("SELECT market, graded_at FROM bet_results WHERE game_id = %s ORDER BY market", dal) == first
    assert client.calls["summary_calls"]["401900004"] == 1


def test_final_summary_fetched_once_and_kept(client):
    dal = _ids(client)["DAL"]
    client.get(f"/api/games/{dal}")
    _age_summary(dal, 86400)
    client.get(f"/api/games/{dal}")
    assert client.calls["summary_calls"]["401900004"] == 1


def test_live_summary_cached_30_seconds(client):
    kc = _ids(client)["KC"]
    client.get(f"/api/games/{kc}")
    client.get(f"/api/games/{kc}")
    assert client.calls["summary_calls"]["401900003"] == 1
    _age_summary(kc, 31)
    client.get(f"/api/games/{kc}")
    assert client.calls["summary_calls"]["401900003"] == 2


def test_regrade_on_changed_score_no_duplicates(client):
    dal = _ids(client)["DAL"]
    client.get(f"/api/games/{dal}")
    _sql("UPDATE games SET home_score = 31 WHERE id = %s", dal)      # stored final changes: DAL 31, PHI 27
    g = client.get(f"/api/games/{dal}").json()
    bets = {b["market"]: b for b in g["bets"]}
    assert bets["moneyline"]["text"] == "DAL won by 4"
    assert bets["spread"]["text"] == "DAL -3.5 covered by 0.5"
    assert bets["total"]["text"] == "Over 44.5 by 13.5"
    rows = _sql("SELECT market, count(*), max(home_score) FROM bet_results WHERE game_id = %s GROUP BY market", dal)
    assert sorted(rows) == [("moneyline", 1, 31), ("spread", 1, 31), ("total", 1, 31)]


def test_stat_correction_from_scoreboard_regrades(client):
    dal = _ids(client)["DAL"]
    client.get(f"/api/games/{dal}")
    corrected = json.loads(json.dumps(FIXTURE))
    for c in corrected["events"][3]["competitions"][0]["competitors"]:
        if c["homeAway"] == "home":
            c["score"] = "24"                                        # DAL 24, PHI 27
    client.calls["payload"] = corrected
    client.get("/api/scoreboard/nfl?force=true")
    rows = {m: (o, float(mg), hs) for m, o, mg, hs in
            _sql("SELECT market, outcome, margin, home_score FROM bet_results WHERE game_id = %s", dal)}
    # Was 20-27: PHI +3.5 by 10.5, over by 2.5. Now 24-27 with DAL -3.5: -3 - 3.5 = -6.5, 51 - 44.5 = +6.5
    assert rows == {"moneyline": ("away", 3.0, 24), "spread": ("away", 6.5, 24), "total": ("over", 6.5, 24)}


def test_no_line_is_ungraded(client):
    client.calls["summaries"]["401900004"] = make_summary("401900004", "post", "DAL", "PHI", 20, 27, close=None)
    g = client.get(f"/api/games/{_ids(client)['DAL']}").json()
    assert [(b["market"], b["status"], b["text"]) for b in g["bets"]] == [
        ("moneyline", "ungraded", "Ungraded"), ("spread", "ungraded", "Ungraded"), ("total", "ungraded", "Ungraded")]
    assert _sql("SELECT count(*) FROM bet_results")[0][0] == 0


def test_espn_down_serves_stored_summary_with_banner(client):
    sea = _ids(client)["SEA"]
    client.get(f"/api/games/{sea}")
    _age_summary(sea, 3600)
    client.calls["fail"] = True
    g = client.get(f"/api/games/{sea}").json()
    assert g["stale"] is True and g["error"] and g["summary_available"] is True
    assert g["team_stats"]["rows"] and g["leaders"]["rows"] and g["injuries"]["home"]


def test_espn_down_and_nothing_stored_still_renders(client):
    sea = _ids(client)["SEA"]
    client.calls["fail"] = True
    r = client.get(f"/api/games/{sea}")
    assert r.status_code == 200
    g = r.json()
    assert g["stale"] is True and g["summary_available"] is False and g["screen"] == "C1"
    assert g["home"]["abbr"] == "SEA" and g["team_stats"]["rows"] == [] and g["injuries"] is None


def test_wrong_event_in_summary_is_rejected(client):
    client.calls["summaries"]["401900002"] = make_summary("401900001", "pre", "SEA", "SF")
    g = client.get(f"/api/games/{_ids(client)['SEA']}").json()
    assert g["stale"] is True and g["summary_available"] is False


def test_lifecycle_c1_c2_d_on_pulls_grades_espn_close(client):
    """A game opened before kickoff shows C1, then C2 after kickoff, then D after the final, each on a
    pull of the game page. Grading uses ESPN's closing line (-10.5), not the older line we saved (-6.5)."""
    buf = _ids(client)["BUF"]
    assert client.get(f"/api/games/{buf}").json()["screen"] == "C1"

    client.calls["summaries"]["401900001"] = make_summary("401900001", "in", "BUF", "MIA", 7, 3,
                                                          close=(-10.5, 50.5, -900, 600))
    _age_summary(buf, 601)
    g = client.get(f"/api/games/{buf}").json()
    assert g["screen"] == "C2" and (g["home"]["score"], g["away"]["score"]) == (7, 3)
    assert {b["market"]: b["line_source"] for b in g["bets"]}["spread"] == "espn_close"

    client.calls["summaries"]["401900001"] = make_summary("401900001", "post", "BUF", "MIA", 27, 17,
                                                          close=(-10.5, 50.5, -900, 600))
    _age_summary(buf, 31)
    g = client.get(f"/api/games/{buf}").json()
    assert g["screen"] == "D"
    bets = {b["market"]: b for b in g["bets"]}
    # 27-17 is +10; BUF -10.5 -> MIA +10.5 covers by 0.5. On the old saved -6.5 it would read "BUF covered".
    assert bets["spread"]["text"] == "MIA +10.5 covered by 0.5" and bets["spread"]["line_source"] == "espn_close"
    assert bets["total"]["text"] == "Under 50.5 by 6.5"


# ---------------- C1 INTs leader ----------------

def test_c1_leaders_are_passing_rushing_receiving_tackles_ints(client):
    g = client.get(f"/api/games/{_ids(client)['SEA']}").json()
    rows = g["leaders"]["rows"]
    assert [r["label"] for r in rows] == ["Passing", "Rushing", "Receiving", "Tackles", "INTs"]
    ints = rows[-1]
    assert ints["home"] == {"name": "Malik Muhammad II", "last_name": "Muhammad II", "position": "CB",
                            "value": "1 INT", "tied": 0}
    assert client.calls["leader_calls"] == 6                   # stats, allowed stats, leaders: per team
    client.get(f"/api/games/{_ids(client)['SEA']}")
    assert client.calls["leader_calls"] == 6                   # cached for hours: season stats change weekly


def test_c1_team_with_no_interceptions(client):
    client.calls["team_leaders"]["26"] = {"categories": [{"name": "interceptions", "leaders": []}]}   # SEA
    g = client.get(f"/api/games/{_ids(client)['SEA']}").json()
    ints = g["leaders"]["rows"][-1]
    assert ints["home"] == {"name": None, "value": "None this season"}
    assert ints["away"]["name"] == "Malik Muhammad II"


def test_c1_team_leaders_down_keeps_the_page(client):
    sea = _ids(client)["SEA"]
    client.get(f"/api/games/{sea}")                            # stores summary + INT leaders
    _sql("UPDATE team_season_stats SET fetched_at = now() - interval '1 day'")
    _age_summary(sea, 3600)
    client.calls["fail"] = True
    g = client.get(f"/api/games/{sea}").json()
    assert g["stale"] is True
    assert g["leaders"]["rows"][-1]["home"]["name"] == "Malik Muhammad II"   # stored value served
    assert g["team_stats"]["rows"][0]["home_rank"] == "Tied-4th"             # stored rank served
    # The failed refetch isn't saved, so the next open tries again.
    assert _sql("SELECT bool_and(fetched_at < now() - interval '1 hour') FROM team_season_stats")[0][0] is True


# ---------------- C1 league ranks ----------------

def test_c1_ranks_attach_only_to_matching_values(client):
    # SEA's summary carries CHI's real season stats on the home side (31.0 / 23.0 / 443.0 / 374.0) and PHI's
    # on the away side; the fake rank payloads are CHI's for both teams.
    g = client.get(f"/api/games/{_ids(client)['SEA']}").json()
    rows = {r["key"]: r for r in g["team_stats"]["rows"]}
    assert rows["totalPointsPerGame"]["home_rank"] == "Tied-4th"
    assert rows["yardsPerGame"]["home_rank"] == "2nd"
    assert rows["totalPointsPerGameAllowed"]["home_rank"] == "16th"
    assert rows["yardsPerGameAllowed"]["home_rank"] == "26th"
    # PHI's 24.0 PPG isn't the 31.0 that rank belongs to: no rank rather than a wrong one.
    assert all("away_rank" not in r for r in rows.values())


def test_c1_ranks_cached_with_the_team(client):
    client.get(f"/api/games/{_ids(client)['SEA']}")
    data = _sql("SELECT data FROM team_season_stats WHERE team_espn_id = '26'")[0][0]
    assert data["ranks"]["yardsPerGame"] == {"value": 443.0, "rank": "2nd"}
    assert data["interceptions"]["name"] == "Malik Muhammad II"


# ---------------- bug bash ----------------

def test_canceled_game_is_never_graded(client):
    dal = _ids(client)["DAL"]
    client.get(f"/api/games/{dal}")
    assert _sql("SELECT count(*) FROM bet_results WHERE game_id = %s", dal)[0][0] == 3
    canceled = json.loads(json.dumps(FIXTURE))
    comp = canceled["events"][3]["competitions"][0]
    comp["status"]["type"].update({"name": "STATUS_CANCELED", "state": "post", "completed": False,
                                   "detail": "Canceled", "shortDetail": "Canceled"})
    for c in comp["competitors"]:
        c["score"] = "0"
    client.calls["payload"] = canceled
    client.get("/api/scoreboard/nfl?force=true")
    assert _sql("SELECT count(*) FROM bet_results WHERE game_id = %s", dal)[0][0] == 0
    g = client.get(f"/api/games/{dal}").json()
    assert g["completed"] is False
    assert [(b["status"], b["text"]) for b in g["bets"]] == [("not_played", "Not graded · Canceled")] * 3


def test_preseason_and_regular_weeks_do_not_mix(client):
    pre = json.loads(json.dumps(FIXTURE))
    pre["season"]["type"] = 1                                  # preseason week 4, same season
    for ev in pre["events"]:
        ev["id"] = "9" + ev["id"]
    client.calls["payload"] = pre
    client.get("/api/scoreboard/nfl?week=4")                   # browsing stores 4 preseason games
    client.calls["payload"] = FIXTURE
    body = client.get("/api/scoreboard/nfl?force=true").json()
    assert body["week"] == 4 and len(body["games"]) == 4       # not 8
    assert _sql("SELECT count(*) FROM games WHERE week = 4")[0][0] == 8


def test_lagging_summary_cannot_undo_a_final(client):
    kc = _ids(client)["KC"]
    client.get(f"/api/games/{kc}")                             # stores a live summary
    final = json.loads(json.dumps(FIXTURE))
    final["events"][2]["competitions"][0]["status"]["type"].update(
        {"state": "post", "name": "STATUS_FINAL", "completed": True, "detail": "Final", "shortDetail": "Final"})
    client.calls["payload"] = final
    client.get("/api/scoreboard/nfl?force=true")               # scoreboard: KC final 17-10
    g = client.get(f"/api/games/{kc}").json()                  # summary still says live (lagging)
    assert g["screen"] == "D" and g["state"] == "post"
    assert _sql("SELECT state, completed FROM games WHERE id = %s", kc)[0] == ("post", True)
    assert len([b for b in g["bets"] if b["status"] == "graded"]) == 3


def test_flexed_game_card_says_time_tbd(client):
    flex = json.loads(json.dumps(FIXTURE))
    flex["events"][0]["competitions"][0]["timeValid"] = False
    client.calls["payload"] = flex
    body = client.get("/api/scoreboard/nfl?force=true").json()
    buf = next(g for g in body["games"] if g["home"]["abbr"] == "BUF")
    assert buf["time_valid"] is False
    assert next(g for g in body["games"] if g["home"]["abbr"] == "SEA")["time_valid"] is True



# ---------------- independent review findings ----------------

def test_saved_line_used_when_espn_has_no_close(client):
    # BUF's line was saved before kickoff (-6.5); ESPN's final summary has no pickcenter at all.
    buf = _ids(client)["BUF"]
    client.get(f"/api/games/{buf}")
    client.calls["summaries"]["401900001"] = make_summary("401900001", "post", "BUF", "MIA", 27, 17, close=None)
    final = json.loads(json.dumps(FIXTURE))
    t = final["events"][0]["competitions"][0]
    t["status"]["type"].update({"state": "post", "name": "STATUS_FINAL", "completed": True, "shortDetail": "Final"})
    for c in t["competitors"]:
        c["score"] = "27" if c["homeAway"] == "home" else "17"
    client.calls["payload"] = final
    client.get("/api/scoreboard/nfl?force=true")
    g = client.get(f"/api/games/{buf}").json()
    bets = {b["market"]: b for b in g["bets"]}
    assert bets["spread"]["text"] == "BUF -6.5 covered by 3.5" and bets["spread"]["line_source"] == "pre_game"


def test_unreadable_scoreboard_is_stale_not_fresh(client):
    client.get("/api/scoreboard/nfl")
    broken = json.loads(json.dumps(FIXTURE))
    for ev in broken["events"]:
        for c in (ev.get("competitions") or [{}])[0].get("competitors", []):
            c["homeAway"] = "HOME_TEAM"                  # ESPN renames a field: nothing parses
    client.calls["payload"] = broken
    body = client.get("/api/scoreboard/nfl?force=true").json()
    assert body["stale"] is True and "could be read" in body["error"]
    assert len(body["games"]) == 4                       # the stored board, under the stale banner


def test_partly_unreadable_scoreboard_warns(client):
    body = client.get("/api/scoreboard/nfl").json()      # the fixture has one malformed event
    assert body["stale"] is False and body["warning"]
    clean = json.loads(json.dumps(FIXTURE))
    clean["events"] = [e for e in clean["events"] if e["id"] != "401900099"]
    client.calls["payload"] = clean
    assert client.get("/api/scoreboard/nfl?force=true").json()["warning"] is None


def test_c2_withholds_pregame_summary_blocks(client):
    # KC is live on the scoreboard but ESPN's summary still says pre-game (it lags a refresh).
    client.calls["summaries"]["401900003"] = make_summary("401900003", "pre", "KC", "DEN", close=(-3, 43.5, -160, 135))
    g = client.get(f"/api/games/{_ids(client)['KC']}").json()
    assert g["screen"] == "C2" and g["summary_behind"] is True
    assert g["leaders"]["rows"] == [] and g["team_stats"]["rows"] == []   # no season leaders as "Game leaders"
    assert g["header"]["home"]["linescores"] == [] and g["one_liner"] is None
    assert g["state"] == "in"                                               # and the game stays live


def test_scoreboard_cannot_move_a_final_backwards(client):
    dal = _ids(client)["DAL"]
    lagging = json.loads(json.dumps(FIXTURE))
    lagging["events"][3]["competitions"][0]["status"]["type"].update(
        {"state": "in", "name": "STATUS_IN_PROGRESS", "completed": False, "shortDetail": "4:02 - 4th"})
    client.calls["payload"] = lagging
    client.get("/api/scoreboard/nfl?force=true")
    assert _sql("SELECT state, completed FROM games WHERE id = %s", dal)[0] == ("post", True)


def test_ranks_refetched_after_another_game_finishes(client):
    sea = _ids(client)["SEA"]
    client.get(f"/api/games/{sea}")
    n = client.calls["leader_calls"]
    client.get(f"/api/games/{sea}")
    assert client.calls["leader_calls"] == n                  # nothing finished: cached
    final = json.loads(json.dumps(FIXTURE))
    final["events"][2]["competitions"][0]["status"]["type"].update(
        {"state": "post", "name": "STATUS_FINAL", "completed": True, "shortDetail": "Final"})
    client.calls["payload"] = final
    client.get("/api/scoreboard/nfl?force=true")              # KC-DEN goes final: ranks may have moved
    client.get(f"/api/games/{sea}")
    assert client.calls["leader_calls"] > n


# ---------------- independent re-check findings ----------------

def _lagging(event_index, state, name, detail, home=None, away=None):
    p = json.loads(json.dumps(FIXTURE))
    comp = p["events"][event_index]["competitions"][0]
    comp["status"]["type"].update({"state": state, "name": name, "completed": False, "shortDetail": detail})
    for c in comp["competitors"]:
        score = home if c["homeAway"] == "home" else away
        if score is not None:
            c["score"] = str(score)
    return p


def test_lagging_scoreboard_cannot_rewrite_a_final_score_or_its_grades(client):
    # The reviewer's reproduction: PHI 27 @ DAL 20 is final and graded; the scoreboard then lags with
    # "in, 2:00 - 4th, PHI 21-20". Nothing about the final may change: state, score, or bet results.
    dal = _ids(client)["DAL"]
    client.get(f"/api/games/{dal}")
    before = _sql("SELECT market, outcome, margin, home_score, away_score FROM bet_results WHERE game_id = %s ORDER BY market", dal)
    client.calls["payload"] = _lagging(3, "in", "STATUS_IN_PROGRESS", "2:00 - 4th", home=20, away=21)
    client.get("/api/scoreboard/nfl?force=true")
    assert _sql("SELECT state, completed, home_score, away_score FROM games WHERE id = %s", dal)[0] == ("post", True, 20, 27)
    assert _sql("SELECT market, outcome, margin, home_score, away_score FROM bet_results WHERE game_id = %s ORDER BY market", dal) == before
    g = client.get(f"/api/games/{dal}").json()
    assert (g["away"]["score"], g["home"]["score"]) == (27, 20) and {b["text"] for b in g["bets"]} >= {"Over 44.5 by 2.5"}


def test_lagging_pregame_scoreboard_keeps_a_live_score(client):
    kc = _ids(client)["KC"]                                      # live 17-10
    client.calls["payload"] = _lagging(2, "pre", "STATUS_SCHEDULED", "Sun 1:00 PM")
    body = client.get("/api/scoreboard/nfl?force=true").json()
    card = next(g for g in body["games"] if g["id"] == kc)
    assert card["state"] == "in" and (card["home"]["score"], card["away"]["score"]) == (17, 10)


def test_empty_event_list_for_a_week_with_games_is_stale(client):
    client.get("/api/scoreboard/nfl")
    empty = json.loads(json.dumps(FIXTURE))
    empty["events"] = []                                          # ESPN moved or dropped the list
    client.calls["payload"] = empty
    body = client.get("/api/scoreboard/nfl?force=true").json()
    assert body["stale"] is True and "no games" in body["error"] and len(body["games"]) == 4


def test_empty_week_with_no_stored_games_is_just_empty(client):
    client.get("/api/scoreboard/nfl")
    bye = json.loads(json.dumps(FIXTURE))
    bye["events"], bye["week"]["number"] = [], 9              # a week we hold nothing for
    client.calls["payload"] = bye
    body = client.get("/api/scoreboard/nfl?week=9").json()
    assert body["stale"] is False and body["games"] == [] and body["week"] == 9


def test_ranks_rechecked_every_10_minutes_after_a_recent_final(client):
    ids = _ids(client)
    sea, dal = ids["SEA"], ids["DAL"]
    client.get(f"/api/games/{sea}")
    n = client.calls["leader_calls"]
    # The only final (DAL-PHI) kicked off long ago: an 11-minute-old cache is still fresh (1 h rule).
    _sql("UPDATE games SET start_time = now() - interval '2 days' WHERE id = %s", dal)
    _sql("UPDATE team_season_stats SET fetched_at = now() - interval '11 minutes'")
    client.get(f"/api/games/{sea}")
    assert client.calls["leader_calls"] == n
    # A final from 3 hours ago: ESPN may still be recomputing ranks, so re-check after 10 minutes.
    _sql("UPDATE games SET start_time = now() - interval '3 hours' WHERE id = %s", dal)
    _sql("UPDATE team_season_stats SET fetched_at = now() - interval '11 minutes'")
    client.get(f"/api/games/{sea}")
    assert client.calls["leader_calls"] > n


def test_pull_snapshots_are_tagged_pull(client):
    client.get("/api/scoreboard/nfl")
    assert _sql("SELECT DISTINCT source FROM odds_snapshots") == [("pull",)]
