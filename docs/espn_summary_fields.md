# ESPN game summary: field map (Phase 2, step 2.1)

Checked live on 2026-09-27 against three NFL games: **pre** PHI @ CHI (401872963), **in** LAR @ DEN
(401872962, 2nd quarter), **post** LAC @ BUF (401872953). Re-run any time with
`docker compose exec api python -m app.check_summary` (or `--event <id>`).

**Endpoint:** `GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=<ESPN event id>`
One call per game gives every block below. Send httpx's default User-Agent; a custom one gets 403
from Akamai (see CLAUDE.md). Size: about 125 KB pre, 330 KB live, 630 KB final (`drives` is most
of it). We store the raw payload in `game_summaries.payload` and parse it on read in `api/app/summary.py`.

## What each screen reads

✓ = present in that state, – = absent. Paths are relative to the summary JSON.

### Header (all screens)

| Field | Path | pre | in | post | Notes |
| --- | --- | --- | --- | --- | --- |
| Event id | `header.id` | ✓ | ✓ | ✓ | Checked against the game's id before saving |
| State | `header.competitions[0].status.type.state` | ✓ | ✓ | ✓ | `pre` / `in` / `post`; moves the game forward on a game-page pull |
| Status text | `…status.type.shortDetail` | ✓ | ✓ | ✓ | "9/28 - 8:15 PM EDT", "3:07 - 2nd", "Final" |
| Period, clock | `…status.period`, `…status.displayClock` | – | ✓ | – | |
| Team | `…competitors[i].team.abbreviation`, `.displayName`, `.logos[0].href` | ✓ | ✓ | ✓ | Side from `competitors[i].homeAway` |
| Score | `…competitors[i].score` | – | ✓ | ✓ | String; absent before kickoff |
| Record | `…competitors[i].record[type=total].summary` | ✓ | ✓ | ✓ | Also `type=home` / `type=road` for the venue record |
| Score by quarter | `…competitors[i].linescores[n].displayValue` | – | ✓ | ✓ | One entry per period played; OT adds a 5th |
| Possession | `…competitors[i].possession` | ✓ | ✓ | ✓ | Boolean; only meaningful live |
| Down and distance | `drives.current.plays[-1].end.downDistanceText` | – | ✓ | – | "3rd & 8 at DEN 40". `situation` at the top level is always null for NFL |
| Venue | `gameInfo.venue.fullName` | ✓ | ✓ | ✓ | |

### Team stats

`boxscore.teams[i].statistics[]` with `name`, `displayValue`, `label`; side from `boxscore.teams[i].homeAway`.

| Screen | ESPN sends | We show |
| --- | --- | --- |
| C1 (pre) | 8 season averages: `totalPointsPerGame`, `totalPointsPerGameAllowed`, `yardsPerGame`, `yardsPerGameAllowed`, `passingYardsPerGame`, `rushingYardsPerGame`, `passingYardsPerGameAllowed`, `rushingYardsPerGameAllowed` | Points and yards per game, for and against |
| C2, D (in, post) | 25 game stats | `totalYards`, `netPassingYards`, `rushingYards`, `turnovers`, `thirdDownEff` ("4-8", we add the %), `possessionTime`, `totalPenaltiesYards` ("4-40") |

ESPN repeats `interceptions` twice in the game list; the parser takes the first.

### Leaders

`leaders[i].team.abbreviation` → `leaders[i].leaders[]` (categories by `name`) → `.leaders[0].athlete.displayName` and `.leaders[0].displayValue`.

| State | Meaning | Categories |
| --- | --- | --- |
| pre | **Season** leaders | `passingYards`, `rushingYards`, `receivingYards`, `sacks`, `totalTackles`. C1 shows Passing, Rushing, Receiving, Tackles + INTs (INTs from the team endpoint below; sacks unused) |
| in, post | **This game's** leaders | same five; D and C2 show passing, rushing, receiving |

### Injuries (C1)

`injuries[i].team.abbreviation` → `injuries[i].injuries[]` with `athlete.displayName`,
`athlete.position.abbreviation`, `status` ("Out", "Questionable", "Doubtful", "Injured Reserve"),
`details.type` (body part, e.g. "Hamstring").

- **At most 5 per team.** Every game checked returned exactly 5 per team. This is the list ESPN's own
  game page shows, not the full team report. The league-wide `/injuries` endpoint is 8.7 MB and mostly
  "Active" players, so it isn't used.
- On game day the list fills with inactives ("Out", detail "Coach's Decision").

### Lines (grading and C1 fallback)

