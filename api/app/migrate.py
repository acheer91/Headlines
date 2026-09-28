"""Apply db/migrations/*.sql that this database hasn't recorded yet.

This is the only path that applies migrations (the api runs it at startup; Postgres's
docker-entrypoint-initdb.d is deliberately not used, since applying files from two places ran each
one twice on a new volume). Migrations are still written to be idempotent, so a partly applied or
re-run file is harmless; tests/test_migrations.py runs every file twice to prove it.

    python -m app.migrate
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(os.environ.get(
    "MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "db" / "migrations"))
_LOCK_ID = 7_243_001  # advisory lock so two processes never migrate at once


def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Returns the file names applied in this run."""
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations in {directory}")
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for f in files:
                if f.name in done:
                    continue
                with conn.transaction():
                    conn.execute(f.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                applied.append(f.name)
                log.info("applied migration %s", f.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied


if __name__ == "__main__":
    from .db import DATABASE_URL
    print("applied:", migrate(DATABASE_URL) or "nothing (up to date)")
