"""Failover across Groq models (option C), with a fake Groq: no network."""
import json

import pytest

from app.ai import client


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Clean pacing state, and a fake Groq that answers per model from `plan`."""
    monkeypatch.setattr(client, "quota", client.MemoryQuota())
    monkeypatch.setattr(client, "WRITERS", ["big", "qwen", "small"])
    monkeypatch.setattr(client, "CHECKERS", ["qwen", "small", "big"])
    monkeypatch.setenv("AI_WRITER", "groq")
    calls, plan = [], {}

    def post(body):
        calls.append(body["model"])
        what = plan.get(body["model"], "ok")
        if what == "429":
            raise client.RateLimited("groq 429", 120)
        if what == "500":
            raise client.AIError("groq 500")
        return {"choices": [{"message": {"content": json.dumps({"by": body["model"]})}}], "usage": {"total_tokens": 5}}
    monkeypatch.setattr(client, "_groq_post", post)
    return calls, plan


def test_first_writer_used(fresh):
    calls, _ = fresh
    assert json.loads(client.write("x", json_out=True))["by"] == "big"
    assert client.last_writer() == "big" and calls == ["big"]


def test_rate_limited_writer_fails_over_and_cools_down(fresh):
    calls, plan = fresh
    plan["big"] = "429"
    assert json.loads(client.write("x"))["by"] == "qwen"
    assert client.last_writer() == "qwen"
    client.write("y")                       # "big" is cooling down: not tried again
    assert calls == ["big", "qwen", "qwen"]


def test_server_error_fails_over(fresh):
    _, plan = fresh
    plan["big"] = "500"
    assert json.loads(client.write("x"))["by"] == "qwen"


def test_every_model_limited_raises_rate_limited(fresh):
    _, plan = fresh
    plan.update(big="429", qwen="429", small="429")
    with pytest.raises(client.RateLimited):
        client.write("x")


def test_checker_never_checks_its_own_writer(fresh):
    calls, plan = fresh
    plan["big"] = "429"
    client.write("x")                        # written by qwen after the failover
    assert json.loads(client.check("c"))["by"] == "small"
    assert calls[-1] == "small"


def test_bad_json_is_not_failed_over(fresh, monkeypatch):
    def post(body):
        raise client.BadReply("groq: reply was not valid JSON")
    monkeypatch.setattr(client, "_groq_post", post)
    with pytest.raises(client.BadReply):     # the writer rewrites the text instead
        client.write("x")


def test_full_minute_is_not_handed_to_a_backup(fresh, monkeypatch):
    calls, _ = fresh
    monkeypatch.setattr(client, "GROQ_TPM", 5000)
    prompt = "x" * 4000                      # 1,000 + 3,000 reserved: one per model per minute
    client.write(prompt)
    with client.no_wait(), pytest.raises(client.RateLimited):
        client.write(prompt)                 # a page open: "big" is busy this minute -> fallback, not "qwen"
    assert calls == ["big"]


def test_waiting_writer_sleeps_for_the_good_model(fresh, monkeypatch):
    calls, _ = fresh
    monkeypatch.setattr(client, "GROQ_TPM", 5000)
    slept = []
    monkeypatch.setattr(client.time, "sleep", lambda s: (slept.append(s), client.quota._windows["big"].clear()))
    client.write("x" * 4000)
    client.write("x" * 4000)                 # the worker: waits for "big"'s minute instead of using "qwen"
    assert calls == ["big", "big"] and len(slept) == 1


@pytest.mark.parametrize("text,secs", [("Please try again in 6m49.104s.", 409.104), ("try again in 1h2m3s", 3723),
                                       ("try again in 12.5s", 12.5), ("no hint", 60.0)])
def test_retry_after(text, secs):
    assert client._retry_after(text) == pytest.approx(secs)




def test_unreadable_check_goes_to_the_next_checker(fresh, monkeypatch):
    calls, plan = fresh
    client.write("x")                        # written by "big"; checkers in order: qwen, small

    def post(body):
        calls.append(body["model"])
        if body["model"] == "qwen":
            raise client.BadReply("groq: reply was not valid JSON")
        return {"choices": [{"message": {"content": json.dumps({"by": body["model"]})}}], "usage": {"total_tokens": 5}}
    monkeypatch.setattr(client, "_groq_post", post)
    assert json.loads(client.check("c"))["by"] == "small"


def test_all_models_cooling_retries_when_the_first_frees_up(fresh):
    for m, secs in (("big", 3 * 3600), ("qwen", 2 * 3600), ("small", 5 * 3600)):
        client.quota.cool(m, secs)
    with pytest.raises(client.RateLimited) as err:
        client.write("x")
    assert err.value.retry_after == pytest.approx(2 * 3600, abs=5)      # not 60 s (audit)


def test_reply_allowance_shrinks_to_fit_the_minute(fresh, monkeypatch):
    seen = []
    monkeypatch.setattr(client, "_groq_write", lambda p, j, name, max_out, checker: (seen.append(max_out) or ("{}", 1)))
    client.write("x" * 4 * 6000)                                        # ~6,000 prompt tokens
    assert seen == [8000 - 6000 - client.MARGIN]
    with pytest.raises(client.TooLarge):
        client.write("x" * 4 * 7500)


def test_no_key_stops_at_once(fresh, monkeypatch):
    calls, _ = fresh

    def nokey(body):
        calls.append(body["model"])
        raise client.NoKey("GROQ_API_KEY not set")
    monkeypatch.setattr(client, "_groq_post", nokey)
    with pytest.raises(client.NoKey):
        client.write("x")
    assert calls == ["big"]


# ---------- the pre-write list (Adam, 2026-09-29) ----------

from app.ai import scope  # noqa: E402

FAVS = {"nfl": ["NE"], "ncaaf": ["TEX"]}


@pytest.mark.parametrize("row,want", [
    ({"league": "nfl", "home_abbr": "BUF", "away_abbr": "NE"}, True),                        # a favorite
    ({"league": "nfl", "home_abbr": "HOU", "away_abbr": "IND"}, False),                      # any other NFL game
    ({"league": "ncaaf", "home_abbr": "TEX", "away_abbr": "UTEP"}, True),
    ({"league": "ncaaf", "home_abbr": "IOWA", "away_abbr": "OSU", "home_rank": 14, "away_rank": 5}, True),
    ({"league": "ncaaf", "home_abbr": "CLEM", "away_abbr": "MIA", "home_rank": None, "away_rank": 4}, False),
])
def test_prewrite_list(row, want):
    assert scope.is_prewritten(row, FAVS) is want


# ---------- OpenRouter free models ("or:<id>"), Oct 1 ----------

def _or_reply(content):
    return {"choices": [{"message": {"content": content}}]}


def test_openrouter_backs_up_the_checkers(fresh, monkeypatch):
    calls, plan = fresh
    monkeypatch.setattr(client, "CHECKERS", ["qwen", "or:vendor/m:free", "big"])
    plan["qwen"] = "429"
    seen = []
    monkeypatch.setattr(client, "_openrouter_post",
                        lambda body: seen.append(body) or _or_reply(json.dumps({"problems": []})))
    client.write("x")                                   # written by "big"
    assert json.loads(client.check("c")) == {"problems": []}
    assert client.last_checker() == "or:vendor/m:free"
    assert seen[0]["model"] == "vendor/m:free" and seen[0]["temperature"] == 0     # the or: prefix is ours
    assert calls == ["big", "qwen"]                     # Groq tried "qwen" (429), never touched by OpenRouter


def test_openrouter_rate_limit_cools_it_down(fresh, monkeypatch):
    calls, _ = fresh
    monkeypatch.setattr(client, "CHECKERS", ["or:vendor/m:free", "small"])

    def post(body):
        calls.append(body["model"])
        raise client.RateLimited("openrouter 429", 90)
    monkeypatch.setattr(client, "_openrouter_post", post)
    client.write("x")
    assert json.loads(client.check("c"))["by"] == "small"
    client.check("c2")                                  # cooling: not asked again
    assert calls.count("vendor/m:free") == 1
    assert client.quota.is_cooling("or:vendor/m:free")


def test_missing_openrouter_key_just_leaves_that_pool_out(fresh, monkeypatch):
    monkeypatch.setattr(client, "CHECKERS", ["or:vendor/m:free", "small"])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client.write("x")
    assert json.loads(client.check("c"))["by"] == "small"      # Groq's NoKey is what stops everything; this isn't


def test_openrouter_alone_without_key_raises_no_key(fresh, monkeypatch):
    monkeypatch.setattr(client, "CHECKERS", ["or:vendor/m:free"])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client.write("x")
    with pytest.raises(client.NoKey):
        client.check("c")


def test_openrouter_unreadable_json_goes_to_the_next_checker(fresh, monkeypatch):
    monkeypatch.setattr(client, "CHECKERS", ["or:vendor/m:free", "small"])
    monkeypatch.setattr(client, "_openrouter_post", lambda body: _or_reply("Sure! here you go"))
    client.write("x")
    assert json.loads(client.check("c"))["by"] == "small"


def test_openrouter_is_counted_in_requests_not_tokens(fresh, monkeypatch):
    monkeypatch.setattr(client, "CHECKERS", ["or:vendor/m:free"])
    monkeypatch.setattr(client, "OPENROUTER_RPM", 2)
    monkeypatch.setattr(client, "_openrouter_post", lambda body: _or_reply("{}"))
    client.write("x")
    client.check("x" * 20000)                           # a big prompt costs one request, not 5,000 tokens
    client.check("y")
    with client.no_wait(), pytest.raises(client.RateLimited):
        client.check("z")                               # the third request inside the minute


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_openrouter_post_429_and_errors(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    send = lambda r: monkeypatch.setattr(client.httpx, "post", lambda *a, **kw: r)
    send(_Resp(429, {"error": {"code": 429, "metadata": {"raw": "temporarily rate-limited upstream"}}}))
    with pytest.raises(client.RateLimited) as err:
        client._openrouter_post({"model": "m"})
    assert err.value.retry_after == 60
    reset_ms = (client.time.time() + 3600) * 1000       # OpenRouter's own limit says when it resets
    send(_Resp(429, {"error": {"code": 429, "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}}}))
    with pytest.raises(client.RateLimited) as err:
        client._openrouter_post({"model": "m"})
    assert err.value.retry_after == pytest.approx(3600, abs=5)
    send(_Resp(200, {"error": {"code": 429, "message": "x"}}))     # an upstream 429 inside a 200
    with pytest.raises(client.RateLimited):
        client._openrouter_post({"model": "m"})
    send(_Resp(200, {"error": {"code": 502, "message": "x"}}))
    with pytest.raises(client.AIError):
        client._openrouter_post({"model": "m"})
    send(_Resp(503, {}))
    with pytest.raises(client.AIError):
        client._openrouter_post({"model": "m"})
    send(_Resp(200, _or_reply("hi")))
    assert client._openrouter_post({"model": "m"})["choices"][0]["message"]["content"] == "hi"


def test_qwen_on_openrouter_does_not_check_qwen_from_groq(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["qwen/q"])
    monkeypatch.setattr(client, "CHECKERS", ["or:qwen/q:free", "small"])
    client.write("x")                                   # written by Groq's qwen/q
    assert json.loads(client.check("c"))["by"] == "small"
    assert client.check_model() == "small"
