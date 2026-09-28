#!/usr/bin/env bash
# Phase 2 automated checks (steps 2.1-2.3, 2.7-2.9, 2.11 in the Phase 2 handoff). Run from the repo
# root after `docker compose up -d --build`. Steps 2.4-2.6, 2.10 and the phone half of 2.11 are manual;
# 2.7 also needs the printed table hand-checked.
set -u
API=${API:-http://localhost:8000}
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
# Windows ships a python3 stub that only opens the Store, so find one that actually runs.
PY=$(for p in python3 python py; do "$p" -c "" >/dev/null 2>&1 && { echo "$p"; break; }; done)
[ -n "$PY" ] || { echo "Need Python on PATH to parse JSON"; exit 2; }
json() { "$PY" -c "import json,sys; b=json.load(sys.stdin); print($1)"; }
sql()  { docker compose exec -T db psql -U scores -d scores -tAc "$1" | tr -d '[:space:]'; }
OUT=${TMPDIR:-/tmp}

echo "2.1 ESPN summary data"
if docker compose exec -T api python -m app.check_summary > "$OUT/check_summary.txt" 2>&1; then
  ok "$(tail -1 "$OUT/check_summary.txt")"
else
  bad "check_summary failed (see $OUT/check_summary.txt)"; tail -15 "$OUT/check_summary.txt"
fi
[ -s docs/espn_summary_fields.md ] && ok "field map written (docs/espn_summary_fields.md)" || bad "docs/espn_summary_fields.md missing"

echo "2.2 Schema"
[ "$(sql "select count(*) from information_schema.tables where table_name in ('game_summaries','bet_results')")" = "2" ] \
  && ok "game_summaries and bet_results exist" || bad "Phase 2 tables missing"
[ "$(sql "select count(*) from schema_migrations where name = '002_game_details.sql'")" = "1" ] \
  && ok "migration 002 recorded" || bad "migration 002 not recorded in schema_migrations"

echo "2.3 Grading math (plus parser and API tests against a throwaway database)"
if docker compose exec -T api python -m pytest -q -p no:cacheprovider tests/test_grading.py > "$OUT/grading_tests.txt" 2>&1; then
  ok "grading table: $(tail -1 "$OUT/grading_tests.txt")"
else
  bad "grading tests failed"; tail -20 "$OUT/grading_tests.txt"
fi
[ "$(sql "select count(*) from pg_database where datname = 'scores_test'")" = "1" ] || docker compose exec -T db createdb -U scores scores_test
if docker compose exec -T api sh -c 'TEST_DATABASE_URL="${DATABASE_URL%/*}/scores_test" python -m pytest -q -p no:cacheprovider' > "$OUT/all_tests.txt" 2>&1; then
  ok "full suite: $(tail -1 "$OUT/all_tests.txt")"
else
  bad "test suite failed (see $OUT/all_tests.txt)"; grep -E "FAILED|Error" "$OUT/all_tests.txt" | head -10
fi

echo "2.7 Last week graded"
CUR=$(curl -fsS "$API/api/scoreboard/nfl" | json "b['week']")
LAST=$((CUR - 1))
if [ "$LAST" -lt 1 ]; then
  echo "  SKIP  week $CUR is the first week of this season type: no last week to grade"
elif docker compose exec -T api python -m app.grade_week --week "$LAST" > "$OUT/grade_week.txt" 2>&1; then
  ok "week $LAST: $(tail -1 "$OUT/grade_week.txt")"
else
  bad "week $LAST: $(tail -1 "$OUT/grade_week.txt")"
fi
echo "        hand-check table: $OUT/grade_week.txt"

echo "2.9 Regrade"
# A graded final from last week (not the current week, so no scoreboard pull refreshes it mid-check),
# in the current season and season type.
read SEASON STYPE <<<"$(docker compose exec -T db psql -U scores -d scores -tAF' ' -c "select season, season_type from fetch_log where ok and requested_week is null order by fetched_at desc limit 1" | tr -d '
')"
GID=$(sql "select g.id from games g join bet_results r on r.game_id = g.id where g.state = 'post' and g.completed and g.week = $LAST and g.season = ${SEASON:-0} and g.season_type = ${STYPE:-0} group by g.id having count(*) = 3 order by g.id limit 1")
if [ -z "$GID" ]; then
  bad "no graded final to regrade"
else
  BEFORE=$(sql "select string_agg(market||':'||outcome||':'||margin, ',' order by market) from bet_results where game_id = $GID")
  ORIG=$(sql "select home_score from games where id = $GID")
  restore() { docker compose exec -T db psql -U scores -d scores -qc "update games set home_score = $ORIG where id = $GID" >/dev/null; curl -fsS "$API/api/games/$GID" >/dev/null; }
  trap restore EXIT
  docker compose exec -T db psql -U scores -d scores -qc "update games set home_score = home_score + 7 where id = $GID" >/dev/null
  curl -fsS "$API/api/games/$GID" >/dev/null
  ROWS=$(sql "select count(*) from bet_results where game_id = $GID")
  GRADED=$(sql "select min(home_score) from bet_results where game_id = $GID")
  AFTER=$(sql "select string_agg(market||':'||outcome||':'||margin, ',' order by market) from bet_results where game_id = $GID")
  [ "$ROWS" = "3" ] && ok "game $GID: still one row per market after regrade" || bad "game $GID: $ROWS rows after regrade"
  [ "$GRADED" = "$((ORIG + 7))" ] && [ "$AFTER" != "$BEFORE" ] && ok "result updated to the changed score" \
    || bad "result not regraded (graded on $GRADED, want $((ORIG + 7)))"
  restore; trap - EXIT
  [ "$(sql "select string_agg(market||':'||outcome||':'||margin, ',' order by market) from bet_results where game_id = $GID")" = "$BEFORE" ] \
    && ok "restored score regrades back to the original result" || bad "original result not restored"
fi

echo "2.11 Speed and cache"
FINAL=$(curl -fsS "$API/api/scoreboard/nfl" | json "next((g['id'] for g in b['games'] if g['state']=='post'), '')")
# Prefer a live game: that's where the 30-second summary cache applies (upcoming games use 10 minutes).
OPEN=$(curl -fsS "$API/api/scoreboard/nfl" | json "next((g['id'] for g in b['games'] if g['state']=='in'), next((g['id'] for g in b['games'] if g['state']=='pre'), ''))")
OPEN_STATE=$(curl -fsS "$API/api/scoreboard/nfl" | json "next((g['state'] for g in b['games'] if g['id']==int('${OPEN:-0}')), '')")
for id in $FINAL $OPEN; do
  curl -fsS -o /dev/null "$API/api/games/$id"                       # warm: a first open may fetch ESPN
  T=$(curl -fsS -o /dev/null -w "%{time_total}" "$API/api/games/$id")
  "$PY" -c "import sys; sys.exit(0 if float('$T') < 2 else 1)" && ok "game $id served in ${T}s" || bad "game $id took ${T}s"
done
if [ -n "$OPEN" ]; then
  # Start from a just-fetched summary, so the two pulls below sit well inside the cache window.
  AGE=$(sql "select floor(extract(epoch from now() - fetched_at)) from game_summaries where game_id = $OPEN")
  if [ -z "$AGE" ] || [ "$AGE" -ge 15 ]; then
    # No summary yet: the pull below fetches one, no wait. Otherwise wait out the 30 s window (live only).
    WAIT=0; [ -n "$AGE" ] && [ "$OPEN_STATE" = "in" ] && WAIT=$((31 - AGE))
    [ "$WAIT" -gt 0 ] && sleep "$WAIT"
    curl -fsS -o /dev/null "$API/api/games/$OPEN"
  fi
  A=$(sql "select fetched_at from game_summaries where game_id = $OPEN")
  curl -fsS -o /dev/null "$API/api/games/$OPEN"; curl -fsS -o /dev/null "$API/api/games/$OPEN"
  B=$(sql "select fetched_at from game_summaries where game_id = $OPEN")
  if [ "$OPEN_STATE" = "in" ]; then RULE="live game: 30 s rule"; else RULE="no live game now, upcoming game: 10 min rule"; fi
  [ "$A" = "$B" ] && ok "two quick pulls reuse the stored summary ($RULE)" || bad "summary refetched on a quick second pull ($RULE)"
fi
if [ -n "$FINAL" ]; then
  A=$(sql "select fetched_at from game_summaries where game_id = $FINAL")
  curl -fsS -o /dev/null "$API/api/games/$FINAL"
  B=$(sql "select fetched_at from game_summaries where game_id = $FINAL")
  [ -n "$A" ] && [ "$A" = "$B" ] && ok "final served from Postgres without refetching" || bad "final summary refetched"
fi

echo
echo "Automated: $PASS passed, $FAIL failed. Now hand-check $OUT/grade_week.txt and do 2.4-2.6, 2.10, 2.11 on the phone."
[ "$FAIL" = "0" ]
