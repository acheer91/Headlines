# Phase 4 — status and handoff (Sep 29, 2026, updated evening)

**Stage 1 prototype on `phase-4-prototype`; Stage 2 built on `phase-4-ai-text` (from `phase-5a-ncaaf`, worktree
`../scores-app-p4`). Adam said to move forward with what we can (Sep 29). Nothing is deployed or pushed.** Adam has not seen or approved any samples yet. Details and raw numbers:
[phase4-results.md](phase4-results.md). Latest samples (partly rate-limited, not for Adam):
[phase4-samples.md](phase4-samples.md).

## Where it stands

| Step | Status |
|---|---|
| 1.1 Key and model check | Done. Gemini key works (`gemini-3.6-flash`); Groq key works |
| 1.2 Free-tier limits | Done, measured from the APIs (table below) |
| 1.3 Does a Saturday fit? | **No on any single free model** (~350K Groq tokens vs 200K/day). Plan below |
| 1.4 Prompts | Built; being improved (fixes 1, 2 done; 4 waits for Adam) |
| 1.5 Search + 8-day rule | Built and tested |
| 1.6 Samples for Adam | **Not ready.** Rerun blocked by Groq's daily limit |
| 1.7 Timing | Measured; previews 11–27 s total, recaps 3–14 s (targets 15 s / 8 s) |
| Stage 2 | **Built and tested locally** (below). Deploy still waits for sample approval, Phase 5b and a Tue/Wed |

## What changed from the handoff (all Adam's decisions, Sep 29)

- **Writer is Groq, not Gemini.** Gemini free tier = 20 requests/day/model, and Google Search grounding is
  blocked on the free key. Writer: `openai/gpt-oss-120b`. Gemini code stays (`AI_WRITER=gemini`).
- **Search is Groq** (`openai/gpt-oss-20b` browser search), major outlets only (`sources.OUTLETS`), max 4 articles.
  Stored ESPN news first; search only if fewer than 2 fresh ESPN articles. Links are taken only from the search
  tool's raw results: the model's own reply invents links.
- **A date in the article URL counts as confirmed** (8-day rule). No "Search on Google" box.
- **One-liner** is stale after 15 min (not 5), refreshed only on page load. No retries after a rate limit.
- **Fact check by a second model**, `qwen/qwen3.8-27b` (see below).
- **Direction for Stage 2 (not built):** option C, i.e. failover across free Groq models, run by Temporal; all
  models in parallel; previews pre-written midweek (NCAAF Wed–Thu, NFL Thu–Fri, or Fri–Sat) with a game-morning
  refresh only when injuries/articles changed (rewrite step only); Gemini as a side pool (headlines, weekday
  pre-writing, last fallback); recaps for every final; articles fetched ahead in the headlines job; a nightly job
  that spends leftover quota. **Needs a PRD change and Adam's OK before Stage 2.**

## Stage 2 (built Sep 29 on `phase-4-ai-text`, per `phase4-stage2-temporal-spec.md`)

- Migration `007_ai_text.sql`: `ai_texts` (claim, basis, fingerprint, extract, writer/checker), `ai_calls` +
  `ai_cooling` (free-tier quota shared by api and worker, `AI_QUOTA=db`).
- `app/ai/`: `store.py` (claim/save), `jobs.py` (one write path for worker and api), `quota.py` (Postgres quota);
  client failover across Groq models, `no_wait()` for the api, checker failover on unreadable replies.
