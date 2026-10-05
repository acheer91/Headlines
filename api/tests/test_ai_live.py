"""The live one-liner's play-by-play facts and its checks (Adam, Oct 4): no live model calls."""
import json
from pathlib import Path

import pytest

from app import summary
from app.ai import client, live_facts, prompts, writer

FIX = Path(__file__).parent / "fixtures" / "ai" / "live_lac_sea.json"


@pytest.fixture
def payload():
    return json.loads(FIX.read_text(encoding="utf-8"))


def _game(p):
    """The game page's live fields, as games.detail builds them from the stored summary."""
    t, st = summary.teams(p), summary.status(p)
    side = lambda s: {"name": t[s]["name"], "short": t[s]["name"].split()[-1], "abbr": t[s]["abbr"],   # noqa: E731
                      "score": st[f"{s}_score"]}
    return {"league": "nfl", "state": "in", "home": side("home"), "away": side("away"), "status_detail": st["detail"],
            "header": {s: {"linescores": t[s]["linescores"]} for s in ("home", "away")},
            "team_stats": {"kind": "game", "rows": []}, "leaders": {"kind": "game", "rows": []},
            "situation": summary.situation(p), "live": summary.live_context(p)}


def test_live_context_reads_the_play_by_play(payload):
    live = summary.live_context(payload)
    assert live["period"] == 1 and live["clock"] == "1:05"
    assert [s["team"] for s in live["scores"]] == ["LAC", "SEA"]
    assert (live["scores"][1]["away"], live["scores"][1]["home"]) == (3, 7)
    assert len(live["recent"]) == summary.RECENT_PLAYS
    assert not any("Timeout" in x["text"] or "End" in x["text"] for x in live["recent"])
    assert [d["result"] for d in live["drives"] if d["abbr"] == "SEA"][-2:] == ["Punt", "Punt"]
    assert live["win_prob"]["home"] == 79 and live["win_prob"]["plays_back"] == summary.WIN_PROB_BACK


def test_live_context_is_none_unless_the_game_is_live(payload):
    payload["header"]["competitions"][0]["status"]["type"]["state"] = "post"
    assert summary.live_context(payload) is None
    assert summary.live_context({}) is None


def test_live_context_survives_missing_parts(payload):
    for key in ("drives", "scoringPlays", "winprobability"):
        p = {k: v for k, v in payload.items() if k != key}
        live = summary.live_context(p)
        assert live is not None and live["period"] == 1


def test_live_facts_are_a_short_sheet_with_the_freshest_plays(payload):
    lines = live_facts.live_facts(_game(payload))["facts"]
    text = "\n".join(lines)
    assert "Game phase: middle, a one-score game." in lines       # 1:05 left in the first quarter: past the first 15%
    assert "Ball: Chargers, 1st & 10 at LAC 21." in lines
    assert ("Recent play (Q1 1:12, Seahawks on offense): M.Dickson punts 54 yards to LAC 21, Center-C.Stoll, "
            "fair catch by D.Davis.") in lines
    assert sum(x.startswith("Recent play") for x in lines) == live_facts.LIVE_LAST_PLAYS
    assert ("Latest score, Q1 9:55: Seahawks Emanuel Wilson 23 Yd pass from Sam Darnold (Jason Myers Kick). "
            "Chargers 3, Seahawks 7.") in lines
    assert "Seahawks' last 2 drives all ended in a punt." in lines and "Chargers' last 2 drives all ended in a punt." in lines
    assert len(lines) == 10                                         # was 20 before the hooks were ranked
    assert "Seahawks's" not in text and "Chargers's" not in text and "None" not in text
    assert not any(w in text for w in ("win probability", "changed hands", "finished drives"))   # no hook: left out


def test_live_facts_without_play_by_play_still_have_the_score(payload):
    g = _game(payload)
    g["live"] = None
    g["situation"] = None
    lines = live_facts.live_facts(g)["facts"]
    assert not any(w in "\n".join(lines) for w in ("Recent play", "win probability", "Ball:", "Latest score"))
    assert "Game phase: a one-score game." in lines


