"""Writer checks with a fake model: no live AI calls in tests."""
import json
import os

import pytest

from app.ai import client, writer

GAME = {"league": "nfl", "state": "post", "home": {"name": "Chicago Bears", "short": "Bears", "score": 27},
        "away": {"name": "Philadelphia Eagles", "short": "Eagles", "score": 7},
        "header": {"home": {"linescores": [7, 3, 10, 7]}, "away": {"linescores": [0, 7, 0, 0]}},
        "bets": [{"label": "Spread", "text": "CHI +3 covered by 23"}]}
ARTICLE = {"url": "https://www.espn.com/nfl/story/_/id/1/x", "outlet": "ESPN", "title": "Bears host Eagles",
           "published": "2026-09-28T12:00+00:00",
           "text": "The Chicago Bears defense has forced 9 turnovers this season and Eagles writer Jane Doe "
                   "picks the Eagles to win a tight one in the cold at Soldier Field on Sunday night."}


def fake(replies):
    """Replace the model with canned JSON replies, in order."""
    it = iter(replies)
    calls = []

    def write(prompt, json_out=False, light=False, reasoning=None):
        calls.append(prompt)
        return json.dumps(next(it))
    return write, calls


@pytest.fixture(autouse=True)
def checker(monkeypatch):
    """The fact-checker passes everything unless a test gives it verdicts."""
    verdicts = []

    def check(prompt):
        return json.dumps({"problems": verdicts.pop(0) if verdicts else []})
    monkeypatch.setattr(client, "check", check)
    return verdicts


@pytest.fixture
def model(monkeypatch):
    def use(replies):
        w, calls = fake(replies)
        monkeypatch.setattr(client, "write", w)
        return calls
    return use


