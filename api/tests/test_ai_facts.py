"""Code-built fact sheets, on real /api/games payloads saved 2026-09-29."""
import json
import re
from pathlib import Path

from app.ai import facts

FIX = Path(__file__).parent / "fixtures" / "ai"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def test_recap_turnovers_spelled_out_both_ways():
    # The Sep 29 error: "Turnovers: BAL 0, DAL 1" became "the Ravens forced no turnovers".
    lines = facts.recap_facts(load("final_bal_dal"))["facts"]
    assert ("Giveaways (turnovers each team committed): Ravens 0, Cowboys 1. "
            "Takeaways (turnovers each team forced): Ravens 1, Cowboys 0.") in lines
    assert not any(l.startswith("turnovers:") for l in lines)      # the ambiguous raw row is gone


def test_recap_scoring_and_leads():
    lines = facts.recap_facts(load("final_bal_dal"))["facts"]
    assert "Points scored in Q3: Ravens 7, Cowboys 8." in lines
    assert "End of Q1 score: Ravens 7, Cowboys 10 (Cowboys led by 3)." in lines
    assert "Halftime score: Ravens 17, Cowboys 13 (Ravens led by 4)." in lines
    assert "Times the lead changed hands between quarter breaks: 1." in lines
    assert "Baltimore Ravens won by 3 points; 65 points were scored in all." in lines


def test_recap_no_bets():
    text = " ".join(facts.recap_facts(load("final_phi_chi"))["facts"]).lower()
    assert "spread" not in text and "moneyline" not in text


def test_preview_line_names_the_favorite():
    lines = facts.preview_facts(load("pre_osu_iowa"))["facts"]
    assert "Point spread: Ohio State Buckeyes favored by 14 points (Draft Kings)." in lines
    assert "Over/under total: 45.5 points." in lines
    assert "Kickoff: Saturday, October 3, 3:30 PM ET, on CBS." in lines


def test_preview_allowed_ranks_count_from_fewest():
    lines = facts.preview_facts(load("pre_osu_iowa"))["facts"]
    assert "Season points allowed / game: Ohio State 12.3, Iowa 8.0 (1st fewest nationally)." in lines
    assert "Season points / game: Ohio State 45.0 (6th nationally), Iowa 32.8 (58th nationally)." in lines


def test_live_facts_mark_the_current_quarter():
    g = load("final_bal_dal")
    g.update(state="in", status_detail="Q3 4:12", situation={"possession": "BAL", "down_distance": "3rd & 4 at DAL 35"})
    g["home"]["score"], g["away"]["score"] = 18, 21
    g["header"]["home"]["linescores"], g["header"]["away"]["linescores"] = [10, 3, 5], [7, 10, 4]
    lines = facts.live_facts(g)["facts"]
    assert lines[0] == "Live, Q3 4:12: Baltimore Ravens 21, Dallas Cowboys 18."
    assert "Baltimore Ravens leads by 3 right now." in lines
    assert "Points in Q2: Ravens 10, Cowboys 3." in lines
    assert "Points in Q3 (in progress): Ravens 4, Cowboys 5." in lines
    assert not any("has the ball" in l for l in lines)       # not a hook the one-liner uses (M4)
    assert not any("Final" in l or "won by" in l for l in lines)


def test_live_facts_keep_only_the_one_liner_hooks():
    # M4 (2026-10-02): the lines ONE_LINER and Adam's examples can use, nothing else. The passers stay: their lines
    # are the only interception count a live box score has ("Two picks already").
    g = load("final_phi_chi")
    g.update(state="in", status_detail="Q3 10:12", situation={"possession": "CHI", "down_distance": "2nd & 7 at CHI 34"},
             line={"home_spread": -3.0, "total": 44.5, "provider": "Draft Kings"})
    sheet = facts.live_facts(g)
    lines = sheet["facts"]
    for kept in ("Home team: Chicago Bears. Visiting team: Philadelphia Eagles.",
                 "Before kickoff, point spread: Chicago Bears favored by 3 points (Draft Kings).",
                 "Chicago Bears leads by 20 right now.",
                 "So far, total yards: Eagles 248, Bears 375.", "So far, passing: Eagles 141, Bears 247.",
                 "So far, rushing: Eagles 107, Bears 128.", "So far, 3rd down: Eagles 2-9 (22%), Bears 5-13 (38%).",
                 "Eagles Passing leader (so far): Jalen Hurts, QB: 16/25, 153 YDS, 1 INT."):
        assert kept in lines
    assert any(l.startswith("So far, giveaways") for l in lines)
    for dropped in ("over/under", "has the ball", "time of possession", "penalties", "Rushing leader",
                    "Receiving leader"):
        assert not any(dropped in l for l in lines), dropped
    # The code check gets the same sheet: only the passers, only the kept stats.
    assert [p["name"] for p in sheet["players"]] == ["Jalen Hurts", "Case Keenum"]
    assert set(sheet["stats"]) == set(facts.LIVE_STATS)
    assert facts.recap_facts(g)["stats"]["possessionTime"]            # the recap's sheet is untouched
    assert "Over/under total: 44.5 points." in facts.preview_facts(g)["facts"]


