# Phase 4 Stage 2 — AI text on Temporal (proposal)

**Status: built Sep 29, 2026 on branch `phase-4-ai-text` (Adam: "move forward with what we can"); not deployed.
Later changes: pre-write list narrowed to favorites + NCAAF ranked vs ranked, one queue per model (Adam, Sep 29).**
It replaces the "Stage 2" worker steps of the
Phase 4 handoff (2.3–2.5) with the plan agreed on Sep 29: several free Groq models with failover, previews written
midweek, recaps for every final, and durable retries. Evidence and numbers: `phase4-results.md`,
`phase4-status.md`. Code it builds on: `api/app/ai/` on branch `phase-4-prototype`.

Build gate lifted by Adam on Sep 29. Deploy gates: Adam approves the samples, Phase 5b signed off (from the
original handoff; no code depends on 5b, only on 5a), a Tue/Wed slot.

## Decisions this needs from Adam

| # | Decision | Proposed |
|---|---|---|
| D1 | Previews written midweek, not game morning (PRD says "3 hours before start") | NCAAF Wed and Thu evening, NFL Thu and Fri evening; game-morning refresh only when something changed |
| D2 | Every game gets AI text written ahead, not just option C's list | Yes: the midweek budget covers the whole weekend slate |
| D3 | A second and third model provider (Groq; Gemini as a side pool) | Yes; free tiers only, billing stays off |
| D4 | Recaps for every final, written by the worker at the final | Yes |
| D5 | Text the fact-checker can't clear shows template text | Yes (never unchecked text) |

## Who does what

| Job | Where |
|---|---|
| Which model writes or checks this text right now; failover on a rate limit | `app/ai/client.py` (built) |
| Facts, prompts, checks, fact check | `app/ai/facts.py`, `prompts.py`, `writer.py` (built) |
| **When** each text is written; waiting and retrying for hours; batches; surviving restarts | **Temporal (this spec)** |
| A text nobody wrote ahead, when a page is opened | API, as the handoff says (fallback path) |

The app still never depends on Temporal: with the worker down, pages show stored text or write on open.

## Workflows

### 1. `WriteTextWorkflow` (new) — one text, done durably

- **Input:** `kind` (preview, recap, headlines), `league`, `espn_id` (none for headlines), `reason`
  (midweek, refresh, final, nightly, manual).
- **Workflow ID:** `ai-<kind>-<league>-<espn_id>` (headlines: `ai-headlines-<date>-<am|pm>`). ID reuse policy
  "allow duplicate", conflict policy "use existing": starting it twice while one runs does nothing, so every
  caller can start it freely.
- **Body:** one activity, `write_text(kind, league, espn_id, reason)`, which claims the `ai_texts` row
  (handoff 2.2 claim, unchanged), writes through `app/ai/writer.py`, and stores the result.
- **Retries:** the activity raises `ApplicationError(next_retry_delay=...)` on `RateLimited`, using the wait
  Groq gives ("try again in 6m49s"), else 30 min. Policy: up to 8 attempts over at most 12 hours; a check
  failure (the text itself) is not retried by Temporal (the writer already rewrote it once) and is stored as
  `failed`. After the last attempt the row stays `failed`; the page shows template text and can still write on
  open.
- **Why a child workflow and not an activity inside GameWorkflow:** a recap waiting 4 hours for quota must not
  hold up the game's regrade or its close, and its retries show up on their own in the Temporal UI.

### 2. `PreviewBatchWorkflow` (new) — the midweek writing

- **Schedules:** `previews-ncaaf` Wed and Thu 7:00 PM PT; `previews-nfl` Thu and Fri 7:00 PM PT (D1).
- **Body:** activity `upcoming_without_preview(league, through)` lists games in the next weekend window whose
  preview is missing, failed, or stale; then it starts one `WriteTextWorkflow` per game, **at most 3 running at
  once** (one per Groq model), waiting for each to finish before starting the next. That is the "all models at
  once" idea: three per-minute budgets in parallel, about 24K tokens a minute instead of 8K.
- Runs twice per league so the second evening picks up anything the first missed (rate limits, late-added
  games).

### 3. `GameWorkflow` (existing) — two small, versioned changes

- **Game-morning refresh: activity change only.** `generate_preview` already runs at 8 AM game day (Phase 3
  placeholder). Its body becomes: fetch the summary and news (no model calls), compute the preview's
  **fingerprint** (injury list, kept article URLs, line) and compare it with the stored one. Unchanged: done.
  Changed injuries or line only: start `WriteTextWorkflow(preview, reason=refresh)` in rewrite-only mode (the
  saved article extract, new game facts: one write call instead of two). New articles: a full rewrite. No
  workflow code changes, so no versioning.
- **Recap at the final: workflow change, behind `workflow.patched("ai-recap")`.** In `_final`, after
  `_grade()`: start `WriteTextWorkflow(recap, reason=final)` as a child with `parent_close_policy=ABANDON` and
  don't wait for it. After the regrade, if the score changed, start it again with the new basis (a stat
  correction rewrites the recap once).

