"""Summary cache rules (games.needs_refresh). Pure: no database, no network."""
from datetime import datetime, timedelta, timezone

from app.games import needs_refresh

NOW = datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc)


def game(state="pre", minutes_to_kickoff=60, time_valid=True):
    return {"state": state, "start_time": NOW + timedelta(minutes=minutes_to_kickoff), "time_valid": time_valid}


def stored(state="pre", age_seconds=0):
    return {"game_state": state, "fetched_at": NOW - timedelta(seconds=age_seconds)}


def test_missing_summary_is_fetched():
    assert needs_refresh(game(), None, NOW)


def test_upcoming_refreshes_every_10_minutes():
    assert not needs_refresh(game(), stored(age_seconds=599), NOW)
    assert needs_refresh(game(), stored(age_seconds=600), NOW)


def test_after_kickoff_time_the_30_second_rule_applies():
    assert not needs_refresh(game(minutes_to_kickoff=-1), stored(age_seconds=29), NOW)
    assert needs_refresh(game(minutes_to_kickoff=-1), stored(age_seconds=30), NOW)
    assert needs_refresh(game("in"), stored("in", 30), NOW)


def test_flexed_placeholder_time_is_not_a_kickoff():
    # Week 18 before flex: start_time is midnight Eastern on the game date, already "past" on game day.
    assert not needs_refresh(game(minutes_to_kickoff=-600, time_valid=False), stored(age_seconds=60), NOW)


def test_final_fetched_once_then_kept():
    assert not needs_refresh(game("post"), stored("post", 10 ** 6), NOW)
    # Went final since: fetch the final, but at most once per 30 s while ESPN's summary lags (review finding 4).
    assert not needs_refresh(game("post"), stored("in", 5), NOW)
    assert needs_refresh(game("post"), stored("in", 30), NOW)


def test_postponed_games_summary_is_not_kept_as_a_final():
    # Postponed: state 'post' but not completed. Once rescheduled and played, it must be refetched.
    postponed = {**game("post", minutes_to_kickoff=-600), "completed": False}
    assert needs_refresh(postponed, stored("post", 31), NOW)
    completed = {**game("post", minutes_to_kickoff=-600), "completed": True}
    assert not needs_refresh(completed, stored("post", 10 ** 6), NOW)
