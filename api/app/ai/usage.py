"""What the AI text pipeline actually spent and wrote: the numbers behind the held decisions (a spend-aware batch,
storing the unchecked draft). Read-only, from the tables the app already keeps.

    docker compose exec worker python -m app.ai.usage             # the last 7 days, as markdown
    docker compose exec worker python -m app.ai.usage --days 3 --json

Sources and their limits (say so when reading a number):
- `ai_calls`: one row per model call, `reserved` tokens (prompt + reply allowance) until the call reports `used`.
  Kept `quota.KEEP` (14 days). A call that failed or never reported stays at `reserved`: counted as spent (that is
  what the daily budget does too), and shown as "unreported" so the leak is visible. OpenRouter and Gemini are
  counted in requests, not tokens.
- `ai_texts`: one row per game and kind (headlines: one per run), holding the LATEST outcome only: a rate-limit
  retry or a rewrite leaves no history, so rejection numbers are of texts as they stand, not of attempts. A
  preview whose refresh was rejected stays `ready` (its earlier good text keeps showing) and counts as rejected.
- No kind on `ai_calls`, so tokens are per model and day, not per kind of text; "tokens per finished text" divides a
  model's day by the texts a model wrote that day (`written_at`). A per-kind figure needs a `kind` column on `ai_calls`.
- "Last N days" is a rolling cutoff shown as whole Pacific days, so the oldest day is partial.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date

from .. import db
from . import client, quota, store

TZ = "America/Los_Angeles"
POOL = client.OPENROUTER_PREFIX       # every "or:" model shares one account budget


def budget(model: str) -> tuple[str, int]:
    """(unit, daily limit): the same rule client._over_budget applies."""
    if model.startswith(POOL):
        return "requests", client.OPENROUTER_RPD
    if model.startswith("gemini"):
        return "requests", client.GEMINI_RPD
    return "tokens", client.GROQ_TPD


_CHECK = "check failed twice: "
_CHECK_KINDS = (("fact check", "fact check by the checker model"), ("numbers not in the facts", "numbers not in the facts"),
                ("copied from an article", "copied from an article"), ("reply was not JSON", "reply was not JSON"))


def classify(error: str) -> str:
    """A bucket for a failure reason: by the rule that fired, not by the quoted sentence or the durations in it."""
    e = " ".join((error or "").split())
    if e.startswith("rate limited"):
        return "rate limited"
    if e.startswith(_CHECK):
        rest = e[len(_CHECK):]
        for prefix, label in _CHECK_KINDS:
            if rest.startswith(prefix):
                return _CHECK + label
        m = re.match(r"""(['"]).*?\1: (.+)$""", rest)            # the code's box-score checks: "'<clause>': <rule>"
        if m:
            return _CHECK + "code check: " + re.sub(r"\d+(?:\.\d+)?", "N", m.group(2))[:70]
        return _CHECK + re.sub(r"\d+(?:\.\d+)?", "N", rest)[:60]
    return re.sub(r"(\d+(?:\.\d+)?)s\b", "Ns", e)[:70]


def collect(days: int = 7) -> dict:
    p = {"days": days, "tz": TZ, "cap": store.REJECTION_CAP}
    with db.connect() as conn:
        covers = conn.execute("SELECT min(at) AS first, count(*) AS n FROM ai_calls").fetchone()
        calls = conn.execute("""
            SELECT (at AT TIME ZONE %(tz)s)::date AS day, model, count(*) AS calls,
                   coalesce(sum(coalesce(used, reserved)), 0)::bigint AS tokens,
                   count(*) FILTER (WHERE used IS NULL) AS unreported,
                   coalesce(sum(reserved) FILTER (WHERE used IS NULL), 0)::bigint AS unreported_tokens
            FROM ai_calls WHERE at > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2 ORDER BY 1 DESC, 2""", p).fetchall()
        peaks = {(r["day"], r["model"]): r["peak"] for r in conn.execute("""
            WITH w AS (
                SELECT model, (at AT TIME ZONE %(tz)s)::date AS day,
                       sum(reserved) OVER (PARTITION BY model ORDER BY at
                                           RANGE BETWEEN INTERVAL '60 seconds' PRECEDING AND CURRENT ROW) AS minute
                FROM ai_calls WHERE at > now() - make_interval(days => %(days)s))
            SELECT day, model, max(minute)::bigint AS peak FROM w GROUP BY 1, 2""", p).fetchall()}
        last24 = conn.execute("""
            SELECT model, count(*) AS calls, coalesce(sum(coalesce(used, reserved)), 0)::bigint AS tokens
            FROM ai_calls WHERE at > now() - interval '24 hours' GROUP BY model ORDER BY model""").fetchall()
        texts = conn.execute("""
            SELECT (updated_at AT TIME ZONE %(tz)s)::date AS day, kind, status, count(*) AS texts,
                   coalesce(sum(attempts), 0)::bigint AS attempts,
                   count(*) FILTER (WHERE rejections > 0) AS rejected,
                   count(*) FILTER (WHERE rejections >= %(cap)s) AS held
            FROM ai_texts WHERE updated_at > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2, 3""", p).fetchall()
        finished = conn.execute("""
            SELECT (coalesce(written_at, updated_at) AT TIME ZONE %(tz)s)::date AS day, count(*) AS texts
            FROM ai_texts WHERE status = 'ready' AND body IS NOT NULL
              AND coalesce(written_at, updated_at) > now() - make_interval(days => %(days)s)
            GROUP BY 1 ORDER BY 1 DESC""", p).fetchall()
        errors = [r["last_error"] for r in conn.execute("""
            SELECT last_error FROM ai_texts
            WHERE last_error IS NOT NULL AND updated_at > now() - make_interval(days => %(days)s)
            LIMIT 5000""", p).fetchall()]
        who = conn.execute("""
            SELECT kind, coalesce(writer, '-') AS writer, coalesce(checker, '-') AS checker, count(*) AS texts
            FROM ai_texts WHERE status = 'ready' AND body IS NOT NULL
              AND coalesce(written_at, updated_at) > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2, 3 ORDER BY 4 DESC, 1""", p).fetchall()
    reasons = [{"reason": k, "texts": n} for k, n in sorted(Counter(classify(e) for e in errors).items(),
                                                              key=lambda kv: (-kv[1], kv[0]))[:12]]
    return {"days": days, "calls_since": covers["first"], "calls_rows": covers["n"], "keep": quota.KEEP,
            "calls": calls, "peaks": {f"{d}|{m}": v for (d, m), v in peaks.items()}, "last24": last24,
            "texts": texts, "finished": finished, "reasons": reasons, "who": who}


def _cell(c) -> str:
    return " ".join(str(c).split()).replace("|", "\\|")


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_nothing in this window_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _pct(n: float, d: float) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


def finished_by_day(finished: list[dict]) -> dict[date, int]:
    """Texts a model wrote and the app shows, by the day they were written (written_at: updated_at also moves when
    a refresh claims or fails)."""
    return {r["day"]: r["texts"] for r in finished}


def rejection_rate(texts: list[dict]) -> tuple[int, int]:
    """(rejected, rejected + written): texts whose latest attempt was a rejection, over those with a known outcome.
    A preview whose refresh was rejected stays 'ready' with its earlier text, and counts as rejected (a successful
    write clears the count, so a ready text with rejections can only be that). A text rejected once and then
    written has its count cleared too, so this under-counts rejections. no_sources texts made no model call and are
    left out."""
    rejected = sum(r["rejected"] for r in texts if r["status"] in ("failed", "ready", "no_sources"))
    written = sum(r["texts"] - r["rejected"] for r in texts if r["status"] == "ready")
    return rejected, rejected + written


def render(d: dict) -> str:
    done = finished_by_day(d["finished"])
    out = [f"# AI usage, last {d['days']} days (days are Pacific)\n"]
    if d["calls_since"] is None:
        out.append("No model calls recorded yet.\n")
    else:
        out.append(f"Model calls on record since {d['calls_since']:%Y-%m-%d %H:%M} UTC ({d['calls_rows']} rows, kept "
                   f"{d['keep']}). Days before that are not in this report. The window is rolling, so the oldest day "
                   f"shown is partial.\n")

    out.append("## Last 24 hours against each budget\n")
    rows, pool = [], [r for r in d["last24"] if r["model"].startswith(POOL)]
    for r in d["last24"]:
        unit, limit = budget(r["model"])
        spent = r["calls"] if unit == "requests" else r["tokens"]
        if r["model"].startswith(POOL):
            rows.append([r["model"], r["calls"], f"{spent:,} requests", "(one budget for every or: model, below)", "-"])
        else:
            rows.append([r["model"], r["calls"], f"{spent:,} {unit}", f"{limit:,}", _pct(spent, limit)])
    if pool:
        used = sum(r["calls"] for r in pool)
        rows.append([f"{POOL}* (all OpenRouter models)", used, f"{used:,} requests", f"{client.OPENROUTER_RPD:,}",
                     _pct(used, client.OPENROUTER_RPD)])
    out.append(_table(["model", "calls", "spent", "daily budget", "used"], rows))

    out.append("\n## Model calls by day\n")
    rows = []
    for r in d["calls"]:
        peak = d["peaks"].get(f"{r['day']}|{r['model']}")
        unit, _limit = budget(r["model"])
        counted = f"{r['tokens']:,}" if unit == "tokens" else "n/a (counted in requests)"
        per_text = f"{r['tokens'] // done[r['day']]:,}" if unit == "tokens" and done.get(r["day"]) else "-"
        rows.append([r["day"], r["model"], r["calls"], counted,
                     f"{r['unreported']} ({r['unreported_tokens']:,} tokens)" if unit == "tokens" else "-",
                     f"{peak:,}" if peak and unit == "tokens" else "-", per_text])
    out.append(_table(["day", "model", "calls", "tokens counted", "unreported calls", "peak 60 s reserved",
                       "tokens per written text*"], rows))
    out.append(f"\n\\* the model's tokens that day over the texts written that day (by `written_at`), all kinds; Groq's "
               f"minute limit is {client.GROQ_TPM:,} tokens. An unreported call failed or never reported its usage and "
               f"stays counted at its reservation.\n")

    out.append("\n## Texts by day, kind and outcome (latest outcome of each text, by last update)\n")
    out.append(_table(["day", "kind", "status", "texts", "claims (lifetime)", "rejected at least once",
                       f"held by the cap ({store.REJECTION_CAP})"],
                      [[r["day"], r["kind"], r["status"], r["texts"], r["attempts"], r["rejected"], r["held"]]
                       for r in d["texts"]]))

    rejected, known = rejection_rate(d["texts"])
    out.append(f"\n## Rejections\n\nTexts whose latest attempt was a rejection: {rejected} of {known} with a known "
               f"outcome ({_pct(rejected, known)}). Texts whose latest attempt ended in each way:\n")
    out.append(_table(["reason", "texts"], [[r["reason"], r["texts"]] for r in d["reasons"]]))

    out.append("\n## Who wrote and checked what (shown texts)\n")
    out.append(_table(["kind", "writer", "checker", "texts"], [[r["kind"], r["writer"], r["checker"], r["texts"]]
                                                               for r in d["who"]]))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=7, help="how many days back (default 7; calls are kept 14)")
    ap.add_argument("--json", action="store_true", help="the raw numbers instead of the report")
    a = ap.parse_args()
    data = collect(max(a.days, 1))
    print(json.dumps(data, indent=1, default=str) if a.json else render(data))


if __name__ == "__main__":
    main()
