"""ai_texts: one row per game and kind (headlines: one per run). Claiming a row is how two writers (the worker
writing ahead, a page open writing on demand) avoid writing the same text twice (handoff 2.2).

A claim marks the row 'writing' and records what the writer is writing from (claim_basis, claim_fingerprint) and
a token (attempts). It never touches body/basis, so the last good text is still there while a new one is written.
Only the writer holding the latest claim can save (the token), so a slow writer whose claim was taken over can't
overwrite newer work (audit, 2026-09-29).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from psycopg.types.json import Jsonb

ONE_LINER_TTL = timedelta(minutes=15)   # Adam, 2026-09-29: reused until the score changes or 15 minutes pass
# A row stuck at 'writing' this long can be claimed again. Longer than the worker's 15-minute activity limit, since
# a worker writer can wait minutes for quota while holding its claim.
WRITING_TIMEOUT = timedelta(minutes=16)
FAILED_QUIET = timedelta(minutes=30)    # a page open doesn't retry a text that failed this recently (same basis)


class Claim(NamedTuple):
    id: int
    token: int


def get(conn, game_id: int, kind: str) -> dict | None:
    return conn.execute("SELECT * FROM ai_texts WHERE game_id = %s AND kind = %s", (game_id, kind)).fetchone()


def _age(row: dict) -> timedelta:
    return datetime.now(timezone.utc) - row["updated_at"]


def current(row: dict | None, basis: str, fingerprint: str | None = None) -> bool:
    """Ready (or checked and sourceless) and written from what the game looks like now."""
    if not row or row["status"] not in ("ready", "no_sources") or row["basis"] != basis:
        return False
    if fingerprint is not None and row["fingerprint"] != fingerprint:
        return False
    if row["kind"] == "one_liner":
        return _age(row) < ONE_LINER_TTL
    return True


def being_written(row: dict | None) -> bool:
    """Someone holds a live claim on it."""
    return bool(row) and row["status"] == "writing" and _age(row) < WRITING_TIMEOUT


def failed_recently(row: dict | None, basis: str) -> bool:
    """Failed for the same inputs within FAILED_QUIET: a page open shows the fallback instead of paying again."""
    return (bool(row) and row["status"] == "failed" and row["claim_basis"] == basis
            and _age(row) < FAILED_QUIET)


def showable(row: dict | None) -> dict | None:
    """The text a page may show now: a ready row, or, while a preview is being refreshed, its last good version."""
    if not row:
        return None
    if row["status"] in ("ready", "no_sources"):
        return row
    if row["status"] == "writing" and row["kind"] == "preview" and row["body"] is not None:
        return row
    return None


def claim(conn, game_id: int, kind: str, basis: str, fingerprint: str | None = None,
          reason: str = "") -> Claim | None:
    """A Claim if this caller should write it now; None if it's current or someone else is writing it."""
    row = conn.execute("""
        INSERT INTO ai_texts (game_id, kind, status, claim_basis, claim_fingerprint, reason, attempts)
        VALUES (%(g)s, %(k)s, 'writing', %(b)s, %(f)s, %(r)s, 1)
        ON CONFLICT (game_id, kind) WHERE game_id IS NOT NULL DO UPDATE
            SET status = 'writing', claim_basis = EXCLUDED.claim_basis,
                claim_fingerprint = EXCLUDED.claim_fingerprint, reason = EXCLUDED.reason,
                attempts = ai_texts.attempts + 1, updated_at = now()
            WHERE ai_texts.status = 'failed'
               OR (ai_texts.status IN ('ready', 'no_sources') AND (
                      ai_texts.basis IS DISTINCT FROM EXCLUDED.claim_basis
                   OR ai_texts.fingerprint IS DISTINCT FROM EXCLUDED.claim_fingerprint
                   OR (ai_texts.kind = 'one_liner' AND ai_texts.updated_at < now() - %(ttl)s)))
               OR (ai_texts.status = 'writing' AND ai_texts.updated_at < now() - %(stuck)s)
        RETURNING id, attempts""", {"g": game_id, "k": kind, "b": basis, "f": fingerprint, "r": reason,
                                    "ttl": ONE_LINER_TTL, "stuck": WRITING_TIMEOUT}).fetchone()
    conn.commit()
    return Claim(row["id"], row["attempts"]) if row else None


def save(conn, c: Claim, result: dict) -> bool:
    """Store a writer result (writer.write_*) under its claim. False when the claim was taken over meanwhile: the
    result is dropped. A success replaces the text and takes the claim's basis. A failure keeps the last good
    text: a preview that had one stays 'ready' (a refresh that fails shouldn't blank a good midweek preview);
    anything else becomes 'failed' (the app shows fallback text)."""
    if result["status"] in ("ready", "no_sources"):
        row = conn.execute("""
            UPDATE ai_texts SET status = %s, body = %s, sources = %s, extract = %s, writer = %s, checker = %s,
                                basis = claim_basis, fingerprint = claim_fingerprint, last_error = NULL,
                                updated_at = now()
            WHERE id = %s AND status = 'writing' AND attempts = %s RETURNING id""",
                           (result["status"], Jsonb(result.get("body")), Jsonb(result.get("sources")),
                            Jsonb(result.get("extract")), result.get("model"), result.get("checker"),
                            c.id, c.token)).fetchone()
    else:
        row = conn.execute("""
            UPDATE ai_texts SET status = CASE WHEN kind = 'preview' AND body IS NOT NULL THEN 'ready'
                                              ELSE 'failed' END,
                                last_error = %s, updated_at = now()
            WHERE id = %s AND status = 'writing' AND attempts = %s RETURNING id""",
                           (result.get("reason"), c.id, c.token)).fetchone()
    conn.commit()
    return row is not None


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