def test_tied_quarter_break():
    g = load("final_phi_chi")
    g["header"]["home"]["linescores"], g["header"]["away"]["linescores"] = [7, 0, 0, 0], [7, 0, 0, 3]
    lines = facts.recap_facts(g)["facts"]
    assert "End of Q1 score: tied 7-7." in lines
    assert "Times the lead changed hands between quarter breaks: 0." in lines


def test_recap_states_home_edges_and_the_lead_change():
    # Each of these was a recap error on 2026-09-30.
    lines = facts.recap_facts(load("final_lac_buf"))["facts"]
    assert "Home team: Buffalo Bills. Visiting team: Los Angeles Chargers." in lines
    assert "Lead change: Bills took the lead between the end of Q3 and end of Q4 scores." in lines
    lines = facts.recap_facts(load("final_lv_no"))["facts"]
    assert "Time of possession edge: Saints, by 4:56." in lines
    assert "Total yards edge: Saints, by 41." in lines
    assert sum(l.startswith("Lead change:") for l in lines) == 3


def test_recap_players_and_stats_for_the_code_check():
    f = facts.recap_facts(load("final_phi_chi"))
    assert {"name": "D'Andre Swift", "last_name": "Swift", "side": "home", "value": "20 CAR, 84 YDS"} in f["players"]
    assert f["stats"]["possessionTime"] == {"home": "36:53", "away": "23:07"}


# ---------- M3 (2026-10-02): the recap outline, built by code ----------

FINALS = sorted(p.stem for p in FIX.glob("final_*.json"))
# Code's angle for each week-4 final (the rules in facts.py, margin first since Oct 2). Z5 compared the first rules
# with a person's blind picks (11/16); car_cle, hou_ind and sea_wsh (close_finish) and ne_jax (blowout) moved to the
# label with the margin rules first. lac_buf (comeback; labelled late_lead_change) is a known miss, left alone.
WEEK4_ANGLES = {"final_ari_sf": "front_runner", "final_atl_gb": "blowout", "final_bal_dal": "close_finish",
                "final_car_cle": "close_finish", "final_cin_pit": "close_finish", "final_hou_ind": "close_finish",
                "final_kc_mia": "routine_win", "final_lac_buf": "comeback", "final_lar_den": "comeback",
                "final_lv_no": "late_lead_change", "final_min_tb": "front_runner",
                "final_ne_jax": "blowout", "final_nyj_det": "routine_win", "final_phi_chi": "blowout",
                "final_sea_wsh": "close_finish", "final_ten_nyg": "front_runner"}


def _stat(g, key, away, home):
    row = next(r for r in g["team_stats"]["rows"] if r["key"] == key)
    row["away"], row["home"] = str(away), str(home)
    return g


def _overtime(g):
    """phi_chi turned into a 20-20 game after four quarters that the Bears won 23-20 in overtime."""
    g["header"]["home"]["linescores"], g["header"]["away"]["linescores"] = [7, 3, 10, 0, 3], [0, 7, 0, 13, 0]
    g["home"]["score"], g["away"]["score"] = 23, 20
    return g


def _tie(g):
    g["header"]["home"]["linescores"], g["header"]["away"]["linescores"] = [7, 3, 7, 3], [0, 7, 10, 3]
    g["home"]["score"] = g["away"]["score"] = 20
    return g


def _seesaw(g):
    """The lead changes hands at two breaks and the winner led after Q3: no comeback, no late change."""
    g["header"]["home"]["linescores"], g["header"]["away"]["linescores"] = [7, 0, 14, 3], [0, 10, 3, 3]
    g["home"]["score"], g["away"]["score"] = 24, 16
    return g


