"""Phase 3 activities called as normal functions against a real Postgres (set TEST_DATABASE_URL), with the
Phase 1 scoreboard fixture and Phase 2 summaries in place of live ESPN (handoff step 4). Every activity must be
safe to run twice."""
import copy
import json
import os
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")

FIX = Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIX / "nfl_scoreboard.json").read_text())
NEWS = json.loads((FIX / "nfl_news.json").read_text())


@pytest.fixture()
def acts(monkeypatch):
    import psycopg
    from app import db, migrate
    from app.temporal import activities
    from test_api import default_summaries
    with psycopg.connect(TEST_DB) as conn:
        conn.execute("DROP TABLE IF EXISTS news_items, team_season_stats, team_season_leaders, bet_results, game_summaries,"
                     " fetch_log, odds_snapshots, games, teams, schema_migrations CASCADE")
        conn.commit()
    migrate.migrate(TEST_DB)
    monkeypatch.setattr(db, "DATABASE_URL", TEST_DB)

    boards = {}                                   # (week, season_type) -> payload; default: the fixture
    calls = {"boards": [], "boards_map": boards, "summaries": default_summaries(), "news": NEWS}

    def fake_scoreboard(league, week=None, season_type=None, base_url=None, **kw):
        calls["boards"].append((week, season_type))
        return copy.deepcopy(boards.get((week, season_type), FIXTURE))

    def fake_summary(league, event_id, base_url=None, **kw):
        return copy.deepcopy(calls["summaries"][event_id])

    monkeypatch.setattr(activities.espn, "fetch_scoreboard", fake_scoreboard)
    monkeypatch.setattr(activities.espn, "fetch_summary", fake_summary)
    monkeypatch.setattr(activities.espn, "fetch_news", lambda league, base_url=None, **kw: copy.deepcopy(calls["news"]))
    env = ActivityEnvironment()
    run = lambda fn, *a: env.run(fn, *a)          # noqa: E731
    run.calls = calls
    return run


def q(sql, *args):
    import psycopg
    with psycopg.connect(TEST_DB) as conn:
        return conn.execute(sql, args).fetchall()


def test_sync_schedule_returns_games_not_final(acts):
    from app.temporal import activities as a
    refs = acts(a.sync_schedule, "nfl")
    assert [r.espn_id for r in refs] == ["401900001", "401900003", "401900002"]      # DAL is final
    buf = refs[0]
    assert buf.start_iso == "2026-09-28T17:00:00+00:00"
    assert buf.preview_iso == "2026-09-28T15:00:00+00:00"                            # 8:00 AM PDT
    assert acts.calls["boards"] == [(None, None), (5, 2)]                             # this week and next
    # Lines saved by the worker are tagged, and a rerun adds nothing.
    n = q("select count(*) from odds_snapshots where source = 'workflow'")[0][0]
    assert n == 3
    assert [r.espn_id for r in acts(a.sync_schedule, "nfl")] == [r.espn_id for r in refs]
    assert q("select count(*) from odds_snapshots")[0][0] == 3


def test_sync_schedule_rolls_into_next_season_type(acts):
    from app.temporal import activities as a
    empty = copy.deepcopy(FIXTURE)
    empty["events"] = []
    acts.calls["boards_map"][(5, 2)] = empty
    acts(a.sync_schedule, "nfl")
    assert acts.calls["boards"] == [(None, None), (5, 2), (1, 3)]


def test_preview_time_uses_pacific_date():
    from datetime import datetime, timezone
    from app.temporal.activities import preview_iso
    # Sunday night game, 5:20 PM PT = 00:20 UTC Monday: preview is Sunday 8 AM PT.
    assert preview_iso(datetime(2026, 10, 5, 0, 20, tzinfo=timezone.utc)) == "2026-10-04T15:00:00+00:00"
    # After the clocks change (PST): 8 AM PT = 16:00 UTC.
    assert preview_iso(datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)) == "2026-11-08T16:00:00+00:00"


def test_save_line_adds_a_snapshot_only_when_the_line_moves(acts):
    from app.temporal import activities as a
    acts(a.sync_schedule, "nfl")
    acts(a.save_line, "nfl", "401900001")
    assert q("select count(*) from odds_snapshots")[0][0] == 3
    moved = copy.deepcopy(FIXTURE)
    o = moved["events"][0]["competitions"][0]["odds"][0]
    o["details"], o["spread"] = "BUF -7.5", -7.5
    acts.calls["boards_map"][(4, 2)] = moved
    acts(a.save_line, "nfl", "401900001")
    rows = q("""select s.home_spread, s.source from odds_snapshots s join games g on g.id = s.game_id
                where g.espn_id = '401900001' order by s.id""")
    assert [(float(h), s) for h, s in rows] == [(-6.5, "workflow"), (-7.5, "workflow")]


