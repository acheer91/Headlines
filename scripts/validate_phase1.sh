#!/usr/bin/env bash
# Phase 1 automated checks (steps 1.1-1.7 and 1.9 in the PRD). Run from the repo root
# after `docker compose up -d --build`. Steps 1.8 and 1.10-1.12 are manual (see PRD).
set -u
API=${API:-http://localhost:8000}
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
# Windows ships a python3 stub that only opens the Store, so find one that actually runs.
PY=$(for p in python3 python py; do "$p" -c "" >/dev/null 2>&1 && { echo "$p"; break; }; done)
[ -n "$PY" ] || { echo "Need Python on PATH to parse JSON"; exit 2; }
json() { "$PY" -c "import json,sys; b=json.load(sys.stdin); print($1)"; }

echo "1.1 Stack is up"
check "db container healthy" "docker compose ps db | grep -q healthy"
check "api /health returns ok" "curl -fsS $API/api/health | grep -q '\"ok\":true'"

echo "1.2 Schema applied"
TABLES=$(docker compose exec -T db psql -U scores -d scores -tAc "select count(*) from information_schema.tables where table_name in ('teams','games','odds_snapshots','fetch_log')" 2>/dev/null | tr -d '[:space:]')
[ "$TABLES" = "4" ] && ok "4 tables present" || bad "expected 4 tables, got '$TABLES'"

echo "1.3 ESPN adapter parses every live event"
if docker compose exec -T api python -m app.check_espn > /tmp/check_espn.txt 2>&1; then
  ok "$(head -1 /tmp/check_espn.txt)"
else
  bad "check_espn failed (see /tmp/check_espn.txt)"; tail -5 /tmp/check_espn.txt
fi

echo "1.4 Scoreboard API"
SB=$(curl -fsS "$API/api/scoreboard/nfl?force=true")
N=$(echo "$SB" | json "len(b['games'])")
[ "${N:-0}" -gt 0 ] && ok "$N games for week $(echo "$SB" | json "b['week']")" || bad "no games returned"
[ "$(echo "$SB" | json "b['stale']")" = "False" ] && ok "fresh data (stale=false)" || bad "served stale data"
STORED=$(docker compose exec -T db psql -U scores -d scores -tAc "select count(*) from games where league='nfl'" | tr -d '[:space:]')
[ "${STORED:-0}" -ge "${N:-1}" ] && ok "$STORED games stored in Postgres" || bad "games not stored"

echo "1.5 Lines captured"
PRE=$(echo "$SB" | json "sum(1 for g in b['games'] if g['state']=='pre')")
PRE_LINE=$(echo "$SB" | json "sum(1 for g in b['games'] if g['state']=='pre' and g['line'])")
if [ "${PRE:-0}" = "0" ]; then echo "  SKIP  no upcoming games right now"
elif [ "$PRE_LINE" = "$PRE" ]; then ok "$PRE_LINE/$PRE upcoming games have a line"
else bad "only $PRE_LINE/$PRE upcoming games have a line (check which in the app)"; fi
SNAPS=$(docker compose exec -T db psql -U scores -d scores -tAc "select count(*) from odds_snapshots" | tr -d '[:space:]')
[ "${SNAPS:-0}" -gt 0 ] && ok "$SNAPS odds snapshots stored" || bad "no odds snapshots"

echo "1.6 Ordering: favorites, then live, upcoming, final"
ORDER_OK=$(echo "$SB" | json "(lambda k: k == sorted(k))([(0 if g['favorite'] else 1, {'in':0,'pre':1,'post':2}[g['state']]) for g in b['games']])")
[ "$ORDER_OK" = "True" ] && ok "cards in the right order" || bad "cards out of order"

echo "1.7 Cache: two pulls inside 30s make one ESPN call"
# Age the current-week cache first, so the first pull must refetch and the second must not:
# exactly 1 call proves both halves (a check that allows 0 passes even if the cache never expires).
docker compose exec -T db psql -U scores -d scores -qc \
  "update fetch_log set fetched_at = fetched_at - interval '5 minutes' where league = 'nfl' and requested_week is null" >/dev/null
before=$(docker compose exec -T db psql -U scores -d scores -tAc "select count(*) from fetch_log" | tr -d '[:space:]')
curl -fsS "$API/api/scoreboard/nfl" >/dev/null; curl -fsS "$API/api/scoreboard/nfl" >/dev/null
after=$(docker compose exec -T db psql -U scores -d scores -tAc "select count(*) from fetch_log" | tr -d '[:space:]')
[ $((after - before)) -eq 1 ] && ok "expired cache: 1 ESPN call for 2 pulls" || bad "$((after - before)) ESPN calls for 2 pulls (want exactly 1)"

echo "1.9 Tap routing"
GID=$(echo "$SB" | json "b['games'][0]['id']")
SCREEN=$(curl -fsS "$API/api/games/$GID" | json "b['screen']")
[ -n "$SCREEN" ] && ok "game $GID routes to screen $SCREEN" || bad "game endpoint failed"

echo "PWA assets"
check "manifest served" "curl -fsS $API/manifest.webmanifest | grep -q standalone"
check "service worker served" "curl -fsS -o /dev/null $API/sw.js"
check "512px icon served" "curl -fsS -o /dev/null $API/icon-512.png"

echo
echo "Automated: $PASS passed, $FAIL failed. Now do the manual steps (1.8, 1.10-1.12)."
[ "$FAIL" = "0" ]