- Temporal: `WriteTextWorkflow` (activity `write_text` on queue `ai`, 3 at once; rate limits retry after Groq's own
  wait — confirmed 5.0 s on the laptop's real server), `PreviewBatchWorkflow` (NCAAF Wed+Thu, NFL Thu+Fri 7 PM PT),
  `LeftoverWorkflow` (9:30 PM PT), recap at the final (`ai-recap` patch), headlines (`ai-headlines` patch), the 8 AM
  step starts a fingerprint refresh (activity-only change).
- API: `GET /api/games/{id}/ai` (current text at once, else writes within 20 s / 10 s, never waits for quota),
  `GET /api/headlines`. Web: preview/edges/picks/sources on C1, recap + team summaries on D, AI one-liner on C2,
  Home (Screen A) headlines with fallback to the NFL board.
- Compose passes `GROQ_API_KEY`, `GEMINI_API_KEY`, `AI_QUOTA=db`; no key = fallback text everywhere.
- Tests: 307 pass (DB, API, AI, 22 workflow tests, replay 9/9: 4 pre-AI histories + 5 with the patches).
- Not done: a live end-to-end run on the laptop stack (needs the rebuilt containers and quota), the deploy runbook.

## Measured limits (free tiers)

| Model | Per minute | Per day | Notes |
|---|---|---|---|
| gemini-3.6-flash | 5 requests | **20 requests** | Frequent 503 "high demand"; grounding blocked |
| groq gpt-oss-120b (writer) | 8K tokens | 200K tokens, 1K requests | **Daily window is rolling 24 h**, not midnight |
| groq gpt-oss-20b (search) | 8K tokens | 200K tokens, 1K requests | Targeted search ≈ 1.6K tokens |
| groq qwen3.8-27b (checker) | 8K tokens | 1K requests; token/day unconfirmed | |

Groq counts prompt + the whole `max_completion_tokens` against the minute; pacing reserves that.

## Accuracy work (Adam chose fixes 1, 2, 4 of 5; one at a time)

- **Found by hand in today's samples:** wrong team city, a push called a win, a lead in the wrong quarter,
  "forced no turnovers" when none were committed, a "seventh quarter". Code checks alone missed most of these.
- **Fix 1: facts built by code** (`app/ai/facts.py`): recap fact sheet entirely from the box score (no model
  extract step, one call); preview game facts from code, model extracts only from articles. Giveaways/takeaways,
  score at every quarter break and who led, lead changes, "favored by", "fewest" ranks. **Only 1 recap ran before
  the daily limit (clean apart from a "seventh‑quarter" the check now catches). Needs the full eval.**
- **Fix 2: fact check** (`writer._fact_check`, prompt `FACT_CHECK`): Qwen at temperature 0 lists unsupported
  claims; one rewrite told what was wrong; checker unreachable → failed (never unchecked).
  `python -m app.ai.check_eval`: **15/15 known errors caught, 0/8 false alarms** over two runs.
- **Fix 4: approved examples in the prompt** waits until Adam approves samples.
- Also in place: bet results written by code (`bets_line`), not the model; no-advice and no-bet-talk word checks;
  8-word copy check; numbers must appear in the facts; edges/picks must come from the linked article.

## Next steps, in order

1. When gpt-oss-120b's rolling window frees up (a few hours), run the eval: all 16 NFL finals from last weekend
   as recaps + the 5 previews (`python -m app.ai.samples`, extend `RECAPS`). Read each against the box score.
   The fact-check rejections show the writer's first-draft error rate (fix 1); the final texts show fixes 1+2.
2. If clean: PM sends the samples to Adam (step 1.6) with the proposal for the Stage 2 direction above.
3. After Adam approves: fix 4 (2–3 approved samples in the write prompts), then Stage 2.

## Files (all uncommitted)

```
api/app/ai/client.py      Groq writer/checker/search, Gemini option, per-model pacing, key only from env
api/app/ai/facts.py       fact sheets built by code (recap, preview)
api/app/ai/prompts.py     every prompt; voice and guardrails
api/app/ai/writer.py      write_preview/recap/one_liner/headlines + all checks + fact check
api/app/ai/sources.py     ESPN-first articles, Groq search, 8-day rule, relevance, text extraction
api/app/ai/dates.py       8-day rule (ported from Phase 0, URL dates allowed)
api/app/ai/samples.py     python -m app.ai.samples -> docs/phase4-samples.md (laptop API + DB)
api/app/ai/check_eval.py  python -m app.ai.check_eval: fact-checker on 12 known cases
api/tests/test_ai_*.py    70 tests, no live AI calls; fixtures in tests/fixtures/ai/ (real game payloads)
api/requirements.txt      + google-genai
```

## Things to know

- **Keys:** `GEMINI_API_KEY` and `GROQ_API_KEY` are only in `scores-app/.env` (gitignored; checked, and no key
  appears in any tracked or new file). Adam declined to rotate the Gemini key even though it was pasted into the
  handoff doc and chat. The repo is still **public**.
- Samples need the laptop stack running (`docker compose up -d`): they read games through the local API and news
  from the local DB (password from `.env`).
- On Windows, the Bash tool mangles `\n` and `\b` inside heredoc'd Python; edit files with the Edit tool.
- Not done yet: the one-liner still gets raw game JSON (not a code fact sheet); Stage 2 pieces (ai_texts table,
  claim, API endpoint, Temporal failover, UI) not started.
