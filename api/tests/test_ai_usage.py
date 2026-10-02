"""The usage report (python -m app.ai.usage) over rows the app really keeps, and the retention it relies on."""
import pytest

from app.ai import usage
from test_api import TEST_DB, _sql, client  # noqa: F401 — the client fixture

pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")

WRITER, CHECKER = "openai/gpt-oss-120b", "qwen/qwen3.8-27b"


def call(model, reserved, used, ago="1 hour"):
    _sql("INSERT INTO ai_calls (model, reserved, used, at) VALUES (%s, %s, %s, now() - %s::interval)",
         model, reserved, used, ago)


def text(kind, status, *, game=None, err=None, rejections=0, attempts=1, writer=WRITER, checker=CHECKER,
         body="{}", ago="1 hour"):
    _sql("""INSERT INTO ai_texts (game_id, kind, status, last_error, rejections, attempts, writer, checker, body,
                                  updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now() - %s::interval)""",
         game, kind, status, err, rejections, attempts, writer, checker, body, ago)


def test_nothing_recorded_yet_is_said_plainly(client):  # noqa: F811
    out = usage.render(usage.collect(7))
    assert "No model calls recorded yet" in out and "nothing in this window" in out


def test_tokens_calls_unreported_and_the_peak_minute(client):  # noqa: F811
    call(WRITER, 5000, 4200, "30 minutes")
    call(WRITER, 3000, 2900, "29 minutes 40 seconds")        # 20 s after the first: one minute holding 8,000
    call(WRITER, 6000, None, "10 minutes")                   # failed: stays at its reservation
    call(CHECKER, 2000, 1500, "5 minutes")
    d = usage.collect(7)
    rows = {r["model"]: r for r in d["calls"]}
    assert rows[WRITER]["calls"] == 3 and rows[WRITER]["tokens"] == 4200 + 2900 + 6000
    assert rows[WRITER]["unreported"] == 1 and rows[WRITER]["unreported_tokens"] == 6000
    assert rows[CHECKER]["tokens"] == 1500
    peaks = {k.split("|")[1]: v for k, v in d["peaks"].items()}
    assert peaks[WRITER] == 5000 + 3000 and peaks[CHECKER] == 2000      # the two reservations inside one minute


def test_the_last_24_hours_are_measured_against_each_budget(client):  # noqa: F811
    call(WRITER, 5000, 50000, "2 hours")
    call(WRITER, 5000, 50000, "30 hours")                    # outside the rolling day
    call("or:qwen/qwen3.8-27b:free", 1, 1, "1 hour")
    call("or:other/model:free", 1, 1, "1 hour")
    out = usage.render(usage.collect(7))
    assert "| openai/gpt-oss-120b | 1 | 50,000 tokens | 200,000 | 25% |" in out
    assert "2 requests" not in out and "1 requests | 50 | 2%" in out        # per model, shared budget noted
    assert "shared by every or: model" in out


def test_texts_rejections_and_reasons(client):  # noqa: F811
    text("headlines", "ready", body='{"items": []}')
    text("headlines", "failed", err="check failed twice: fact check: 'x led 17-3'", rejections=1, attempts=2)
    text("headlines", "failed", err="check failed twice: fact check: 'y won by 21'", rejections=1)
    text("headlines", "failed", err="rate limited: every model cooling down for 300s", attempts=3)
    text("headlines", "failed", err="rate limited: every model cooling down for 90s")
    text("headlines", "failed", err="check failed twice: numbers not in the facts: ['17']", rejections=2)
    d = usage.collect(7)
    assert usage.rejection_rate(d["texts"]) == (3, 4)                         # 3 rejected of 3 + 1 ready
    reasons = {r["reason"]: r["texts"] for r in d["reasons"]}
    assert reasons["check failed twice: fact check"] == 2
    assert reasons["check failed twice: numbers not in the facts"] == 1
    assert reasons["rate limited: every model cooling down for Ns"] == 2         # digits masked, so they merge
    out = usage.render(d)
    assert "3 of 4 with a known outcome (75%)" in out and "headlines" in out


def test_tokens_per_finished_text_divide_by_the_days_finished_texts(client):  # noqa: F811
    call(WRITER, 9000, 9000, "1 hour")
    call(CHECKER, 1000, 1000, "1 hour")
    text("headlines", "ready", body='{"items": []}')
    text("headlines", "ready", body='{"items": []}')
    out = usage.render(usage.collect(7))
    assert "| 4,500 |" in out                                     # 9,000 tokens over 2 finished texts
    assert "| 500 |" in out


def test_who_wrote_and_checked(client):  # noqa: F811
    text("headlines", "ready", body='{"items": []}')
    text("headlines", "ready", body='{"items": []}', writer="other/w", checker="other/c")
    text("headlines", "failed", err="x")                       # not a shown text
    who = {(r["writer"], r["checker"]): r["texts"] for r in usage.collect(7)["who"]}
    assert who == {(WRITER, CHECKER): 1, ("other/w", "other/c"): 1}


def test_old_rows_fall_outside_the_window(client):  # noqa: F811
    call(WRITER, 1000, 1000, "10 days")
    text("headlines", "ready", body='{"items": []}', ago="10 days")
    d = usage.collect(7)
    assert d["calls"] == [] and d["texts"] == []
    assert usage.collect(14)["calls"][0]["tokens"] == 1000


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
