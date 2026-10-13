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
A live one-liner's fingerprint is its turnovers and win probability (one_liner_fingerprint), for the api too.

No database connection is held while a model writes (seconds, sometimes a minute of waiting for quota).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db, espn, favorites, games, ncaaf, summary
from . import client, facts, scope, sources, store, weekend_facts, writer

log = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_FOR = {"preview": "pre", "recap": "post", "one_liner": "in"}
HEADLINE_POOL = 150      # stories read from the table for the headlines: every outlet's last two days
HEADLINE_NEWS = 30       # stories the extract sees (the writer's minute holds ~30 headlines and snippets)


def _page(game_id: int, base_url: str, live: bool = False) -> dict | None:
    with db.connect() as conn:
        return games.page(conn, game_id, base_url=base_url, favorites=favorites.load(), live=live)


def basis(kind: str, page: dict) -> str:
    """What a text is written from; a different basis makes the stored text stale (handoff 2.2)."""
    if kind == "preview":        # the game day: a moved kickoff means a new preview
        return datetime.fromisoformat(page["start_time"]).astimezone(PACIFIC).date().isoformat()
    return f"{page['away'].get('score')}-{page['home'].get('score')}"     # recap: the final; one-liner: the score


def kind_for(row: dict) -> str | None:
    """Which text a game page shows now (handoff 2.6): preview before, one-liner during (cut by the CTO on Oct 1,
    back on Adam's call the same day; the box-score template is its fallback), recap after a played final; nothing
    for a canceled or postponed game."""
    if row["state"] == "pre":
        return "preview"
    if row["state"] == "in":
        return "one_liner"
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


LIVE_WIN_SWING = 10          # points of the home side's win probability that make a new line worth writing (Adam, Oct 4)
LIVE_SWING_MIN_AGE = timedelta(minutes=4)    # ... but not within this long of the last claim (review, Oct 4)
_MARK = re.compile(r"t(-|\d+)\|w(-|\d+)")


def one_liner_fingerprint(before: dict | None, mark: dict | None) -> str | None:
    """What a live one-liner is written under besides the score: "t<turnovers in all>|w<home win %>" from the
    stored summary (summary.live_mark). The stored line is reused while nothing material happened (store.current,
    the 30-minute TTL): a new turnover, or the win probability 10 points from where the last attempt left it, makes
    this a different fingerprint, so a rewrite. While quiet it IS the last claim's, so a drifting percentage is not
    new inputs (the rejection cap and the failed-quiet window key on it). None: ESPN sent neither."""
    if not mark:
        return None
    now = f"t{'-' if mark['turnovers'] is None else mark['turnovers']}|w{'-' if mark['win'] is None else mark['win']}"
    last = (before or {}).get("claim_fingerprint") or ""
    then = _MARK.fullmatch(last)
    if not then:
        return now
    old_t, old_w = (None if g == "-" else int(g) for g in then.groups())
    # A number ESPN had not sent before counts as new, so the baseline gets set.
    if mark["turnovers"] is not None and mark["turnovers"] != old_t:
        return now
    # A swing counts only once the last claim is a few minutes old: a late game's win probability can swing 10 points
    # back and forth every play, and each swing would be a rewrite (a turnover is rare enough to go straight through).
    updated = (before or {}).get("updated_at")
    settled = updated is None or datetime.now(timezone.utc) - updated >= LIVE_SWING_MIN_AGE
    if mark["win"] is not None and (old_w is None or (settled and abs(mark["win"] - old_w) >= LIVE_WIN_SWING)):
        return now
    return last


