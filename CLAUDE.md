# CLAUDE.md — Scores App

Personal, pull-based scores app (ESPN-app replacement). Full PRD: "Scores App — Mini PRD".
This file is the working brief for Claude Code. Keep it current as phases land.

## Current phase: 5a — NCAAF (built 2026-09-29 on branch `phase-5a-ncaaf`; deploy planned Tue 2026-10-06)
Scope: an NCAAF tab with a filtered board, AP/CFP ranks on cards, the same C1/C2/D screens and grading as NFL, and a
GameWorkflow for every board game. Handoff: "Phase 5a Handoff — NCAAF" (Claude Doc). Out of scope: NBA/EPL/MLS (5b),
AI text, neutral-site labels (stored, not shown). Bowls/Playoff plumbing is built (browse across season types); real
bowl games are confirmed with the handoff's December checklist by Dec 12.
- **Board filter** lives in `api/app/ncaaf.py` (`is_featured`); the API (`/api/scoreboard/ncaaf`) and the worker
  (`sync_schedule`) both call it, so the board and the workflows always agree. A game shows when either team is SEC,
  Big Ten, Big 12, ACC or Pac-12 (ESPN conferenceId 8, 5, 4, 1, 9), is ranked 1-25 (PRD, added 2026-09-29), or is
  Notre Dame (team 87), and neither team is FCS (a conference outside `FBS_CONFERENCES`): a ranked team vs an FCS
  opponent still hides. Favorites always show. A missing conference hides the game and the parser
  warns "no conference". Every FBS game is still stored; the filter only decides what shows and what gets a workflow.
- **Conference, rank and neutral site are stored per game** (`games.home_conf` ... `neutral_site`, migration 006).
  ESPN's rank 99 = unranked, stored as NULL. A completed game's rank is frozen (next week's poll never relabels it).
- **Season calendar:** `espn.parse_calendar` flattens ESPN's `leagues[0].calendar` into stages (NCAAF: weeks 1-15,
  Bowls = type 3 week 1, CFP = type 3 week 999; NFL: preseason, 18 weeks, Wild Card ... Super Bowl), stored in
  `league_calendar`. The scoreboard takes `?season_type=&week=` (week up to 999), is cached per (week, season type),
  and returns `season_type` + `calendar`; the web app's Prev/Next walk it (`?st=3&week=1`). `sync_schedule` fetches the
  calendar's next stage (falls back to week + 1 when ESPN sends no calendar).
- **Favorites** are read on every call by `api/app/favorites.py` (API and worker; the worker mounts `./config` too).
  Never resolve its path at import time: the workflow sandbox imports activities and refuses `Path.resolve()`.
- Step 1 census (2026 weeks 1-5, 2026-09-29): 99 / 86 / 75 / 71 / 59 games, unchanged with `limit=300`; FBS ids seen
  1, 4, 5, 8, 9, 12, 15, 17, 18, 37, 151; every other id seen (20, 21, 24, 25, 27, 29, 30, 31, 32, 48, 177, 179) is FCS.
  Texas is `TEX`.

