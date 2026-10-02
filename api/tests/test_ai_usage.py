"""The usage report (python -m app.ai.usage) over rows the app really keeps, and the retention it relies on."""
import json

import pytest

from app.ai import usage
from test_api import TEST_DB, _ids, _sql, client  # noqa: F401 — the client fixture

WRITER, CHECKER = "openai/gpt-oss-120b", "qwen/qwen3.8-27b"
OR_A, OR_B = "or:qwen/qwen3.8-27b:free", "or:other/model:free"

TZ = "'America/Los_Angeles'"
# Fixed Pacific times, so no test straddles midnight whenever it runs: noon yesterday, and the two minutes around
# the midnight that starts yesterday (the day before it, then yesterday: different Pacific days, the same UTC date).
NOON = f"((date_trunc('day', now() AT TIME ZONE {TZ}) - interval '12 hours') AT TIME ZONE {TZ})"
MIDNIGHT = f"((date_trunc('day', now() AT TIME ZONE {TZ}) - interval '1 day') AT TIME ZONE {TZ})"


def call(model, reserved, used, ago="1 hour", at=None):
    """A model call `ago` before now, or at the SQL time `at`."""
    when = at or f"now() - interval '{ago}'"
    _sql(f"INSERT INTO ai_calls (model, reserved, used, at) VALUES (%s, %s, %s, {when})", model, reserved, used)


def text(kind, status, *, game=None, err=None, rejections=0, attempts=1, writer=WRITER, checker=CHECKER,
         body="{}", ago="1 hour", at=None, written=True):
    """An ai_texts row last updated `ago` before now (or at the SQL time `at`); a text that was written has
    written_at too, at the same time unless `written` is a different interval string."""
    when = at or f"now() - interval '{ago}'"
    wrote = "NULL" if not written else (when if written is True else f"now() - interval '{written}'")
    _sql(f"""INSERT INTO ai_texts (game_id, kind, status, last_error, rejections, attempts, writer, checker, body,
                                   updated_at, written_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, {when}, {wrote})""",
         game, kind, status, err, rejections, attempts, writer, checker, body)


def by_model(rows, key):
    out = {}
    for r in rows:
        out[r["model"]] = out.get(r["model"], 0) + r[key]
    return out


# ---------------------------------------------------------------- pure

@pytest.mark.parametrize("error,bucket", [
    ("rate limited: every model cooling down for 300s", "rate limited"),
    ("rate limited: openai/gpt-oss-120b: no room this minute", "rate limited"),
    ("check failed twice: fact check: 'x led 17-3' (the score was 14-3)",
     "check failed twice: fact check by the checker model"),
    ("check failed twice: numbers not in the facts: ['17']", "check failed twice: numbers not in the facts"),
    ("check failed twice: copied from an article: 'four touchdowns and no'", "check failed twice: copied from an article"),
    ("check failed twice: 'Jones threw for 301 yards': isn't on the player's line (his line: 289)",
     "check failed twice: code check: isn't on the player's line (his line: N)"),
    ("check failed twice: 'Bears led by 20': FACTS show the lead was 17 | at half",
     "check failed twice: code check: FACTS show the lead was N | at half"),
    ("crashed: KeyError", "crashed: KeyError"),
    ("no key", "no key"),
])
def test_classify_buckets_by_the_rule_that_fired(error, bucket):
    assert usage.classify(error) == bucket


def test_two_clauses_of_one_rule_share_a_bucket():
    a = usage.classify("check failed twice: 'Jones threw for 301 yards': isn't on the player's line (his line: 289)")
    b = usage.classify("check failed twice: 'Smith ran for 88 yards': isn't on the player's line (his line: 71)")
    assert a == b


def test_budget_units():
    from app.ai import client as ai_client
    assert usage.budget(WRITER) == ("tokens", ai_client.GROQ_TPD)
    assert usage.budget(OR_A) == ("requests", ai_client.OPENROUTER_RPD)
    assert usage.budget("gemini-3.6-flash") == ("requests", ai_client.GEMINI_RPD)


def test_rejection_rate_counts_a_refresh_that_kept_its_earlier_text():
    rows = [{"status": "ready", "texts": 3, "rejected": 1},          # 1 of 3 shown texts: its refresh was rejected
            {"status": "failed", "texts": 4, "rejected": 2},         # 2 rejected, 2 only rate limited (unknown)
            {"status": "no_sources", "texts": 5, "rejected": 0},     # no model ran: left out
            {"status": "writing", "texts": 1, "rejected": 0}]
    assert usage.rejection_rate(rows) == (3, 5)                      # 3 rejected of 3 + 2 written cleanly


