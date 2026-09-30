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


def test_full_minute_moves_to_next_model_instead_of_waiting(fresh, monkeypatch):
    monkeypatch.setattr(client, "GROQ_TPM", 5000)
    prompt = "x" * 4000                      # 1,000 + 3,000 reserved: one per model per minute
    assert [json.loads(client.write(prompt))["by"] for _ in range(3)] == ["big", "qwen", "small"]


@pytest.mark.parametrize("text,secs", [("Please try again in 6m49.104s.", 409.104), ("try again in 1h2m3s", 3723),
                                       ("try again in 12.5s", 12.5), ("no hint", 60.0)])
def test_retry_after(text, secs):
    assert client._retry_after(text) == pytest.approx(secs)


def test_no_wait_fails_fast_when_every_minute_is_full(fresh, monkeypatch):
    monkeypatch.setattr(client, "GROQ_TPM", 5000)
    prompt = "x" * 4000
    for _ in range(3):
        client.write(prompt)                 # one per model fills every minute
    with client.no_wait(), pytest.raises(client.RateLimited):
        client.write(prompt)                 # the api's page open: no waiting behind the free tier


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
