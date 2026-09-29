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
- **Live-score backup (backup.py):** display-only, scores and status of live games, only when an ESPN refresh fails.
  Never stored, never graded, never used for lines. No MLS.

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
4. AI text (Claude Haiku-class): extract facts, then write in house voice. 8-day article rule is hard.
5. 5a NCAAF built (see the top). 5b: NBA, EPL, MLS (scores only).
   Needs a date-window query: NBA and soccer have no weeks.
6. Deploy to Oracle Cloud always-free, Tailscale only.
