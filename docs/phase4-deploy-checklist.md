# Phase 4 AI text: deploy checklist (draft, Oct 1, 2026)

For the first deploy of `phase-4-ai-text` to the server. **Nothing here has been run on the server.** Each step with a
command was rehearsed on throwaway infrastructure on the laptop (a scratch Postgres at migration 008, a local dev
Temporal server, a fresh image built from the branch), not on the live stack. The branch contains Phase 5a, so 5a
(planned Tue Oct 6) ships first or with it.

## 1. Gates (from CLAUDE.md; none recorded as met)

- [ ] Adam approves the samples (`docs/phase4-samples*.md`).
- [ ] Phase 5b signed off.
- [ ] A Tuesday or Wednesday (a changed workflow is deployed mid-week, never before a game day).
- [ ] The branch is settled: other work on `phase-4-ai-text` (the live one-liner) is committed, and the commit being
      deployed is the one that was tested.

## 2. Before touching the server (laptop)

- [ ] Full suite in a fresh image built from the commit to deploy, against a throwaway Postgres (not a shared
      `scores_test`: two sessions running tests at once drop each other's tables):
      `docker build -t scores-ai-verify .` then `docker run --rm --network <net> -e TEST_DATABASE_URL=... scores-ai-verify python -m pytest -q`.
      Expect only the git-history tests (`test_ai_replay`, `test_ai_review_sheet`) skipped.
- [ ] `tests/test_replay.py` is in that run: the pre-AI histories and the four post-`ai-recap` ones replay.
- [ ] The server `.env` (never in the repo) has `GROQ_API_KEY` (required) and `OPENROUTER_API_KEY` (the overflow
      checker; without it that pool is left out and a cooling Groq Qwen means a retry, not an unchecked text).
      `GEMINI_API_KEY` is unused today. `docker-compose.yml` passes all three to `api` and `worker` and sets
      `AI_LEAGUES: nfl` and `AI_QUOTA: db` on both.
- [ ] Back up first if the nightly dump hasn't run since the last data you care about (`scripts/backup.sh`; log
      `~/backup.log` on the server).

## 3. Deploy

On the server (`ssh ubuntu@scores`, repo in `~/scores-app`):

1. `git pull && docker compose up -d --build`. The api applies migrations at startup. A server at Phase 5a (006)
   gets `007_ai_text`, `008_ai_written_at` and `009_ai_rejections`, in that order. Rehearsed: an upgrade from 008 to
   009 on a database holding data keeps its rows (`rejections` = 0) and a rerun applies nothing.
2. `docker compose exec api python -m app.migrate` prints nothing (all applied).
3. `docker compose exec worker python -m app.temporal.schedules --show`, then without `--show`. Expect `created
   ai-leftover` and `created ai-previews-nfl` (NFL only: `AI_LEAGUES`), and the two existing ones (`schedule-sync`,
   `headlines`) `updated`. A schedule that
   is no longer wanted is deleted by this command (only `ai-*` ones); `--show` names it first.
4. `docker compose logs worker`: "worker polling task queues scores and ai". The `ai` queue runs one text at a time.
5. Temporal UI (`https://scores.tailca897c.ts.net:8443`): the four schedules (`schedule-sync`, `headlines`, `ai-leftover`, `ai-previews-nfl`) exist and are not paused.

## 4. Smoke test (no schedule needed)

- [ ] One favorite's recap or preview by hand, which also lifts any cap:
      `docker compose exec temporal-admin-tools temporal workflow start --type WriteTextWorkflow --task-queue scores
      --workflow-id ai-preview-nfl-<espn_id> --input '{"kind":"preview","league":"nfl","espn_id":"<espn_id>","reason":"manual"}'`
- [ ] The workflow finishes `ready` (or `no_sources` / `failed`, with its reason in the UI), and
      `GET /api/games/<id>/ai` returns it.
- [ ] `docker compose exec worker python -m app.ai.usage --days 1` shows the calls, with no unreported ones.

## 5. If it goes wrong

- **Turn AI off, keep the code:** remove `GROQ_API_KEY` from `.env`, `docker compose up -d`. Every AI section shows its
  template or fallback text. Also set `AI_LEAGUES=` (empty) and rerun the schedules command: the `ai-*` schedules are
  deleted, the rest are untouched.
- **Do not revert the code under running games.** Once a game has passed the `ai-recap` patch point, its history holds
  a patch marker; code without that `workflow.patched` call fails replay for it (non-determinism). Use the off switch.
- A capped text (failed 3 times for the same inputs): lift one with the manual start above, or all with
  `UPDATE ai_texts SET rejections = 0 WHERE rejections > 0;` (`docker compose exec db psql -U scores`).

## 6. First week: what to read, and what it decides

Run once a day from the first scheduled previews: `docker compose exec worker python -m app.ai.usage --days 7`.
`ai_calls` is kept 14 days, so the week survives.

| Read | Why |
|---|---|
| "Last 24 hours against each budget": `gpt-oss-120b` tokens against 200,000; OpenRouter requests against 50 | The writer's budget is the scarce one. How close a Saturday gets decides whether a spend-aware batch (held) is worth building. |
| "peak 60 s reserved" against 8,000 | The writer's minute. Persistent peaks at the limit mean texts queue behind each other. |
| "unreported calls" | Calls that failed or never reported usage stay counted at their reservation. A growing number is budget lost to errors. |
| "tokens per finished text" | The first real per-text cost (all kinds mixed; per kind needs a `kind` on `ai_calls`). |
| Rejections: the share of texts whose latest outcome is a rejection, and the top reasons | Qwen's false-rejection rate in production. This decides whether storing the unchecked draft (migration 010, held) pays. |
| Worker log lines `rejected N times for these inputs` | Texts the cap is holding. |

Suggested triggers (a proposal for Adam, not agreed): build the spend-aware batch if the writer passes about 60% of its
daily budget on a normal game day; reconsider the draft migration if more than about 1 in 6 finished texts are
rejections and most of those cite the fact check. Adjust once there are numbers.
