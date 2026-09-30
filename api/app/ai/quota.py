"""Free-tier quota shared by every process through Postgres (spec: "Sharing quota between the API and the worker").

The api (writing on open) and the worker (writing ahead) call the same Groq models. Pacing in memory, each would
think it had a model's whole minute to itself. Here every call reserves its tokens in `ai_calls` under a
per-model advisory lock, so two processes can never both take a model's last slot, and a 429's cool-down in
`ai_cooling` is seen by both. Turned on with AI_QUOTA=db (docker-compose sets it for api and worker).
"""
from __future__ import annotations

import time

from .. import db
from . import client

KEEP = "2 days"      # ai_calls rows older than this are pruned: they only feed the per-minute sum and daily totals


class DbQuota:
    def reserve(self, models: list[str], cost: int, limit: int, wait: bool) -> tuple[str, int] | None:
        while True:
            soonest = None           # seconds until the first usable model's oldest reservation leaves its minute
            any_usable = False
            for m in models:
                with db.connect() as conn:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ai-quota:{m}",))
                    cooling = conn.execute("SELECT 1 FROM ai_cooling WHERE model = %s AND until > now()",
                                           (m,)).fetchone()
                    if cooling:
                        continue
                    any_usable = True
                    row = conn.execute("""
                        SELECT coalesce(sum(reserved), 0) AS held, count(*) AS n,
                               extract(epoch FROM (min(at) + interval '60 seconds' - now())) AS frees_in
                        FROM ai_calls WHERE model = %s AND at > now() - interval '60 seconds'""", (m,)).fetchone()
                    if row["n"] == 0 or row["held"] + cost <= limit:
                        rid = conn.execute("INSERT INTO ai_calls (model, reserved) VALUES (%s, %s) RETURNING id",
                                           (m, cost)).fetchone()["id"]
                        if rid % 200 == 0:
                            conn.execute(f"DELETE FROM ai_calls WHERE at < now() - interval '{KEEP}'")
                        return m, rid
                    if soonest is None:
                        soonest = max(float(row["frees_in"] or 0), 0.0) + 0.1
            if not any_usable or not wait:
                return None
            client.paced_seconds += soonest
            time.sleep(soonest)

    def cool(self, model_name: str, seconds: float) -> None:
        with db.connect() as conn:
            conn.execute("""
                INSERT INTO ai_cooling (model, until) VALUES (%s, now() + make_interval(secs => %s))
                ON CONFLICT (model) DO UPDATE SET until = GREATEST(ai_cooling.until, EXCLUDED.until)""",
                         (model_name, seconds))

    def is_cooling(self, model_name: str) -> bool:
        with db.connect() as conn:
            return conn.execute("SELECT 1 FROM ai_cooling WHERE model = %s AND until > now()",
                                (model_name,)).fetchone() is not None

    def cooling_left(self, models: list[str]) -> float | None:
        """Seconds until the first of `models` stops cooling down; None if one of them isn't cooling."""
        with db.connect() as conn:
            rows = conn.execute("""
                SELECT m, extract(epoch FROM (c.until - now())) AS left FROM unnest(%s::text[]) AS m
                LEFT JOIN ai_cooling c ON c.model = m""", (models,)).fetchall()
        left = [float(r["left"]) if r["left"] is not None else 0.0 for r in rows]
        return None if not left or min(left) <= 0 else min(left)

    def used(self, handle: object, tokens: int) -> None:
        if handle is not None:
            with db.connect() as conn:
                conn.execute("UPDATE ai_calls SET used = %s WHERE id = %s", (tokens, handle))


def install() -> None:
    """Share quota through Postgres from now on (api and worker startup, when AI_QUOTA=db)."""
    client.quota = DbQuota()
