"""Grading math (step 2.3). Table-driven: every case in the rules. No database, no network."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.grading import (MARKETS, Line, choose_lines, describe, grade, grade_moneyline, grade_spread, grade_total,
                         live_status, points_needed_for_over, spread_label)

# (case, home_score, away_score, home_spread, total, spread outcome, spread margin, total outcome, total margin, ml outcome)
CASES = [
    # Handoff example: BUF -6.5 at home, BUF 27 MIA 17 -> BUF covers by 3.5; 44 vs 48.5 under by 4.5
    ("home fav, half point, covers", 27, 17, -6.5, 48.5, "home", "3.5", "under", "4.5", "home"),
    ("home fav, half point, fails", 20, 17, -6.5, 48.5, "away", "3.5", "under", "11.5", "home"),
    ("away fav, half point, covers", 20, 24, 2.5, 43.5, "away", "1.5", "over", "0.5", "away"),
    ("away fav, half point, fails", 21, 23, 2.5, 44.5, "home", "0.5", "under", "0.5", "away"),
    ("home fav, whole number, covers", 28, 21, -3, 44, "home", "4", "over", "5", "home"),
    ("home fav, whole number, push", 24, 21, -3, 45, "push", "0", "push", "0", "home"),
    ("away fav, whole number, push", 17, 20, 3, 40, "push", "0", "under", "3", "away"),
    ("away fav, whole number, covers", 10, 24, 7, 30, "away", "7", "over", "4", "away"),
    ("home dog wins outright", 20, 17, 3, 37, "home", "6", "push", "0", "home"),
    ("pick'em", 20, 17, 0, 41.5, "home", "3", "under", "4.5", "home"),
    ("tie: ML push, fav fails", 20, 20, -3, 40, "away", "3", "push", "0", "push"),
    ("tie on a pick'em: all push", 17, 17, 0, 34, "push", "0", "push", "0", "push"),
    ("shutout, total over by whole", 31, 0, -13.5, 30, "home", "17.5", "over", "1", "home"),
]


@pytest.mark.parametrize("case,hs,as_,spread,total,s_out,s_m,t_out,t_m,ml_out", CASES, ids=[c[0] for c in CASES])
def test_grading_table(case, hs, as_, spread, total, s_out, s_m, t_out, t_m, ml_out):
    s = grade_spread(hs, as_, spread)
    assert (s.outcome, s.margin) == (s_out, Decimal(s_m))
    t = grade_total(hs, as_, total)
    assert (t.outcome, t.margin) == (t_out, Decimal(t_m))
    ml = grade_moneyline(hs, as_)
    assert ml.outcome == ml_out and ml.margin == abs(hs - as_)


def test_margins_are_never_negative():
    for hs, as_, spread, total in [(0, 50, -10.5, 99.5), (50, 0, 10.5, 0.5)]:
        assert grade_spread(hs, as_, spread).margin >= 0
        assert grade_total(hs, as_, total).margin >= 0


def test_db_decimals_and_floats_agree():
    # NUMERIC columns come back as Decimal('-6.5'); ESPN parsing gives floats. Same answer either way.
    assert grade_spread(27, 17, Decimal("-6.5")) == grade_spread(27, 17, -6.5)
    assert grade_total(27, 17, Decimal("48.5")) == grade_total(27, 17, 48.5)


def _line(**kw):
    base = dict(source="pre_game", provider="DK", home_spread=Decimal("-6.5"), total=Decimal("48.5"),
                home_ml=-285, away_ml=230)
    base.update(kw)
    return Line(**base)


def _all(line):
    """The same line for every market (what choose_lines returns when one source has them all)."""
    return {m: line for m in MARKETS}


def test_grade_skips_markets_without_a_number():
    assert grade(27, 17, {}) == {}                                         # no line: Ungraded
    only_total = grade(27, 17, _all(_line(home_spread=None, home_ml=None, away_ml=None)))
    assert set(only_total) == {"total"}
    assert set(grade(27, 17, _all(_line()))) == {"moneyline", "spread", "total"}


def test_describe_words():
    line = _line()
    r = grade(27, 17, _all(line))
    assert describe(r["moneyline"], line, "BUF", "MIA") == "BUF won by 10"
    assert describe(r["spread"], line, "BUF", "MIA") == "BUF -6.5 covered by 3.5"
    assert describe(r["total"], line, "BUF", "MIA") == "Under 48.5 by 4.5"
    away_line = _line(home_spread=Decimal("2.5"), total=Decimal("44"))
    r = grade(20, 24, _all(away_line))
    assert describe(r["spread"], away_line, "SEA", "SF") == "SF -2.5 covered by 1.5"
    assert describe(r["total"], away_line, "SEA", "SF") == "Push at 44"
    tie = grade(20, 20, _all(_line(home_spread=Decimal("0"))))
    assert describe(tie["moneyline"], _line(), "A", "B") == "Tie · push"
    assert describe(tie["spread"], _line(home_spread=Decimal("0")), "A", "B") == "PK · push"


def test_spread_label_shows_the_favorite():
    assert spread_label(-6.5, "BUF", "MIA") == "BUF -6.5"
    assert spread_label(2.5, "SEA", "SF") == "SF -2.5"
    assert spread_label(-7.0, "BUF", "LAC") == "BUF -7"
    assert spread_label(0, "A", "B") == "PK"


# ---------- which line is used ----------

T0 = datetime(2026, 9, 28, 17, tzinfo=timezone.utc)


def _snap(i, state, spread, minutes):
    return {"id": i, "game_state": state, "captured_at": T0 + timedelta(minutes=minutes), "provider": "DK",
            "home_spread": Decimal(str(spread)), "total": Decimal("44.5"), "home_ml": -150, "away_ml": 130}


CLOSE = {"provider": "DK", "home_spread": -3.0, "total": 45.5, "home_ml": -160, "away_ml": 140}


def test_espn_close_wins_over_an_older_saved_line():
    # Review finding 1: our saved line (-6.5) can be days old; the book closed at -7. Grade on the close.
    snaps = [_snap(1, "pre", -6.5, -3000)]
    close = {"provider": "DK", "home_spread": -7.0, "total": 44.5, "home_ml": -300, "away_ml": 250}
    lines = choose_lines(snaps, close)
    assert {m: ln.source for m, ln in lines.items()} == {m: "espn_close" for m in MARKETS}
    r = grade(24, 17, lines)
    assert r["spread"].outcome == "push"                     # the saved -6.5 would have said "covered by 0.5"


def test_last_pregame_snapshot_when_espn_has_no_close():
    snaps = [_snap(1, "pre", -2.5, -600), _snap(2, "pre", -3.5, -60), _snap(3, "in", -10.5, 30)]
    lines = choose_lines(snaps, None)
    sp = lines["spread"]
    assert (sp.source, sp.home_spread, sp.snapshot_id) == ("pre_game", Decimal("-3.5"), 2)


def test_newest_line_tagged_in_game_as_last_resort():
    lines = choose_lines([_snap(3, "in", -10.5, 30), _snap(4, "post", -9.5, 200)], None)
    assert (lines["spread"].source, lines["spread"].home_spread) == ("in_game", Decimal("-9.5"))


def test_line_chosen_per_market():
    # Review finding 5: ESPN's close has no spread ("OFF") and our saved line has no total.
    close = {"provider": "DK", "home_spread": None, "total": 45.5, "home_ml": -160, "away_ml": 140}
    snap = {**_snap(1, "pre", -3.0, -60), "total": None}
    lines = choose_lines([snap], close)
    assert lines["spread"].source == "pre_game" and lines["spread"].home_spread == Decimal("-3.0")
    assert lines["total"].source == "espn_close" and lines["total"].total == Decimal("45.5")
    assert lines["moneyline"].source == "espn_close"
    assert set(grade(24, 20, lines)) == {"moneyline", "spread", "total"}   # nothing Ungraded


def test_no_line_at_all():
    assert choose_lines([], None) == {}
    assert choose_lines([], {"provider": "DK", "home_spread": None, "total": None, "home_ml": None, "away_ml": None}) == {}


# ---------- live status (C2, never stored) ----------

def test_live_status_reports_never_advises():
    rows = {r["market"]: r["text"] for r in live_status(17, 10, _all(_line(home_spread=Decimal("-3"), total=Decimal("43.5"))), "KC", "LV")}
    assert rows["moneyline"] == "KC leads by 7"
    assert rows["spread"] == "KC -3 covering by 4"
    assert rows["total"] == "27 pts so far vs O/U 43.5 · 17 more to go over"
    for text in rows.values():
        assert not any(w in text.lower() for w in ("take", "bet on", "should", "lock"))


def test_points_needed_for_over():
    assert points_needed_for_over(20, 20, 43.5) == 4    # 44 is over
    assert points_needed_for_over(20, 20, 44) == 5      # 44 pushes, 45 is over
    assert points_needed_for_over(30, 20, 43.5) == 0    # already over
    assert points_needed_for_over(22, 22, 44) == 1      # exactly on it: one more point


def test_live_total_already_over():
    rows = {r["market"]: r["text"] for r in live_status(30, 20, _all(_line()), "A", "B")}
    assert rows["total"] == "50 pts so far vs O/U 48.5 · over by 1.5"
