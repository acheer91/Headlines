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
    checker.append([{"quote": "Bears led all game", "why": "tied after Q1"}])
    calls = model([{"recap": f"Bears led all game. {WORDS}", "home": "x", "away": "y"},
                   {"recap": f"Bears won. {WORDS}", "home": "x", "away": "y"}])
    res = writer.write_recap(GAME)
    assert res["status"] == "ready" and res["checks"] == 2
    assert "Bears led all game" in calls[1] and "rejected" in calls[1]     # the rewrite is told what was wrong


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