def test_a_table_cell_cannot_break_the_markdown():
    assert "a\\|b c" in usage._table(["h"], [["a|b\nc"]])


# ---------------------------------------------------------------- over the database
pytestmark_db = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")


@pytestmark_db
def test_nothing_recorded_yet_is_said_plainly(client):  # noqa: F811
    out = usage.render(usage.collect(7))
    assert "No model calls recorded yet" in out and "nothing in this window" in out


@pytestmark_db
def test_tokens_calls_unreported_and_the_peak_minute(client):  # noqa: F811
    for model, reserved, used, minute in ((WRITER, 5000, 4200, 0), (WRITER, 3000, 2900, 0.33), (WRITER, 6000, None, 20),
                                          (CHECKER, 2000, 1500, 25)):
        call(model, reserved, used, at=f"{NOON} + interval '{minute} minutes'")
    d = usage.collect(7)
    assert by_model(d["calls"], "tokens") == {WRITER: 4200 + 2900 + 6000, CHECKER: 1500}
    assert by_model(d["calls"], "unreported") == {WRITER: 1, CHECKER: 0}
    assert by_model(d["calls"], "unreported_tokens") == {WRITER: 6000, CHECKER: 0}
    peaks = {k.split("|")[1]: v for k, v in d["peaks"].items()}
    assert peaks[WRITER] == 5000 + 3000 and peaks[CHECKER] == 2000    # the two reservations inside one minute


@pytestmark_db
def test_the_peak_minute_is_per_model(client):  # noqa: F811
    call(WRITER, 5000, 4500, at=f"{NOON}")
    call(CHECKER, 4000, 3500, at=f"{NOON} + interval '10 seconds'")      # the same minute, another model
    peaks = {k.split("|")[1]: v for k, v in usage.collect(7)["peaks"].items()}
    assert peaks == {WRITER: 5000, CHECKER: 4000}                         # not 9,000 each


@pytestmark_db
def test_days_are_pacific_days(client):  # noqa: F811
    call(WRITER, 100, 100, at=f"{MIDNIGHT} - interval '1 minute'")
    call(WRITER, 200, 200, at=f"{MIDNIGHT} + interval '1 minute'")
    days = sorted(r["day"] for r in usage.collect(7)["calls"])
    assert len(days) == 2 and (days[1] - days[0]).days == 1               # one UTC date, two Pacific ones


@pytestmark_db
def test_the_last_24_hours_are_measured_against_each_budget(client):  # noqa: F811
    call(WRITER, 5000, 50000, "2 hours")
    call(WRITER, 5000, 50000, "30 hours")                     # outside the rolling day
    call(OR_A, 1, 1, "1 hour")
    call(OR_B, 1, 1, "1 hour")
    call(CHECKER, 2000, None, "1 hour")                       # unreported: counted at its reservation
    out = usage.render(usage.collect(7))
    assert "| openai/gpt-oss-120b | 1 | 50,000 tokens | 200,000 | 25% |" in out
    assert "| qwen/qwen3.8-27b | 1 | 2,000 tokens | 200,000 | 1% |" in out
    assert "| or:* (all OpenRouter models) | 2 | 2 requests | 50 | 4% |" in out          # one pool, not 2% twice
    assert f"| {OR_A} | 1 | 1 requests | (one budget for every or: model, below) | - |" in out


@pytestmark_db
def test_request_counted_models_are_not_shown_as_tokens(client):  # noqa: F811
    call(OR_A, 1, 1, at=f"{NOON}")
    out = usage.render(usage.collect(7))
    assert "n/a (counted in requests)" in out


