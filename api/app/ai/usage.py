"""What the AI text pipeline actually spent and wrote: the numbers behind the held decisions (a spend-aware batch,
storing the unchecked draft). Read-only, from the tables the app already keeps.

    docker compose exec worker python -m app.ai.usage             # the last 7 days, as markdown
    docker compose exec worker python -m app.ai.usage --days 3 --json

Sources and their limits (say so when reading a number):
- `ai_calls`: one row per model call, `reserved` tokens (prompt + reply allowance) until the call reports `used`.
  Kept `quota.KEEP` (14 days). A call that failed or never reported stays at `reserved`: counted as spent (that is
  what the daily budget does too), and shown as "unreported" so the leak is visible.
- `ai_texts`: one row per game and kind (headlines: one per run), holding the LATEST outcome only: a rate-limit
  retry or a rewrite leaves no history, so rejection numbers are of texts as they stand, not of attempts.
- No kind on `ai_calls`, so tokens are per model and day, not per kind of text; "tokens per ready text" divides a
  model's day by all the texts that finished that day. A per-kind figure needs a `kind` column on `ai_calls`.
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from .. import db
from . import client, quota

TZ = "America/Los_Angeles"


def budget(model: str) -> tuple[str, int]:
    """(unit, daily limit): the same rule client._over_budget applies."""
    if model.startswith(client.OPENROUTER_PREFIX):
        return "requests", client.OPENROUTER_RPD
    if model.startswith("gemini"):
        return "requests", client.GEMINI_RPD
    return "tokens", client.GROQ_TPD


def collect(days: int = 7) -> dict:
    p = {"days": days, "tz": TZ}
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
                   count(*) FILTER (WHERE rejections > 0) AS rejected
            FROM ai_texts WHERE updated_at > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2, 3""", p).fetchall()
        reasons = conn.execute("""
            SELECT regexp_replace(substring(last_error from '^[^:]*(?::[^:]*)?'), '[0-9]+', 'N', 'g') AS reason,
                   count(*) AS texts
            FROM ai_texts WHERE last_error IS NOT NULL AND updated_at > now() - make_interval(days => %(days)s)
            GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 12""", p).fetchall()
        who = conn.execute("""
            SELECT kind, coalesce(writer, '-') AS writer, coalesce(checker, '-') AS checker, count(*) AS texts
            FROM ai_texts WHERE status IN ('ready', 'no_sources') AND body IS NOT NULL
              AND updated_at > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2, 3 ORDER BY 4 DESC, 1""", p).fetchall()
    return {"days": days, "calls_since": covers["first"], "calls_rows": covers["n"], "keep": quota.KEEP,
            "calls": calls, "peaks": {f"{d}|{m}": v for (d, m), v in peaks.items()}, "last24": last24,
            "texts": texts, "reasons": reasons, "who": who}


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_nothing in this window_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _pct(n: float, d: float) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


def finished_by_day(texts: list[dict]) -> dict[date, int]:
    """Texts that reached an outcome the app shows (ready, or checked and sourceless), by day."""
    out: dict[date, int] = {}
    for r in texts:
        if r["status"] in ("ready", "no_sources"):
            out[r["day"]] = out.get(r["day"], 0) + r["texts"]
    return out


def rejection_rate(texts: list[dict]) -> tuple[int, int]:
    """(rejected, rejected + ready): texts whose latest outcome is a rejection, over texts with a known outcome.
    A text that was rejected once and then written has its count cleared, so this under-counts rejections."""
    rejected = sum(r["rejected"] for r in texts if r["status"] == "failed")
    ready = sum(r["texts"] for r in texts if r["status"] in ("ready", "no_sources"))
    return rejected, rejected + ready


def render(d: dict) -> str:
    done = finished_by_day(d["texts"])
    out = [f"# AI usage, last {d['days']} days (days are Pacific)\n"]
    if d["calls_since"] is None:
        out.append("No model calls recorded yet.\n")
    else:
        out.append(f"Model calls on record since {d['calls_since']:%Y-%m-%d %H:%M} UTC ({d['calls_rows']} rows, kept "
                   f"{d['keep']}). Days before that are not in this report.\n")

    out.append("## Last 24 hours against each budget\n")
    rows, or_total = [], 0
    for r in d["last24"]:
        unit, limit = budget(r["model"])
        spent = r["calls"] if unit == "requests" else r["tokens"]
        if r["model"].startswith(client.OPENROUTER_PREFIX):
            or_total += spent
        rows.append([r["model"], r["calls"], f"{spent:,} {unit}", f"{limit:,}", _pct(spent, limit)
                     + (" (shared by every or: model)" if r["model"].startswith(client.OPENROUTER_PREFIX) else "")])
    out.append(_table(["model", "calls", "spent", "daily budget", "used"], rows))

    out.append("\n## Model calls by day\n")
    rows = []
    for r in d["calls"]:
        peak = d["peaks"].get(f"{r['day']}|{r['model']}")
        unit, limit = budget(r["model"])
        per_text = (f"{r['tokens'] // done[r['day']]:,}" if unit == "tokens" and done.get(r["day"]) else "-")
        rows.append([r["day"], r["model"], r["calls"], f"{r['tokens']:,}",
                     f"{r['unreported']} ({r['unreported_tokens']:,} tokens)", f"{peak:,}" if peak else "-", per_text])
    out.append(_table(["day", "model", "calls", "tokens counted", "unreported calls", "peak 60 s reserved",
                       "tokens per finished text*"], rows))
    out.append(f"\n\\* the model's tokens that day over every text that finished that day, all kinds; Groq's minute "
               f"limit is {client.GROQ_TPM:,} tokens. An unreported call failed or never reported its usage and "
               f"stays counted at its reservation.\n")

    out.append("\n## Texts by day, kind and outcome (latest outcome of each text)\n")
    out.append(_table(["day", "kind", "status", "texts", "claims", "held for rejections"],
                      [[r["day"], r["kind"], r["status"], r["texts"], r["attempts"], r["rejected"]]
                       for r in d["texts"]]))

    rejected, known = rejection_rate(d["texts"])
    out.append(f"\n## Rejections\n\nTexts whose latest outcome is a rejection: {rejected} of {known} with a known "
               f"outcome ({_pct(rejected, known)}). Count of texts whose latest attempt ended in each way:\n")
    out.append(_table(["reason (digits masked)", "texts"], [[r["reason"], r["texts"]] for r in d["reasons"]]))

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
