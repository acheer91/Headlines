"""The weekend column's fact sheet (app.ai.weekend_facts): pure, built from rows the job reads."""
from datetime import datetime, timedelta, timezone

from app.ai import weekend_facts as wf

T0 = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)


def game(away, a, home, h, i=0, **kw):
    return {"league": "ncaaf", "away": away, "away_score": a, "home": home, "home_score": h, "status_detail": "Final",
            "start_time": T0 + timedelta(hours=i), "home_rank": None, "away_rank": None, "recap": None, **kw}


def test_result_line_puts_the_winner_first_and_tags_what_the_score_proves():
    assert wf.result_line(game("Eagles", 7, "Bears", 31)) == "Bears 31, Eagles 7 (blowout; margin 24)"
    assert wf.result_line(game("Bills", 24, "Jets", 21)) == "Bills 24, Jets 21 (one-score game; margin 3)"
    assert wf.result_line(game("Bills", 20, "Jets", 20)) == "Jets 20, Bills 20 (tie)"


def test_an_unranked_or_lower_ranked_winner_over_a_ranked_team_is_an_upset():
    assert "upset" not in wf.tags(game("Oklahoma", 17, "Texas", 20, home_rank=5))      # the ranked home team held
    assert "upset" in wf.tags(game("Oklahoma", 31, "Texas", 20, home_rank=5))           # an unranked winner
    both = game("Utah", 28, "Baylor", 14, away_rank=5, home_rank=20)                      # No. 5 beats No. 20: expected
    assert "upset" not in wf.tags(both) and "ranked vs ranked" in wf.tags(both)
    flip = game("Utah", 28, "Baylor", 14, away_rank=20, home_rank=5)                      # No. 20 beats No. 5: an upset
    assert "upset" in wf.tags(flip)
    assert wf.result_line(flip) == "No. 20 Utah 28, No. 5 Baylor 14 (upset; ranked vs ranked; margin 14)"


def test_overtime_comes_from_the_status():
    assert "overtime" in wf.tags(game("A", 27, "B", 30, status_detail="Final/OT"))
    assert "overtime" not in wf.tags(game("A", 27, "B", 30))


def test_notes_come_only_from_stored_recaps_and_the_most_interesting_games_get_them():
    rows = [game("A", 10, "B", 40, i=0, recap="One. Two. Three."),                       # a blowout, has a recap
            game("C", 28, "D", 27, i=1, recap="Close one. Late stop. Third."),         # one point: interesting
            game("E", 20, "F", 17, i=2)]                                                 # interesting, but no recap
    facts = wf.build("ncaaf", rows, [])
    assert [n["note"] for n in facts["notes"]] == ["One. Two.", "Close one. Late stop."]
    assert facts["games_played"] == 3 and len(facts["results"]) == 3 and facts["league"] == "NCAAF"
    many = [game(f"A{i}", 10, f"B{i}", 14, i=i, recap="x. y.") for i in range(10)]
    assert len(wf.build("nfl", many, [])["notes"]) == wf.NOTE_GAMES


def test_news_is_trimmed_and_capped():
    news = [{"headline": f"Story {i}", "description": "d" * 400} for i in range(9)]
    out = wf.build("nfl", [game("A", 1, "B", 2)], news)["news"]
    assert len(out) == wf.NEWS_ITEMS and all(len(x) <= wf.NEWS_CHARS for x in out)
    assert wf.build("nfl", [game("A", 1, "B", 2)], [{"headline": "Just a headline", "description": None}])["news"] == ["Just a headline"]