@pytest.mark.parametrize("period,clock,phase", [(1, "15:00", "early"), (1, "6:30", "early"), (1, "5:00", "middle"),
                                                (2, "0:00", "middle"), (3, "0:01", "middle"), (4, "15:00", "late"),
                                                (4, "0:30", "late"), (5, "10:00", "overtime"), (None, "5:00", None),
                                                (2, "bad", None)])
def test_game_phase(period, clock, phase):
    assert live_facts.game_phase(period, clock) == phase


def test_score_run_counts_lead_changes_and_unanswered_points():
    sc = [{"away": 0, "home": 7}, {"away": 3, "home": 7}, {"away": 10, "home": 7}, {"away": 10, "home": 14},
          {"away": 10, "home": 21}, {"away": 10, "home": 24}]
    leaders, flips, last, run = live_facts._score_run(sc)
    assert leaders == ["home", "home", "away", "home", "home", "home"]
    assert flips == 2 and last == "home" and run == 17


def _hooks_game(**live):
    """A live game for the hook tests: Chicago 24, Philadelphia 3 in the third quarter, no box score."""
    return {"home": {"name": "Chicago Bears", "short": "Bears", "abbr": "CHI", "score": 24},
            "away": {"name": "Philadelphia Eagles", "short": "Eagles", "abbr": "PHI", "score": 3},
            "status_detail": "Q3 4:12", "header": {}, "live": dict({"period": 3, "clock": "4:12", "scores": [
                {"period": 1, "clock": "9:00", "team": "CHI", "text": "A 5 Yd Run", "away": 0, "home": 7},
                {"period": 2, "clock": "3:00", "team": "PHI", "text": "A 30 Yd Field Goal", "away": 3, "home": 7},
                {"period": 3, "clock": "9:00", "team": "CHI", "text": "B 12 Yd pass", "away": 3, "home": 14},
                {"period": 3, "clock": "5:00", "team": "CHI", "text": "C 8 Yd Run", "away": 3, "home": 21},
                {"period": 3, "clock": "4:12", "team": "CHI", "text": "D 40 Yd Field Goal", "away": 3, "home": 24}]}, **live)}


def test_hooks_unanswered_points_and_never_trailed():
    lines = live_facts.live_facts(_hooks_game(), hooks=None)["facts"]
    assert "Not trailed at any point in this game: Bears." in lines
    assert "Unanswered points, latest run: Bears 17." in lines
    assert "Game phase: middle, a three-score game." in lines          # a blowout only from the fourth quarter
    assert not any("changed hands" in x or "led by" in x for x in lines)


def test_hooks_are_ranked_and_only_the_strongest_make_the_sheet():
    live = {"win_prob": {"home": 97, "plays_back": 15, "home_before": 90}, "drives": [
        {"side": "away", "abbr": "PHI", "result": "Punt"}, {"side": "away", "abbr": "PHI", "result": "Punt"},
        {"side": "away", "abbr": "PHI", "result": "Punt"}]}
    g = _hooks_game(**live)
    g["team_stats"] = {"kind": "game", "rows": [
        {"key": "turnovers", "label": "Turnovers", "home": "0", "away": "3"},
        {"key": "totalYards", "label": "Total yards", "home": "400", "away": "150"}]}
    g["line"] = {"home_spread": 3.5}                                  # Philadelphia was the pregame favorite: now trailing
    all_hooks = live_facts.live_facts(g, hooks=None)["facts"]
    top = live_facts.live_facts(g)["facts"]
    assert len(all_hooks) > len(top)
    hooks = top[len(top) - live_facts.LIVE_HOOKS:]
    assert hooks[0].startswith("So far, giveaways")                       # 3 more than the other side: salience 9
    assert hooks[1].startswith("The pregame favorite, Philadelphia Eagles, is trailing")     # salience 8
    assert not any("Not trailed" in x for x in hooks)                                        # the weakest, cut