def test_fact_check_problem_rewrites_with_feedback(model, checker):
    checker.append([{"quote": "Bears led comfortably throughout", "why": "tied after Q1"}])
    calls = model([{"recap": f"Bears led comfortably throughout. {WORDS}", "home": "x", "away": "y"},
                   {"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    res = writer.write_recap(GAME)
    assert res["status"] == "ready" and res["checks"] == 2
    assert "Bears led comfortably throughout" in calls[1] and "rejected" in calls[1]     # the rewrite is told what was wrong


def test_fact_check_problem_twice_fails(model, checker):
    checker.extend([[{"quote": "a", "why": "b"}], [{"quote": "a", "why": "b"}]])
    model([{"recap": f"A. {WORDS}", "home": "x", "away": "y"}, {"recap": f"A. {WORDS}", "home": "x", "away": "y"}])
    assert writer.write_recap(GAME)["status"] == "failed"


def test_checker_unreachable_fails_never_unchecked(model, monkeypatch):
    def down(prompt):
        raise client.RateLimited("groq 429")
    monkeypatch.setattr(client, "check", down)
    model([{"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    assert writer.write_recap(GAME)["status"] == "failed"


WORDS = " ".join(["word"] * 70)


def test_numbers_ok():
    assert writer.numbers_ok("won 27-7 by 20", '{"final": "27-7", "margin": 20}')
    assert not writer.numbers_ok("won by 21", '{"final": "27-7", "margin": 20}')


def test_copy_check_ignores_stat_lines():
    art = [{"text": "he completed 12 passes for 217 yards and four touchdowns in a rout of the visitors today"}]
    assert writer.copied("Smith had 12 passes for 217 yards and four touchdowns", art) is None
    assert writer.copied("It was a rout of the visitors today by any measure", art) is None   # 7 words shared
    assert writer.copied("It ended in four touchdowns in a rout of the visitors", art) == \
        "four touchdowns in a rout of the visitors"


def test_recap_ready(model):
    calls = model([{"recap": f"Bears won 27-7. {WORDS}", "home": "Bears good.", "away": "Eagles not."}])
    res = writer.write_recap(GAME)
    assert res["status"] == "ready" and res["calls"] == 1 and len(calls) == 1     # no extract call
    assert res["body"]["bets"] == "Spread: CHI +3 covered by 23."


def test_no_fifth_quarter_any_hyphen():
    assert writer.BAD_PERIOD.search("a final seventh‑quarter‑long drive")   # seen 2026-09-29
    assert not writer.BAD_PERIOD.search("the fourth-quarter drive")


def test_no_fifth_quarter(model):
    bad = {"recap": f"A seventh-quarter touchdown sealed it. {WORDS}", "home": "x", "away": "y"}
    model([bad, bad])
    assert writer.write_recap(GAME)["rejected"][0].startswith("no such quarter")


def test_invalid_json_is_rewritten(model, monkeypatch):
    replies = iter(["bad", {"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])

    def write(prompt, json_out=False, light=False):
        r = next(replies)
        if r == "bad":
            raise client.BadReply("groq: reply was not valid JSON")
        return json.dumps(r)
    monkeypatch.setattr(client, "write", write)
    assert writer.write_recap(GAME)["status"] == "ready"


def test_bet_talk_in_recap_rejected(model):
    bad = {"recap": f"Chicago covered easily. {WORDS}", "home": "x", "away": "y"}
    model([bad, bad])
    assert writer.write_recap(GAME)["rejected"][0].startswith("bet talk")


def test_stray_number_retries_once_then_fails(model):
    bad = {"recap": f"Bears won by 21. {WORDS}", "home": "x", "away": "y"}
    model([bad, bad])
    res = writer.write_recap(GAME)
    assert res["status"] == "failed" and res["calls"] == 2
    assert all("21" in r for r in res["rejected"])


def test_stray_number_fixed_on_retry(model):
    model([{"recap": f"By 21. {WORDS}", "home": "x", "away": "y"},
           {"recap": f"By 20. {WORDS}", "home": "x", "away": "y"}])
    assert writer.write_recap(GAME)["status"] == "ready"


def test_advice_rejected(model):
    bad = {"recap": f"Next week take the over. {WORDS}", "home": "x", "away": "y"}
    model([bad, bad])
    assert writer.write_recap(GAME)["status"] == "failed"


def test_preview_extract_inventing_numbers_fails(model):
    invented = dict(PREVIEW_FACTS, storylines=[{"fact": "Bears are 5-0 at home", "article": 1}])
    model([invented, invented])
    assert writer.write_preview(dict(GAME, state="pre"), [ARTICLE])["status"] == "failed"


def test_wrong_shape_is_a_failed_check(model):
    model([["not", "an", "object"], {"recap": None}])
    assert writer.write_recap(GAME)["status"] == "failed"


def test_rate_limit_gives_failed(monkeypatch):
    def limited(prompt, json_out=False, light=False):
        raise client.RateLimited("groq 429")
    monkeypatch.setattr(client, "write", limited)
    res = writer.write_recap(GAME)
    assert res["status"] == "failed" and "rate limited" in res["reason"]


def test_no_articles_is_no_sources_without_calls(model):
    calls = model([])
    res = writer.write_preview(dict(GAME, state="pre"), [])
    assert res["status"] == "no_sources" and calls == []


PREVIEW_FACTS = {"storylines": [],
                 "edges": {"home": [{"fact": "Bears forced 9 turnovers", "article": 1}], "away": []},
                 "picks": [{"writer": "Jane Doe", "outlet": "ESPN", "pick": "Eagles", "article": 1},
                           {"writer": "Made Up", "outlet": "ESPN", "pick": "Bears", "article": 1}]}


def test_preview_edges_and_picks(model):
    model([PREVIEW_FACTS, {"preview": WORDS, "edges": {"home": [{"text": "Chicago has 9 takeaways.", "article": 1}],
                                                        "away": []}}])
    res = writer.write_preview(dict(GAME, state="pre"), [ARTICLE])
    assert res["status"] == "ready"
    assert res["body"]["edges"]["home"][0]["url"] == ARTICLE["url"]
    assert [p["writer"] for p in res["body"]["picks"]] == ["Jane Doe"]    # the writer not in the article is dropped


def test_edge_must_come_from_its_article(model):
    bad = dict(PREVIEW_FACTS, edges={"home": [{"fact": "Bears 27 points", "article": 1}], "away": []})
    model([bad, bad])
    assert writer.write_preview(dict(GAME, state="pre"), [ARTICLE])["status"] == "failed"


def test_edge_with_unknown_article_fails(model):
    bad = dict(PREVIEW_FACTS, edges={"home": [{"fact": "x", "article": 7}], "away": []})
    model([bad, bad])
    assert writer.write_preview(dict(GAME, state="pre"), [ARTICLE])["status"] == "failed"


def test_one_liner_from_live_facts(model):
    live = dict(GAME, state="in", status_detail="Q3 4:12")
    calls = model([{"line": "Chicago has this one in a headlock, and it is only the third quarter."}])
    res = writer.write_one_liner(live)
    assert res["status"] == "ready" and "Live, Q3 4:12" in calls[0]


def test_one_liner_too_long(model):
    long = {"line": "word " * 46}          # 230 characters: over the 225 limit
    model([long, long])
    res = writer.write_one_liner(dict(GAME, state="in"))
    assert res["status"] == "failed" and "characters" in res["rejected"][0]


def test_live_one_liner_may_say_tied_inside_a_quarter(model):
    g = {"league": "nfl", "state": "in", "status_detail": "3rd 12:00", "home": {"name": "Chicago Bears", "short": "Bears",
         "score": 14}, "away": {"name": "Philadelphia Eagles", "short": "Eagles", "score": 14},
         "header": {"home": {"linescores": [7, 7, 0]}, "away": {"linescores": [7, 7, 0]}}}
    model([{"line": "The Eagles responded in the second quarter, and the third is starting dead even."}])
    assert writer.write_one_liner(g)["status"] == "ready"


def _live_phi_chi():
    from tests.test_ai_facts import load
    g = load("final_phi_chi")
    g.update(state="in", status_detail="Q3 10:12", line={"home_spread": -3.0, "total": 44.5})
    return g


def test_one_liner_writer_and_checker_see_the_same_trimmed_facts(model, monkeypatch):
    # M4 (2026-10-02): the writer's prompt and the fact-checker's carry the same FACTS, trimmed to the prompt's hooks.
    seen = []
    monkeypatch.setattr(client, "check", lambda prompt: seen.append(prompt) or json.dumps({"problems": []}))
    calls = model([{"line": "Three giveaways already. Philadelphia is gift-wrapping this one."}])
    assert writer.write_one_liner(_live_phi_chi())["status"] == "ready"
    facts_in = lambda p: p.split("FACTS:\n", 1)[1].split("\n\nTEXT:", 1)[0].strip()
    assert facts_in(calls[0]) == facts_in(seen[0])
    assert "Game phase: " in facts_in(calls[0])
    assert not any(w in facts_in(calls[0]) for w in ("Rushing leader", "possession", "over/under", "penalties"))


def test_one_liner_refuses_what_the_trimmed_sheet_left_out(model):
    # The rushing leader's 84 yards and the 36:53 of possession were on the old sheet; now they're unsupported.
    model([{"line": "Swift has 84 yards already. Chicago is in no hurry."},
           {"line": "Chicago has held it for 36:53. The Eagles are spectators."}])
    res = writer.write_one_liner(_live_phi_chi())
    assert res["status"] == "failed"
    assert "'84'" in res["rejected"][0] and "'36', '53'" in res["rejected"][1]


def test_one_liner_code_checks_know_the_leaders_the_trimmed_sheet_left_out(model, checker):
    # 128 is Chicago's rushing so far, not Swift's. The sheet no longer carries the rushing line (a hook only when the
    # yardage gap is big), so the draft is refused as a number not in the facts; and claims_ok, given a sheet that does
    # carry it, still refuses it as a team total handed to a player, because it knows every leader on the box score
    # (review of M4, Oct 2). The writer's FACTS leave Swift out.
    from app.ai import facts, live_facts
    g = _live_phi_chi()
    assert facts.stats(g)["rushingYards"] == {"home": "128", "away": "107"}     # Chicago's team total, not Swift's
    sheet = dict(live_facts.live_facts(g), players=facts.players(g),
                 facts=["So far, rushing: Eagles 107, Bears 128."])
    with pytest.raises(writer.CheckFailed) as exc:
        writer.claims_ok(["Swift has 128 rushing yards already."], g, sheet, final=False, live=True)
    assert "isn't on the player's line" in str(exc.value) and "D'Andre Swift" in str(exc.value)
    calls = model([{"line": "Swift has 128 rushing yards already."},
                   {"line": "Chicago is cruising and nobody on the other sideline looks surprised."}])
    res = writer.write_one_liner(g)
    assert res["status"] == "ready" and len(calls) == 2
    assert "numbers not in the facts" in res["rejected"][0]
    assert "Swift" not in calls[0].split("FACTS:", 1)[1]


def test_no_checker_outside_the_family_fails_closed_without_a_retry(model, monkeypatch):
    def check(prompt):
        raise client.NoChecker("no checker outside the openai family")
    monkeypatch.setattr(client, "check", check)
    model([{"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    res = writer.write_recap(GAME)
    assert res["status"] == "failed" and "retry_after" not in res     # never published unchecked, never retried


def test_each_text_runs_on_its_own_route(model, monkeypatch):
    kinds = []
    monkeypatch.setattr(client, "begin", kinds.append)
    model([{"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    writer.write_recap(GAME)
    assert kinds == ["recap"]


def test_recap_fallback():
    assert writer.recap_fallback(GAME) == "Final: Bears 27, Eagles 7. Spread: CHI +3 covered by 23."


# ---------- audit fixes (2026-09-29) ----------

PRE = dict(GAME, state="pre")


@pytest.mark.parametrize("pick,why", [
    ({"writer": "Jane Doe", "pick": "You should hammer the Bears, lock of the year", "article": 1}, "advice wording"),
    ({"writer": "Jane Doe", "pick": "Eagles 31-10", "article": 1}, "numbers not in its article"),
    ({"writer": "Jane Doe", "pick": "the over", "article": 1}, "names neither team"),
    ({"writer": "Jane Doe", "pick": "Eagles", "article": 7}, "no such article"),
    ({"writer": "Nobody Here", "pick": "Eagles", "article": 1}, "writer not named near a team"),
])
def test_bad_picks_are_dropped(pick, why):
    assert why in (writer.pick_problem(pick, {1: ARTICLE}.get(pick["article"]), PRE) or "")


def test_good_pick_passes():
    assert writer.pick_problem({"writer": "Jane Doe", "pick": "Eagles", "article": 1}, ARTICLE, PRE) is None


def test_saved_extract_follows_the_articles_not_their_order(model):
    a1 = dict(ARTICLE, url="https://www.espn.com/nfl/story/_/id/1/a", text="Bears defense rolls " * 30)
    a2 = dict(ARTICLE, url="https://www.espn.com/nfl/story/_/id/2/b", text="Eagles quarterback hurt " * 30)
    saved = {"storylines": [{"fact": "Eagles QB hurt", "url": a2["url"]}],
             "edges": {"home": [], "away": [{"fact": "Eagles QB is hurt", "url": a2["url"]}]}, "picks": []}
    calls = model([{"preview": WORDS, "edges": {"home": [], "away": [{"text": "The Eagles QB is hurt.", "article": 1}]}}])
    res = writer.write_preview(PRE, [a2, a1], extract=saved)           # same articles, other order
    assert res["status"] == "ready" and len(calls) == 1                 # no extract call
    assert '"article": 1' in calls[0]                                   # a2 is article 1 today
    assert res["body"]["edges"]["away"][0]["url"] == a2["url"]
    assert res["extract"]["edges"]["away"][0]["url"] == a2["url"]       # stored by URL again


def test_extract_is_stored_by_url(model):
    model([PREVIEW_FACTS, {"preview": WORDS, "edges": {"home": [{"text": "Chicago has 9 takeaways.", "article": 1}],
                                                        "away": []}}])
    res = writer.write_preview(PRE, [ARTICLE])
    assert res["extract"]["edges"]["home"][0] == {"fact": "Bears forced 9 turnovers", "url": ARTICLE["url"]}


def test_extract_carries_its_articles_and_survives_a_rate_limit_on_the_write(model, monkeypatch):
    """A rate limit after the extract call hands the extract back, so the retry doesn't pay for it again."""
    replies = iter([PREVIEW_FACTS])

    def write(prompt, json_out=False, light=False):
        try:
            return json.dumps(next(replies))
        except StopIteration:
            raise client.RateLimited("every model cooling down", 600) from None
    monkeypatch.setattr(client, "write", write)
    res = writer.write_preview(PRE, [ARTICLE])
    assert res["status"] == "failed" and res["retry_after"] == 600
    assert res["extract"]["urls"] == [ARTICLE["url"]]
    assert res["extract"]["edges"]["home"][0]["url"] == ARTICLE["url"]


def test_failures_say_whether_the_setup_or_the_text_was_at_fault(monkeypatch):
    def failing(exc):
        def write(prompt, json_out=False, light=False):
            raise exc
        monkeypatch.setattr(client, "write", write)
        return writer.write_recap(GAME)
    assert failing(client.NoKey("no key"))["unconfigured"] is True            # fix the setup, not the text
    assert failing(client.NoChecker("none"))["unconfigured"] is True
    assert "unconfigured" not in failing(client.TooLarge("too big"))           # the text's own inputs
    limited = failing(client.RateLimited("429", 90))
    assert limited["retry_after"] == 90 and "unconfigured" not in limited
    assert failing(client.AIError("503"))["retry_after"] == writer.TRANSIENT_RETRY


def test_a_check_failure_does_not_hand_back_the_extract(model):
    model([PREVIEW_FACTS, {"preview": "too short", "edges": {"home": [], "away": []}},
           {"preview": "too short", "edges": {"home": [], "away": []}}])
    res = writer.write_preview(PRE, [ARTICLE])
    assert res["status"] == "failed" and "extract" not in res


# ---------- box-score claims checked in code (2026-09-30 eval: real sentences from it) ----------

def _claims(fixture, text):
    from app.ai import facts
    from tests.test_ai_facts import load
    g = load(fixture)
    try:
        writer.claims_ok([text], g, facts.recap_facts(g))
        return ""
    except writer.CheckFailed as exc:
        return str(exc)


@pytest.mark.parametrize("fixture,text,why", [
    ("final_phi_chi", "Chicago amassed 375 total yards, with 247 passing yards from Case Keenum and 128 rushing yards "
                      "from D'Andre Swift.", "128 isn't on the player's line"),
    ("final_phi_chi", "Philadelphia's offense produced 141 passing yards from Jalen Hurts.", "141 isn't"),
    ("final_atl_gb", "Time of possession favored Green Bay 22:55 to 37:05 for Atlanta.", "Falcons had more"),
    ("final_min_tb", "The Buccaneers fell to 0-3, scoring 16 points on the road.", "were the home team"),
    ("final_ari_sf", "The 49ers held the ball for 21:16; the Cardinals dominated possession at 38:44.", "'dominated'"),
    ("final_ten_nyg", "The Titans scored the game's only touchdown.", "the game's only"),
    ("final_phi_chi", "Three giveaways kept them off balance.", "off balance"),
    ("final_bal_dal", "The Ravens out‑gained the Cowboys in total yardage, 378 to 415.", "Cowboys had more yards"),
    ("final_min_tb", "The Vikings held the ball slightly longer, 30:57 to 29:03.", "Buccaneers had the ball longer"),
])
def test_box_score_claims_rejected(fixture, text, why):
    assert why in _claims(fixture, text)


@pytest.mark.parametrize("fixture,text", [
    ("final_cin_pit", "Burrow was 28 of 37 for 282 yards and three touchdowns. Chase Brown ran 13 carries for 61 yards "
                      "and Ja’Marr Chase hauled in 9 catches for 98 yards and a touchdown. The Bengals fell to 2‑1."),
    ("final_lac_buf", "Time of possession tilted slightly to the Chargers at 31:22 versus 28:38, while rushing "
                      "favored Buffalo 176 to 131."),
    ("final_hou_ind", "Turnovers favored Houston, which forced 3 giveaways and committed none."),
    ("final_lar_den", "Rams amassed 482 total yards to Denver’s 257, outgaining the Broncos on the ground 116‑78."),
    ("final_cin_pit", "The Steelers outgained their opponent 411 to 352 and improved to 2‑1."),
    ("final_lac_buf", "James Cook carried 24 times for 154 yards and a touchdown."),          # ESPN: James Cook III
    ("final_atl_gb", "Michael Penix Jr. went 18 of 25 for 256 yards, a touchdown and an interception."),
    ("final_min_tb", "The Vikings won on the road, 23-16."),
])
def test_true_box_score_claims_pass(fixture, text):
    assert _claims(fixture, text) == ""


def test_preview_line_must_match_ours():
    g = {"line": {"home_spread": -14.5}}
    with pytest.raises(writer.CheckFailed, match="14.5"):
        writer.line_ok("Ohio State heads to Kinnick as a 14‑point favorite.", g)
    writer.line_ok("Ohio State is a 14.5-point favorite.", g)
    writer.line_ok("Both defenses allow under 13 points a game.", g)


def test_checker_problem_called_fine_is_ignored(model, checker):
    checker.append([{"quote": "Bears won", "verdict": "fine", "why": "I see no problems."}])
    model([{"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    assert writer.write_recap(GAME)["status"] == "ready"


def test_checker_reason_is_cut_short(model, checker):
    checker.extend([[{"quote": "a", "verdict": "wrong", "why": "x" * 900}]] * 2)
    model([{"recap": f"A. {WORDS}", "home": "x", "away": "y"}] * 2)
    res = writer.write_recap(GAME)
    assert res["status"] == "failed" and len(res["reason"]) < 300


# ---------- recap length: a one-minute read, longer for bigger games (2026-10-01) ----------

@pytest.mark.parametrize("fixture,edit,want", [
    ("final_phi_chi", {}, ("standard", "standard")),
    ("final_ne_jax", {}, ("featured", "a favorite team")),
    ("final_cin_pit", {}, ("featured", "decided by 3 or less")),
    ("final_lv_no", {}, ("featured", "lead changed 2+ times")),
    # 3-0 and 2-1 after a game the first team won: 2-0 and 2-0 going in.
    ("final_phi_chi", {"records": ("2-1", "3-0")}, ("featured", "two winning teams")),
    ("final_phi_chi", {"ranks": (5, 14), "league": "ncaaf"}, ("featured", "ranked vs ranked")),
    ("final_phi_chi", {"ot": True}, ("featured", "overtime")),
])
def test_recap_length_tier(fixture, edit, want):
    from tests.test_ai_facts import load
    g = load(fixture)
    if "records" in edit:
        g["header"]["away"]["record"], g["header"]["home"]["record"] = edit["records"]
    if "ranks" in edit:
        g["away"]["rank"], g["home"]["rank"] = edit["ranks"]
        g["league"] = edit["league"]
    if edit.get("ot"):
        g["header"]["away"]["linescores"] += [0]
        g["header"]["home"]["linescores"] += [0]
    assert writer.recap_length(g) == want


def test_recap_is_about_120_words_while_the_one_minute_read_is_off(model):
    # Back to ~120 words until Adam confirms 200-230 (2026-10-01): no tiers, no total cap, as before 8d2066a.
    assert writer.ONE_MINUTE_READ is False
    calls = model([{"recap": f"Bears won. {WORDS}", "home": " ".join(["x"] * 90), "away": " ".join(["y"] * 90)}])
    res = writer.write_recap(GAME)
    assert res["status"] == "ready" and len(calls) == 1
    assert "about 120 words" in calls[0] and "2-3 sentences, on" in calls[0] and "one-minute" not in calls[0]
    assert res["length"]["why"] == "about 120 words"


def test_recap_over_200_words_is_rewritten(model):
    calls = model([{"recap": " ".join(["word"] * 201), "home": "x", "away": "y"},
                   {"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    assert writer.write_recap(GAME)["status"] == "ready" and "recap is 201 words" in calls[1]


def test_recap_over_its_length_is_rewritten_shorter(model, monkeypatch):
    monkeypatch.setattr(writer, "ONE_MINUTE_READ", True)
    long = {"recap": f"Bears won. {WORDS}", "home": " ".join(["x"] * 80), "away": " ".join(["y"] * 80)}
    calls = model([long, {"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    res = writer.write_recap(GAME)
    assert res["status"] == "ready" and "220 in all at most" in calls[1]
    assert res["length"] == {"tier": "standard", "why": "standard", "words": 74}
    assert "about 110 words" in calls[0] and "about 45 words" in calls[0]


# ---------- Oct 1 accuracy fixes: comparisons, time inside a quarter, causes (the reviewed errors, 24 + 15) ----------

@pytest.mark.parametrize("fixture,text,why", [
    # order inside a quarter: only the score at each quarter break is known
    ("final_phi_chi", "The Bears opened with a 7-0 first quarter and added 3 in the second before the Eagles "
                      "narrowed the gap.", "'before' inside a quarter"),
    ("final_car_cle", "The Panthers put three points on the board in the second while the Browns responded with ten.",
     "'responded' inside a quarter"),
    ("final_phi_chi", "The Bears added a field goal in the second before the Eagles tied with a touchdown.",
     "'tied' is a tie the sheet doesn't show"),
    ("final_atl_gb", "The Packers managed a late field goal to end at 35-14.", "'late field goal'"),
    ("final_lac_buf", "Chargers opened with a 10-0 lead in the first quarter, the only time they scored first.",
     "'scored first'"),
    ("final_lar_den", "Denver won it in the fourth, and the visitors controlled the early minutes.",
     "'controlled the early'"),
    # the whole game
    ("final_nyj_det", "New York managed 24 points despite trailing all game.", "'all game'"),
    ("final_min_tb", "The Vikings never saw the lead change from start to finish.", "'start to finish'"),
    ("final_car_cle", "The Browns held on, preserving the lead through the end.", "through the end"),
    ("final_nyj_det", "Detroit led at halftime and kept a 7-point edge through the third and fourth quarters.",
     "'edge through the third'"),
    # causes
    ("final_ten_nyg", "New York improved to 2-1, bolstered by a clean ball and 34:26 of possession.", "'bolstered'"),
    ("final_lar_den", "Denver checked out for the first two quarters, allowing Los Angeles to build a 16-0 lead.",
     "'checked out'"),
    ("final_lar_den", "The Broncos capitalized on key opportunities.", "'capitalized'"),
    ("final_phi_chi", "Chicago took advantage of a 13:46 possession edge to control the game.", "'took advantage'"),
    ("final_lv_no", "The team finished winless in four games before this one.", "'winless'"),
    # who led, and the right direction
    ("final_lac_buf", "A field goal in the third kept the Chargers ahead 13-10, then the Bills scored 14.",
     "did not lead at the break before that quarter"),
    ("final_lac_buf", "Los Angeles added three points in the third to lead 13-10, the only lead change between "
                      "quarter breaks.", "never took the lead"),
    ("final_sea_wsh", "Seattle surged ahead with 14 points in Q4.", "Seahawks did not lead at the end of that quarter"),
    ("final_lar_den", "The fourth quarter provided the final scramble, with each team adding points to close the gap.",
     "no gap to close"),
    ("final_nyj_det", "Detroit outgained the Jets 381 to 335, with a rushing advantage of 131 to 58 and a passing "
                      "advantage of 250 to 277.", "Jets had more"),
    ("final_ari_sf", "Turnovers were split, each side accounting for one.", "turnovers were not even"),
    ("final_lv_no", "The Saints held the ball for over five minutes longer.", "the possession gap was 4:56"),
    # a number given to the wrong thing
    ("final_ne_jax", "New England's offense sputtered: 199 passing yards, 83 on the ground, 3 turnovers and only 3 "
                     "points.", "199 is Drake Maye's own line"),
    ("final_ne_jax", "New England's offense sputtered: 199 passing yards, 83 on the ground, 3 turnovers and only 3 "
                     "points.", "Patriots scored 6 in all"),
    ("final_ten_nyg", "The Giants built a steady lead, scoring three points in the first, six at halftime and three "
                      "more in the third.", "Giants had 9 at halftime"),
])
def test_oct1_claims_rejected(fixture, text, why):
    assert why in _claims(fixture, text)


@pytest.mark.parametrize("fixture,text", [
    ("final_kc_mia", "The first quarter ended tied 7-7."),
    ("final_lac_buf", "The Bills answered with 10 points in the second to tie it at halftime."),
    ("final_lar_den", "Denver scored 16 unanswered points in the third to force a 16‑16 tie."),
    # across quarters the order is plain from the breaks: only one team scored in each
    ("final_ten_nyg", "The Giants added 3 in the third before Tennessee's lone touchdown in the fourth quarter."),
    ("final_sea_wsh", "Both teams scored 7 in the third, keeping the Commanders ahead 24-17."),
    ("final_nyj_det", "The Lions added seven in the third, extending the lead to seven."),
    ("final_atl_gb", "The Falcons surged ahead with ten unanswered points in the second."),
    ("final_atl_gb", "Each side forced one turnover."),
    ("final_lac_buf", "The Chargers owned the time‑of‑possession edge, 31:22 to 28:38."),
    ("final_lac_buf", "Buffalo outgained Los Angeles by two total yards, 350 to 348, and held a slight edge in "
                      "rushing, 176 to 131."),
    ("final_hou_ind", "The Colts held the ball for 35:13, ten minutes and twenty‑six seconds longer than the Texans."),
    ("final_lar_den", "Los Angeles opened with a 7‑0 first quarter and extended the lead to 16‑0 by halftime, thanks "
                      "to 7 points in the first and 9 in the second."),
    ("final_lar_den", "Denver sat down 0‑16 at halftime but rallied with 16 points in the third quarter and 14 in "
                      "the fourth to win 30‑26."),
    ("final_sea_wsh", "Washington never trailed at a quarter break and held on 33-31."),
    ("final_ne_jax", "Jacksonville had 3 passing touchdowns and a rushing score, and New England had 234 passing yards."),
])
def test_oct1_true_claims_pass(fixture, text):
    assert _claims(fixture, text) == ""


@pytest.mark.parametrize("fixture,text", [
    ("final_atl_gb", "The Falcons allowed 328 yards to Green Bay."),
    ("final_atl_gb", "The Packers allowed 498 total yards to the Falcons."),
    ("final_ari_sf", "The 49ers led at every break through the end of the third quarter."),
])
def test_cause_rules_leave_plain_stats_alone(fixture, text):
    assert _claims(fixture, text) == ""


def test_allowing_x_to_verb_is_a_cause():
    assert "'allowing Los Angeles to establish'" in _claims("final_lar_den", "Denver trailed, allowing Los Angeles to "
                                                                          "establish a 16-0 lead.")


def test_advantage_goes_to_the_first_number_not_the_first_team_named():
    # "Despite Detroit's rushing edge of 131 to 58, the Jets had a passing advantage of 277 to 250" is right.
    assert _claims("final_nyj_det", "Despite Detroit's rushing edge of 131 to 58, the Jets had a passing advantage of "
                                    "277 to 250.") == ""
    assert "Lions had the 250" in _claims("final_nyj_det", "The Lions had a passing advantage of 250 to 277.")


def test_live_claims_may_say_tied_inside_a_quarter():
    # The order/lead/tie rules are for finished games; "tied 14-14 early in the third" describes a game in progress.
    # No live text uses this now (the one-liner is the template, CTO 2026-10-01); kept for an AI one later.
    from app.ai import facts
    g = {"league": "nfl", "state": "in", "status_detail": "3rd 12:00", "home": {"name": "Chicago Bears", "short": "Bears",
         "score": 14}, "away": {"name": "Philadelphia Eagles", "short": "Eagles", "score": 14},
         "header": {"home": {"linescores": [7, 7, 0]}, "away": {"linescores": [7, 7, 0]}}}
    line = "The Eagles responded in the second quarter and it is tied 14-14."
    assert writer.claim_problems([line], g, facts.live_facts(g), final=False) == []


def test_the_same_line_is_refused_in_a_recap():
    assert "'responded' inside a quarter" in _claims("final_car_cle", "The Browns responded in the second quarter.")


# ---------- Adam's voice (2026-10-01): few-shot examples, rounding with a word, "push" in football ----------

EXAMPLE_GAMES = {"eagles": "final_phi_chi", "patriots": "final_ne_jax", "seahawks": "final_sea_wsh"}


@pytest.mark.parametrize("teams,text", [(t, x) for t, x in __import__("app.ai.prompts", fromlist=["x"]).RECAP_EXAMPLES])
def test_the_example_recaps_pass_our_own_checks(teams, text):
    # An example the checks would refuse teaches the model to write something refused.
    import json as _json
    from app.ai import facts
    from tests.test_ai_facts import load
    g = load(EXAMPLE_GAMES[teams[0]])
    sheet = facts.recap_facts(g)
    writer._texts_ok([text], _json.dumps(sheet["facts"], ensure_ascii=False))
    writer.claims_ok([text], g, sheet)
    assert writer.bet_talk([text]) is None
    assert 100 <= len(text.split()) <= 130           # about 120 words, as Adam wrote them


def test_a_recap_prompt_never_shows_its_own_games_example(model):
    calls = model([{"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    writer.write_recap(GAME)                                        # Bears vs Eagles: that example is left out
    assert "Example 1:" in calls[0] and "Example 2:" in calls[0] and "Example 3:" not in calls[0]
    assert "Chicago jumped ahead early" not in calls[0] and "New England outgained Jacksonville" in calls[0]
    assert "tension in FACTS" in calls[0]


def test_a_recap_that_reuses_an_example_joke_or_line_is_rewritten(model):
    calls = model([{"recap": f"The Bears played like a surprisingly competent substitute teacher. {WORDS}",
                    "home": "x", "away": "y"},
                   {"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    assert writer.write_recap(GAME)["status"] == "ready" and "reused the examples" in calls[1]


@pytest.mark.parametrize("text,ok", [
    ("The Bears controlled the ball for nearly 37 minutes.", True),            # 36:53
    ("The Bears held the ball for about 37 minutes.", True),
    ("The Bears held the ball for nearly 45 minutes.", False),
    ("The Bears controlled the ball for 37 minutes.", False),                  # no word, no rounding
    ("The Bears piled up nearly 375 yards.", True),
    ("The Bears piled up nearly 500 yards.", False),
])
def test_rounding_with_a_word_is_allowed_within_a_unit(text, ok):
    facts = "time of possession: Eagles 23:07, Bears 36:53. total yards: Eagles 248, Bears 375."
    if ok:
        writer._texts_ok([text], facts)
    else:
        with pytest.raises(writer.CheckFailed, match="numbers not in the facts"):
            writer._texts_ok([text], facts)


def test_push_is_football_unless_it_comes_with_betting_words():
    assert writer.bet_talk(["Washington survived Seattle's fourth-quarter push."]) is None
    assert writer.bet_talk(["The spread ended in a push."]) == "spread"
    assert writer.bet_talk(["It was a push on the total."]) == "push"


def test_the_difference_is_fine_for_a_stat_and_a_cause_otherwise():
    assert _claims("final_sea_wsh", "The difference was the turnover column: three Seattle giveaways, zero for "
                                    "Washington.") == ""
    assert "The difference'" in _claims("final_sea_wsh", "The difference was Washington's grit.")


@pytest.mark.parametrize("fixture,text", [
    ("final_car_cle", "The Browns held a 14-second time-of-possession edge."),            # seconds, not the second quarter
    ("final_ten_nyg", "Tennessee's second touchdown never came."),                         # an ordinal, not a quarter
    ("final_ne_jax", "The Jags held the ball for just under 32 minutes, three minutes longer than the Patriots."),
    ("final_car_cle", "Cleveland's ground game outgained Carolina's, 132 to 98."),
])
def test_oct1_voice_run_false_alarms_stay_fixed(fixture, text):
    # Found by the first live run with the new voice: each of these was refused although it is true.
    assert _claims(fixture, text) == ""


def test_a_quarter_means_a_quarter():
    assert writer._quarters("Seattle scored in the second, a 14-second drive, then a second touchdown in Q4.") == [1, 3]


def test_recap_voice_can_be_switched_off(model, monkeypatch):
    monkeypatch.setattr(writer, "RECAP_VOICE", False)
    calls = model([{"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    assert writer.write_recap(GAME)["status"] == "ready"
    assert "Example 1:" not in calls[0] and "tension in FACTS" not in calls[0]


@pytest.mark.parametrize("voice", [True, False])
def test_the_recap_prompt_starts_with_the_text_every_game_shares(model, monkeypatch, voice):
    # M2 (2026-10-02): the text that is the same for every game comes first and the game's own parts (examples, team
    # names, FACTS) last, so Groq can reuse the cached prefix from one recap to the next. Same words, new order.
    from app.ai import prompts
    monkeypatch.setattr(writer, "RECAP_VOICE", voice)
    other = {**GAME, "home": {**GAME["home"], "name": "Dallas Cowboys", "short": "Cowboys"},
             "away": {**GAME["away"], "name": "Baltimore Ravens", "short": "Ravens"}}
    calls = model([{}] * 4)                                         # the wrong shape: two calls a game, then failed
    writer.write_recap(GAME)
    writer.write_recap(other)
    shared = os.path.commonprefix([calls[0], calls[2]])
    for fixed in (prompts.VOICE, prompts.GUARDRAILS, "Also (each of these was a real error)", "FACTS is a list"):
        assert fixed in shared
    assert (prompts.RECAP_STYLE in shared) is voice and ("Examples, from other games" in shared) is voice
    for own in ("Chicago Bears", "Dallas Cowboys", "Example 1:\nNew England", "FACTS:\n["):
        assert own not in shared
    assert calls[0].index("Return JSON only:") > calls[0].index(prompts.GUARDRAILS)


# ---------- M3 (2026-10-02): the code-built recap outline and a low-reasoning writer, in T3's arms only ----------

def _fixture(name):
    from tests.test_ai_facts import load
    return load(name)


def test_the_outline_is_off_and_the_default_prompt_is_unchanged():
    # Off unless asked (only outline_eval asks), and off means the prompt is exactly today's (the 16 finals' request
    # bodies were diffed byte for byte before and after M3). On, the only change is the outline block, before FACTS.
    from app.ai import facts
    for name in ("final_lar_den", "final_ne_jax", "final_phi_chi"):
        g = _fixture(name)
        off, on = writer.recap_prompt(g), writer.recap_prompt(g, outline=True)
        assert off == writer.recap_prompt(g, outline=False) and "OUTLINE" not in off
        block = writer.outline_text(facts.recap_outline(g))
        assert block and on == off.replace("\nFACTS:\n", "\n" + block + "FACTS:\n", 1)


def test_the_outline_sits_after_the_shared_text():
    from app.ai import facts, prompts
    games = [_fixture("final_lar_den"), _fixture("final_sea_wsh")]
    prompts_on = [writer.recap_prompt(g, outline=True) for g in games]
    plan = facts.recap_outline(games[0])
    assert "Angle: Broncos trailed by double digits at a quarter break and won." in prompts_on[0]
    assert all(f"{i}. {line}" in prompts_on[0] for i, line in enumerate(plan["lines"], 1))
    assert prompts_on[0].index("OUTLINE") > prompts_on[0].index("Return JSON only:")
    assert prompts_on[0].index("FACTS:\n[") > prompts_on[0].index("OUTLINE")
    shared = os.path.commonprefix(prompts_on)
    assert prompts.GUARDRAILS in shared and "OUTLINE" not in shared       # the cached prefix is unchanged (M2)


def test_production_recaps_have_no_outline_or_reasoning_switch(monkeypatch):
    # Review of M3 (Oct 2): RECAP_OUTLINE / RECAP_REASONING turned T3's arms on in production with no Z5 or T3 gate.
    assert not hasattr(writer, "RECAP_OUTLINE") and not hasattr(writer, "RECAP_REASONING")
    efforts = []

    def write(prompt, json_out=False, light=False, **kw):
        efforts.append((kw.get("reasoning", "unset"), "OUTLINE" in prompt))
        return json.dumps({"recap": "Bears won 27-7. " + WORDS, "home": "Bears good.", "away": "Eagles not."})
    monkeypatch.setattr(client, "write", write)
    assert writer.write_recap(GAME)["status"] == "ready" and efforts == [("unset", False)]   # the client's own


def test_only_the_extraction_steps_ask_for_extract_mode(monkeypatch):
    flags = []
    replies = iter([PREVIEW_FACTS, {"preview": WORDS, "edges": {"home": [], "away": []}},
                    {"items": [{"fact": "Bears won 27-7", "news": 1}]},
                    {"items": [{"text": f"Headline {c}", "news": 1} for c in "ABCDEF"]}])

    def write(prompt, json_out=False, light=False):
        flags.append(light)
        return json.dumps(next(replies))
    monkeypatch.setattr(client, "write", write)
    assert writer.write_preview(dict(GAME, state="pre"), [ARTICLE])["status"] == "ready"
    from datetime import datetime, timezone
    news = [{"published_at": datetime(2026, 9, 28, tzinfo=timezone.utc), "headline": "Bears beat Eagles 27-7", "league": "nfl",
             "description": "", "url": "https://www.espn.com/nfl/story/_/id/2/y"}]
    assert writer.write_headlines(news)["status"] == "ready"
    assert flags == [True, False, True, False]          # preview: extract, write; headlines: extract, write


def test_headlines_must_cover_every_league_with_news(monkeypatch):
    # Adam, 2026-10-06: the feed is every sport we load, not three NFL lines. A set with no college stories is rewritten.
    from datetime import datetime, timezone
    news = [{"league": lg, "published_at": datetime(2026, 10, 5, tzinfo=timezone.utc), "headline": f"{lg} story {i}",
             "description": "", "url": f"https://www.espn.com/{lg}/story/{i}"} for lg in ("nfl", "ncaaf") for i in range(3)]
    nfl_only = {"items": [{"text": f"NFL line {c}", "news": 1 + i % 3} for i, c in enumerate("ABCDEFGH")]}
    both = {"items": [{"text": f"Line {c}", "news": n} for c, n in zip("ABCDEFGH", (1, 2, 3, 4, 5, 6, 1, 4))]}
    prompts, replies = [], iter([{"items": [{"fact": "a story", "news": 1, "league": "NFL"}]}, nfl_only, both])

    def write(prompt, json_out=False, light=False):
        prompts.append(prompt)
        return json.dumps(next(replies))
    monkeypatch.setattr(client, "write", write)
    out = writer.write_headlines(news)
    assert out["status"] == "ready" and len(out["body"]["items"]) == 8
    assert len(prompts) == 3 and "every league needs coverage" in prompts[2] and "NCAAF" in prompts[2]
    assert "NCAAF | ESPN | 2026-10-05" in prompts[0] and "FINALS" not in prompts[0]      # each story is tagged; no scores in
    assert {i["url"].split("/")[3] for i in out["body"]["items"]} == {"nfl", "ncaaf"}
    assert {i["league"] for i in out["body"]["items"]} == {"nfl", "ncaaf"}          # each line carries its league for the chip


# ---------- weekend columns (Adam, 2026-10-06) ----------

def _weekend_facts():
    from datetime import datetime, timezone
    from app.ai import weekend_facts
    rows = [{"league": "nfl", "away": "Philadelphia Eagles", "away_score": 7, "home": "Chicago Bears", "home_score": 31,
             "status_detail": "Final", "start_time": datetime(2026, 10, 4, 17, tzinfo=timezone.utc), "home_rank": None,
             "away_rank": None, "recap": "The Bears won 31-7. Their defense set the tone."}]
    return weekend_facts.build("nfl", rows, [{"headline": "Eagles lose again", "description": ""}])


def _column(sentence="The Bears flattened the Eagles 31-7 and the whole afternoon played like a long nap with a scoreboard.", n=6):
    return {"title": "The Bears are a problem and the Eagles are a mystery",
            "paragraphs": [" ".join([sentence] * (n // 2)), " ".join([sentence] * (n - n // 2))]}


def test_a_weekend_column_is_written_checked_and_returned(model):
    calls = model([_column()])
    res = writer.write_weekend(_weekend_facts())
    assert res["status"] == "ready" and res["checks"] == 1
    assert res["body"]["title"].startswith("The Bears") and len(res["body"]["paragraphs"]) == 2
    assert '"league": "NFL"' in calls[0] and "Chicago Bears 31, Philadelphia Eagles 7" in calls[0]
    assert "Ringer-style" in calls[0] and "FACTS" in calls[0]


def test_a_weekend_column_with_a_wrong_length_is_rewritten(model):
    calls = model([_column(n=2), _column()])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"
    assert "words: write about 115" in calls[1]


@pytest.mark.parametrize("bad, why", [
    ("The Bears are on a three-game winning streak after beating the Eagles 31-7 on a nap of an afternoon.", "history or a record"),
    ("The Bears covered the spread against the Eagles 31-7 on a nap of an afternoon.", "bet talk"),
    ("The Bears beat the Eagles 31-9 on a nap of an afternoon, and the Eagles will be sad.", "numbers not in the facts"),
])
def test_a_weekend_column_with_history_betting_or_a_made_up_number_is_rewritten(model, bad, why):
    calls = model([_column(sentence=bad), _column()])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"
    assert why in calls[1]


def test_a_weekend_column_that_reuses_an_example_is_rewritten(model):
    from app.ai import prompts
    copied_line = prompts.ONE_LINER_EXAMPLES[10].split("] ", 1)[1]
    calls = model([_column(sentence=copied_line), _column()])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"
    assert "reused an example" in calls[1]


def test_a_weekend_column_the_fact_checker_rejects_three_times_fails(model, checker):
    checker.extend([[{"quote": "x", "why": "not in FACTS"}]] * 3)
    calls = model([_column(), _column(), _column()])
    res = writer.write_weekend(_weekend_facts())
    assert res["status"] == "failed" and "fact check" in res["reason"] and len(calls) == 3      # a third draft is tried


def test_a_weekend_column_the_third_draft_can_pass(model, checker):
    checker.extend([[{"quote": "x", "why": "not in FACTS"}]] * 2)
    model([_column(), _column(), _column()])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"


def test_the_weekend_fact_check_prompt_is_the_weekend_one(model, monkeypatch):
    prompts_seen = []
    monkeypatch.setattr(client, "check", lambda p: (prompts_seen.append(p), json.dumps({"problems": []}))[1])
    model([_column()])
    writer.write_weekend(_weekend_facts())
    assert "weekend sports column" in prompts_seen[0]


def test_the_weekend_column_is_written_at_low_reasoning_and_other_texts_are_not(monkeypatch):
    seen = []

    def write(prompt, json_out=False, light=False, reasoning=None):
        seen.append(reasoning)
        return json.dumps(_column())
    monkeypatch.setattr(client, "write", write)
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"
    assert seen == ["low"]


def test_a_weekend_column_may_not_invent_a_venue_a_day_or_a_show(model):
    bad = "The Bears flattened the Eagles 31-7 at Soldier Field on Sunday and it felt like a Netflix finale."
    calls = model([_column(sentence=bad), _column()])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"
    assert "names FACTS never gives" in calls[1] and "Soldier" in calls[1] and "Sunday" in calls[1] and "Netflix" in calls[1]


def test_unknown_names_skips_sentence_starts_possessives_and_names_in_the_facts():
    facts = '{"results": ["Atlanta Falcons 45, New Orleans Saints 24"], "notes": [{"leaders": ["Falcons passing: Michael Penix 20/30"]}]}'
    ok = ["Meanwhile the Falcons' offense and the Saints’ defense met. Penix did the rest, and NFL fans noticed."]
    assert writer.unknown_names(ok, facts) == []
    assert writer.unknown_names(["The Falcons beat Chicago in the Superdome."], facts) == ["Chicago", "Superdome"]


def test_unknown_names_lets_acronyms_through_and_the_title_is_not_checked(model):
    assert writer.unknown_names(["Nine TDs and a BBQ later, the NFL shrugged."], '{"x": "NFL"}') == []
    title_case = {"title": "Bears Flatten Eagles In Cold", "paragraphs": _column()["paragraphs"]}
    model([title_case])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"


def test_a_weekend_column_that_ranks_games_is_rewritten(model):
    ranky = "The Bears flattened the Eagles 31-7 in the biggest statement of the afternoon, a nap with a scoreboard."
    calls = model([_column(sentence=ranky), _column()])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"
    assert "ranking games" in calls[1] and "biggest" in calls[1]


def test_the_checker_prompts_guard_roles_and_winners():
    from app.ai import prompts
    assert "an analyst called a player" in prompts.FACT_CHECK
    assert "A name before a colon" in prompts.EXTRACT_HEADLINES
    assert "The team listed first in a" in prompts.WEEKEND_FACT_CHECK


def test_a_weekend_column_with_a_junk_paragraph_is_rewritten(model):
    junk = _column()
    junk["paragraphs"].append(")")
    calls = model([junk, _column()])
    assert writer.write_weekend(_weekend_facts())["status"] == "ready"
    assert "is not a paragraph" in calls[1]


def test_ranking_words_catch_an_adjective_in_between():
    assert writer.RANKING.search("The Falcons were the only true blowout of the day.")
    assert writer.RANKING.search("Just the only upset.")
    assert not writer.RANKING.search("There were no surprises, and one blowout.")


def test_a_headline_with_a_bracketed_placeholder_is_rewritten(model):
    from datetime import datetime, timezone
    news = [{"league": "nfl", "published_at": datetime(2026, 10, 7, tzinfo=timezone.utc), "headline": "Cam Jurgens traded to Ravens",
             "description": "", "url": f"https://www.espn.com/nfl/story/_/id/{i}/x"} for i in range(1, 4)]
    bad = {"items": [{"text": f"[PERSON_NAME] moves again {c}", "news": 1 + i % 3} for i, c in enumerate("ABCDEF")]}
    good = {"items": [{"text": f"Cam Jurgens moves again {c}", "news": 1 + i % 3} for i, c in enumerate("ABCDEF")]}
    calls = model([{"items": [{"fact": "Cam Jurgens was traded to the Ravens", "news": 1, "league": "NFL"}]}, bad, good])
    assert writer.write_headlines(news)["status"] == "ready"
    assert "bracketed placeholder" in calls[2] and "[PERSON_NAME]" in calls[2]


def test_headlines_get_three_drafts_before_failing(model, checker):
    from datetime import datetime, timezone
    news = [{"league": "nfl", "published_at": datetime(2026, 10, 7, tzinfo=timezone.utc), "headline": "Story",
             "description": "", "url": "https://www.espn.com/nfl/story/_/id/1/x"}]
    ok = {"items": [{"text": f"Story line {c}", "news": 1} for c in "ABCDEF"]}
    checker.extend([[{"quote": "x", "why": "not in FACTS"}]] * 3)
    calls = model([{"items": [{"fact": "a story", "news": 1, "league": "NFL"}]}, ok, ok, ok])
    res = writer.write_headlines(news)
    assert res["status"] == "failed" and len(calls) == 4          # one extract and three writes


def test_a_headline_naming_someone_no_story_names_is_rewritten(model):
    from datetime import datetime, timezone
    news = [{"league": "nfl", "published_at": datetime(2026, 10, 7, tzinfo=timezone.utc), "headline": "Cam Jurgens traded to Ravens",
             "description": "", "url": f"https://www.espn.com/nfl/story/_/id/{i}/x"} for i in range(1, 4)]
    made_up = {"items": [{"text": f"Cam Jurgens and Tom Brady move again {c}", "news": 1 + i % 3} for i, c in enumerate("ABCDEF")]}
    good = {"items": [{"text": f"Cam Jurgens moves again {c}", "news": 1 + i % 3} for i, c in enumerate("ABCDEF")]}
    calls = model([{"items": [{"fact": "Cam Jurgens was traded to the Ravens", "news": 1, "league": "NFL"}]}, made_up, good])
    assert writer.write_headlines(news)["status"] == "ready"
    assert "names that are in none of the stories" in calls[2] and "Brady" in calls[2]


def test_an_extract_with_a_placeholder_is_redone(model):
    from datetime import datetime, timezone
    news = [{"league": "nfl", "published_at": datetime(2026, 10, 7, tzinfo=timezone.utc), "headline": "Cam Jurgens traded to Ravens",
             "description": "", "url": "https://www.espn.com/nfl/story/_/id/1/x"}]
    ok = {"items": [{"text": f"Cam Jurgens moves again {c}", "news": 1} for c in "ABCDEF"]}
    calls = model([{"items": [{"fact": "[PERSON_NAME] was traded", "news": 1, "league": "NFL"}]},
                   {"items": [{"fact": "Cam Jurgens was traded", "news": 1, "league": "NFL"}]}, ok])
    assert writer.write_headlines(news)["status"] == "ready"
    assert "bracketed placeholder" in calls[1]