def _outgained(g):
    """kc_mia (Chiefs 24, Dolphins 10) with the Dolphins outgaining the Chiefs 420 to 334."""
    return _stat(g, "totalYards", 334, 420)


def _turnovers(g):
    """kc_mia with the Chiefs giving it away 4 times to the Dolphins' once."""
    return _stat(g, "turnovers", 4, 1)


SYNTHETIC = {"overtime": ("final_phi_chi", _overtime), "close_finish": ("final_phi_chi", _tie),
             "seesaw": ("final_phi_chi", _seesaw), "outgained_but_lost": ("final_kc_mia", _outgained),
             "turnovers": ("final_kc_mia", _turnovers)}


def _all_games():
    yield from ((name, load(name)) for name in FINALS)
    yield from ((f"{base} as {angle}", make(load(base))) for angle, (base, make) in SYNTHETIC.items())


def test_every_outline_line_is_a_fact_line_copied_exactly():
    # The outline adds nothing to FACTS: 4-6 of its lines, each exactly as recap_facts wrote it, none twice.
    for name, g in _all_games():
        sheet = facts.recap_facts(g)
        plan = facts.recap_outline(g, sheet)
        assert plan["angle"] in facts.ANGLES, name
        assert facts.OUTLINE_MIN <= len(plan["lines"]) <= facts.OUTLINE_MAX, name
        assert all(line in sheet["facts"] for line in plan["lines"]), name
        assert len(set(plan["lines"])) == len(plan["lines"]), name


def test_the_frame_names_teams_but_adds_no_number_or_refused_claim():
    from app.ai import writer
    for name, g in _all_games():
        sheet = facts.recap_facts(g)
        frame = facts.recap_outline(g, sheet)["frame"]
        unnamed = re.sub(r"\b(?:Q[1-4]|OT\d*)\b", "", frame)            # FACTS' own quarter labels are fine
        for t in sheet["teams"].values():
            unnamed = unnamed.replace(t["short"], "")                   # a name can hold digits: the 49ers
        assert not re.search(r"\d", unnamed), (name, frame)
        assert writer.claim_problems([frame], g, sheet) == [], (name, frame)
        assert writer.bet_talk([frame]) is None and not writer.ADVICE.search(frame)


def test_the_outline_is_deterministic_and_leaves_the_game_alone():
    for name in FINALS:
        g = load(name)
        before = json.dumps(g, sort_keys=True)
        assert facts.recap_outline(g) == facts.recap_outline(load(name)) == facts.recap_outline(g, facts.recap_facts(g))
        assert json.dumps(g, sort_keys=True) == before


def test_week4_angles():
    assert {name: facts.recap_outline(load(name))["angle"] for name in FINALS} == WEEK4_ANGLES


def test_the_margin_comes_before_yards_and_giveaways():
    # Adam, Oct 2: NE @ JAX "was absolutely a blowout". The Patriots outgained the Jaguars 317 to 315 and lost by 29;
    # the old "any yardage edge in a loss by 17 or more" branch called it outgained_but_lost. It is gone.
    plan = facts.recap_outline(load("final_ne_jax"))
    assert (plan["angle"], plan["frame"]) == ("blowout", "Jaguars won by a lopsided margin.")
    assert plan["lines"][0] == "Jacksonville Jaguars won by 29 points; 41 points were scored in all."
    # A 3-point game is close_finish even when the loser outgained the winner by 101 (car_cle) or 179 (sea_wsh),
    # or the winner gave it away 3 times to none (hou_ind).
    for name, frame in (("final_car_cle", "Browns won a close game."), ("final_sea_wsh", "Commanders won a close game."),
                        ("final_hou_ind", "Colts won a close game.")):
        assert facts.recap_outline(load(name))["frame"] == frame, name
    # A blowout stays a blowout when the loser outgains the winner by more than OUTGAINED_YARDS.
    g = _stat(load("final_phi_chi"), "totalYards", 375 + facts.OUTGAINED_YARDS + 10, 375)
    assert facts.recap_outline(g)["angle"] == "blowout"
    # Between the margins, the stat angles still fire: a 14-point loss with 86 more yards, or 3 more giveaways.
    assert facts.recap_outline(_outgained(load("final_kc_mia")))["angle"] == "outgained_but_lost"
    assert facts.recap_outline(_turnovers(load("final_kc_mia")))["angle"] == "turnovers"
    # ... but not a yardage edge under OUTGAINED_YARDS (kc_mia as played: the Dolphins outgained the Chiefs by 5).
    assert facts.recap_outline(load("final_kc_mia"))["angle"] == "routine_win"
    g = _stat(load("final_kc_mia"), "totalYards", 334, 334 + facts.OUTGAINED_YARDS - 1)
    assert facts.recap_outline(g)["angle"] == "routine_win"


