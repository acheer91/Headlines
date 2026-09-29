#!/usr/bin/env bash
# Phase 3 automated checks (steps 3.1-3.8 and 3.11 in the Phase 3 handoff). Run from the repo root after
# `docker compose up -d --build` and `docker compose exec worker python -m app.temporal.schedules`.
# 3.7 takes ~20 minutes (a 10-minute ESPN outage, then recovery) and 3.8 ~4 minutes; SKIP_SLOW=1 skips both.
# 3.5/3.6 run the time-skipping tests, which need an x86 machine (the laptop, not the ARM server).
# 3.9, 3.10 and 3.12 are manual.
set -u
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
PY=$(for p in python3 python py; do "$p" -c "" >/dev/null 2>&1 && { echo "$p"; break; }; done)
[ -n "$PY" ] || { echo "Need Python on PATH to parse JSON"; exit 2; }
json() { "$PY" -c "import json,sys; b=json.load(sys.stdin); print($1)"; }
sql()  { docker compose exec -T db psql -U scores -d scores -tAc "$1" | tr -d '[:space:]'; }
tctl() { docker compose exec -T temporal-admin-tools temporal "$@"; }
failed() { tctl workflow count --query "ExecutionStatus='Failed'" -o json | json "b.get('count', 0)"; }
running_games() {   # "workflowId runId" per running GameWorkflow, sorted
  tctl workflow list --query "WorkflowType='GameWorkflow' AND ExecutionStatus='Running'" -o json --limit 1000 \
    | json "'\n'.join(sorted(w['execution']['workflowId']+' '+w['execution']['runId'] for w in b if w['execution']['workflowId'].startswith('nfl-')))"
}
sync_now() { tctl workflow execute --type ScheduleSyncWorkflow --task-queue scores --workflow-id "validate-sync-$(date +%s)" \
               --input '["nfl"]' -o json | json "b['result']"; }
OUT=${TMPDIR:-/tmp}
FAILED_AT_START=$(failed)

echo "3.1 Stack boots"
for s in db temporal temporal-admin-tools; do
  st=$(docker compose ps --format '{{.Health}}' "$s" 2>/dev/null)
  [ "$st" = "healthy" ] && ok "$s healthy" || bad "$s: '${st:-not running}'"
done
for s in temporal-ui worker api; do
  st=$(docker compose ps --format '{{.State}}' "$s" 2>/dev/null)
  [ "$st" = "running" ] && ok "$s running" || bad "$s: '${st:-not running}'"
done
curl -fsS -o /dev/null http://localhost:8080 && ok "Temporal UI answers on :8080" || bad "Temporal UI not answering on :8080"
POLLERS=$(tctl task-queue describe --task-queue scores | sed -n '/Pollers:/,$p' | grep -cE " (workflow|activity) ")
[ "${POLLERS:-0}" -ge 2 ] && ok "worker polling task queue scores (workflow and activity pollers)" \
  || bad "worker not polling task queue scores"

echo "3.2 Schedules exist"
BEFORE=$(docker compose exec -T worker python -m app.temporal.schedules --show 2>&1)
docker compose exec -T worker python -m app.temporal.schedules > "$OUT/schedules.txt" 2>&1
AFTER=$(docker compose exec -T worker python -m app.temporal.schedules --show 2>&1)
echo "$AFTER" | json "[(s['id'], s['time_zone'], [c['hour'] for c in s['calendars']]) for s in b]" > "$OUT/schedules_short.txt" 2>&1
grep -q "('schedule-sync', 'America/Los_Angeles', \[\[6\]\])" "$OUT/schedules_short.txt" \
  && ok "schedule-sync daily at 6:00 AM PT" || bad "schedule-sync wrong: $(cat "$OUT/schedules_short.txt")"
grep -q "('headlines', 'America/Los_Angeles', \[\[7, 17\]\])" "$OUT/schedules_short.txt" \
  && ok "headlines at 7:00 AM and 5:00 PM PT" || bad "headlines wrong: $(cat "$OUT/schedules_short.txt")"
[ "$BEFORE" = "$AFTER" ] && ok "rerunning the setup changes nothing" || { bad "schedules changed on rerun"; diff <(echo "$BEFORE") <(echo "$AFTER"); }

