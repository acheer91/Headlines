"""Writer checks with a fake model: no live AI calls in tests."""
import json

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

    def write(prompt, json_out=False):
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

    def write(prompt, json_out=False):
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
    def limited(prompt, json_out=False):
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
    calls = model([{"line": "Chicago leads by 20 in the third."}])
    res = writer.write_one_liner(live)
    assert res["status"] == "ready" and "Live, Q3 4:12" in calls[0]


def test_one_liner_too_long(model):
    long = {"line": " ".join(["word"] * 45)}
    model([long, long])
    assert writer.write_one_liner(dict(GAME, state="in"))["status"] == "failed"


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


def test_live_one_liner_may_say_tied_inside_a_quarter(model):
    # The order/lead/tie rules are for finished games; "tied 14-14 early in the third" describes a game in progress.
    g = {"league": "nfl", "state": "in", "status_detail": "3rd 12:00", "home": {"name": "Chicago Bears", "short": "Bears",
         "score": 14}, "away": {"name": "Philadelphia Eagles", "short": "Eagles", "score": 14},
         "header": {"home": {"linescores": [7, 7, 0]}, "away": {"linescores": [7, 7, 0]}}}
    model([{"line": "The Eagles responded in the second quarter and it is tied 14-14."}])
    assert writer.write_one_liner(g)["status"] == "ready"


def test_the_same_line_is_refused_in_a_recap():
    assert "'responded' inside a quarter" in _claims("final_car_cle", "The Browns responded in the second quarter.")