def test_save_line_for_unknown_game_fails_without_retry(acts):
    from app.temporal import activities as a
    with pytest.raises(ApplicationError) as e:
        acts(a.save_line, "nfl", "999")
    assert e.value.non_retryable


def test_fetch_game_state(acts):
    from app.temporal import activities as a
    acts(a.sync_schedule, "nfl")
    kc = acts(a.fetch_game_state, "nfl", "401900003")
    assert (kc.state, kc.home_score, kc.completed, kc.postponed) == ("in", 17, False, False)
    dal = acts(a.fetch_game_state, "nfl", "401900004")
    assert (dal.state, dal.completed) == ("post", True)
    assert dal.start_iso == "2026-09-27T00:15:00+00:00"


def test_fetch_game_state_postponed(acts):
    from app.temporal import activities as a
    acts(a.sync_schedule, "nfl")
    pp = copy.deepcopy(FIXTURE)
    status = pp["events"][0]["competitions"][0]["status"]
    status["type"].update({"name": "STATUS_POSTPONED", "state": "post", "completed": False})
    acts.calls["boards_map"][(4, 2)] = pp
    s = acts(a.fetch_game_state, "nfl", "401900001")
    assert (s.state, s.postponed, s.completed) == ("post", True, False)
    # ScheduleSync keeps a postponed game (a new date may come).
    acts.calls["boards_map"][(None, None)] = pp
    assert "401900001" in [r.espn_id for r in acts(a.sync_schedule, "nfl")]


def test_fetch_game_state_falls_back_to_summary(acts):
    from app.temporal import activities as a
    acts(a.sync_schedule, "nfl")
    moved = copy.deepcopy(FIXTURE)
    moved["events"] = [e for e in moved["events"] if e["id"] != "401900003"]
    acts.calls["boards_map"][(4, 2)] = moved
    s = acts(a.fetch_game_state, "nfl", "401900003")
    assert s.state == "in" and s.home_score == 17


def test_summary_and_grading_are_idempotent(acts):
    from app.temporal import activities as a
    acts(a.sync_schedule, "nfl")
    acts(a.fetch_summary, "nfl", "401900004")
    assert acts(a.grade_game, "nfl", "401900004") == [20, 27]
    acts(a.fetch_summary, "nfl", "401900004")
    assert acts(a.grade_game, "nfl", "401900004") == [20, 27]
    rows = q("""select r.market, r.outcome, r.line_source from bet_results r join games g on g.id = r.game_id
                where g.espn_id = '401900004' order by market""")
    assert rows == [("moneyline", "away", "espn_close"), ("spread", "away", "espn_close"), ("total", "over", "espn_close")]
    # Not a final: nothing to grade.
    assert acts(a.grade_game, "nfl", "401900001") == []


def test_regrade_picks_up_a_stat_correction(acts):
    from app.temporal import activities as a
    from test_api import make_summary
    acts(a.sync_schedule, "nfl")
    acts(a.fetch_summary, "nfl", "401900004")
    acts(a.grade_game, "nfl", "401900004")
    acts.calls["summaries"]["401900004"] = make_summary("401900004", "post", "DAL", "PHI", 20, 30,
                                                        close=(-3.5, 44.5, -170, 145))
    acts(a.fetch_summary, "nfl", "401900004")
    assert acts(a.grade_game, "nfl", "401900004") == [20, 30]
    assert q("select count(*) from bet_results")[0][0] == 3


def test_fetch_news_dedupes(acts):
    from app.temporal import activities as a
    assert acts(a.fetch_news, "nfl") == 4
    assert acts(a.fetch_news, "nfl") == 0
    more = copy.deepcopy(NEWS)
    more["articles"][0]["id"] = 1
    acts.calls["news"] = more
    assert acts(a.fetch_news, "nfl") == 1
    assert q("select count(*), count(distinct espn_id) from news_items")[0] == (5, 5)


def test_generate_preview_is_a_no_op(acts):
    from app.temporal import activities as a
    assert acts(a.generate_preview, "nfl", "401900001") is None