def live_fingerprint(game_id: int, before: dict | None) -> str | None:
    """one_liner_fingerprint from the stored summary (a page pull refreshed it just before). None when there is
    none to read: the basis and the TTL decide, as before."""
    with db.connect() as conn:
        stored = db.get_summary_mark(conn, game_id)
    return one_liner_fingerprint(before, summary.live_mark(stored["payload"])) if stored else None


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
    page = _page(game_id, base_url, live=kind == "one_liner")     # the one-liner reads the play-by-play too
    if page is None:
        return {"status": "missing", "id": None}
    with db.connect() as conn:
        game_row = db.game_by_id(conn, game_id)
    if (page["state"] != STATE_FOR[kind] or (kind == "recap" and not page.get("completed"))
            or not scope.ai_game(game_row)):
        return {"status": "skipped", "id": None}
    b = basis(kind, page)
    articles, fp = [], None
    if kind == "preview":
        with db.connect() as conn:
            news = store.news(conn, [page["league"]], espn_only=True)
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
    if kind == "one_liner":
        fp = live_fingerprint(game_id, before)
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
        elif kind == "recap":
            result = writer.write_recap(page)
        else:
            body = (before or {}).get("body")
            result = writer.write_one_liner(page, body.get("line") if isinstance(body, dict) else None,
                                            (before or {}).get("basis"))
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


def is_video(url: str | None) -> bool:
    """A video clip page (ESPN's show clips, AP's video pages): no article to read, so never the link under a headline."""
    return "/video/" in (url or "").lower()


def balanced(news: list[dict], n: int) -> list[dict]:
    """The n stories the headlines are written from: newest first within each (league, outlet), taken in turn across
    them. A plain newest-first cut would be whichever feed posts most (ESPN's NFL news alone is ~50 stories a day, Yahoo's
    feed as many), and the extract is sized to ~30 stories. Deterministic: the headlines fingerprint reads the result."""
    groups: dict[tuple, list[dict]] = {}
    for item in news:                                        # already newest first
        groups.setdefault((item.get("league"), sources.outlet(item.get("url") or "")), []).append(item)
    order = sorted(groups, key=lambda k: (groups[k][0]["published_at"], str(k)), reverse=True)
    out: list[dict] = []
    for rank in range(max((len(g) for g in groups.values()), default=0)):
        for key in order:
            if rank < len(groups[key]) and len(out) < n:
                out.append(groups[key][rank])
    return sorted(out, key=lambda i: (i["published_at"], i["url"] or ""), reverse=True)


def headlines_fingerprint(news: list[dict]) -> str:
    """What a headline set is written from: the stored news it reads."""
    data = {"news": sorted(n["url"] or n["headline"] for n in news)}
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def featured_final(row: dict, favs: dict[str, list[str]]) -> bool:
    """Which finals the headlines may list: NCAAF follows the board filter (a Saturday stores every FBS final,
    ~60 games; the feed should see the board's, favorites included), every other league passes."""
    if row["league"] != "ncaaf":
        return True
    return ncaaf.is_featured_row(row, set(favs.get("ncaaf", [])))


def write_headlines(leagues: list[str], reason: str, preflight: bool = False) -> dict:
    """The Home headlines (HEADLINE_LEAGUES: wider than the per-game texts' AI_LEAGUES): news only, every league
    (Adam, 2026-10-06: no final-score lines; scores live on the sport tabs). Nothing new since the last ready set
    (same news) means nothing to write: a run with the same inputs would only spend the writer's tokens on the same
    feed. A manual run always writes."""
    leagues = [lg for lg in leagues if scope.headline_league(lg)]
    if not leagues:
        return {"status": "skipped", "id": None}
    with db.connect() as conn:
        news = balanced([n for n in store.news(conn, leagues, days=2, limit=HEADLINE_POOL) if not is_video(n["url"])],
                        HEADLINE_NEWS)
        last = store.latest_headlines(conn)
    if not news:
        return {"status": "skipped", "id": None}
    fp = headlines_fingerprint(news)
    if reason != "manual" and last and last["fingerprint"] == fp:
        return {"status": "current", "id": last["id"]}
    if reason != "manual":
        with db.connect() as conn:
            if store.headlines_rejected_out(conn, fp):
                log.info("ai headlines: rejected %d times for these inputs, waiting for new ones", store.REJECTION_CAP)
                return {"status": "capped", "id": None}
    if preflight and (wait := client.unavailable_for("headlines")) is not None:
        return _unavailable("headlines", wait)
    result = writer.write_headlines(news)
    with db.connect() as conn:
        rid = store.save_headlines(conn, result, reason, fp)
    log.info("ai headlines (%s): %s by %s", reason, result["status"], result.get("model"))
    return {"status": result["status"], "id": rid, "retry_after": result.get("retry_after")}