## Phase 4 — AI text (Stage 2 built 2026-09-29 on branch `phase-4-ai-text`; NOT deployed)
Scope: previews (preview, edges, writers' picks), recaps and team summaries, the live one-liner, Home headlines.
The CTO cut the AI one-liner on Oct 1; **Adam put it back the same day** (his call outranks the CTO's): written on
open, reused until the score changes or 15 minutes pass, never handed to the worker; any failure shows the box-score
template (`summary.one_liner`). Previews are about 110 words (Adam, Oct 1: ~10% shorter than the PRD's 120).
Free tiers only, billing off (Adam). Stage 1 notes: `docs/phase4-status.md`, `phase4-results.md`; Stage 2 design:
`docs/phase4-stage2-temporal-spec.md`. Deploy only after Adam approves samples and Phase 5b is signed off, Tue/Wed.
- **Routing (CTO, 2026-10-01; `client.ROUTES`):** one row per kind (recap, preview, headlines): writer
  `openai/gpt-oss-120b`, **no backup writer** (with 120b out: recap = stats-only template, preview "unavailable",
  headlines keep the last set), checker `qwen/qwen3.8-27b` on Groq, overflow `or:qwen/qwen3.8-27b:free` (OpenRouter,
  only while Groq's Qwen is cooling or out of budget). Rules: one named writer + at most one named backup, every
  failover logged (`ai failover: ...`); **the checker is never the writer's family** (`client.family`, the vendor:
  gpt-oss-20b can't check gpt-oss-120b); no such checker = `NoChecker`, the text fails closed, never unchecked.
  A model's name picks the provider: `or:<id>` OpenRouter, `gemini-*` Gemini, else Groq. `AI_WRITERS` (at most 2) /
  `AI_CHECKERS` override every route (samples bake-off, `check_eval` only). Search: `openai/gpt-oss-20b`.
  Deferred (CTO): preview extraction on 20b, Gemini for headlines (after the key is replaced), Nemotron trial.
  Nemotron-3-Ultra (`or:nvidia/nemotron-3-ultra-550b-a55b:free`): in `json_object` mode it returned `{}` 2 of 3 tries.
- **Daily budgets** (`client._over_budget`, both quotas' `spent_today`): Groq 200K tokens per model on a rolling 24 h
  (`GROQ_TPD`), Gemini 20 requests per model (`GEMINI_RPD`), OpenRouter free **50 requests a day for the whole account**
  (`OPENROUTER_RPD`; more keys add nothing; $10 of credits would make it 1,000, CTO: not now). A model that has spent
  its budget is cooled down and skipped before it is asked. Bake-off (Sep 29): Qwen as a writer invents claims.
- **AI leagues:** `AI_LEAGUES` (default and compose: `nfl`), separate from `ENABLED_LEAGUES`. Other leagues get no AI
  text: not written ahead, not on open (`/ai` says `none`, the app hides the preview block), not in headlines.
- **Recap length: back to ~120 words** (2026-10-01, until Adam confirms 200-230 was his call): the recap paragraph is
  about 120 words (code accepts 60-200), each team 2-3 sentences. The one-minute-read tiers (standard ~200 words,
  featured ~230 for a favorite, ranked vs ranked, two winning NFL teams, overtime, a margin of 3 or less, or 2+ lead
  changes; `writer.recap_length`, commit 8d2066a) are still built: set `writer.ONE_MINUTE_READ = True` to bring them back.
- **Box-score claims are refused in code** (`writer.claim_problems`, reviewed errors of Sep 30 and Oct 1): a player's
  numbers come from that player's line; "favored / outgained / advantage of N to M / held the ball N minutes longer" and
  "each side / split" must match the stats; lead changes, "kept/extended/took the lead", "close the gap", "only N points"
  and "N at halftime" must match the quarter-break scores; no "before / responded / scored first / late / early" inside a
  quarter where both teams scored, and "tied" only for a tie shown at a break; no "all game", "start to finish", "through
  the end", streaks or records going in, and a long list of cause words (bolstered, capitalized, checked out, allowing X
  to, to control the game, ...). `python -m app.ai.replay` replays them on the saved eval outputs (3dacc26, bffeb78)
  against `tests/fixtures/ai/known_errors.json` (39 hand-found errors, each tagged by cause): 24/24 and 13/15, 0 of the 6
  passed-clean texts flagged. Not caught: a "touchdown" for 7 points (borderline). The model fact-checker still runs after.
- **Recap voice** (Adam's three samples, Oct 1; `prompts.RECAP_STYLE`, `RECAP_EXAMPLES`): the recap prompt carries a
  style guide and the two examples that are not about this game; a recap that copies 6 words or a joke from them is
  rewritten. Rounding with a word ("nearly 37 minutes" for 36:53), "push" outside betting talk and "the difference was
  the turnover column" are allowed. First live run (gpt-oss-120b, 5 games): 2 ready, 3 failed on banned cause words,
  and the ready ones were not in the samples' voice. `writer.RECAP_VOICE = False` turns it off.
- **Review sheet:** `python -m app.ai.review_sheet` writes `docs/phase4-review-sheet.csv` from an eval output (one row
  per claim, with its facts or article; reviewers fill verdict and cause); `--tally <filled.csv>` counts errors by cause.
  The eval (`checks/phase4_eval.py`) runs a preflight (branch/commit pushed, Docker answers, the stale
  `engine.sock`, Groq quota) and then this.
- **Facts only from inputs, enforced in code** (`app/ai/`): recap and preview game facts are built by code
  (`facts.py`), never read raw by a model; numbers must appear in the facts; bet results are written by code
  (`bets_line`); no advice or bet words; no 8-word copy from an article; edges/picks must come from the linked
  article; then a second model fact-checks every text (`writer._fact_check`). Any failure: one rewrite told what
  was wrong, then status `failed` and the app shows fallback text. Never unchecked text.
- **8-day rule is hard** (`dates.py`, from Phase 0): no confirmable date (page or URL) or older than 8 days =
  dropped; none left = "No fresh previews". Articles: stored ESPN news first, Groq search (major outlets only,
  links taken from the tool's raw results, never the model's reply) only when ESPN has fewer than 2.
- **Written ahead only for the pre-write list** (Adam, 2026-09-29; `app/ai/scope.py`): games with a favorite
  (`config/favorites.json`) or college ranked vs ranked. Every other text is written the first time its page is
  opened (~15 s). Checked in activities at the moment a text is due, so the list keeps itself current.
- **One queue per model, 120b first:** a model whose minute is full is waited for (worker) or given up on (page open:
  fallback text); a backup is used only when one is cooling down after a 429 or erroring (Qwen invented claims as a
  writer; since 2026-10-01 the routes name no backup writer, so a limited writer means a Temporal retry or the
  fallback). Claims carry a token and their own basis; a failed refresh keeps the last good preview.
- **When text is written (Temporal):** `WriteTextWorkflow` per text (ID `ai-<kind>-<league>-<espn_id>`), activity
  `write_text` on task queue `ai` (1 at a time, `AI_AT_ONCE`: one writer, 8K tokens a minute); a rate limit retries
  after Groq's own wait (`next_retry_delay`), up to 8 tries / 12 h. Midweek `PreviewBatchWorkflow` (NCAAF Wed+Thu, NFL
  Thu+Fri, 7 PM PT); the 8 AM `generate_preview` step starts a refresh that rewrites only if the fingerprint (injuries,
  line, articles) changed; at a final, the `ai-recap` patch asks activity `recap_due` and starts the recap only for a
  pre-write-list game in an AI league (again after a changed regrade); headlines after each news pull (`ai-headlines`
  patch); `LeftoverWorkflow` nightly 9:30 PM PT. The `ai-*` schedules are created only for `AI_LEAGUES`.
- **Worker savings (2026-10-01):** *preflight* (`client.unavailable_for`, `jobs.write_for_game/write_headlines(preflight=True)`,
  worker only): when a text needs a model and the writer, or every checker outside its family that has a key, is
  cooling down or out of budget, `write_text` raises `RateLimited` with the wait before the text is claimed or
  drafted (nothing is spent on a draft nobody can check); a text that is already current is still "current", and a
  preview with no articles still becomes "no sources". The page, and for a preview the article lookup (a Groq search
  when ESPN has under 2 fresh articles, ~1.6K tokens of the search model's own budget), run first: the fingerprint
  that decides those cases needs them. A preview's saved extract
  carries its article URLs (`extract.urls`) and is kept on a rate-limit or 5xx failure, so the retry skips the extract
  call (a check failure keeps none). Headlines with the same news and finals as the last ready set return `current`
  (fingerprint in `ai_texts.fingerprint`; a manual run always writes).
- **Rejection cap (Adam, 2026-10-01; migration 009 `ai_texts.rejections`, `store.REJECTION_CAP = 3` failed writes in
  total, the first and two retries):** a text that fails 3 times for a reason retrying the same inputs won't fix (the fact check rejected it twice, a crash, too large) is not
  written again until its inputs change: `jobs.write_for_game` returns `capped` (any caller: worker, refresh, nightly,
  open) and the activity ends without a Temporal retry. Inputs = the claim's game day or score (api, which doesn't
  compute fingerprints) plus, for the worker, the fact-sheet/article fingerprint. Rate limits, 5xx and a missing key or
  checker (`unconfigured`) never count. The next claim for different inputs resets the count; a ready text clears it.
  Headlines count failed rows for the same fingerprint since the last ready set. A "manual" job ignores the cap and
  the pre-write list (runbook below); a search that returns different links is a new fingerprint, so the cap holds
  best for a stable article set; the api's cap is basis-only, so an open-only game stays on its fallback for the day.
  Held: spend-aware batch (E2, no token numbers yet) and storing the unchecked draft (migration 011, only if Qwen's
  production rejection rate says it pays).
- **The `ai-recap` patch has never run anywhere**, so it was changed in place on 2026-10-01 (the `recap_due` step) and
  the four post-patch histories in `tests/fixtures/histories/` re-recorded (`SAVE_HISTORIES=1`). After the first
  deploy, any change here needs a new `workflow.patched` id.
- **On open (api):** `GET /api/games/{id}/ai` returns current text at once, else writes it within 20 s (preview) or
  10 s, **never waiting for quota** (`client.no_wait()`); `GET /api/headlines` for Screen A. A live game gets the one-liner
  (`one_liner`, 10 s). **A preview or recap page open that runs out of quota hands the text to the worker** (Oct 1, `main._hand_to_worker`: a
  `WriteTextWorkflow` with reason `open`, which the worker writes even off the pre-write list, waiting for the minute);
  the row's reason becomes `queued`, `/ai` says `queued` and the app "Writing the preview… pull again in a minute".
  A preview's extract and write don't fit one minute of 120b together (PIT @ CLE, Oct 1: 9,029 + 3,587 tokens), so an
  opened preview usually finishes there. No `TEMPORAL_ADDRESS` (api env) or Temporal down: the old fallback. `written_at` (migration 008) is when the shown text was written: a failed refresh keeps the last good
  preview, and the app shows "Updated <time>" under it (CTO: acceptable with the timestamp).
- **Quota is shared through Postgres** (`AI_QUOTA=db`: `ai_calls`, `ai_cooling`), so the api and worker can't
  collide; Groq counts prompt + max reply against the minute, so that is what's reserved. **gpt-oss prompts are
  counted** with its tokenizer (`tiktoken` `o200k_harmony`, baked into the image via `TIKTOKEN_CACHE_DIR`) plus 100
  for Groq's chat template (measured Oct 1: 71 plain, 95 JSON); other models ~4 characters a token. The old estimate
  ran 20-35% low (an extract reserved 7,800 and used 9,029).
- **Extraction steps and the one-liner run light** (`client.write(light=True)`: the preview's article extract, the
  headlines' news extract, the live one-liner): low reasoning, reply allowance `AI_LIGHT_MAX_OUT` (1,500; the one
  medium extract measured used its whole 3,000, a medium one-liner 4,391 in all). **Article text is cut to the paragraphs about the game** (`sources.relevant_text`: a team or a player
  from our injury report or leaders), at most 500 words an article (was 700 of the page as it came).
- **Keys** (`GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) live only in `.env`; no key, or removing it, is the off switch (nothing new is
  written, so every AI section shows fallback text; texts already stored keep showing). Prompts carry only public sports data (free tiers may use prompts for training).
- **Usage report** (`python -m app.ai.usage [--days N] [--json]`, read-only): the last 24 hours against each budget (the
  OpenRouter models as one pool), then per model and day the calls, tokens, peak 60 s and unreported calls; texts by outcome; the rejection rate and top reasons; who wrote and checked.
  `ai_calls` is kept 14 days (`quota.KEEP`; it was 2) so a week of real numbers exists. First-deploy steps and the
  first-week reading guide: `docs/phase4-deploy-checklist.md`.
- **Prep layer (2026-10-02, branch `phase-4-prep-layer`; code only, no local model):** M1 logs each call's
  `ai_calls.kind` (`<text>:<step>`, `client.call_kind`), Groq's cached prompt tokens and its per-minute rate-limit
  headers (migration 010, log only: budgets still count every token until T1, the log-only week, passes; the usage
  report's T1 and M2 sections read them). M2 puts the recap prompt's shared text first (examples after the rules; the
  report compares recap rewrites per first draft with 0.586). M4 trims the one-liner's FACTS to its prompt's hooks
  (`facts.LIVE_STATS`/`LIVE_LEADERS`; claims_ok still knows every leader). M3 (code's recap outline,
  `facts.recap_outline`, and a low-reasoning recap writer) has no production switch: Z5 (`python -m app.ai.angle_eval
  --labels ...`) matched 11/16 on Oct 2, FAIL. T3 (`python -m app.ai.outline_eval`, built, never run) refuses a real
  run until `--labels` pass Z5 on `--z5-fixtures <dir>`, a fresh set (`angle_eval.fresh_set_problem`: not under
  tests/, its manifest's facts.py sha256 still facts.py's, none of the 16 finals the rules were tuned on), so the Oct 2
  labels can't open it. M9 (headline candidates by rule) is not built: Z3 failed. The compose worker
  waits for the api to be healthy (migrations applied).
- **Z5 week-5 test** (pre-registered Oct 2: `docs/z5-preregistration.txt`, facts.py sha256 `554727d6...8387a4`, rule
  order also in commit 92491ad). Adam ruled NE @ JAX a blowout, so the recap angle now checks the margin first
  (blowout, close_finish, then outgained_but_lost, turnovers): 15/16 on the Oct 2 labels, but in-sample, so it proves
  nothing. "Week 5" is ESPN's
  regular-season **week 4** (Oct 1-5): the 16 fixtures (Sep 24-28) are ESPN's week 3, which the Oct 2 notes call week 4.
  1. After Monday night's game: `python -m app.ai.angle_fixtures --season 2026 --week 4 --out <dir>` (ESPN only: no
     tokens, no database; never under tests/). A game not final yet (not canceled or postponed) stops it with nothing
     written: rerun once it is. `--partial` writes the week without it (manifest `partial`): not the pre-registered week.
  2. A blind labeller: Adam, or an agent that has never seen facts.py, angle_eval output or an outline, **started
     outside `C:/Users/axos2/Desktop/Headliners` with no file tools and the text of `<dir>/labels_sheet.md` pasted into
     its prompt** (this file and the project memory name the rule order; never the session that changed the rules).
     Save its reply (the labels JSON, the sheet's reply format) and its sha256 before scoring.
  3. `python -m app.ai.angle_eval --fixtures <dir> --labels <labels.json>`: it refuses a set that isn't fresh and prints
     facts.py's sha256: check it is the pre-registered one. **Pass bar: code matches at least 85% of the finals, rounded
     up** (13 of 15, 14 of 16). On a pass, T3: `outline_eval --run --labels <labels.json> --z5-fixtures <dir>`.
     Any rule change after a look needs another fresh week.
- **Tests:** `tests/test_ai_*.py` (no live model calls), `test_workflows.py` (every simulated game runs a fake `ai`
  worker: a waiting AI task stops time-skipping), `test_replay.py` (old histories replay with the patches), `test_ai_replay.py` / `test_ai_review_sheet.py` (need git history; skipped without it).

## Phase 3 — Temporal (built 2026-09-28, deployed to the server 2026-09-29 05:13 UTC)
Scope: scheduled work off the pull path. ScheduleSync (daily 6:00 AM PT) starts one GameWorkflow per NFL game not yet
final; GameWorkflow saves the line, runs the (empty) preview step, waits for kickoff, polls for the final every 2.5 min (Adam), grades, and
regrades once 1 h later (Adam); Headlines (7:00 AM and 5:00 PM PT) stores ESPN news in `news_items`, no AI. Handoff: "Scores
App — Phase 3 Handoff". Status: `validate_phase3.sh` 23/23 on the laptop and 22/22 on the server (3.5/3.6 run on x86 only), 157 tests; 3.9
API side passed; every runbook command run once on the server. 2.7 (week 3): 48/48 results match an independent
regrade from ESPN's finals and closes. Decisions (Adam, 2026-09-28): last line save 30 min before kickoff, polls every
2.5 min, one regrade 1 h after the final, headlines without AI, pull path stays. No server resize: the whole stack
uses ~1.1 GB of 3.8 GB (Temporal ~90 MB). Still to do: 3.9 in the app, 3.10 (unattended weekend), 3.12 (a patched
change deployed mid-week), 3 real game histories as replay fixtures.

### Temporal
- **Containers:** `temporal` (server 1.32.0, :7233 localhost), `temporal-admin-tools` (1.32.0: sets up the `temporal`
  and `temporal_visibility` databases in our Postgres on every start, creates namespace `default` with 30-day
  retention, then stays up for the CLI), `temporal-ui` (2.54.1, :8080 localhost; `tailscale serve` for other devices),
  `worker` (same image as the api, `python -m app.temporal.worker`, task queue `scores`). All tags have arm64 builds.
  After first boot: `docker compose exec worker python -m app.temporal.schedules` (safe to rerun).
- **Code** in `api/app/temporal/`: `models.py` (dataclasses only), `activities.py` (sync, wrap Phase 1/2 code),
  `workflows.py` (decide and wait only), `starter.py` (signal-with-start activity), `worker.py`, `schedules.py`.
- **Workflows decide, activities do.** No clock, random, network, DB, env, files or zoneinfo in `workflows.py`;
  times travel as UTC ISO strings; Pacific-time math (the 8 AM preview) happens in `sync_schedule`.
- **Every activity is idempotent** (upserts; snapshots only on a line change; news dedupes on ESPN's id).
- **Shared scoreboard fetch:** `save_line` and `fetch_game_state` read the game's week through a 60 s in-worker cache,
  so a Sunday's simultaneous polls make one ESPN call per week, not one per game.
- **Workflow IDs** `<league>-<espn_id>`, reuse policy "allow duplicate failed only": a completed game is never
  reopened; a failed or terminated one is restarted by the next ScheduleSync.
- **Retries:** ESPN activities 30 s per try, 10 s doubling to 10 min, give up after 6 h (the workflow then skips that
  step, never fails); `grade_game` 5 tries, then the workflow fails (visible in the UI); preview none.
- **Reschedule signal** `Times(start_iso, preview_iso)`: ScheduleSync sends the current times daily; identical times
  change nothing. A bad or offset-less time is logged and ignored. A kickoff ESPN moves later is also picked up by
  the status poll.
- **Closed as** `graded [h, a]` (plus `regraded ...` after a stat correction), `not played (canceled)`,
  `unresolved: never went final` (7 days of polling) or `unresolved: postponed, no new date in 14 days`. An unresolved
  game completes, so it is not restarted; the pull path still grades it.
- **Snapshots are tagged** `odds_snapshots.source`: `pull` (the app) or `workflow` (the worker).
- **The app never depends on Temporal.** The pull path still refreshes and grades with Temporal stopped.
- **Workflow changes are versioned.** Once games are running, wrap any change to workflow logic in
  `workflow.patched("<name>")` and run `tests/test_replay.py` (replays `tests/fixtures/histories/`); deploy Tuesday or
  Wednesday. Activity code can change any time (restart the worker).
- **Tests:** `tests/test_workflows.py` uses the time-skipping test server (x86 only: run on the laptop, not the ARM
  server); `tests/test_activities.py` needs TEST_DATABASE_URL.

### Runbook (`docker compose exec temporal-admin-tools temporal ...`)
- Running games: `workflow list --query "WorkflowType='GameWorkflow' AND ExecutionStatus='Running'"`
- One game: `workflow show -w nfl-<espn_id>` (or the UI)
- ScheduleSync now: `schedule trigger --schedule-id schedule-sync`
- Kickoff changed, sync hasn't run: `workflow signal -w nfl-<espn_id> --name reschedule --input '{"start_iso": "2026-10-04T20:25:00+00:00"}'`
  (UTC with offset; preview_iso optional, default keeps the same lead)
- Stuck after a bug fix: `workflow reset -w nfl-<espn_id> --type LastWorkflowTask`
- Write one AI text now, lifting a cap (any game in an AI league): `workflow start --type WriteTextWorkflow
  --task-queue scores --workflow-id ai-recap-nfl-<espn_id> --input '{"kind":"recap","league":"nfl","espn_id":"<espn_id>","reason":"manual"}'`
  (`preview` for a preview). Lift every cap: `UPDATE ai_texts SET rejections = 0 WHERE rejections > 0;` (psql in `db`).
- Beyond saving: `workflow terminate -w nfl-<espn_id> --reason ...`, then trigger ScheduleSync (starts a fresh one)
- Failures: UI filter `ExecutionStatus='Failed'`, every Monday after the weekend
- Save a real history for the replay test: `workflow show -w nfl-<espn_id> -o json > api/tests/fixtures/histories/<name>.json`

## Phase 2 — Game screens and bet grading (built 2026-09-27; Sunday checks passed 2026-09-28)
Scope: C1 pre-game, C2 live, D post-game from the ESPN game summary; ML / spread / O/U grading; summary cache.
Out of scope: AI text (spots ship as "Coming in Phase 4" placeholders), Temporal, other leagues.
Status: `validate_phase2.sh` 14/14, 124 tests. Self-review fixed 10 issues; an independent review found 10 more, its
re-check found a regression + 5 small items; all fixed and verified by the reviewer (verdict: ready apart from 2.10/2.11).
Known residual (accepted): if ESPN drops the event list AND the season/week fields in one change, the board is served as
fresh with stored cards (needs two simultaneous format changes). Bug bash (2026-09-27) fixed 8 issues across Phases 1 and 2. Steps 2.4-2.6 checked against espn.com on 2026-09-27
(3 upcoming, a live game at halftime, 3 finals). 2.10 passed on PHI @ CHI 2026-09-28 (C1 -> C2 -> D on pulls, all
bets graded right by hand on ESPN's close, plus LAR @ DEN; report in `phase2_check_PHI-CHI.md`) and 2.11 passed on the
phone. Still open: Adam's hand check of 2.7's table and the 4 "Decisions to confirm" in the Phase 2 handoff.
Server shape since 2026-09-28: A1.Flex 2 OCPU / 4 GB + 2 GB swap (~0.6 GB used before Temporal).

Phase 1 (NFL scoreboard) done 2026-09-27: installed on the phone via `tailscale serve --bg 8000`
at https://technologic.tailca897c.ts.net (tailnet only).

**Deployed 2026-09-28** to the Oracle ARM server `scores` (`ssh ubuntu@scores`, repo at `~/scores-app`, cloned from
github.com/acheer91/Headlines with a read-only deploy key `~/.ssh/scores_deploy`). Served at
https://scores.tailca897c.ts.net (tailnet only). Phase 1 15/15 and Phase 2 14/14 passed there; survives a reboot.
Updates: `git pull && docker compose up -d --build` on the server. Temporal UI: https://scores.tailca897c.ts.net:8443
(`tailscale serve --bg --https=8443 8080`, tailnet only).
**Backups** (PRD: nightly dump stored off the server): `scripts/backup.sh` runs from the server's crontab at 10:30 UTC
(3:30 AM Pacific), `pg_dumpall` of every database (app + Temporal from Phase 3) gzipped into the Oracle Object Storage
bucket `scores-backups` (root compartment, always free; lifecycle rule deletes objects after 30 days). Upload uses a
write-only pre-authenticated request in `~/.backup_url` (mode 600, never in the repo) that **expires 2027-09-28 23:00 UTC**:
make a new one before then. Log: `~/backup.log`. Restore (tested 2026-09-28): the PAR can't read, so download with
`~/.local/oci-cli/bin/oci os object get --auth instance_principal --bucket-name scores-backups --name <file> --file <file>`,
then `gunzip -c <file> | docker exec -i <empty postgres:16 container> psql -U postgres` and check `select count(*) from games`.
OS security updates install daily (Ubuntu unattended-upgrades). The laptop stack runs again for Phase 3 testing (its own data).

## Stack
- `api/` FastAPI + psycopg 3, plain SQL (no ORM). Python 3.12.
- `web/` React 18 + Vite + vite-plugin-pwa, TypeScript. Built into `web/dist` and served by the API (one origin).
- `db/migrations/` plain SQL, applied only by the API at startup (`app/migrate.py`, tracked in
  `schema_migrations`). Every file must be idempotent: `tests/test_migrations.py` applies each twice.
- `docker-compose.yml` db + api, plus Phase 3's temporal, temporal-admin-tools, temporal-ui, worker.

## Rules
- **Pull model.** Data refreshes only when the user pulls or opens the app. No polling timers in the web app.
- **Never blank a screen.** If ESPN fails, serve stored data with `stale: true`. Every block has an explicit
  empty state ("None reported", "Ungraded", "Not available yet").
- **ESPN is unofficial.** Read every field defensively. Scoreboard quirks live in `api/app/espn.py`, game-summary
  quirks in `api/app/summary.py` (each block parses on its own; a bad block comes back empty). Field map:
  `docs/espn_summary_fields.md`.
- **Never fake a field.** If ESPN doesn't send a stat, cut it or say so. Turnover margin: not wanted (Adam).
- **C1 league ranks** (Adam wants them): offense from the core team statistics, "allowed" from the site team
  statistics `results.opponent` (1st = allowed the least). A rank is shown only when ESPN's ranked value equals the
  number on the row; otherwise no rank.
- **C1 leaders are Passing, Rushing, Receiving, Tackles, INTs** (Adam, 2026-09-27). The first four come from the
  summary; INTs from ESPN's core team leaders (`games.team_interceptions`, cached 6 h in `team_season_leaders`).
  C2 and D show this game's Passing, Rushing, Receiving.
- **Weeks are per season type.** Preseason (1), regular season (2) and postseason (3) each start at week 1; every
  week query filters on `games.season_type`, and browsing follows ESPN's calendar (never a hard-coded last week).
- **Only completed games are graded.** Canceled/postponed games are `state = 'post'` with `completed = false`
  (usually 0-0); they show "Not graded · Canceled" and any stored results are removed.
- **Flexed kickoffs:** `time_valid = false` means `start_time` is ESPN's placeholder; show the date and "time TBD".
- **Nothing moves a game backwards** (completed final -> live, live -> pre): not a lagging summary, not a lagging
  scoreboard. When the stored summary trails the game's state, the game screens withhold its box-score blocks
  (`summary_behind`) and refetch at most every 30 s.
- **Unreadable ESPN data is not fresh data:** if no game in a scoreboard response parses, the refresh counts as failed
  (stale banner); if some don't, the board shows a warning.

## Latency (2026-09-27 pass)
- **Web app paints last-seen data instantly** (`web/src/lastSeen.ts`, localStorage, per device) and pulls fresh data
  over it; a game page opens with the tapped card while its detail loads. Every open still pulls.
- **ESPN client** (`espn._get_json`): one shared keep-alive client, 2 attempts 0.25 s apart, 2 s connect / 4 s read
  timeouts, and a per-host "down" switch: after an *outage* (connection error, timeout, 5xx, 403, 429), skip that
  host for 30 s and serve stored data at once. A 404/400 is one bad request: not retried, doesn't trip the switch.
  Batch commands (`grade_week`, `check_summary`) turn the switch off.
- **C1 team season stats** (ranks + INTs leader) fetch in parallel with each other and with the summary
  (`games.start_team_season` / `finish_team_season`); cached 6 h in `team_season_stats`.
- **Postgres pool** (`psycopg_pool`, no prepared statements, 5 s wait so a down database fails fast); game pages read only the summary blocks they use
  (`db.get_summary_view`). **GZip** on responses; hashed `/assets` cached for a year, `index.html`/`sw.js` no-cache.
- **Lines are append-only.** `odds_snapshots` is never updated; a row is added only when the line changes.
- **Spread convention:** `home_spread` is from the home team's side, negative = home favored. The UI shows the favorite ("SF -2.5").
- **No custom User-Agent on site.api.espn.com.** Akamai returns 403 for it; httpx's default works.
- **Soccer never gets odds.** `BETTING_LEAGUES` in `espn.py`.
- **Bet status reports, never advises.** "KC -3 covering by 4", never "take the over".
- **No new signups or paid services** without asking Adam.
- Favorites live in `config/favorites.json` (`{"nfl": ["NE"], "ncaaf": ["TEX"]}`, ESPN team abbreviations).
- **NCAAF board filter lives in `ncaaf.py`; the API and worker both call it.** Never filter NCAAF anywhere else.

## Bet grading (`api/app/grading.py`, pure functions, no DB or network)
- **Line used**, chosen per market (moneyline, spread, total) in this order (Adam, 2026-09-27, after the independent
  review): ESPN's closing line from the summary's `pickcenter[].close` (`espn_close`: the book's line at kickoff, so
  results match the sportsbook); else our last `odds_snapshots` row captured while the game was `pre` (`pre_game`;
  snapshots only come from pulls, so they can be days old); else the newest snapshot (`in_game`, tagged on screen);
  else Ungraded. The post-game screen tags the source only when it isn't ESPN's close.
- **Moneyline:** winner; a tie is a push. **Spread:** home margin + home spread, >0 home covers, <0 away, 0 push.
  **Total:** points vs the total, equal is a push. Margins are always >= 0. A market whose number is missing is Ungraded.
- **When:** `games.grade_game` runs on every pull of a final game page and after every scoreboard refresh for
  finals. It writes only when the score, line or result changed. One `bet_results` row per game per market
  (`UNIQUE (game_id, market)`), overwritten on regrade, never duplicated.
- **Live (C2):** same math on the current score, labelled "so far", never stored.

## Game pages (`GET /api/games/{id}`, `api/app/games.py`)
- Screen from the game's state: pre -> C1, in -> C2, post -> D.
- Summary cache (`game_summaries`, raw ESPN JSON, parsed on read): at most one ESPN call per game per 30 s once
  kickoff time has passed; upcoming games every 10 min (`SUMMARY_PRE_SECONDS`); a final summary is fetched once and
  kept. A summary fetch also moves the game's state and score forward, so a game-page pull goes C1 -> C2 -> D.

## Commands
```bash
docker compose up -d --build            # full stack on :8000 (applies new migrations at startup)
./scripts/validate_phase1.sh            # Phase 1 automated checks
./scripts/validate_phase2.sh            # Phase 2 automated checks (includes the full test suite in the container)
./scripts/validate_phase3.sh            # Phase 3 automated checks (~25 min; SKIP_SLOW=1 skips 3.7/3.8)
docker compose exec worker python -m app.temporal.schedules [--show]  # create/update (or print) the schedules
docker compose exec api python -m app.check_espn [--week N]        # live scoreboard parse check
docker compose exec api python -m app.check_summary [--event ID]   # live summary blocks, one game per state
docker compose exec api python -m app.grade_week [--league ncaaf] --week N [--season-type 3]   # grade a week, hand-check table
docker compose exec api python -m app.migrate                      # apply migrations by hand

# Local dev without Docker
cd api && pip install -r requirements.txt && uvicorn app.main:app --reload     # :8000
cd web && npm install && npm run dev                                            # :5173, proxies /api

# Tests (parser and grading tests need nothing; API tests need a throwaway Postgres)
cd api && pytest
docker compose exec db createdb -U scores scores_test   # once
# Use 127.0.0.1, not localhost: on Windows localhost tries ::1 first and the connect hangs.
cd api && TEST_DATABASE_URL=postgresql://scores:<POSTGRES_PASSWORD>@127.0.0.1:5432/scores_test pytest
```

## Layout
```
api/app/espn.py           ESPN adapter: LEAGUES map, parse_scoreboard (pure), fetch_scoreboard / fetch_summary (retries)
api/app/summary.py        ESPN game summary -> screen blocks (header, stats, leaders, injuries, lines, one-liner)
api/app/grading.py        ML / spread / O/U grading, line choice, live status, result wording (pure)
api/app/games.py          summary cache rules, state moves on pull, grade_game, per-screen payloads
api/app/db.py             upserts, odds snapshots, fetch_log, card query, summaries, bet results
api/app/ncaaf.py          NCAAF board filter (pure; API + worker)
api/app/favorites.py      reads config/favorites.json on every call (API + worker)
api/app/main.py           /api/health, /api/scoreboard/{league}, /api/games/{id}, serves web/dist
api/app/migrate.py        applies db/migrations at startup
api/app/check_espn.py     validation CLI (scoreboard)
api/app/check_summary.py  validation CLI (summary, step 2.1)
api/app/grade_week.py     grade a week, print the hand-check table (step 2.7)
api/app/temporal/         Phase 3: models, activities, workflows, starter, worker, schedules
api/tests/                parser, summary, grading and cache-rule tests (fixtures: real ESPN payloads), API tests
                          (real Postgres), static-serving tests (need web/dist)
db/migrations/            001_init.sql (Phase 1), 002_game_details.sql (game_summaries, bet_results),
                          003_season_type_and_status.sql (season_type, completed, time_valid),
                          004_team_season_stats.sql (per-team cache: ranks + INTs leader),
                          005_temporal.sql (odds_snapshots.source, news_items),
                          006_ncaaf.sql (conference/rank/neutral site per game, league_calendar, fetch cache key)
temporal/                 admin-tools entrypoint (schema setup + namespace), dynamic config
docs/                     espn_summary_fields.md (field map)
web/src/                  Scoreboard.tsx (B), GamePage.tsx (C1 / C2 / D), GameCard.tsx, usePull.tsx (pull to refresh),
                          lastSeen.ts (instant paint from the last response)
```

## Next phases (don't start without Adam's go-ahead)
3. (Built, see above.) Not in the Phase 3 handoff, still to do: move the nightly backup (`scripts/backup.sh`, now cron) into Temporal for retries and visibility (Adam, 2026-09-28).
   Until then a failed backup alerts no one: glance at `~/backup.log` and the bucket on the server once a week.
4. AI text: built on `phase-4-ai-text` (Groq free tier; see the Phase 4 section). Not deployed.
5. 5a NCAAF built (see the top). 5b: NBA, EPL, MLS (scores only).
   Needs a date-window query: NBA and soccer have no weeks.
6. Deploy to Oracle Cloud always-free, Tailscale only.
