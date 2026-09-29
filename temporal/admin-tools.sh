#!/bin/sh
# temporal-admin-tools entrypoint: set up (or upgrade) the temporal and temporal_visibility schemas in the app's
# Postgres, mark the container healthy so the server starts, create the default namespace, then stay up so the
# runbook's `temporal` commands can run here. Safe on every start: nothing is recreated.
set -eu
SQL="temporal-sql-tool --plugin postgres12 --ep ${POSTGRES_SEEDS} -p ${DB_PORT:-5432} -u ${POSTGRES_USER} --pw ${POSTGRES_PWD}"
SCHEMA=/etc/temporal/schema/postgresql/v12
rm -f /tmp/schema-ready

until nc -z -w 5 "${POSTGRES_SEEDS}" "${DB_PORT:-5432}"; do echo "waiting for Postgres"; sleep 2; done

for pair in temporal:temporal temporal_visibility:visibility; do
  db=${pair%%:*}; dir=${pair##*:}
  $SQL --db "$db" create 2>/dev/null && echo "created database $db" || echo "database $db exists"
  # A new database has no schema_version table: update-schema fails, so set up from version 0 first.
  if ! $SQL --db "$db" update-schema -d "$SCHEMA/$dir/versioned"; then
    $SQL --db "$db" setup-schema -v 0.0
    $SQL --db "$db" update-schema -d "$SCHEMA/$dir/versioned"
  fi
done
touch /tmp/schema-ready
echo "schemas ready"

export TEMPORAL_ADDRESS=${TEMPORAL_ADDRESS:-temporal:7233}
until temporal operator cluster health >/dev/null 2>&1; do echo "waiting for the Temporal server"; sleep 5; done
temporal operator namespace describe -n default >/dev/null 2>&1 \
  || temporal operator namespace create -n default --retention 30d
echo "namespace default ready"
exec sleep infinity
