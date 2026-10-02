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

# A row stuck at 'writing' this long can be claimed again. Longer than the worker's 15-minute activity limit, since
# a worker writer can wait minutes for quota while holding its claim.
WRITING_TIMEOUT = timedelta(minutes=16)
FAILED_QUIET = timedelta(minutes=30)    # a page open doesn't retry a text that failed this recently (same basis)
QUEUED = "queued"                       # reason on a failed row a page open handed to the worker (main._hand_to_worker)
# A text that failed this many times for reasons retrying won't fix (check failed twice, a non-retryable error) is
# not written again for the same inputs (Adam, 2026-10-01): it waits for new ones. Rate limits and 5xx never count.
REJECTION_CAP = 2
UNKNOWN = object()                      # "the inputs' fingerprint isn't known here" (the api doesn't compute one)


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
    return fingerprint is None or row["fingerprint"] == fingerprint


def counts_as_rejection(result: dict) -> bool:
    """A failure retrying the same inputs won't fix: any failure that doesn't name a time to try again, except a
    missing key or checker (the setup's fault, not the text's)."""
    return result["status"] == "failed" and not result.get("retry_after") and not result.get("unconfigured")


def rejected_out(row: dict | None, basis: str, fingerprint: object = UNKNOWN) -> bool:
    """The last claim, for these same inputs, has failed REJECTION_CAP times. The api passes no fingerprint (it
    doesn't fetch articles): a text rejected twice for a game day or score stays on its fallback until that changes,
    and the worker, which does compute the fingerprint, writes again once the fact sheet or articles have."""
    return (bool(row) and row["rejections"] >= REJECTION_CAP and row["claim_basis"] == basis
            and (fingerprint is UNKNOWN or row["claim_fingerprint"] == fingerprint))


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
                rejections = CASE WHEN ai_texts.claim_basis IS NOT DISTINCT FROM EXCLUDED.claim_basis
                                   AND ai_texts.claim_fingerprint IS NOT DISTINCT FROM EXCLUDED.claim_fingerprint
                                  THEN ai_texts.rejections ELSE 0 END,
                attempts = ai_texts.attempts + 1, updated_at = now()
            WHERE ai_texts.status = 'failed'
               OR (ai_texts.status IN ('ready', 'no_sources') AND (
                      ai_texts.basis IS DISTINCT FROM EXCLUDED.claim_basis
                   OR ai_texts.fingerprint IS DISTINCT FROM EXCLUDED.claim_fingerprint))
               OR (ai_texts.status = 'writing' AND ai_texts.updated_at < now() - %(stuck)s)
        RETURNING id, attempts""", {"g": game_id, "k": kind, "b": basis, "f": fingerprint, "r": reason,
                                    "stuck": WRITING_TIMEOUT}).fetchone()
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
                                rejections = 0, written_at = now(), updated_at = now()
            WHERE id = %s AND status = 'writing' AND attempts = %s RETURNING id""",
                           (result["status"], Jsonb(result.get("body")), Jsonb(result.get("sources")),
                            Jsonb(result.get("extract")), result.get("model"), result.get("checker"),
                            c.id, c.token)).fetchone()
    else:
        # A transient failure (rate limit, 5xx) after the extract call hands back that extract: keep it, so the
        # retry doesn't pay for the extract again. A check failure hands back none: a row that had none extracts
        # afresh next time (a row that already holds one, from a text it wrote, keeps it).
        extract = result.get("extract")
        row = conn.execute("""
            UPDATE ai_texts SET status = CASE WHEN kind = 'preview' AND body IS NOT NULL THEN 'ready'
                                              ELSE 'failed' END,
                                extract = COALESCE(%s, extract), last_error = %s,
                                rejections = rejections + %s, updated_at = now()
            WHERE id = %s AND status = 'writing' AND attempts = %s RETURNING id""",
                           (Jsonb(extract) if extract else None, result.get("reason"),
                            int(counts_as_rejection(result)), c.id, c.token)).fetchone()
    conn.commit()
    return row is not None


def mark_queued(conn, row_id: int) -> None:
    """A page open ran out of quota and handed this text to the worker: the app says "being written" until the
    worker claims it (its claim sets its own reason) or FAILED_QUIET passes."""
    conn.execute("UPDATE ai_texts SET reason = %s WHERE id = %s AND status = 'failed'", (QUEUED, row_id))
    conn.commit()


def save_headlines(conn, result: dict, reason: str, fingerprint: str | None = None) -> int:
    """fingerprint: what the set was written from (jobs.headlines_fingerprint), so an unchanged run can skip."""
    row = conn.execute("""
        INSERT INTO ai_texts (kind, status, body, writer, checker, reason, last_error, attempts, fingerprint,
                              rejections, written_at)
        VALUES ('headlines', %s, %s, %s, %s, %s, %s, 1, %s, %s, CASE WHEN %s = 'ready' THEN now() END) RETURNING id""",
                       (result["status"], Jsonb(result.get("body")), result.get("model"), result.get("checker"),
                        reason, result.get("reason"), fingerprint, int(counts_as_rejection(result)),
                        result["status"])).fetchone()
    conn.commit()
    return row["id"]


def headlines_rejected_out(conn, fingerprint: str) -> bool:
    """This run's inputs have failed REJECTION_CAP times since the last ready set (one failed row per run)."""
    n = conn.execute("""
        SELECT count(*) AS n FROM ai_texts
        WHERE kind = 'headlines' AND status = 'failed' AND rejections > 0 AND fingerprint = %s
          AND created_at > coalesce((SELECT max(created_at) FROM ai_texts
                                     WHERE kind = 'headlines' AND status = 'ready'), '-infinity')""",
                     (fingerprint,)).fetchone()["n"]
    return n >= REJECTION_CAP


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
