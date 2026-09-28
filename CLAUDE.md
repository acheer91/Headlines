# CLAUDE.md — Scores App

Personal, pull-based scores app (ESPN-app replacement). Full PRD: "Scores App — Mini PRD".
This file is the working brief for Claude Code. Keep it current as phases land.

## Current phase: 2 — Game screens and bet grading (built 2026-09-27; Sunday checks pending)
Scope: C1 pre-game, C2 live, D post-game from the ESPN game summary; ML / spread / O/U grading; summary cache.
Out of scope: AI text (spots ship as "Coming in Phase 4" placeholders), Temporal, other leagues.
Status: `validate_phase2.sh` 14/14, 124 tests. Self-review fixed 10 issues; an independent review found 10 more, its
re-check found a regression + 5 small items; all fixed and verified by the reviewer (verdict: ready apart from 2.10/2.11).
Known residual (accepted): if ESPN drops the event list AND the season/week fields in one change, the board is served as
fresh with stored cards (needs two simultaneous format changes). Bug bash (2026-09-27) fixed 8 issues across Phases 1 and 2. Steps 2.4-2.6 checked against espn.com on 2026-09-27
(3 upcoming, a live game at halftime, 3 finals). Still to do on a real Sunday: 2.10 (one game
C1 -> C2 -> D on pulls) and 2.11 on the phone; 2.7's table is for Adam to hand-check.

Phase 1 (NFL scoreboard) done 2026-09-27: installed on the phone via `tailscale serve --bg 8000`
at https://technologic.tailca897c.ts.net (tailnet only).

## Stack
- `api/` FastAPI + psycopg 3, plain SQL (no ORM). Python 3.12.
- `web/` React 18 + Vite + vite-plugin-pwa, TypeScript. Built into `web/dist` and served by the API (one origin).
- `db/migrations/` plain SQL, applied only by the API at startup (`app/migrate.py`, tracked in
  `schema_migrations`). Every file must be idempotent: `tests/test_migrations.py` applies each twice.
- `docker-compose.yml` db + api. Phase 3 adds temporal, temporal-ui, worker.

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
  summary; INTs from ESPN's core team leaders (`games.start_team_season`, cached in `team_season_stats`).
  C2 and D show this game's Passing, Rushing, Receiving.
- **Weeks are per season type.** Preseason (1), regular season (2) and postseason (3) each start at week 1; every
  week query filters on `games.season_type`.
- **Only completed games are graded.** Canceled/postponed games are `state = 'post'` with `completed = false`
  (usually 0-0); they show "Not graded · Canceled" and any stored results are removed.
- **Flexed kickoffs:** `time_valid = false` means `start_time` is ESPN's placeholder; show the date and "time TBD".
- **Nothing moves a game backwards** (completed final -> live, live -> pre): not a lagging summary, not a lagging
  scoreboard. When the stored summary trails the game's state, the game screens withhold its box-score blocks
  (`summary_behind`) and refetch at most every 30 s.
- **Unreadable ESPN data is not fresh data:** if no game in a scoreboard response parses, the refresh counts as failed
  (stale banner); if some don't, the board shows a warning.
- **Lines are append-only.** `odds_snapshots` is never updated; a row is added only when the line changes.
- **Spread convention:** `home_spread` is from the home team's side, negative = home favored. The UI shows the favorite ("SF -2.5").
- **No custom User-Agent on site.api.espn.com.** Akamai returns 403 for it; httpx's default works.
- **Soccer never gets odds.** `BETTING_LEAGUES` in `espn.py`.
- **Bet status reports, never advises.** "KC -3 covering by 4", never "take the over".
- **No new signups or paid services** without asking Adam.
- Favorites live in `config/favorites.json` (`{"nfl": ["SEA"]}` style, team abbreviations).
- **Top fantasy performer** (Adam, 2026-09-27): per team, the highest half-PPR scorer in this game's player box
  score (`summary.fantasy_top`), shown in the bets card on C2 ("so far") and D. Offense only: kicker scoring needs
  field-goal distances the box score lacks, and two-point conversions aren't in it, so neither is counted.

## Latency (2026-09-27 pass)
- **Web app paints last-seen data instantly** (`web/src/lastSeen.ts`, localStorage, per device) and pulls fresh data
  over it; a game page opens with the tapped card while its detail loads. Every open still pulls.
- **ESPN client** (`espn._get_json`): one shared keep-alive client, 2 attempts 0.25 s apart, 2 s connect / 4 s read
  timeouts, and a per-host "down" switch: after an *outage* (connection error, timeout, 5xx, 403, 429), skip that
  host for 30 s and serve stored data at once. A 404/400 is one bad request: not retried, doesn't trip the switch.
  Batch commands (`grade_week`, `check_summary`) turn the switch off.
- **C1 team season stats** (ranks + INTs leader) fetch in parallel with each other and with the summary
  (`games.start_team_season` / `finish_team_season`); cached in `team_season_stats` for 1 h, 10 min while a final
  is under 6 h old, and refetched whenever another game has finished (ranks depend on all 32 teams).
- **Postgres pool** (`psycopg_pool`, no prepared statements, 5 s wait so a down database fails fast); game pages read only the summary blocks they use
  (`db.get_summary_view`). **GZip** on responses; hashed `/assets` cached for a year, `index.html`/`sw.js` no-cache.

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
docker compose exec api python -m app.check_espn [--week N]        # live scoreboard parse check
docker compose exec api python -m app.check_summary [--event ID]   # live summary blocks, one game per state
docker compose exec api python -m app.grade_week --week N [--season-type 3]   # grade a week, print a hand-check table
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
api/app/main.py           /api/health, /api/scoreboard/{league}, /api/games/{id}, serves web/dist
api/app/migrate.py        applies db/migrations at startup
api/app/check_espn.py     validation CLI (scoreboard)
api/app/check_summary.py  validation CLI (summary, step 2.1)
api/app/grade_week.py     grade a week, print the hand-check table (step 2.7)
api/tests/                parser, summary, grading and cache-rule tests (fixtures: real ESPN payloads), API tests
                          (real Postgres), static-serving tests (need web/dist)
db/migrations/            001_init.sql (Phase 1), 002_game_details.sql (game_summaries, bet_results),
                          003_season_type_and_status.sql (season_type, completed, time_valid),
                          004_team_season_stats.sql (per-team cache: ranks + INTs leader)
docs/                     espn_summary_fields.md (field map)
web/src/                  Scoreboard.tsx (B), GamePage.tsx (C1 / C2 / D), GameCard.tsx, usePull.tsx (pull to refresh),
                          lastSeen.ts (instant paint from the last response)
```

## Next phases (don't start without Adam's go-ahead)
3. Temporal: ScheduleSync, GameWorkflow (one per game, ID = league + ESPN id; saves the line early, grades on final), Headlines.
4. AI text (Claude Haiku-class): extract facts, then write in house voice. 8-day article rule is hard.
5. NCAAF (major conferences + Notre Dame, no FCS, plus favorites), NBA, EPL, MLS (scores only).
   Needs a date-window query: NBA and soccer have no weeks.
6. Deploy to Oracle Cloud always-free, Tailscale only.
