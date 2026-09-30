"""Code-built fact sheets, on real /api/games payloads saved 2026-09-29."""
import json
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
    assert "Baltimore Ravens has the ball, 3rd & 4 at DAL 35." in lines
    assert not any("Final" in l or "won by" in l for l in lines)


def test_tied_quarter_break():
    g = load("final_phi_chi")
    g["header"]["home"]["linescores"], g["header"]["away"]["linescores"] = [7, 0, 0, 0], [7, 0, 0, 3]
    lines = facts.recap_facts(g)["facts"]
    assert "End of Q1 score: tied 7-7." in lines
    assert "Times the lead changed hands between quarter breaks: 0." in lines