def test_a_sloppy_game_and_a_comeback_are_hooks():
    g = _hooks_game()
    g["team_stats"] = {"kind": "game", "rows": [{"key": "turnovers", "label": "Turnovers", "home": "2", "away": "1"}]}
    assert any("giveaways" in x for x in live_facts.live_facts(g, hooks=None)["facts"])      # 3 between them
    g["team_stats"]["rows"][0].update(home="1", away="1")
    assert not any("giveaways" in x for x in live_facts.live_facts(g, hooks=None)["facts"])
    back = _hooks_game()
    back.update(home={**back["home"], "score": 17}, away={**back["away"], "score": 14})
    back["live"]["scores"] = [{"period": 1, "clock": "8:00", "team": "PHI", "text": "X", "away": 7, "home": 0},
                              {"period": 1, "clock": "2:00", "team": "PHI", "text": "Y", "away": 14, "home": 0},
                              {"period": 2, "clock": "9:00", "team": "CHI", "text": "Z", "away": 14, "home": 7},
                              {"period": 3, "clock": "9:00", "team": "CHI", "text": "W", "away": 14, "home": 14},
                              {"period": 3, "clock": "4:12", "team": "CHI", "text": "V", "away": 14, "home": 17}]
    lines = live_facts.live_facts(back, hooks=None)["facts"]
    assert "Eagles led by 14 earlier in this game." in lines              # and it is a 3-point game now
    assert "The lead has changed hands 1 time." not in lines              # one flip is not a hook


def test_stat_hooks_need_a_real_gap():
    g = _hooks_game()
    rows = [{"key": "totalYards", "label": "Total yards", "home": "300", "away": "250"},
            {"key": "rushingYards", "label": "Rushing", "home": "100", "away": "30"},
            {"key": "thirdDownEff", "label": "3rd down", "home": "1-6 (17%)", "away": "5-8 (63%)"}]
    g["team_stats"] = {"kind": "game", "rows": rows}
    got = live_facts.live_facts(g, hooks=None)["facts"]
    assert any("rushing" in x for x in got) and any("3rd down" in x for x in got)
    assert not any("total yards" in x for x in got)                  # 50 yards is not a gap


def test_a_passer_with_two_picks_is_a_hook():
    g = _hooks_game()
    g["leaders"] = {"kind": "game", "rows": [{"key": "passingYards", "label": "Passing", "home": None,
                    "away": {"name": "Jalen Hurts", "position": "QB", "value": "12/25, 140 YDS, 2 INT"}}]}
    assert any("Jalen Hurts" in x for x in live_facts.live_facts(g, hooks=None)["facts"])
    g["leaders"]["rows"][0]["away"]["value"] = "12/25, 140 YDS, 1 INT"
    assert not any("Jalen Hurts" in x for x in live_facts.live_facts(g, hooks=None)["facts"])


# ---------------------------------------------------------------- the checks

GAME = {"home": {"score": 24}, "away": {"score": 7}}


@pytest.mark.parametrize("line,hit", [("It's 24-7 and nobody is surprised.", "24-7"), ("7–24, and counting.", "7–24"),
                                      ("Chicago is up 17 and cruising.", "up 17"),
                                      ("Down seventeen with no pulse.", "Down seventeen"),
                                      ("Leading by 17 is not the same as leading.", "Leading by 17"),
                                      ("A 17-point game is a nap with a scoreboard.", "17-point game"),
                                      ("Seventeen-point lead, zero pulse.", "Seventeen-point lead"),
                                      ("Bears 24, Eagles 7 and the band is bored.", "24, Eagles 7")])
def test_restates_score(line, hit):
    assert writer.restates_score(line, GAME) == hit


@pytest.mark.parametrize("line", ["Chicago has this in a headlock.", "Up 7 at the half would have been a nightmare.",
                                  "A 3:12 drive and nothing to show for it.", "He is 17 for 24 and still wrong.",
                                  "Down 24 at half was a lot.", "Picked apart by 17 straight punts.",
                                  "Taken one by one, these drives are tragic."])
def test_does_not_flag_other_numbers(line):
    assert writer.restates_score(line, GAME) is None


def test_restates_score_needs_a_score():
    assert writer.restates_score("up 7", {"home": {"score": None}, "away": {"score": None}}) is None