echo "3.3 One workflow per game"
N1=$(sync_now)
RUN1=$(running_games)
UPCOMING=$(sql "select string_agg(espn_id, ',' order by espn_id) from games where league = 'nfl' and state <> 'post' and start_time > now() - interval '7 days'")
MISSING=""
for id in ${UPCOMING//,/ }; do echo "$RUN1" | grep -q "^nfl-$id " || MISSING="$MISSING $id"; done
[ -n "$UPCOMING" ] && [ -z "$MISSING" ] && ok "every upcoming game has a running workflow ($(echo "${UPCOMING//,/ }" | wc -w) games, ScheduleSync ensured $N1)" \
  || bad "games without a running workflow:${MISSING:- (no upcoming games in the database)}"
DUPES=$(echo "$RUN1" | cut -d' ' -f1 | sort | uniq -d)
[ -z "$DUPES" ] && ok "no game has two running workflows" || bad "duplicate running workflows: $DUPES"
N2=$(sync_now)
RUN2=$(running_games)
[ "$RUN1" = "$RUN2" ] && ok "triggering ScheduleSync again added none (same workflows, same runs)" \
  || { bad "rerun changed the running workflows"; diff <(echo "$RUN1") <(echo "$RUN2") | head; }

echo "3.4 Lines saved unattended"
NOLINE=$(sql "select count(*) from games g where league = 'nfl' and state = 'pre' and start_time > now()
              and exists (select 1 from odds_snapshots s where s.game_id = g.id)
              and not exists (select 1 from odds_snapshots s where s.game_id = g.id and s.source = 'workflow')")
WITH=$(sql "select count(distinct g.id) from games g join odds_snapshots s on s.game_id = g.id and s.source = 'workflow'
            where g.league = 'nfl' and g.state = 'pre' and g.start_time > now()")
NONE=$(sql "select string_agg(espn_id, ' ') from games g where league = 'nfl' and state = 'pre' and start_time > now()
            and not exists (select 1 from odds_snapshots s where s.game_id = g.id)")
[ "$NOLINE" = "0" ] && [ "${WITH:-0}" -gt 0 ] && ok "$WITH upcoming games have a line captured by the worker (source 'workflow')" \
  || bad "$NOLINE upcoming games have lines only from pulls, $WITH from the worker"
[ -n "$NONE" ] && echo "  INFO  ESPN has no line yet for: $NONE"

echo "3.5/3.6 Game lifecycle and edge cases (time-skipping tests)"
if [ "$(uname -m)" = "aarch64" ] || [ "$(docker compose exec -T worker uname -m | tr -d '\r')" = "aarch64" ]; then
  echo "  SKIP  the time-skipping test server doesn't run on ARM Linux: run on the laptop"
elif docker compose exec -T worker python -m pytest -q -p no:cacheprovider tests/test_workflows.py > "$OUT/workflow_tests.txt" 2>&1; then
  ok "$(tail -1 "$OUT/workflow_tests.txt")"
else
  bad "workflow tests failed (see $OUT/workflow_tests.txt)"; grep -E "FAILED|Error" "$OUT/workflow_tests.txt" | head -10
fi

if [ "${SKIP_SLOW:-0}" = "1" ]; then
  echo "3.7/3.8 SKIPPED (SKIP_SLOW=1)"
else
  echo "3.7 ESPN outage (${OUTAGE_SECONDS:-600} s with ESPN pointed at a bad host)"
  WORKER_ESPN_BASE=https://invalid.example.com docker compose up -d worker >/dev/null 2>&1
  WID="validate-outage-$(date +%s)"
  tctl workflow start --type ScheduleSyncWorkflow --task-queue scores --workflow-id "$WID" --input '["nfl"]' >/dev/null
  sleep "${OUTAGE_SECONDS:-600}"
  ATTEMPT=$(tctl workflow describe -w "$WID" -o json | json "max([int(a.get('attempt', 1)) for a in b.get('pendingActivities') or []] or [0])")
  STATUS=$(tctl workflow describe -w "$WID" -o json | json "b['workflowExecutionInfo']['status']")
  [ "${ATTEMPT:-0}" -gt 1 ] && [ "$STATUS" = "WORKFLOW_EXECUTION_STATUS_RUNNING" ] \
    && ok "during the outage the activity kept retrying (attempt $ATTEMPT), workflow still running" \
    || bad "during the outage: status $STATUS, attempt ${ATTEMPT:-none}"
  docker compose up -d worker >/dev/null 2>&1                     # ESPN restored
  RESULT=$(tctl workflow result -w "$WID" -o json 2>&1 | json "b.get('result', b)" 2>/dev/null)
  STATUS=$(tctl workflow describe -w "$WID" -o json | json "b['workflowExecutionInfo']['status']")
  [ "$STATUS" = "WORKFLOW_EXECUTION_STATUS_COMPLETED" ] && ok "recovered once ESPN was back (ensured $RESULT games)" \
    || bad "after restoring ESPN: $STATUS"
  [ "$(failed)" = "$FAILED_AT_START" ] && ok "no workflow failed during the outage" || bad "failed workflows: $(failed) (was $FAILED_AT_START)"

  echo "3.8 Worker restart (worker down for 2 minutes while a timer is due)"
  # A canary GameWorkflow for a game that doesn't exist: its kickoff timer comes due while the worker is down.
  # Its activities fail (game not stored) and the workflow carries on, as designed; it is terminated at the end.
  CID="validate-canary-$(date +%s)"
  KICK=$("$PY" -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(seconds=70)).isoformat())")
  tctl workflow start --type GameWorkflow --task-queue scores --workflow-id "$CID" \
    --input "{\"league\":\"nfl\",\"espn_id\":\"canary\",\"start_iso\":\"$KICK\",\"preview_iso\":\"$KICK\"}" >/dev/null
  sleep 10
  RUN_BEFORE=$(running_games)
  docker compose stop worker >/dev/null 2>&1
  sleep 120
  docker compose start worker >/dev/null 2>&1
  sleep 60
  tctl workflow show -w "$CID" -o json > "$OUT/canary.json"
  read FIRED POLLED <<<"$("$PY" -c "
import json, sys
from datetime import datetime
ev = json.load(sys.stdin)['events']
kick = datetime.fromisoformat('$KICK')
t = lambda e: (datetime.fromisoformat(e['eventTime'].replace('Z', '+00:00')) - kick).total_seconds()
fired = [t(e) for e in ev if e.get('eventType') == 'EVENT_TYPE_TIMER_FIRED']
polls = [t(e) for e in ev if e.get('eventType') == 'EVENT_TYPE_ACTIVITY_TASK_SCHEDULED'
         and e['activityTaskScheduledEventAttributes']['activityType']['name'] == 'fetch_game_state']
print(round(min(fired, key=abs)) if fired else 'none', round(polls[0]) if polls else 'none')" < "$OUT/canary.json")"
  [ "$FIRED" != "none" ] && [ "${FIRED#-}" -le 5 ] 2>/dev/null && [ "$POLLED" != "none" ] \
    && ok "kickoff timer fired on time while the worker was down (${FIRED}s off); status poll ran ${POLLED}s after kickoff, once the worker was back" \
    || bad "kickoff timer: fired ${FIRED}s off, status poll at ${POLLED}s (see $OUT/canary.json)"
  tctl workflow terminate -w "$CID" --reason "validation canary" >/dev/null
  RUN_AFTER=$(running_games)
  [ "$RUN_BEFORE" = "$RUN_AFTER" ] && ok "every game workflow resumed (same runs after the restart)" \
    || { bad "running workflows changed across the restart"; diff <(echo "$RUN_BEFORE") <(echo "$RUN_AFTER") | head; }
  [ "$(failed)" = "$FAILED_AT_START" ] && ok "no workflow failed" || bad "failed workflows: $(failed) (was $FAILED_AT_START)"
  echo "        confirm in the UI (http://localhost:8080) that a game's pending timer still shows its original fire time"
fi

echo "3.11 Headlines stored"
A1=$(tctl workflow execute --type HeadlinesWorkflow --task-queue scores --workflow-id "validate-headlines-$(date +%s)" --input '["nfl"]' -o json | json "b['result']")
A2=$(tctl workflow execute --type HeadlinesWorkflow --task-queue scores --workflow-id "validate-headlines-$(date +%s)-2" --input '["nfl"]' -o json | json "b['result']")
TOTAL=$(sql "select count(*) from news_items where league = 'nfl'")
DUP=$(sql "select count(*) - count(distinct espn_id) from news_items where league = 'nfl'")
[ "${TOTAL:-0}" -gt 0 ] && [ "$DUP" = "0" ] && [ "$A2" = "0" ] \
  && ok "$TOTAL news items stored, no duplicates (runs added $A1, then $A2)" \
  || bad "news: total $TOTAL, duplicates $DUP, second run added $A2"
RUNS=$(tctl workflow count --query "WorkflowType='HeadlinesWorkflow' AND WorkflowId STARTS_WITH 'headlines-' AND ExecutionStatus='Completed'" -o json | json "b.get('count', 0)")
echo "  INFO  scheduled Headlines runs completed so far: $RUNS (two a day expected)"

echo
echo "Phase 3 automated: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
