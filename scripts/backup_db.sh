#!/usr/bin/env bash
# Nightly Postgres backup (PRD, Hosting and ops: "nightly Postgres dump stored off the server").
# Dumps the database, checks the dump is readable, keeps the newest $BACKUP_KEEP on the server and
# uploads each one to Oracle Object Storage (always-free tier), keeping the newest $BACKUP_KEEP_REMOTE there.
# Object Storage auth is the server's instance principal: no keys or passwords live on the server.
# Run from the repo root or anywhere (cron does); exits non-zero if any step fails.
#
# Restore (into the running stack; stop the api first so nothing writes mid-restore):
#   docker compose stop api
#   docker compose exec -T db pg_restore -U scores -d scores --clean --if-exists < ~/backups/scores-<stamp>.dump
#   docker compose start api
# From Object Storage: ~/.local/oci-cli/bin/oci os object get --auth instance_principal \
#   --bucket-name scores-backups --name scores-<stamp>.dump --file scores-<stamp>.dump
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=${BACKUP_DIR:-$HOME/backups}
KEEP=${BACKUP_KEEP:-14}
KEEP_REMOTE=${BACKUP_KEEP_REMOTE:-30}
BUCKET=${BACKUP_BUCKET:-scores-backups}
OCI=${OCI_CLI:-$HOME/.local/oci-cli/bin/oci}

mkdir -p "$DIR"
name="scores-$(date -u +%Y%m%d-%H%M%S).dump"
docker compose exec -T db pg_dump -U scores -d scores -Fc > "$DIR/$name.part"
# A dump pg_restore can't list is not a backup.
docker compose exec -T db pg_restore -l < "$DIR/$name.part" > /dev/null
mv "$DIR/$name.part" "$DIR/$name"
echo "$(date -u +%FT%TZ) dumped $name ($(du -h "$DIR/$name" | cut -f1))"

ls -1t "$DIR"/scores-*.dump | tail -n +$((KEEP + 1)) | xargs -r rm --

"$OCI" os object put --auth instance_principal --bucket-name "$BUCKET" --name "$name" \
  --file "$DIR/$name" --force > /dev/null
echo "$(date -u +%FT%TZ) uploaded $name to bucket $BUCKET"

# Names sort by time, so everything before the newest $KEEP_REMOTE is old.
old=$("$OCI" os object list --auth instance_principal --bucket-name "$BUCKET" --prefix scores- --all \
  --query 'data[].name' --raw-output | python3 -c "import json,sys; n=sorted(json.load(sys.stdin) or []); print('\n'.join(n[:-$KEEP_REMOTE]))")
for o in $old; do
  "$OCI" os object delete --auth instance_principal --bucket-name "$BUCKET" --name "$o" --force
  echo "$(date -u +%FT%TZ) removed old remote $o"
done