def test_live_mode_allows_momentum_and_causes_but_not_records_before_this_game():
    sheet = {"teams": {"home": {"name": "Chicago Bears", "short": "Bears", "abbr": "CHI"},
                       "away": {"name": "Philadelphia Eagles", "short": "Eagles", "abbr": "PHI"}}, "facts": []}
    game = {"home": {"score": 1}, "away": {"score": 0}}
    for ok in ("The momentum is all Chicago's, because the Eagles keep punting.", "All game long, one team has cared."):
        assert writer.claim_problems([ok], game, sheet, final=False, live=True) == []
    assert writer.claim_problems(["The momentum is all Chicago's."], game, sheet, final=False, live=False)
    assert writer.claim_problems(["Chicago is winless and it shows."], game, sheet, final=False, live=True)
    assert writer.claim_problems(["The worst loss in franchise history is brewing."], game, sheet, final=False, live=True)


def test_live_player_numbers_may_come_from_a_play_line():
    sheet = {"teams": {"home": {"name": "Seattle Seahawks", "short": "Seahawks", "abbr": "SEA"},
                       "away": {"name": "Los Angeles Chargers", "short": "Chargers", "abbr": "LAC"}},
             "players": [{"name": "Sam Darnold", "last_name": "Darnold", "side": "home", "value": "4/7, 41 YDS, 1 TD"}],
             "facts": ["Recent play (Q1 1:54, Seahawks on offense): (Shotgun) S.Darnold pass short left to "
                       "J.Smith-Njigba to SEA 25 for 6 yards (R.Mickens)."]}
    game = {"home": {"score": 7}, "away": {"score": 3}}
    assert writer.claim_problems(["Darnold hit a 6 yard pass."], game, sheet, final=False, live=True) == []
    assert writer.claim_problems(["Darnold hit a 6 yard pass."], game, dict(sheet, facts=[]), final=False, live=True)
    # A scoring line's distance is one throw, not his total (review, Oct 4); a name inside another name doesn't count.
    scored = dict(sheet, facts=sheet["facts"] + ["Score, Q1 9:55: Seahawks Emanuel Wilson 23 Yd pass from Sam Darnold "
                                                  "(Jason Myers Kick). Chargers 3, Seahawks 7."])
    assert writer.claim_problems(["Darnold has 23 yards."], game, scored, final=False, live=True)
    assert writer._play_yards([{"name": "Pat Hill", "last_name": "Hill"}],
                              ["Recent play (Q1 1:00, Seahawks on offense): R.Hilliard up the middle for 9 yards."]) == set()


def test_live_text_check_allows_a_takes_words_but_not_advice():
    assert writer._texts_ok(["I expect this to get weird, and the Chargers will fade."], "[]", live=True) is None
    with pytest.raises(writer.CheckFailed):
        writer._texts_ok(["I expect this to get weird."], "[]")                            # the default list keeps it out
    with pytest.raises(writer.CheckFailed):
        writer._texts_ok(["You should take the over here."], "[]", live=True)


def test_live_bet_talk_allows_cover_the_receiver_not_the_spread():
    assert writer.bet_talk(["Nobody in this secondary can cover him."], live=True) is None
    assert writer.bet_talk(["Nobody in this secondary can cover him."]) == "cover"
    assert writer.bet_talk(["They are not covering the spread."], live=True)
    assert writer.bet_talk(["The over is already sweating."], live=True)


# ---------------------------------------------------------------- the writer


@pytest.fixture(autouse=True)
def passing_checker(monkeypatch):
    seen = []
    monkeypatch.setattr(client, "check", lambda prompt: seen.append(prompt) or json.dumps({"problems": []}))
    return seen


@pytest.fixture
def model(monkeypatch):
    def use(replies):
        it, calls = iter(replies), []
        monkeypatch.setattr(client, "write", lambda prompt, json_out=False, light=False:
                            calls.append(prompt) or json.dumps(next(it)))
        return calls
    return use


def test_a_null_line_is_ready_and_unchecked(model, passing_checker, payload):
    model([{"line": None}])
    res = writer.write_one_liner(_game(payload))
    assert res["status"] == "ready" and res["body"] == {"line": None}
    assert passing_checker == []                       # nothing to fact-check


