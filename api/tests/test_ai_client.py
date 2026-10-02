"""Failover across Groq models (option C), with a fake Groq: no network."""
import json

import pytest

from app.ai import client


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Clean pacing state, and a fake Groq that answers per model from `plan`."""
    monkeypatch.setattr(client, "quota", client.MemoryQuota())
    monkeypatch.setattr(client, "WRITERS", ["big", "qwen"])
    monkeypatch.setattr(client, "CHECKERS", ["qwen", "small", "big"])
    client.forget_last()
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
    plan.update(big="429", qwen="429")
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
                                       ("try again in 12.5s", 12.5), ("no hint", 60.0),
                                       ("Please try again in 20ms.", 0.02), ("try again in 340ms", 0.34),
                                       ("try again in 419.999999ms", 0.42), ("try again in 1m30.5s", 90.5)])
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
    monkeypatch.setattr(client, "_groq_write", lambda p, j, name, max_out, checker, **kw: (seen.append(max_out) or ("{}", 1)))
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
def test_prewrite_list(row, want, monkeypatch):
    monkeypatch.setattr(scope, "AI_LEAGUES", {"nfl", "ncaaf"})
    assert scope.is_prewritten(row, FAVS) is want


def test_ai_leagues_default_to_nfl_only():
    # CTO, 2026-10-01: college football gets no AI text until someone turns it on (AI_LEAGUES).
    assert scope.AI_LEAGUES == {"nfl"}
    assert not scope.is_prewritten({"league": "ncaaf", "home_abbr": "TEX", "away_abbr": "UTEP"}, FAVS)
    assert scope.is_prewritten({"league": "nfl", "home_abbr": "BUF", "away_abbr": "NE"}, FAVS)


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


# ---------- CTO rules (2026-10-01): routes, families, failover log, daily budgets ----------

def test_family_is_the_vendor():
    assert client.family("openai/gpt-oss-120b") == client.family("openai/gpt-oss-20b") == "openai"
    assert client.family("or:qwen/qwen3.8-27b:free") == client.family("qwen/qwen3.8-27b") == "qwen"
    assert client.family("gemini-3.6-flash") == "gemini"


def test_gpt_oss_never_checks_gpt_oss(fresh, monkeypatch):
    calls, _ = fresh
    monkeypatch.setattr(client, "WRITERS", ["openai/gpt-oss-120b"])
    monkeypatch.setattr(client, "CHECKERS", ["openai/gpt-oss-20b", "qwen/q"])
    client.write("x")
    assert json.loads(client.check("c"))["by"] == "qwen/q"
    assert "openai/gpt-oss-20b" not in calls and client.check_model() == "qwen/q"


def test_no_checker_outside_the_family_fails_closed(fresh, monkeypatch):
    calls, _ = fresh
    monkeypatch.setattr(client, "WRITERS", ["openai/gpt-oss-120b"])
    monkeypatch.setattr(client, "CHECKERS", ["openai/gpt-oss-20b", "openai/gpt-oss-120b"])
    client.write("x")
    with pytest.raises(client.NoChecker):          # it once fell back to the whole list, the writer included
        client.check("c")
    assert calls == ["openai/gpt-oss-120b"] and client.check_model() is None


def test_routes_name_one_writer_and_a_checker_from_another_family(monkeypatch):
    monkeypatch.setattr(client, "WRITERS", [])
    monkeypatch.setattr(client, "CHECKERS", [])
    for kind, r in client.ROUTES.items():
        writers, checkers = client.route(kind)
        assert writers == [r.writer] + ([r.backup] if r.backup else []) and len(writers) <= 2
        assert all(client.family(c) != client.family(w) for c in checkers for w in writers), kind
    assert client.route("recap") == (["openai/gpt-oss-120b"], ["qwen/qwen3.8-27b", "or:qwen/qwen3.8-27b:free"])
    client.begin("preview")
    assert client.model() == "openai/gpt-oss-120b"
    with pytest.raises(ValueError):
        client.begin("one_liner")                   # no AI one-liner any more


def test_at_most_one_backup_writer(monkeypatch):
    monkeypatch.setenv("AI_WRITERS", "a/x,b/y,c/z")
    with pytest.raises(ValueError):
        client._override("AI_WRITERS", 2)


def test_failover_is_logged(fresh, caplog):
    _, plan = fresh
    plan["big"] = "429"
    with caplog.at_level("WARNING", logger="app.ai.client"):
        client.write("x")
    assert "ai failover: writer big -> qwen" in caplog.text


def test_spent_daily_budget_is_skipped_before_asking(fresh, monkeypatch):
    calls, _ = fresh
    monkeypatch.setattr(client, "GROQ_TPD", 2999)              # less than one call's reservation (~3,000)
    with pytest.raises(client.RateLimited) as err:
        client.write("x")
    assert calls == [] and client.quota.is_cooling("big") and err.value.retry_after > 59


def test_daily_spend_counts_tokens_used_and_requests():
    q = client.MemoryQuota()
    _, h = q.reserve(["a"], 3000, 8000, False)
    q.used(h, 5)
    q.reserve(["a"], 3000, 8000, False)                        # not reported yet: its reservation counts
    assert q.spent_today("a", False)[0] == 3005 and q.spent_today("a", True)[0] == 2
    q.reserve(["or:v/a:free"], 1, 20, False)
    q.reserve(["or:v/b:free"], 1, 20, False)
    assert q.spent_today("or:", True)[0] == 2 and q.spent_today("or:v/a:free", True)[0] == 1


def test_openrouter_daily_budget_is_the_whole_accounts(fresh, monkeypatch):
    calls, _ = fresh
    monkeypatch.setattr(client, "OPENROUTER_RPD", 1)
    monkeypatch.setattr(client, "CHECKERS", ["or:v/a:free", "or:v/b:free", "small"])
    seen = []
    monkeypatch.setattr(client, "_openrouter_post", lambda body: seen.append(body["model"]) or _or_reply("{}"))
    client.write("x")
    client.check("c1")
    assert json.loads(client.check("c2"))["by"] == "small"     # the account's one request a day is spent
    assert seen == ["v/a:free"]


def test_gemini_in_a_route_has_its_requests_a_day(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["gemini-x"])
    monkeypatch.setattr(client, "GEMINI_RPD", 1)
    asked = []
    monkeypatch.setattr(client, "_gemini_call", lambda prompt, json_out, name: asked.append(name) or "{}")
    assert client.write("x") == "{}" and client.last_writer() == "gemini-x"
    assert json.loads(client.check("c"))["by"] == "qwen"       # a checker from another family
    with pytest.raises(client.RateLimited):
        client.write("y")
    assert asked == ["gemini-x"]


# ---------- preflight (2026-10-01): is a text of this kind writable at all right now? ----------

def test_unavailable_for_is_none_when_writer_and_a_checker_can_be_asked(fresh):
    assert client.unavailable_for("recap") is None


def test_unavailable_for_a_cooling_writer(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["big"])
    client.quota.cool("big", 300)
    assert 290 < client.unavailable_for("recap") <= 300


def test_unavailable_for_a_writer_that_has_spent_its_day(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["big"])
    monkeypatch.setattr(client, "GROQ_TPD", 3000)
    assert client.unavailable_for("recap") is None
    client.quota.reserve(["big"], 1500, 8000, wait=False)          # 1,500 spent: 1,500 + a 2,000 floor is over 3,000
    assert 86000 < client.unavailable_for("recap") <= 86400        # until that spend leaves the rolling day
    monkeypatch.setattr(client, "GROQ_TPD", 3500)
    assert client.unavailable_for("recap") is None                 # room for a call again


def test_unavailable_for_is_none_when_a_backup_writer_is_free(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["big", "qwen"])
    client.quota.cool("big", 300)
    assert client.unavailable_for("recap") is None                 # a backup writer is free (AI_WRITERS only)


def test_unavailable_for_waits_for_the_soonest_of_two_limited_writers(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["big", "qwen"])
    client.quota.cool("big", 300)
    client.quota.cool("qwen", 120)
    assert 110 < client.unavailable_for("recap") <= 120


def test_unavailable_for_checks_the_family_of_the_writer_that_will_be_used(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["openai/a", "qwen/q"])
    monkeypatch.setattr(client, "CHECKERS", ["qwen/q", "openai/b"])
    client.quota.cool("openai/a", 600)                             # the backup, qwen/q, will write ...
    client.quota.cool("openai/b", 200)                             # ... so only openai/b may check, and it is cooling
    assert 190 < client.unavailable_for("recap") <= 200


def test_unavailable_for_when_every_allowed_checker_is_limited(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["big"])
    monkeypatch.setattr(client, "CHECKERS", ["qwen", "small", "big"])
    client.quota.cool("qwen", 600)
    assert client.unavailable_for("recap") is None                 # "small" can still check
    client.quota.cool("small", 120)
    assert 110 < client.unavailable_for("recap") <= 120            # the soonest of the checkers
    # "big" is the writer's own family: it being free doesn't make the text checkable


def test_unavailable_for_leaves_a_missing_checker_to_the_normal_path(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["openai/gpt-oss-120b"])
    monkeypatch.setattr(client, "CHECKERS", ["openai/gpt-oss-20b"])
    assert client.unavailable_for("recap") is None                 # NoChecker fails the text closed, not retried


def test_unavailable_for_counts_a_checkers_daily_requests(fresh, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(client, "WRITERS", ["big"])
    monkeypatch.setattr(client, "CHECKERS", ["or:q:free"])
    monkeypatch.setattr(client, "OPENROUTER_RPD", 1)
    client.quota.reserve(["or:q:free"], 1, 20, wait=False)
    assert client.unavailable_for("recap") > 59


# ---------- schedules (2026-10-01): AI schedules cover only AI leagues ----------

def test_ai_schedules_cover_only_ai_leagues(monkeypatch):
    from app.ai import scope
    from app.temporal import schedules
    monkeypatch.setattr(schedules, "LEAGUES", ["nfl", "ncaaf"])
    monkeypatch.setattr(scope, "AI_LEAGUES", {"nfl"})
    out = schedules._schedules()
    assert set(out) == {"schedule-sync", "headlines", "ai-leftover", "ai-previews-nfl"}
    assert out["ai-leftover"][1] == ["nfl"] and out["schedule-sync"][1] == ["nfl", "ncaaf"]
    assert out["headlines"][1] == ["nfl", "ncaaf"]
    monkeypatch.setattr(scope, "AI_LEAGUES", {"nfl", "ncaaf"})
    assert set(schedules._schedules()) == {"schedule-sync", "headlines", "ai-leftover", "ai-previews-nfl",
                                           "ai-previews-ncaaf"}
    monkeypatch.setattr(scope, "AI_LEAGUES", set())
    assert set(schedules._schedules()) == {"schedule-sync", "headlines"}


def test_a_checker_without_a_key_is_not_a_checker_for_preflight(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["openai/w"])
    monkeypatch.setattr(client, "CHECKERS", ["qwen/q", "or:qwen/q:free"])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client.quota.cool("qwen/q", 600)
    assert 590 < client.unavailable_for("recap") <= 600            # the keyless overflow pool can't check
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert client.unavailable_for("recap") is None                 # with a key it can


def test_unavailable_for_with_the_real_routes(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", [])
    monkeypatch.setattr(client, "CHECKERS", [])
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for kind in ("recap", "preview", "headlines"):
        assert client.unavailable_for(kind) is None
    client.quota.cool("qwen/qwen3.8-27b", 600)
    assert client.unavailable_for("recap") is None                 # OpenRouter's Qwen can still check
    client.quota.cool("or:qwen/qwen3.8-27b:free", 300)
    assert 290 < client.unavailable_for("preview") <= 300          # both Qwens limited: the sooner one
    client.quota.cool("openai/gpt-oss-120b", 900)
    assert 890 < client.unavailable_for("headlines") <= 900        # no backup writer: its wait is the answer


def test_a_missing_key_releases_the_reservation(fresh, monkeypatch):
    def no_key(body):
        raise client.NoKey("GROQ_API_KEY not set")
    monkeypatch.setattr(client, "_groq_post", no_key)
    for _ in range(5):
        with pytest.raises(client.NoKey):
            client.write("x")
    assert client.quota.spent_today("big", requests=False)[0] == 0   # nothing was asked, nothing spent


def test_reusable_extract():
    from app.ai.jobs import _reusable_extract as reusable
    arts = [{"url": "u1"}, {"url": "u2"}]
    assert reusable(None, arts) is None
    kept = {"urls": ["u2", "u1"], "storylines": []}
    assert reusable({"extract": kept, "body": None, "sources": None}, arts) == kept       # a failed row's, any order
    assert reusable({"extract": {"urls": ["u1"]}, "body": None, "sources": None}, arts) is None
    old = {"extract": {"storylines": []}, "body": {"preview": "p"}, "sources": [{"url": "u1"}, {"url": "u2"}]}
    assert reusable(old, arts) == old["extract"]                                          # saved before "urls"
    assert reusable(dict(old, body=None), arts) is None
    assert reusable(dict(old, sources=[{"url": "u1"}]), arts) is None


def test_rejected_out():
    from app.ai import store
    row = {"rejections": store.REJECTION_CAP, "claim_basis": "b", "claim_fingerprint": "f"}
    assert store.rejected_out(row, "b", "f") and store.rejected_out(row, "b")        # api: fingerprint unknown
    assert not store.rejected_out(row, "b", "other") and not store.rejected_out(row, "other")
    assert not store.rejected_out(dict(row, rejections=store.REJECTION_CAP - 1), "b", "f")
    assert not store.rejected_out(None, "b")
    assert store.rejected_out(dict(row, claim_fingerprint=None), "b", None)          # a recap has no fingerprint
    assert store.counts_as_rejection({"status": "failed", "reason": "x"})
    assert not store.counts_as_rejection({"status": "failed", "retry_after": 5.0})
    assert not store.counts_as_rejection({"status": "failed", "unconfigured": True})
    assert not store.counts_as_rejection({"status": "ready"})


# ---------- under the writer's minute (Oct 1): counted prompts, low-reasoning extracts ----------

def test_gpt_oss_prompts_are_counted_with_its_tokenizer():
    import tiktoken
    p = "Pittsburgh at Cleveland: Steelers -2.5, total 38.5. T.J. Watt (hamstring) is questionable. " * 20
    n = len(tiktoken.get_encoding("o200k_harmony").encode(p))
    assert client.prompt_tokens("openai/gpt-oss-120b", p) == n + client.GPT_OSS_OVERHEAD
    assert client.prompt_tokens("or:openai/gpt-oss-20b:free", p) == n + client.GPT_OSS_OVERHEAD
    assert client.prompt_tokens("qwen/qwen3.8-27b", p) == len(p) // 4      # other models: still estimated
    assert n > len(p) // 4                                                  # the old estimate ran low here


def test_without_the_tokenizer_gpt_oss_is_estimated_on_the_high_side(monkeypatch):
    monkeypatch.setattr(client, "_encoding", False)                         # tiktoken couldn't load
    assert client.prompt_tokens("openai/gpt-oss-120b", "x" * 3000) == 1000 + client.GPT_OSS_OVERHEAD


def test_the_minute_is_reserved_from_the_counted_prompt(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["openai/gpt-oss-120b"])
    seen = []
    monkeypatch.setattr(client, "_groq_write", lambda p, j, name, max_out, checker, **kw: (seen.append(max_out) or ("{}", 1)))
    p = "Steelers 2.5, 38.5; T.J. Watt questionable. " * 300                 # ~5,200 counted, ~3,300 estimated
    counted = client.prompt_tokens("openai/gpt-oss-120b", p)
    client.write(p)
    assert seen == [client.GROQ_TPM - counted - client.MARGIN] and seen[0] < client.MAX_OUT     # shrunk by the real size


def test_extract_calls_use_low_reasoning_and_a_smaller_reply(fresh, monkeypatch):
    monkeypatch.setattr(client, "WRITERS", ["openai/gpt-oss-120b"])
    bodies = []

    def post(body):
        bodies.append(body)
        return {"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 5}}
    monkeypatch.setattr(client, "_groq_post", post)
    client.write("x", json_out=True, extract=True)
    client.write("x", json_out=True)
    assert (bodies[0]["reasoning_effort"], bodies[0]["max_completion_tokens"]) == ("low", client.EXTRACT_MAX_OUT)
    assert (bodies[1]["reasoning_effort"], bodies[1]["max_completion_tokens"]) == (client.REASONING, client.MAX_OUT)
