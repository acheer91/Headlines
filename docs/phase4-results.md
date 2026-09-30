# Phase 4 Stage 1 — results note

## 1.1 Key and model check (Sep 29, 2026)
- Key works: `models.list()` succeeds.
- Newest Flash models (`gemini-3.8-flash`, `gemini-3.7-flash`): 503 "high demand" on 3 tries.
- `gemini-3.6-flash`: prints OK in 1.3 s. **Pass.**
- `gemini-2.5-flash`: 404, not available to new users.
- Proposed `GEMINI_MODEL`: `gemini-3.6-flash` (retest 3.8 before Stage 2).

## 1.2 Free-tier limits
Measured from the API's own 429 responses (Sep 29), not the AI Studio page.

| Model | RPM | TPM | RPD | Search grounding |
|---|---|---|---|---|
| gemini-3.6-flash | **5** (quota `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`) | not hit | not measured (can't read without using it up) | **blocked** |

- **Requests per day: 20** (quota `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, hit 2026-09-29 after the
  tests above). Per model, per project; resets at midnight Pacific.
- Default thinking took 20–30 s per call and hit the 30 s timeout; `thinking_level: low` took ~3 s.
- 503 "high demand" seen repeatedly on 3.5, 3.7 and 3.8 Flash during the day.
- Search grounding returns 429 with no quota detail on every Flash model tried (3.5, 3.6, 3.8, 3.1-flash-lite, flash-latest), even after the minute window reset: not available on this free-tier project.
- 5 RPM means the 8:00 AM preview burst needs step 2.5's stagger.

## Groq search test (Sep 29) — hybrid: Groq searches, Gemini writes
- Free-tier key: `openai/gpt-oss-20b` + `browser_search` **works**. `groq/compound-mini`: 404 (not on this key).
- Headers: 1,000 requests/day, 8,000 tokens/minute.
- One search = **30K–60K prompt tokens** (search results are fed to the model). Default settings failed (context_length_exceeded, 54 s); `reasoning_effort: low` + `max_completion_tokens: 1000` works in 7–10 s.
- Against the published 200K tokens/day, that is **~3–6 searches a day**, if search tokens count toward it (not yet confirmed).
- Results for Oklahoma–Texas: ~12 distinct links (SI, Yahoo, texaslonghorns.com, sports-reference, several small SEO sites). No publish dates in the tool output; the model's own dates were partly unconfirmed, so our date check has to fetch each page.

### Targeted search (same day)
- System prompt "one search, do not open any page" + a `site:` list of major outlets: **~1,600 prompt tokens** per search (was 30K–60K), 3 s. ~100 searches/day fit in 200K.
- Without the site list the model still opened a page (~5,400 tokens).
- **Take URLs only from `executed_tools[].search_results`, never from the model's reply.** With the site list, its reply invented plausible links (si.com/.../2026/10/10/..., dated after today); the real results were different.
- Real results are all from the allowlist but many are from 2025; the 8-day date check removes those.

## Writer switched to Groq (Adam, Sep 29)
- Gemini's 20 requests/day can't cover a Saturday (~110 calls), so Groq writes too (`AI_WRITER=groq`, default).
- Writer `openai/gpt-oss-120b`, search `openai/gpt-oss-20b`: separate per-model quotas. Headers for both:
  1,000 requests/day, 8,000 tokens/minute. Groq's docs: 200K tokens/day for the gpt-oss models.
- First run: preview 4.5 s of writing, recap 3.5 s (plus waits for the per-minute token limit).
- Articles are cut to 700 words each so 4 of them fit in one 8K-token minute.
- Measured tokens per text (medium reasoning, 3rd sample run): preview 9–15K, recap 5–9K, headlines ~5K.
- **Step 1.3, college Saturday:** 6 pre-written × (preview 12K + recap 7K) ≈ 114K; 15 opened × ~10K ≈ 150K;
  40 one-liners × ~2K ≈ 80K; headlines ≈ 10K. **Total ≈ 350K tokens vs the free tier's 200K a day: does not fit.**
- Writing time on medium reasoning: recap 8–14 s, preview 8–20 s (targets 8 s / 15 s).
- Quality (3rd run, read against the box scores): prose improved, but team summaries still slipped
  (BAL "matching Dallas quarter by quarter", "forced no turnovers" when it committed none). The code checks
  can't catch wrong claims built from real numbers.
- **Groq's daily limit is a rolling 24-hour window, not a midnight reset** (429 text, Sep 29: "tokens per day
  (TPD): Limit 200000, Used 199286 ... try again in 6m49s"). Headers only report the per-minute limit.
- Fix #1 (facts built by code, Sep 29): the one recap that ran before the daily limit got turnovers, halftime
  and lead changes right; it slipped once ("seventh‑quarter" with a U+2011 hyphen, which the check now catches).
- Fix #2 (fact check, Sep 29): a second model, `qwen/qwen3.8-27b` at temperature 0, checks every text against
  the facts; any problem -> one rewrite told what was wrong; checker unreachable -> failed (never unchecked).
  `python -m app.ai.check_eval` on 12 real sentences from today's runs (8 wrong, 4 right): after telling it the
  score is only known at quarter breaks, **15/15 errors caught, 0/8 false alarms** over two runs; ~0.5–1 s and
  ~1.5K tokens per check. Groq counts prompt + max reply allowance against the minute, so pacing reserves that.
- Paid Groq for comparison (console.groq.com/docs/models, Sep 29): gpt-oss-120b $0.15/M input, $0.60/M output
  → a Saturday ≈ $0.10, ≈ $2–3 a month; Groq has account spend limits.

## Decisions since the handoff (Adam, Sep 29)
- One-liner is stale after 15 minutes (not 5) or a score change; it refreshes on page load/refresh.
- Previews use stored ESPN news first; skip the grounded search when ESPN gives 2+ fresh articles; cache sources per game.
- A publish date in the article URL (e.g. `/2026/09/28/`) counts as a confirmed date.
- No Search Suggestions box.