def test_nothing_new_keeps_the_last_line_while_it_still_holds(model, payload):
    model([{"line": None}, {"line": None}])
    g = _game(payload)                                 # Seahawks lead 7-3
    kept = writer.write_one_liner(g, "Seattle is doing the bare minimum.", "3-7")     # written at 3-7: Seattle ahead then too
    assert kept["status"] == "ready" and kept["body"] == {"line": "Seattle is doing the bare minimum."}
    stale = writer.write_one_liner(g, "Seattle is up 4 and bored.", "3-7")           # now restates the margin
    assert stale["body"] == {"line": None}
    model([{"line": None}, {"line": None}])
    flipped = writer.write_one_liner(g, "Seattle is doing the bare minimum.", "10-7")  # the Chargers led when it was written
    assert flipped["body"] == {"line": None}
    assert writer.write_one_liner(g, "Seattle is doing the bare minimum.", None)["body"] == {"line": None}


def test_a_play_without_a_matching_team_does_not_print_none():
    g = {"home": {"name": "Seattle Seahawks", "short": "Seahawks", "abbr": "SEA", "score": 7},
         "away": {"name": "Los Angeles Chargers", "short": "Chargers", "abbr": "LAC", "score": 3}, "header": {},
         "live": {"recent": [{"period": 1, "clock": "5:00", "team": None, "side": "home", "text": "Run for 3 yards."},
                             {"period": 1, "clock": "4:00", "team": None, "side": None, "text": "Run for 2 yards."}]}}
    lines = live_facts.live_facts(g)["facts"]
    assert "Recent play (Q1 5:00, Seahawks on offense): Run for 3 yards." in lines
    assert "Recent play (Q1 4:00, A team on offense): Run for 2 yards." in lines


def test_a_reply_without_a_line_is_refused(model, payload):
    model([{"take": "x"}, {"take": "x"}])
    assert writer.write_one_liner(_game(payload))["status"] == "failed"


def test_the_live_fact_check_prompt_judges_game_facts_only(model, passing_checker, payload):
    model([{"line": "The crowd just went quiet in a way that sounds like a coach's buyout kicking in."}])
    assert writer.write_one_liner(_game(payload))["status"] == "ready"
    assert "the one line under a live score" in passing_checker[0]
    assert "Not a problem, so never list them" in passing_checker[0]


def test_a_restated_score_is_rewritten_with_the_reason(model, payload):
    calls = model([{"line": "Seattle is up 4 and nobody is worried."}, {"line": "Seattle is fine and the Chargers are not."}])
    res = writer.write_one_liner(_game(payload))
    assert res["status"] == "ready" and "restates the score or margin" in calls[1]


def test_the_previous_line_is_in_the_prompt(model, payload):
    calls = model([{"line": "Two straight punts apiece makes this the NFL's staring contest."}])
    writer.write_one_liner(_game(payload), previous="Seattle is doing the bare minimum.")
    assert "Seattle is doing the bare minimum." in calls[0] and "Don't repeat that take" in calls[0]
    calls = model([{"line": None}])
    writer.write_one_liner(_game(payload))
    assert "Don't repeat that take" not in calls[0]


def test_the_prompt_asks_for_225_characters_and_the_examples_are_clean():
    assert prompts.ONE_LINER_MAX_CHARS == 225 == writer.ONE_LINER_MAX_CHARS
    assert all(not writer.bet_talk([ex]) for ex in prompts.ONE_LINER_EXAMPLES)
    assert all(len(ex) <= 160 for ex in prompts.ONE_LINER_EXAMPLES)
    assert "{max_chars}" in prompts.ONE_LINER and "{last}" in prompts.ONE_LINER


# ---------------------------------------------------------------- tokens: the prompt, the caps, the repair


def test_examples_follow_the_phase_and_are_the_same_every_time():
    for phase, tag in (("early", "[early]"), ("middle", "[middle]"), ("late", "[late]"), ("overtime", "[late]")):
        picked = prompts.one_liner_examples(phase)
        assert len(picked) == prompts.EXAMPLES_SHOWN and picked == prompts.one_liner_examples(phase)
        have = sum(e.startswith(tag) for e in prompts.ONE_LINER_EXAMPLES)
        assert sum(x.startswith(tag) for x in picked) == min(prompts.EXAMPLES_SAME_PHASE, have)
    assert len(prompts.one_liner_examples(None)) == prompts.EXAMPLES_SHOWN