# Weekend columns (Adam, 2026-10-06): the NFL weekend is Thursday night to Monday night (written Tuesday morning), the
# NCAAF one Thursday to Saturday (written Sunday morning).
WEEKEND_WINDOW = {"nfl": timedelta(days=5), "ncaaf": timedelta(days=3)}
WEEKEND_MIN_GAMES = 3          # a bye week or a holiday gap has no weekend to write about
WEEKEND_NEWS_DAYS = 5


def weekend_fingerprint(facts_: dict) -> str:
    return hashlib.sha1(json.dumps(facts_, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _game_leader_lines(conn, row: dict) -> list[str]:
    """The game's top passer, rusher and receiver per team from the stored summary (a final's is kept); [] if it has none."""
    stored = db.get_summary_view(conn, row["id"])
    if not stored or not stored.get("payload"):
        return []
    return weekend_facts.leader_lines(summary.leaders(stored["payload"], "game"), row["home"], row["away"])


def build_weekend_facts(league: str, window: timedelta | None = None) -> dict | None:
    """The league's weekend fact sheet (weekend_facts.build) from what is stored, or None when there is no weekend to
    write about: not a weekend league, not a headline league, or fewer than WEEKEND_MIN_GAMES finals in the window.
    window: a wider look back than WEEKEND_WINDOW, for a first column written mid-week (the launch's NCAAF one)."""
    if league not in WEEKEND_WINDOW or not scope.headline_league(league):
        return None
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT g.id, g.league, a.name AS away, g.away_score, h.name AS home, g.home_score, g.status_detail, g.start_time,
                   g.home_conf, g.away_conf, g.home_rank, g.away_rank,
                   h.espn_id AS home_espn_id, a.espn_id AS away_espn_id, h.abbr AS home_abbr, a.abbr AS away_abbr,
                   t.body ->> 'recap' AS recap
            FROM games g
            JOIN teams h ON h.id = g.home_team_id JOIN teams a ON a.id = g.away_team_id
            LEFT JOIN ai_texts t ON t.game_id = g.id AND t.kind = 'recap' AND t.status = 'ready'
            WHERE g.state = 'post' AND g.completed IS TRUE AND g.league = %s AND g.start_time > now() - %s
              AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
            ORDER BY g.start_time, g.espn_id""", (league, window or WEEKEND_WINDOW[league])).fetchall()
        news = store.news(conn, [league], days=WEEKEND_NEWS_DAYS, limit=weekend_facts.NEWS_ITEMS, espn_only=True)
    favs = favorites.load()
    rows = [r for r in rows if featured_final(r, favs)]
    if len(rows) < WEEKEND_MIN_GAMES:
        return None
    with db.connect() as conn:
        leaders = {r["id"]: _game_leader_lines(conn, r) for r in weekend_facts.featured(rows)}
    return weekend_facts.build(league, rows, news, leaders)


def write_weekend(league: str, reason: str, preflight: bool = False) -> dict:
    """The league's weekend column, written from build_weekend_facts: the finals of the window (NCAAF follows the board
    filter), the stored recaps and leaders of the most interesting of them, and a few news items. The same facts as
    the last ready column mean nothing to write; a manual run always writes."""
    facts_ = build_weekend_facts(league)
    if facts_ is None:
        return {"status": "skipped", "id": None}
    with db.connect() as conn:
        last = store.latest_weekend(conn, league)
    fp = weekend_fingerprint(facts_)
    if reason != "manual" and last and last["fingerprint"] == fp:
        return {"status": "current", "id": last["id"]}
    if reason != "manual":
        with db.connect() as conn:
            if store.weekend_rejected_out(conn, league, fp):
                log.info("ai weekend %s: rejected %d times for these facts, waiting for new ones", league,
                         store.REJECTION_CAP)
                return {"status": "capped", "id": None}
    if preflight and (wait := client.unavailable_for("weekend")) is not None:
        return _unavailable(f"weekend {league}", wait)
    result = writer.write_weekend(facts_)
    with db.connect() as conn:
        rid = store.save_weekend(conn, league, result, reason, fp)
    log.info("ai weekend %s (%s): %s by %s", league, reason, result["status"], result.get("model"))
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
