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
- `ai_calls.kind` (migration 010) says what a call was for, '<text kind>:<step>' (`client.call_kind`: write, rewrite,
  extract, re-extract, check, search); calls made before it have none ("not recorded"). "Tokens per written text" by
  day divides a model's day by the texts written that day (all kinds; one `ai_texts` row per game and kind, so a
  one-liner rewritten every 15 minutes or a refreshed preview counts once and its figure runs high); by kind, a
  kind's Groq tokens (every step and model) over its first drafts (`<kind>:write` calls that reported usage).
- `ai_calls.cached_tokens`, `remaining_tokens`, `reset_tokens_secs` (migration 010): the prompt tokens Groq served
  from its cache, and its per-minute rate-limit headers after the call, as Groq reported them. LOG ONLY: every
  budget, and every token figure here, still counts cached tokens (M1 waits for the log-only week, T1).
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
                ("copied from an article", "copied from an article"), ("reply was not JSON", "reply was not JSON"),
                ("reused the examples", "reused the examples"))
REUSED = _CHECK + "reused the examples"      # a recap that copied Adam's examples (M2 moved them next to FACTS)
# Recap rewrites per first draft before M2 (prep plan §4, measured): M2 put the examples after the rules, untested,
# so the first week compares against this. Higher, with "reused the examples" among the reasons: put them back.
RECAP_REWRITES_BEFORE = 0.586


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
                   coalesce(sum(reserved) FILTER (WHERE used IS NULL), 0)::bigint AS unreported_tokens,
                   coalesce(sum(cached_tokens), 0)::bigint AS cached
            FROM ai_calls WHERE at > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2 ORDER BY 1 DESC, 2""", p).fetchall()
        kinds = conn.execute("""
            SELECT kind, model, count(*) AS calls, coalesce(sum(coalesce(used, reserved)), 0)::bigint AS tokens,
                   count(*) FILTER (WHERE used IS NULL) AS unreported,
                   count(*) FILTER (WHERE used IS NOT NULL) AS reported,
                   coalesce(sum(cached_tokens), 0)::bigint AS cached,
                   count(*) FILTER (WHERE cached_tokens > 0) AS cache_hits
            FROM ai_calls WHERE at > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2 ORDER BY 1 NULLS LAST, 2""", p).fetchall()
        # Each call next to the one before it on the same model (T1): the rate-limit headers' fall between them.
        steps = conn.execute("""
            SELECT model, kind, reserved, used, cached_tokens AS cached, remaining_tokens AS remaining,
                   lag(remaining_tokens) OVER w AS prev_remaining, lag(reset_tokens_secs) OVER w AS prev_reset,
                   extract(epoch FROM at - lag(at) OVER w)::float AS gap
            FROM ai_calls WHERE at > now() - make_interval(days => %(days)s)
            WINDOW w AS (PARTITION BY model ORDER BY at, id) ORDER BY at, id""", p).fetchall()
        peaks = {(r["day"], r["model"]): r["peak"] for r in conn.execute("""
            WITH w AS (
                SELECT model, (at AT TIME ZONE %(tz)s)::date AS day,
                       sum(reserved) OVER (PARTITION BY model ORDER BY at
                                           RANGE BETWEEN INTERVAL '60 seconds' PRECEDING AND CURRENT ROW) AS minute
                FROM ai_calls WHERE at > now() - make_interval(days => %(days)s))
            SELECT day, model, max(minute)::bigint AS peak FROM w GROUP BY 1, 2""", p).fetchall()}
        last24 = conn.execute("""
            SELECT model, count(*) AS calls, coalesce(sum(coalesce(used, reserved)), 0)::bigint AS tokens,
                   coalesce(sum(cached_tokens), 0)::bigint AS cached
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
        finished_kinds = {r["kind"]: r["texts"] for r in conn.execute("""
            SELECT kind, count(*) AS texts FROM ai_texts WHERE status = 'ready' AND body IS NOT NULL
              AND coalesce(written_at, updated_at) > now() - make_interval(days => %(days)s)
            GROUP BY 1""", p).fetchall()}
        errors = [r["last_error"] for r in conn.execute("""
            SELECT last_error FROM ai_texts
            WHERE last_error IS NOT NULL AND updated_at > now() - make_interval(days => %(days)s)
            LIMIT 5000""", p).fetchall()]
        who = conn.execute("""
            SELECT kind, coalesce(writer, '-') AS writer, coalesce(checker, '-') AS checker, count(*) AS texts
            FROM ai_texts WHERE status = 'ready' AND body IS NOT NULL
              AND coalesce(written_at, updated_at) > now() - make_interval(days => %(days)s)
            GROUP BY 1, 2, 3 ORDER BY 4 DESC, 1""", p).fetchall()
    buckets = Counter(classify(e) for e in errors)
    reasons = [{"reason": k, "texts": n} for k, n in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))[:12]]
    return {"days": days, "calls_since": covers["first"], "calls_rows": covers["n"], "keep": quota.KEEP,
            "calls": calls, "peaks": {f"{d}|{m}": v for (d, m), v in peaks.items()}, "last24": last24,
            "kinds": kinds, "finished_kinds": finished_kinds, "t1": t1_pairs(steps), "reused": buckets[REUSED],
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


NO_KIND = "(not recorded)"      # a call made before migration 010


def text_kind(kind: str | None) -> str | None:
    """The kind of text a call was for ('recap' for 'recap:rewrite'); None outside a text (check_eval's 'check') or
    for a call made before kinds were logged."""
    return kind.split(":", 1)[0] if kind and ":" in kind else None


def by_text_kind(kinds: list[dict], written: dict[str, int]) -> list[dict]:
    """Per kind of text: token-counted (Groq) calls, their tokens and cached tokens over every step and model, the
    request-counted pools' requests, its first drafts (`<kind>:write` calls that reported usage: one per attempt,
    so a one-liner rewritten every 15 minutes counts each time) and the texts of that kind shown (`ai_texts`, one
    per game; None for the row of calls outside a text)."""
    out: dict = {}
    for k in [text_kind(r["kind"]) for r in kinds] + list(written):
        out.setdefault(k, {"kind": k, "calls": 0, "tokens": 0, "cached": 0, "requests": 0, "drafts": 0,
                           "written": written.get(k, 0) if k else None})
    for r in kinds:
        row = out[text_kind(r["kind"])]
        if budget(r["model"])[0] == "tokens":
            row["calls"] += r["calls"]
            row["tokens"] += r["tokens"]
            row["cached"] += r["cached"]
        else:
            row["requests"] += r["calls"]
        if row["kind"] and r["kind"] == f"{row['kind']}:write":
            row["drafts"] += r["reported"]
    return sorted(out.values(), key=lambda r: (r["kind"] is None, r["kind"] or ""))


def recap_rewrites(kinds: list[dict]) -> tuple[int, int]:
    """(recap rewrites, recap first drafts), calls that reported usage: M2's first-week check against
    RECAP_REWRITES_BEFORE."""
    n = lambda k: sum(r["reported"] for r in kinds if r["kind"] == k)
    return n("recap:rewrite"), n("recap:write")


T1_GAP = 60          # seconds: the rate-limit headers are per minute


def t1_pairs(steps: list[dict], limit: int | None = None) -> list[dict]:
    """T1's second condition (prep plan §6): does x-ratelimit-remaining-tokens fall by a call's uncached tokens only,
    or by all of them? Read from two back-to-back calls of one Groq model: both reported the header, the later one
    had cached tokens (else the two answers are the same number), and they are under a minute apart. The minute
    refills meanwhile, so the earlier reading is topped up first: to the limit once its reset time has passed, else
    by the gap's share of the minute (limit / 60 a second, GROQ_TPM). A call this table can't see (another process
    on the same Groq organization) also takes from the minute: a fall bigger than the whole call is marked."""
    limit = limit or client.GROQ_TPM
    out = []
    for s in steps:
        if (budget(s["model"])[0] != "tokens" or None in (s["remaining"], s["prev_remaining"], s["used"], s["gap"])
                or not s["cached"] or s["gap"] >= T1_GAP):
            continue
        whole = s["prev_reset"] is not None and s["gap"] >= s["prev_reset"]
        start = limit if whole else min(limit, s["prev_remaining"] + s["gap"] * limit / 60)
        fell = round(start - s["remaining"])
        uncached, total = s["used"] - s["cached"], s["used"]
        nearer = ("more than the call" if fell > total + 0.1 * total else
                  "uncached" if abs(fell - uncached) < abs(fell - total) else "all")
        out.append({"model": s["model"], "kind": s["kind"], "gap": round(s["gap"], 1), "fell": fell,
                    "uncached": uncached, "total": total, "reserved": s["reserved"], "nearer": nearer})
    return out


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
            rows.append([r["model"], r["calls"], f"{spent:,} requests", "(one budget for every or: model, below)", "-",
                         "-"])
        else:
            rows.append([r["model"], r["calls"], f"{spent:,} {unit}", f"{limit:,}", _pct(spent, limit),
                         f"{r['cached']:,}" if unit == "tokens" else "-"])
    if pool:
        used = sum(r["calls"] for r in pool)
        rows.append([f"{POOL}* (all OpenRouter models)", used, f"{used:,} requests", f"{client.OPENROUTER_RPD:,}",
                     _pct(used, client.OPENROUTER_RPD), "-"])
    out.append(_table(["model", "calls", "spent", "daily budget", "used", "of which cached (not credited)"], rows))

    out.append("\n## Model calls by day\n")
    rows = []
    for r in d["calls"]:
        peak = d["peaks"].get(f"{r['day']}|{r['model']}")
        unit, _limit = budget(r["model"])
        counted = f"{r['tokens']:,}" if unit == "tokens" else "n/a (counted in requests)"
        per_text = f"{r['tokens'] // done[r['day']]:,}" if unit == "tokens" and done.get(r["day"]) else "-"
        rows.append([r["day"], r["model"], r["calls"], counted,
                     f"{r['unreported']} ({r['unreported_tokens']:,} tokens)" if unit == "tokens" else "-",
                     f"{peak:,}" if peak and unit == "tokens" else "-", per_text,
                     f"{r['cached']:,}" if unit == "tokens" else "-"])
    out.append(_table(["day", "model", "calls", "tokens counted", "unreported calls", "peak 60 s reserved",
                       "tokens per written text*", "cached tokens"], rows))
    out.append(f"\n\\* the model's tokens that day over the texts written that day (by `written_at`), all kinds. A text "
               f"is one per game and kind, so a one-liner rewritten every 15 minutes or a refreshed preview counts once "
               f"and this runs high; the by-kind table below divides by first drafts. Groq's minute limit is "
               f"{client.GROQ_TPM:,} tokens. An unreported call failed or never reported its usage and stays counted at "
               f"its reservation.\n")

    out.append("\n## Model calls by kind\n")
    rows = []
    for r in d["kinds"]:
        tokens = budget(r["model"])[0] == "tokens"
        rows.append([r["kind"] or NO_KIND, r["model"], r["calls"],
                     f"{r['tokens']:,}" if tokens else "n/a (counted in requests)",
                     r["unreported"] if tokens else "-", f"{r['cached']:,}" if tokens else "-",
                     f"{r['cache_hits']} ({_pct(r['cache_hits'], r['calls'])})" if tokens else "-"])
    out.append(_table(["kind", "model", "calls", "tokens counted", "unreported calls", "cached tokens",
                       "calls with a cache hit"], rows))
    out.append("\nA kind is `<text>:<step>`. write / extract: a first draft; rewrite / re-extract: the same prompt again "
               "with why the draft was rejected; check: the fact check; search: a preview's article search. Cached "
               "tokens are the prompt tokens Groq says it served from its cache. They are logged only: every token "
               "figure in this report and every budget still counts them, until the log-only week (T1) shows whether "
               "Groq counts them.\n")

    out.append("\n## Tokens per first draft, by kind\n")
    rows = []
    for r in by_text_kind(d["kinds"], d["finished_kinds"]):
        rows.append([r["kind"] or "(no text, or not recorded)", r["calls"], f"{r['tokens']:,}", f"{r['cached']:,}",
                     r["requests"], "-" if r["written"] is None else r["written"],
                     "-" if r["kind"] is None else r["drafts"],
                     f"{r['tokens'] // r['drafts']:,}" if r["drafts"] and r["calls"] else "-"])
    out.append(_table(["kind of text", "token-counted calls", "tokens counted", "cached tokens",
                       "OpenRouter / Gemini requests", "texts shown", "first drafts", "tokens per first draft"], rows))
    out.append("\nEvery step and model of a kind (write, rewrites, extract, check, search) over its first drafts: the "
               "`<kind>:write` calls that reported usage, one per attempt (a one-liner rewritten every 15 minutes or a "
               "refreshed preview counts each time; texts shown is one per game). Failed and rejected attempts count "
               "too: this is what a draft costs, all its steps included.\n")

    rewrites, drafts = recap_rewrites(d["kinds"])
    reused = d["reused"]
    out.append("\n## M2: recap rewrites per first draft\n")
    out.append(f"{rewrites} rewrites over {drafts} first drafts: "
               f"{f'{rewrites / drafts:.3f}' if drafts else '-'} (before M2: {RECAP_REWRITES_BEFORE}). Texts whose "
               f"latest attempt failed for reusing the examples: {reused}. M2 moved the examples next to FACTS without "
               "an eval: if the rate rises, with that reason among the rejections, move them back above the rules "
               "(`prompts.WRITE_RECAP`).\n")

    redo = [r for r in d["kinds"] if budget(r["model"])[0] == "tokens" and r["kind"]
            and r["kind"].endswith((":rewrite", ":re-extract"))]
    redone = sum(r["reported"] for r in redo)
    hits = sum(r["cache_hits"] for r in redo)
    pairs = d["t1"]
    count = Counter(p["nearer"] for p in pairs)
    out.append("\n## T1: does Groq count cached tokens? (M1, log only)\n")
    out.append(f"Rewrites and re-extracts with cached tokens: {hits} of {redone} ({_pct(hits, redone)}; the first "
               f"condition needs 50%). Back-to-back calls of one model, under a minute apart, the later one with cached "
               f"tokens: {len(pairs)}. The minute's remaining tokens fell by about the uncached tokens in "
               f"{count['uncached']}, by all of them in {count['all']}, by more than the whole call in "
               f"{count['more than the call']}.\n")
    out.append(_table(["model", "kind", "seconds after the last call", "remaining fell by", "uncached tokens",
                       "all tokens", "reserved"],
                      [[p["model"], p["kind"] or NO_KIND, p["gap"], f"{p['fell']:,}", f"{p['uncached']:,}",
                        f"{p['total']:,}", f"{p['reserved']:,}"] for p in pairs[-20:]]))
    out.append(f"\nThe second condition: the fall matches the uncached tokens. The header is per minute and refills "
               f"meanwhile ({client.GROQ_TPM:,} a minute, added back over the gap), so read it as a guide. A fall "
               f"bigger than the whole call is another call on that model this table can't see (another process on the "
               f"same Groq organization), or Groq still holding the reply allowance (compare reserved). Last 20 "
               f"shown.\n")

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