`pickcenter[]`, one entry per sportsbook (only Draft Kings, `provider.priority` 1, seen so far).

| Field | Path | Notes |
| --- | --- | --- |
| Closing spread | `pickcenter[0].pointSpread.home.close.line` | Home side, signed: "-7", "+4.5" |
| Opening spread | `…pointSpread.home.open.line` | Shown to prove close ≠ open (BUF opened -2.5, closed -7) |
| Closing total | `pickcenter[0].total.over.close.line` | "o50.5" |
| Closing moneyline | `pickcenter[0].moneyline.home.close.odds`, `.away.close.odds` | "-345", "+275", maybe "EVEN" |
| Current line | `pickcenter[0].spread` (home side), `.overUnder`, `.homeTeamOdds.moneyLine`, `.awayTeamOdds.moneyLine`, `.details` ("PHI -4.5") | Before kickoff this equals `close` |

- Live and final games keep `close` (the line locked at kickoff) and have no live line in the summary.
  That makes ESPN's close the grading line when we never saved one before kickoff (last week's games).
- The scoreboard's `odds` block disappears once a game starts; the summary's `pickcenter` stays.
- The summary's own `odds` key is an empty list; ignore it.
- To confirm on the next game we save before kickoff: its last `pre` snapshot should match `close`.
  `grade_week` prints a note when they differ.

## Asked for in the handoff, not in the summary

These aren't in the game summary, so C1 doesn't show them yet (cut, not faked). They **are** in ESPN's
team-level endpoints (checked 2026-09-27 for CHI, team id 3); using them costs extra calls per game.

| Wanted | In the summary? | Team-level source |
| --- | --- | --- |
| Turnover margin (C1) | No | `sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{year}/types/2/teams/{id}/statistics` → `splits.categories[name=miscellaneous].stats[name=turnOverDifferential]` (also `totalTakeaways`, `totalGiveaways`). The site API's `/teams/{id}/statistics` has it too, without ranks. |
| League rank on team stats (C1) | No | **Used.** Offense: core `…/teams/{id}/statistics` → `splits.categories[].stats[name=totalPointsPerGame|yardsPerGame]` `rank`/`rankDisplayValue`. Allowed: site `…/nfl/teams/{id}/statistics?season=&seasontype=` → `results.opponent[].stats[name=totalPointsPerGame|yardsPerGame]` (1st = allowed the least; checked against espn.com's defense leaderboard). Shown only when the ranked `value` equals the summary's number. Note: `yardsPerGame` is gross yards; espn.com's offense leaderboard ranks net yards, so a yards rank can differ from that page by a place. Checked 2026-09-27: CHI 31.0 T-4th, PIT 11.5 32nd, CLE 16.5 T-25th match espn.com. |
| Interceptions leader (C1) | No | **Used.** `…/seasons/{year}/types/{type}/teams/{id}/leaders` → `categories[name=interceptions].leaders[0]` (`value`, `displayValue`); the athlete is a `$ref`, so naming him is one more call. Players on the same value are counted as ties. An empty category means no interceptions yet ("None this season"). Cached per team in `team_season_leaders` for 6 hours. Checked against espn.com team stats pages (PIT T.J. Watt, CLE M. Harden, PHI none) on 2026-09-27. |
| Full injury report | At most 5 per team (see above) | League `/injuries` (8.7 MB, mostly "Active"); not used. |

Turnover margin: not wanted (Adam, 2026-09-27). Ranks and the INTs leader are cached per team for 6 hours
(`team_season_stats`) and fetched in parallel, so a cold C1 open costs about two ESPN round trips.

## Scoreboard fields added in the bug bash (`api/app/espn.py`)

| Field | Path | Why |
| --- | --- | --- |
| Season type | `season.type` (1 pre, 2 regular, 3 post) | Each type restarts at week 1; games are keyed by season + type + week |
| Completed | `competitions[0].status.type.completed` | Canceled/postponed games are `state: post`, `completed: false`, 0-0 (e.g. BUF @ CIN 2023, `STATUS_CANCELED`); never graded |
| Kickoff set | `competitions[0].timeValid` | `false` for flexed games; `date` is then a placeholder (05:00Z = midnight Eastern), shown as "Sun, Jan 10 · time TBD" |

## Present but unused

`winprobability`, `predictor` (matchup predictor %), `againstTheSpread` (team ATS records),
`lastFiveGames`, `standings`, `scoringPlays`, `drives.previous` (play-by-play), `news`, `article`,
`videos`, `boxscore.players` (full player box score). Win probability and play-by-play stay off the
live screen (handoff decision). `news`/`article` are candidate inputs for Phase 4.
