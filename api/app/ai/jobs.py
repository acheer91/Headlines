"""Write one AI text end to end: build the game page, decide whether the stored text is still current, claim the
row, write and fact-check it (writer.py), store it. The one entry point for the worker (Temporal activities)
and the api (writing on open), so both follow the same rules.

A result is {"status": ..., "id": row id or None, "retry_after": seconds (rate limited only)}. Status is the
stored row's (ready / failed / no_sources), or current (nothing to do), busy (another writer holds the row),
skipped (the game isn't in the state this kind needs), missing (no such game) or capped (rejected store.REJECTION_CAP
times for these inputs: it waits for new ones; a "manual" run ignores that).

With preflight (the worker), a text that needs a model while the writer or every allowed checker is cooling down or
out of budget returns failed + retry_after before it is claimed or drafted: no row churn, no wasted draft. The page
is still built first, and for a preview so is the article lookup (a Groq search when ESPN has fewer than 2 fresh
articles, ~1.6K tokens of the search model's own budget): "current", "no sources" and the rejection cap all need the
fingerprint, which includes the articles. The api leaves preflight off: it never waits, and its failed row keeps the
page quiet for 30 minutes.

The rejection cap counts a failure for one set of inputs: the game day or score and, for the worker, the fingerprint
of the fact sheet and the kept articles. Another article set (a search that returned different links) is new inputs.

No database connection is held while a model writes (seconds, sometimes a minute of waiting for quota).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db, espn, favorites, games
from . import client, facts, scope, sources, store, writer

log = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_FOR = {"preview": "pre", "recap": "post"}


def _page(game_id: int, base_url: str) -> dict | None:
    with db.connect() as conn:
        return games.page(conn, game_id, base_url=base_url, favorites=favorites.load())


def basis(kind: str, page: dict) -> str:
    """What a text is written from; a different basis makes the stored text stale (handoff 2.2)."""
    if kind == "preview":        # the game day: a moved kickoff means a new preview
        return datetime.fromisoformat(page["start_time"]).astimezone(PACIFIC).date().isoformat()
    return f"{page['away'].get('score')}-{page['home'].get('score')}"     # recap: the final


def kind_for(row: dict) -> str | None:
    """Which text a game page shows now (handoff 2.6): preview before, recap after a played final; nothing live
    (CTO, 2026-10-01: the live one-liner is the box-score template, summary.one_liner) or for a canceled or
    postponed game."""
    if row["state"] == "pre":
        return "preview"
    if row["state"] == "in":
        return None
    return "recap" if row.get("completed") else None


def row_basis(kind: str, row: dict) -> str:
    """basis() from a games row (the api checks this on every open without rebuilding the page)."""
    if kind == "preview":
        return row["start_time"].astimezone(PACIFIC).date().isoformat()
    return f"{row['away_score']}-{row['home_score']}"


def fingerprint(page: dict, articles: list[dict]) -> str:
    """Everything a preview is written from that can change after it is written: the whole game fact sheet
    (records, ranks, line, stats, injuries without ESPN's shifting return dates, kickoff) and the articles used.
    Injuries and the line alone missed a record or a rank moving before a game written 6 days ahead (audit)."""
    data = {"facts": facts.preview_facts(page)["facts"], "articles": sorted(a["url"] for a in articles)}
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _unavailable(what: str, wait: float) -> dict:
    log.info("ai %s: models limited for another %.0fs, nothing built", what, wait)
    return {"status": "failed", "id": None, "retry_after": wait}


def _reusable_extract(before: dict | None, articles: list[dict]) -> dict | None:
    """The saved article extract for exactly these articles, or None. The extract carries the URLs it was made from,
    so one saved by a failed write (rate limited after the extract call) is reused too; one saved before that, with
    no list, only beside the text it was written for."""
    saved = before.get("extract") if before else None
    if not saved:
        return None
    urls = sorted(a["url"] for a in articles)
    if "urls" in saved:
        return saved if sorted(saved["urls"]) == urls else None
    return saved if before["body"] is not None and sorted(s["url"] for s in before["sources"] or []) == urls else None


def write_for_game(kind: str, game_id: int, reason: str, base_url: str = espn.BASE, preflight: bool = False) -> dict:
    with db.connect() as conn:
        held = store.get(conn, game_id, kind)
    if store.being_written(held):
        # Someone holds a live claim: don't rebuild the page or fetch articles only to find that out.
        return {"status": "busy", "id": held["id"]}
    page = _page(game_id, base_url)
    if page is None:
        return {"status": "missing", "id": None}
    if (page["state"] != STATE_FOR[kind] or (kind == "recap" and not page.get("completed"))
            or not scope.ai_league(page["league"])):
        return {"status": "skipped", "id": None}
    b = basis(kind, page)
    articles, fp = [], None
    if kind == "preview":
        with db.connect() as conn:
            news = store.news(conn, [page["league"]])
        try:
            articles, _trail = sources.find_articles(page, news)
        except client.AIError as exc:
            # The search couldn't run and ESPN had nothing: retry later, never store "no fresh previews".
            log.warning("ai preview game %s: no articles yet, search unavailable: %s", game_id, exc)
            return {"status": "failed", "id": None,
                    "retry_after": getattr(exc, "retry_after", None) or writer.TRANSIENT_RETRY}
        fp = fingerprint(page, articles)
    with db.connect() as conn:
        before = store.get(conn, game_id, kind)
    if store.current(before, b, fp):
        return {"status": "current", "id": before["id"]}
    if reason != "manual" and store.rejected_out(before, b, fp):
        log.info("ai %s game %s: rejected %d times for these inputs, waiting for new ones", kind, game_id,
                 before["rejections"])
        return {"status": "capped", "id": before["id"]}
    # A preview with no articles is written as "no sources" without a model call: nothing to wait for.
    if (preflight and not (kind == "preview" and not articles)
            and (wait := client.unavailable_for(kind)) is not None):
        return _unavailable(f"{kind} game {game_id}", wait)
    with db.connect() as conn:
        c = store.claim(conn, game_id, kind, b, fp, reason)
    if c is None:
        return {"status": "busy", "id": before["id"] if before else None}

    try:
        if kind == "preview":
            # Same articles as a saved extract (a game-morning refresh, or a retry after a rate limit that came
            # after the extract call): reuse it. Stored by article URL, so it maps onto today's article order.
            result = writer.write_preview(page, articles, _reusable_extract(before, articles))
        else:
            result = writer.write_recap(page)
    except Exception as exc:  # noqa: BLE001 — a bug must not leave the row stuck at 'writing'
        log.exception("ai %s game %s crashed", kind, game_id)
        result = {"status": "failed", "reason": f"crashed: {type(exc).__name__}"}
    with db.connect() as conn:
        saved = store.save(conn, c, result)
    if not saved:
        log.warning("ai %s game %s: claim taken over while writing; result dropped", kind, game_id)
        return {"status": "busy", "id": c.id}
    log.info("ai %s game %s (%s): %s in %.1fs by %s", kind, game_id, reason, result["status"],
             result.get("seconds", 0), result.get("model"))
    return {"status": result["status"], "id": c.id, "retry_after": result.get("retry_after")}


def headlines_fingerprint(news: list[dict], lines: list[str]) -> str:
    """What a headline set is written from: the stored news it reads and the finals it lists."""
    data = {"news": sorted(n["url"] or n["headline"] for n in news), "finals": lines}
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def write_headlines(leagues: list[str], reason: str, preflight: bool = False) -> dict:
    """The Home headlines. Nothing new since the last ready set (same news, same finals) means nothing to write:
    a run with the same inputs would only spend the writer's tokens on the same feed. A manual run always writes."""
    leagues = [lg for lg in leagues if scope.ai_league(lg)]
    if not leagues:
        return {"status": "skipped", "id": None}
    with db.connect() as conn:
        news = store.news(conn, leagues, days=2, limit=30)
        finals = conn.execute("""
            SELECT g.league, a.name AS away, g.away_score, h.name AS home, g.home_score FROM games g
            JOIN teams h ON h.id = g.home_team_id JOIN teams a ON a.id = g.away_team_id
            WHERE g.state = 'post' AND g.completed IS TRUE AND g.league = ANY(%s)
              AND g.start_time > now() - interval '2 days'
            ORDER BY g.start_time""", (leagues,)).fetchall()
        last = store.latest_headlines(conn)
    lines = [f"{r['league'].upper()}: {r['away']} {r['away_score']}, {r['home']} {r['home_score']} (final)"
             for r in finals]
    if not news and not lines:
        return {"status": "skipped", "id": None}
    fp = headlines_fingerprint(news, lines)
    if reason != "manual" and last and last["fingerprint"] == fp:
        return {"status": "current", "id": last["id"]}
    if reason != "manual":
        with db.connect() as conn:
            if store.headlines_rejected_out(conn, fp):
                log.info("ai headlines: rejected %d times for these inputs, waiting for new ones", store.REJECTION_CAP)
                return {"status": "capped", "id": None}
    if preflight and (wait := client.unavailable_for("headlines")) is not None:
        return _unavailable("headlines", wait)
    result = writer.write_headlines(news, lines)
    with db.connect() as conn:
        rid = store.save_headlines(conn, result, reason, fp)
    log.info("ai headlines (%s): %s by %s", reason, result["status"], result.get("model"))
    return {"status": result["status"], "id": rid, "retry_after": result.get("retry_after")}


def upcoming(league: str, through: timedelta, kinds: tuple[str, ...] = ("preview",)) -> list[int]:
    """Games from now until `through` whose preview isn't written for their current game day (the batch and the
    nightly job). Soonest first."""
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT g.id, g.start_time, t.status, t.basis FROM games g
            LEFT JOIN ai_texts t ON t.game_id = g.id AND t.kind = 'preview'
            WHERE g.league = %s AND g.state = 'pre' AND g.start_time BETWEEN now() AND now() + %s
            ORDER BY g.start_time""", (league, through)).fetchall()
    out = []
    for r in rows:
        day = r["start_time"].astimezone(PACIFIC).date().isoformat()
        if r["status"] not in ("ready", "no_sources") or r["basis"] != day:
            out.append(r["id"])
    return out


def unwritten_recaps(league: str, within: timedelta) -> list[int]:
    """Finished games in the last `within` with no ready recap, or one written from a score that has since been
    corrected (the nightly job)."""
    with db.connect() as conn:
        return [r["id"] for r in conn.execute("""
            SELECT g.id FROM games g
            LEFT JOIN ai_texts t ON t.game_id = g.id AND t.kind = 'recap'
            WHERE g.league = %s AND g.state = 'post' AND g.completed IS TRUE
              AND g.start_time > now() - %s
              AND (t.status IS NULL OR t.status = 'failed'
                   OR t.basis IS DISTINCT FROM g.away_score || '-' || g.home_score)   -- a stat correction
            ORDER BY g.start_time DESC""", (league, within)).fetchall()]
