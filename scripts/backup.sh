#!/usr/bin/env bash
# Nightly: dump every database (app + Temporal), compress, upload to Object Storage.
# Cron (10:30 UTC = 3:30 AM Pacific): 30 10 * * * /home/ubuntu/scores-app/scripts/backup.sh >> /home/ubuntu/backup.log 2>&1
# ~/.backup_url is a write-only pre-authenticated request for the scores-backups bucket (ends in /o/).
set -euo pipefail
umask 077                        # the dump holds everything; keep the temp file private
cd ~/scores-app
STAMP=$(date -u +%Y%m%dT%H%MZ)
FILE=/tmp/scores-$STAMP.sql.gz
trap 'rm -f "$FILE"' EXIT        # no stray dumps in /tmp when a step fails
docker compose exec -T db pg_dumpall -U scores | gzip > "$FILE"
curl -fsS -T "$FILE" "$(cat ~/.backup_url)scores-$STAMP.sql.gz"
echo "$(date -u +%FT%TZ) uploaded scores-$STAMP.sql.gz ($(stat -c %s "$FILE") bytes)"