def test_the_prompt_front_is_fixed_and_facts_and_the_last_line_come_last():
    p = prompts.ONE_LINER
    assert p.index("{examples}") < p.index("{facts}") < p.index("{last}")          # Groq caches the repeated front
    one = prompts.ONE_LINER.format(facts="F1", max_chars=225, last="", examples="E")
    two = prompts.ONE_LINER.format(facts="F2", max_chars=225, last="LAST", examples="E")
    front = one.index("FACTS:")
    assert one[:front] == two[:front]


def test_the_one_liner_asks_for_a_small_reply():
    client.begin("one_liner")
    assert client._write_cap(True) == client.ONE_LINER_MAX_OUT and client._check_cap() == client.ONE_LINER_CHECK_MAX_OUT
    assert client.ONE_LINER_MAX_OUT < client.LIGHT_MAX_OUT and client.ONE_LINER_CHECK_MAX_OUT < client.CHECK_MAX_OUT
    client.begin("preview")                                    # other kinds keep their allowances
    assert client._write_cap(True) == client.LIGHT_MAX_OUT and client._write_cap(False) == client.MAX_OUT
    assert client._check_cap() == client.CHECK_MAX_OUT
    client.begin(None)


def test_a_second_sentence_over_the_limit_is_cut_not_rewritten(model, payload):
    first = "Seattle keeps punting its way through a game it somehow leads."
    long = first + " " + "And the band is wondering why it came. " * 7
    assert len(long) > writer.ONE_LINER_MAX_CHARS and len(first) < writer.ONE_LINER_MAX_CHARS
    calls = model([{"line": long}])
    res = writer.write_one_liner(_game(payload))
    assert res["status"] == "ready" and res["body"] == {"line": first} and len(calls) == 1
    calls = model([{"line": "word " * 60}, {"line": "word " * 60}])                  # no sentence break: still refused
    assert writer.write_one_liner(_game(payload))["status"] == "failed"


def test_the_sheet_and_prompt_are_small(payload):
    """A regression guard on the minute's budget: the live LAC @ SEA prompt was 1,535 tokens (Oct 4) before the hooks."""
    g = _game(payload)
    sheet = live_facts.live_facts(g)
    fj = json.dumps(sheet["facts"], ensure_ascii=False)
    prompt = prompts.ONE_LINER.format(facts=fj, max_chars=225, last="",
                                      examples="\n".join(prompts.one_liner_examples("middle")))
    assert client.prompt_tokens("openai/gpt-oss-120b", prompt) < 1000


def test_a_small_reply_allowance_is_not_refused_as_too_small():
    """MIN_OUT (600) was above the one-liner's 500: every one-liner would have been refused as 'no room'."""
    pt, out = client._sizing("openai/gpt-oss-120b", "a short prompt", client.ONE_LINER_MAX_OUT)
    assert out == client.ONE_LINER_MAX_OUT
    with pytest.raises(client.TooLarge):                       # a prompt that leaves under the 500 is still refused
        client._sizing("openai/gpt-oss-120b", "word " * 8000, client.ONE_LINER_MAX_OUT)
    pt, out = client._sizing("openai/gpt-oss-120b", "a short prompt", client.MAX_OUT)
    assert out == client.MAX_OUT


def test_same_side_leads():
    g = {"away": {"score": 3}, "home": {"score": 7}}
    assert writer.same_side_leads("0-7", g) and writer.same_side_leads("3-4", g)
    assert not writer.same_side_leads("7-3", g) and not writer.same_side_leads("7-7", g)
    assert not writer.same_side_leads(None, g) and not writer.same_side_leads("garbage", g)
    assert writer.same_side_leads("7-7", {"away": {"score": 3}, "home": {"score": 3}})


@pytest.mark.parametrize("line,hit", [("Down twenty-one and no pulse.", "Down twenty-one"),
                                      ("Trailing by thirty with a quarter to go.", "Trailing by thirty")])
def test_restates_a_big_margin_in_words(line, hit):
    assert writer.restates_score(line, {"home": {"score": 31}, "away": {"score": 10 if "twenty" in line else 1}}) == hit


@pytest.mark.parametrize("line", ["They gave up 7 yards and a smile.", "A third down 7 yards from nowhere.",
                                  "Picked up 7 on a play nobody asked for.", "He was up 7 times in the huddle."])