### 4. `HeadlinesWorkflow` (existing) — versioned change

- After the news loop, behind `workflow.patched("ai-headlines")`: start `WriteTextWorkflow(headlines)`.
  Headlines try Gemini first (4 requests a day of its 20), then the Groq chain (D3).

### 5. `LeftoverWorkflow` (new) — spend what's left each night

- **Schedule:** daily 9:30 PM PT.
- **Body:** finds texts still missing or `failed` for the next 3 days (previews) and the last 2 days (recaps),
  and starts `WriteTextWorkflow` for them, most urgent first, with the same limit of 3 at once. Stops at the
  first `RateLimited` from every model (the day's budget is gone).
- Groq's daily window is a rolling 24 hours, so this mostly catches up on failures; it doesn't "use up" a
  budget that resets.

## Sharing quota between the API and the worker

Today each process paces its own calls, so the API (writing on open) and the worker can't see each other's usage
and can collide on the same Groq model. Proposal:

- **`ai_quota` table:** one row per model with a rolling per-minute window (reserved tokens with timestamps as
  JSONB) and `cooling_until`. `client._reserve` and the 429 cooldown move from memory to this table, updated in
  one `UPDATE ... RETURNING` so two processes never both take the last slot.
- **The API never waits for room.** On open, if no model has room or all are cooling, it returns
  `writing`/fallback at once (the page's 20 s budget) instead of sleeping up to a minute. Only the worker waits.
- **An `ai` task queue** on the worker with `max_concurrent_activities=3`, so AI work never delays ESPN work
  (line saves, status polls, grading) on the `scores` queue.

## Storage (migration 007, extends handoff 2.1)

`ai_texts` as in the handoff, plus:

| Column | Why |
|---|---|
| `fingerprint TEXT` | preview: hash of injuries + article URLs + line, for the game-morning refresh |
| `extract JSONB` | preview: the article extract, so a refresh can rewrite without re-reading articles |
| `writer TEXT`, `checker TEXT` | which models wrote and checked it (voice tracking; failover audits) |
| `attempts INT`, `last_error TEXT` | what the retries saw |

Plus `ai_quota` (above).

## Budget with this plan (college Saturday + NFL Sunday week)

| When | Work | Groq tokens (approx.) |
|---|---|---|
| Wed–Fri evenings | ~60 NCAAF + 16 NFL previews at ~9K (3 articles, 400 words each) + checks | ~750K over 3 days, spread across 3 models' 600K a day |
| Saturday | refreshes (~10 rewrites × 3K), ~60 recaps × ~4K with checks, one-liners on open, headlines on Gemini | ~300K across 3 models |
| Sunday | NFL refreshes and recaps | ~100K |

Tight midweek; that is why previews go to Wednesday for NCAAF. If the bake-off shows a backup model writes
poorly, midweek volume drops to option C's list plus opened games, as in the original handoff.

## Failure handling

| Failure | What happens |
|---|---|
| One model rate-limited | client fails over to the next (built) |
| Every model limited | activity retries at the time Groq gives; batch pauses its starts |
| Fact check fails twice | row `failed`, template text; the nightly job tries again once |
| Worker down | nothing written ahead; pages write on open; schedules catch up within 12 h (existing policy) |
| A model retired by Groq | client skips a model that returns 404 (add to `_failover`); remove it from `AI_WRITERS` |
| Groq down entirely | template text everywhere; screens unchanged otherwise |

## Tests

- `test_workflows.py` (time-skipping): batch starts at most 3 children; a `RateLimited` retry waits the given
  delay; refresh with an unchanged fingerprint writes nothing; recap child starts at the final and again after a
  changed regrade; duplicate starts of the same text do nothing.
- `test_replay.py`: old histories replay with the `ai-recap` and `ai-headlines` patches; save new histories that
  include them.
- `test_activities.py` (Postgres): claim, fingerprint compare, `ai_quota` reservation from two connections at
  once.
- Existing AI tests (84, no network) stay.

## Build order (after the gates)

1. Migration 007 (`ai_texts`, `ai_quota`) and the claim; move pacing to `ai_quota`.
2. `write_text` activity and `WriteTextWorkflow` on the `ai` queue.
3. Recap at the final (`ai-recap` patch) — smallest user-visible win; replay test.
4. `PreviewBatchWorkflow` + schedules; game-morning refresh in `generate_preview`.
5. Headlines (`ai-headlines` patch), `LeftoverWorkflow`.
6. API endpoint and app placeholders (handoff 2.6–2.7), with the no-wait rule.

Deploy on a Tuesday or Wednesday, as the handoff says; the off switch (remove the keys) still turns everything
back into template text.

## Open questions

- Does a backup model write well enough to carry midweek volume? (Bake-off, Sep 29.)
- Qwen's daily token limit (not yet measured).
- Should the midweek windows be Fri–Sat for fresher text (Adam floated it)? The refresh makes either work.