@pytestmark_db
def test_texts_rejections_and_reasons(client):  # noqa: F811
    ids = _ids(client)
    text("headlines", "ready", body='{"items": []}')
    text("headlines", "failed", err="check failed twice: fact check: 'x led 17-3'", rejections=1, attempts=2)
    text("headlines", "failed", err="check failed twice: fact check: 'y won by 21'", rejections=1)
    text("headlines", "failed", err="rate limited: every model cooling down for 300s", attempts=3)
    text("headlines", "failed", err="rate limited: every model cooling down for 90s")
    text("headlines", "failed", err="check failed twice: numbers not in the facts: ['17']", rejections=3)
    # a preview still showing its earlier text, whose refresh was rejected: status stays ready
    text("preview", "ready", game=ids["BUF"], err="check failed twice: 'Bears led by 20': FACTS show the lead was 17",
         rejections=2, written="3 days")
    text("preview", "no_sources", game=ids["SEA"], body="null")           # no model ran
    d = usage.collect(7)
    assert usage.rejection_rate(d["texts"]) == (4, 5)                     # 4 rejected of 4 + 1 written cleanly
    reasons = {r["reason"]: r["texts"] for r in d["reasons"]}
    assert reasons == {"check failed twice: fact check by the checker model": 2,
                       "check failed twice: numbers not in the facts": 1,
                       "rate limited": 2,
                       "check failed twice: code check: FACTS show the lead was N": 1}
    held = {(r["kind"], r["status"]): r["held"] for r in d["texts"]}
    assert held[("headlines", "failed")] == 1 and held[("preview", "ready")] == 0     # only 3 + rejections are held
    out = usage.render(d)
    assert "4 of 5 with a known outcome (80%)" in out
    assert "| headlines | failed | 5 | 8 | 3 | 1 |" in out                  # 5 texts, 8 claims, 3 rejected, 1 held
    assert "held by the cap (3)" in out


@pytestmark_db
def test_tokens_per_written_text_use_when_the_text_was_written(client):  # noqa: F811
    ids = _ids(client)
    call(WRITER, 9000, 9000, at=f"{NOON}")
    call(CHECKER, 1000, 1000, at=f"{NOON}")
    text("headlines", "ready", body='{"items": []}', at=f"{NOON}")
    text("headlines", "ready", body='{"items": []}', at=f"{NOON}")
    # written 5 days ago, updated today by a failed refresh: not a text written yesterday
    text("preview", "ready", game=ids["BUF"], body='{"p": 1}', ago="1 hour", written="5 days", rejections=1)
    d = usage.collect(7)
    assert sum(r["texts"] for r in d["finished"]) == 3
    out = usage.render(d)
    assert "| 4,500 |" in out and "| 500 |" in out                      # 9,000 and 1,000 tokens over 2 texts


@pytestmark_db
def test_who_wrote_and_checked_counts_shown_texts_by_when_they_were_written(client):  # noqa: F811
    ids = _ids(client)
    text("headlines", "ready", body='{"items": []}')
    text("headlines", "ready", body='{"items": []}', writer="other/w", checker="other/c")
    text("headlines", "failed", err="x")                                  # not a shown text
    text("preview", "no_sources", game=ids["SEA"], body="null")           # no model ran: not a written text
    text("preview", "ready", game=ids["BUF"], body='{"p": 1}', ago="1 hour", written="20 days")   # long ago
    who = {(r["writer"], r["checker"]): r["texts"] for r in usage.collect(7)["who"]}
    assert who == {(WRITER, CHECKER): 1, ("other/w", "other/c"): 1}


@pytestmark_db
def test_old_rows_fall_outside_the_window(client):  # noqa: F811
    ids = _ids(client)
    call(WRITER, 1000, 1000, "10 days")
    text("headlines", "ready", body='{"items": []}', ago="10 days")
    text("preview", "failed", game=ids["BUF"], err="check failed twice: numbers not in the facts", rejections=1,
         ago="10 days")
    d = usage.collect(7)
    assert d["calls"] == [] and d["texts"] == [] and d["finished"] == [] and d["who"] == [] and d["reasons"] == []
    assert usage.collect(14)["calls"][0]["tokens"] == 1000


@pytestmark_db
def test_the_command_line(client, capsys, monkeypatch):  # noqa: F811
    call(WRITER, 100, 100, "1 hour")
    monkeypatch.setattr("sys.argv", ["usage", "--days", "3"])
    usage.main()
    assert "# AI usage, last 3 days" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["usage", "--json"])
    usage.main()
    data = json.loads(capsys.readouterr().out)
    assert data["days"] == 7 and data["calls"][0]["tokens"] == 100 and data["keep"] == "14 days"


@pytestmark_db
def test_calls_are_kept_long_enough_to_report_a_week(client):  # noqa: F811
    """quota prunes ai_calls older than KEEP (it kept 2 days, so a week's numbers never existed)."""
    from app.ai import quota
    assert quota.KEEP == "14 days"
    call(WRITER, 100, 100, "20 days")                          # past retention
    call(WRITER, 100, 100, "6 days")                           # inside a week
    _sql("SELECT setval('ai_calls_id_seq', 199)")              # the next reservation is id 200: it prunes
    quota.DbQuota().reserve([WRITER], 100, 8000, wait=False)
    ages = sorted(r[0] for r in _sql("SELECT round(extract(epoch FROM now() - at) / 86400) FROM ai_calls"))
    assert ages == [0, 6]