def test_restates_score_ignores_yards_downs_and_counts(line):
    assert writer.restates_score(line, {"home": {"score": 10}, "away": {"score": 3}}) is None


def test_scored_first_is_refused_live():
    sheet = {"teams": {"home": {"name": "Seattle Seahawks", "short": "Seahawks", "abbr": "SEA"},
                       "away": {"name": "Los Angeles Chargers", "short": "Chargers", "abbr": "LAC"}}, "facts": []}
    game = {"home": {"score": 7}, "away": {"score": 3}}
    assert writer.claim_problems(["Seattle scored first and has not looked back."], game, sheet, final=False, live=True)


def test_hooks_survive_missing_scores_and_linescores():
    g = _hooks_game(calls={"away": {"run": 2, "pass": 12}, "home": {"run": 9, "pass": 9}})
    g["away"]["score"] = None
    g["header"] = {"home": {"linescores": [7, None]}, "away": {"linescores": [None, 7]}}
    assert live_facts.live_facts(g, hooks=None)["facts"]                    # no TypeError


def test_the_other_hooks_fire_and_stay_quiet_when_they_should():
    live = {"win_prob": {"home": 60, "plays_back": 15, "home_before": 40}, "calls": {
        "away": {"run": 2, "pass": 14}, "home": {"run": 8, "pass": 8}}, "drives": [
        {"side": "away", "abbr": "PHI", "result": "Punt"}, {"side": "away", "abbr": "PHI", "result": "Touchdown"},
        {"side": "away", "abbr": "PHI", "result": "Punt"}]}
    g = _hooks_game(**live)
    g["home"]["score"], g["away"]["score"] = 10, 14                         # Philadelphia leads: not a run-less-trailing case
    g["header"] = {"home": {"linescores": [0, 10, 0]}, "away": {"linescores": [14, 0, 0]}}
    g["live"]["scores"] = [{"period": 1, "clock": "5:00", "team": "PHI", "text": "A", "away": 7, "home": 0},
                           {"period": 1, "clock": "1:00", "team": "PHI", "text": "B", "away": 14, "home": 0},
                           {"period": 2, "clock": "4:00", "team": "CHI", "text": "C", "away": 14, "home": 3},
                           {"period": 2, "clock": "1:00", "team": "CHI", "text": "D", "away": 14, "home": 10}]
    lines = live_facts.live_facts(g, hooks=None)["facts"]
    assert any(x.startswith("ESPN's live win probability") for x in lines)  # a 20-point swing
    assert "Points in Q1: Eagles 14, Bears 0." in lines                    # a finished quarter one side owned
    assert not any("Points in Q3" in x for x in lines)                      # the quarter in progress is excluded
    assert not any("last 2 drives" in x for x in lines)                     # a punt, a touchdown, a punt: no streak
    assert not any(x.startswith("Play calls") for x in lines)               # the run-light team is leading, not trailing
    g["home"]["score"], g["away"]["score"] = 14, 10                         # now Philadelphia trails
    g["live"]["scores"][-1].update(away=10, home=14)
    assert any(x.startswith("Play calls so far") for x in live_facts.live_facts(g, hooks=None)["facts"])
    g["live"]["win_prob"] = {"home": 60, "plays_back": 15, "home_before": 55}
    assert not any("win probability" in x for x in live_facts.live_facts(g, hooks=None)["facts"])
    g["live"]["win_prob"] = {"home": 95, "plays_back": 15, "home_before": 94}
    assert any("win probability" in x for x in live_facts.live_facts(g, hooks=None)["facts"])      # nearly settled


def test_the_one_liner_passes_its_caps_to_the_model_call(monkeypatch):
    seen = []
    monkeypatch.setattr(client, "_failover", lambda models, prompt, json_out, max_out, **kw: seen.append(max_out) or ("{}", "m"))
    client.begin("one_liner")
    client.write("p", json_out=True, light=True)
    client.begin("recap")
    client.write("p", json_out=True)
    client.begin(None)
    assert seen == [client.ONE_LINER_MAX_OUT, client.MAX_OUT]


