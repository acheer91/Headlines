"""ai_texts: one row per game and kind (headlines: one per run). Claiming a row is how two writers (the worker
writing ahead, a page open writing on demand) avoid writing the same text twice (handoff 2.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

ONE_LINER_TTL = timedelta(minutes=15)   # Adam, 2026-09-29: reused until the score changes or 15 minutes pass
WRITING_TIMEOUT = timedelta(minutes=2)  # a row stuck at 'writing' this long (a writer died) can be claimed again


def get(conn, game_id: int, kind: str) -> dict | None:
    return conn.execute("SELECT * FROM ai_texts WHERE game_id = %s AND kind = %s", (game_id, kind)).fetchone()


def current(row: dict | None, basis: str, fingerprint: str | None = None) -> bool:
    """Ready (or checked and sourceless) and written from what the game looks like now."""
    if not row or row["status"] not in ("ready", "no_sources") or row["basis"] != basis:
        return False
    if fingerprint is not None and row["fingerprint"] != fingerprint:
        return False
    if row["kind"] == "one_liner":
        return _age(row) < ONE_LINER_TTL
    return True


def _age(row: dict) -> timedelta:
    return datetime.now(timezone.utc) - row["updated_at"]


def claim(conn, game_id: int, kind: str, basis: str, fingerprint: str | None = None, reason: str = "") -> int | None:
    """The row's id if this caller should write it now; None if it's current or someone else is writing it."""
    row = conn.execute("""
        INSERT INTO ai_texts (game_id, kind, status, basis, fingerprint, reason, attempts)
        VALUES (%(g)s, %(k)s, 'writing', %(b)s, %(f)s, %(r)s, 1)
        ON CONFLICT (game_id, kind) WHERE game_id IS NOT NULL DO UPDATE
            SET status = 'writing', basis = EXCLUDED.basis, fingerprint = EXCLUDED.fingerprint,
                reason = EXCLUDED.reason, attempts = ai_texts.attempts + 1, updated_at = now()
            WHERE ai_texts.status = 'failed'
               OR (ai_texts.status IN ('ready', 'no_sources') AND (
                      ai_texts.basis IS DISTINCT FROM EXCLUDED.basis
                   OR ai_texts.fingerprint IS DISTINCT FROM EXCLUDED.fingerprint
                   OR (ai_texts.kind = 'one_liner' AND ai_texts.updated_at < now() - %(ttl)s)))
               OR (ai_texts.status = 'writing' AND ai_texts.updated_at < now() - %(stuck)s)
        RETURNING id""", {"g": game_id, "k": kind, "b": basis, "f": fingerprint, "r": reason,
                          "ttl": ONE_LINER_TTL, "stuck": WRITING_TIMEOUT}).fetchone()
    conn.commit()
    return row["id"] if row else None


def save(conn, row_id: int, result: dict) -> None:
    """Store a writer result (writer.write_*) on a claimed row."""
    conn.execute("""
        UPDATE ai_texts SET status = %s, body = %s, sources = %s, extract = %s, writer = %s, checker = %s,
                            last_error = %s, updated_at = now()
        WHERE id = %s""", (result["status"], Jsonb(result.get("body")), Jsonb(result.get("sources")),
                           Jsonb(result.get("extract")), result.get("model"), result.get("checker"),
                           result.get("reason"), row_id))
    conn.commit()


def save_headlines(conn, result: dict, reason: str) -> int:
    row = conn.execute("""
        INSERT INTO ai_texts (kind, status, body, writer, checker, reason, last_error, attempts)
        VALUES ('headlines', %s, %s, %s, %s, %s, %s, 1) RETURNING id""",
                       (result["status"], Jsonb(result.get("body")), result.get("model"), result.get("checker"),
                        reason, result.get("reason"))).fetchone()
    conn.commit()
    return row["id"]


def latest_headlines(conn) -> dict | None:
    """The newest ready headline set (Screen A)."""
    return conn.execute("""SELECT * FROM ai_texts WHERE kind = 'headlines' AND status = 'ready'
                           ORDER BY created_at DESC LIMIT 1""").fetchone()


def news(conn, leagues: list[str], days: int = 8, limit: int = 60) -> list[dict]:
    """Stored ESPN news, newest first (previews: the ESPN-first articles; headlines: the input)."""
    return conn.execute("""
        SELECT headline, description, url, published_at FROM news_items
        WHERE league = ANY(%s) AND published_at > now() - make_interval(days => %s)
        ORDER BY published_at DESC LIMIT %s""", (leagues, days, limit)).fetchall()
