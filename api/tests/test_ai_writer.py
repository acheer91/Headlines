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


def test_recap_fallback():
    assert writer.recap_fallback(GAME) == "Final: Bears 27, Eagles 7. Spread: CHI +3 covered by 23."