# ---------- reuse: the one-liner's fingerprint (turnovers and win probability) ----------

def _turnovers(payload, home, away):
    """The trimmed fixture has no box score: give it the two teams' turnover lines."""
    payload["boxscore"] = {"teams": [
        {"homeAway": "home", "statistics": [{"name": "turnovers", "displayValue": str(home)}]},
        {"homeAway": "away", "statistics": [{"name": "turnovers", "displayValue": str(away)}]}]}
    return payload


def test_live_mark_reads_turnovers_and_the_last_win_probability(payload):
    assert summary.live_mark(payload) == {"turnovers": None, "win": 79}          # no box score in the fixture
    assert summary.live_mark(_turnovers(payload, 2, 1)) == {"turnovers": 3, "win": 79}    # both teams' together
    assert summary.live_mark({}) is None                                # ESPN sent neither
    assert summary.live_mark({"winprobability": [None, {"homeWinPercentage": "x"}]}) is None


def test_live_mark_works_on_the_slice_the_api_reads(payload):
    _turnovers(payload, 0, 2)
    slim = {"boxscore": {"teams": payload["boxscore"]["teams"]},
            "winprobability": [payload["winprobability"][-1]]}          # db.get_summary_mark's shape
    assert summary.live_mark(slim) == summary.live_mark(payload) == {"turnovers": 2, "win": 79}
    assert summary.live_mark({"boxscore": {"teams": None}, "winprobability": [None]}) is None


def test_the_fingerprint_changes_for_a_turnover_or_a_swing_of_ten_not_a_drift():
    from app.ai.jobs import one_liner_fingerprint as fp
    row = lambda f: {"claim_fingerprint": f}                            # noqa: E731
    assert fp(None, None) is None
    assert fp(None, {"turnovers": 1, "win": 60}) == "t1|w60"             # the first line
    assert fp(row("t1|w60"), {"turnovers": 1, "win": 60}) == "t1|w60"
    assert fp(row("t1|w60"), {"turnovers": 1, "win": 69}) == "t1|w60"    # a drift keeps the last claim's: no new inputs
    assert fp(row("t1|w60"), {"turnovers": 1, "win": 51}) == "t1|w60"
    assert fp(row("t1|w60"), {"turnovers": 1, "win": 70}) == "t1|w70"    # ten points
    assert fp(row("t1|w60"), {"turnovers": 1, "win": 50}) == "t1|w50"
    assert fp(row("t1|w60"), {"turnovers": 2, "win": 61}) == "t2|w61"    # a turnover
    assert fp(row("t1|w60"), {"turnovers": 1, "win": None}) == "t1|w60"  # ESPN stopped sending one: nothing new
    assert fp(row("t-|w60"), {"turnovers": 0, "win": 60}) == "t0|w60"    # a number it had not sent: baseline set
    from datetime import datetime, timedelta, timezone
    fresh = {"claim_fingerprint": "t1|w60", "updated_at": datetime.now(timezone.utc) - timedelta(minutes=2)}
    assert fp(fresh, {"turnovers": 1, "win": 75}) == "t1|w60"            # a swing inside 4 minutes of the last claim waits
    assert fp(fresh, {"turnovers": 2, "win": 75}) == "t2|w75"            # a turnover does not
    aged = dict(fresh, updated_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert fp(aged, {"turnovers": 1, "win": 75}) == "t1|w75"
    assert fp(row("garbage"), {"turnovers": 0, "win": None}) == "t0|w-"
    assert fp(row(None), {"turnovers": None, "win": 40}) == "t-|w40"


def test_a_failure_keeps_the_page_quiet_only_for_the_same_inputs():
    from datetime import datetime, timezone
    from app.ai import store
    failed = {"status": "failed", "claim_basis": "7-3", "claim_fingerprint": "t1|w60",
              "updated_at": datetime.now(timezone.utc)}
    assert store.failed_recently(failed, "7-3")                          # as before: no fingerprint asked
    assert store.failed_recently(failed, "7-3", "t1|w60")
    assert not store.failed_recently(failed, "7-3", "t2|w60")            # a turnover since: try again
    assert not store.failed_recently(failed, "7-3", None)
    assert not store.failed_recently(failed, "10-3", "t1|w60")