def test_the_angles_own_lines_come_first():
    plan = facts.recap_outline(load("final_lar_den"))
    assert plan["frame"] == "Broncos trailed by double digits at a quarter break and won."
    assert plan["lines"][:4] == ["Halftime score: Rams 16, Broncos 0 (Rams led by 16).",
                                 "Points scored in Q3: Rams 0, Broncos 16.",                 # the swing quarter
                                 "Lead change: Broncos took the lead between the end of Q3 and end of Q4 scores.",
                                 "Denver Broncos won by 4 points; 56 points were scored in all."]
    plan = facts.recap_outline(_turnovers(load("final_kc_mia")))
    assert plan["frame"] == "Chiefs had more giveaways and still won."
    assert plan["lines"][0].startswith("Giveaways (turnovers each team committed): Chiefs 4, Dolphins 1.")
    plan = facts.recap_outline(_outgained(load("final_kc_mia")))
    assert plan["frame"] == "Dolphins gained more total yards and lost."
    assert plan["lines"][:3] == ["Total yards edge: Dolphins, by 86.", "total yards: Chiefs 334, Dolphins 420.",
                                 "Kansas City Chiefs won by 14 points; 34 points were scored in all."]
    # A team's top leader: the most touchdowns, then yards (Robinson's 2 TD over Penix's 256 yards and London's 194).
    assert "Falcons Rushing leader (this game): Bijan Robinson, RB: 29 CAR, 194 YDS, 2 TD." in \
        facts.recap_outline(load("final_atl_gb"))["lines"]


def test_overtime_ties_and_seesaws():
    plan = facts.recap_outline(_overtime(load("final_phi_chi")))
    assert plan["angle"] == "overtime" and plan["frame"] == "The game went to overtime."
    assert plan["lines"][:3] == ["End of Q4 score: tied 20-20.", "Points scored in OT: Eagles 0, Bears 3.",
                                 "Chicago Bears won by 3 points; 43 points were scored in all."]
    plan = facts.recap_outline(_tie(load("final_phi_chi")))
    assert (plan["angle"], plan["frame"]) == ("close_finish", "The game ended in a tie.")
    assert plan["lines"][0].startswith("Final score:")
    plan = facts.recap_outline(_seesaw(load("final_phi_chi")))
    assert plan["angle"] == "seesaw"
    assert plan["lines"][0] == "Times the lead changed hands between quarter breaks: 2."


def test_no_outline_without_a_final_score():
    g = load("final_phi_chi")
    g["home"]["score"] = None
    assert facts.recap_outline(g) is None


# ---------- article text cut to the paragraphs about this game (Oct 1) ----------

def test_relevant_text_keeps_the_paragraphs_about_the_game():
    from app.ai import sources
    about = "The Steelers lean on T.J. Watt against Cleveland's line. " * 6
    other = "Elsewhere, the Chiefs and Bills both won big on Sunday. " * 6
    text = "\n".join([about, other, "Watt has 4 sacks in 3 games against this offense. " * 3])
    out = sources.relevant_text(text, ["Steelers", "Cleveland", "Watt"])
    assert "Chiefs" not in out and "4 sacks" in out
    short = "A note on the Steelers.\n" + other
    assert sources.relevant_text(short, ["Steelers"]) == " ".join(short.split())   # too little kept: all of it


def test_player_terms_come_from_injuries_and_leaders():
    from app.ai import sources
    game = {"injuries": {"home": [{"name": "Elgton Jenkins"}], "away": [{"name": "Joey Porter Jr."}]},
            "leaders": {"rows": [{"home": {"name": "Shedeur Sanders"}, "away": {"name": "Aaron Rodgers"}},
                                 {"home": None, "away": {"name": "D.J. Moore"}}]}}
    assert sources.player_terms(game) == ["Jenkins", "Moore", "Porter", "Rodgers", "Sanders"]
