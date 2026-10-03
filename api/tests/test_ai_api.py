"""Phase 4 Stage 2 against a real Postgres (TEST_DATABASE_URL): ai_texts claims and staleness, writing on open,
fallbacks, headlines, the shared quota, and the write_text activity's retry signal. No model calls: the writer
and the article search are faked; ESPN is the test_api fixture."""
import threading
from datetime import timedelta

import pytest

from app.ai.sources import find_articles as REAL_FIND_ARTICLES
from test_api import TEST_DB, _ids, _sql, client  # noqa: F401 — the client fixture

pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to run DB tests")

ARTICLE = {"url": "https://www.espn.com/nfl/story/_/id/1/x", "outlet": "ESPN", "title": "t",
           "published": "2026-09-28T12:00+00:00", "text": "words"}


def ready(kind, **extra):
    body = {"preview": {"preview": "p", "edges": {"home": [], "away": []}, "picks": []},
            "recap": {"recap": "r", "bets": "", "home": "h", "away": "a"},
            "one_liner": {"line": "l"}}[kind]
    return {"status": "ready", "body": body, "model": "fake", "checker": "fake-check", "seconds": 0.1, **extra}


@pytest.fixture()
def fake(client, monkeypatch):  # noqa: F811
    """Count writer calls per kind; articles from `state['articles']`."""
    from app.ai import sources, writer
    state = {"calls": {"preview": [], "recap": 0, "one_liner": 0}, "articles": [ARTICLE], "result": None}

    def preview(page, articles, extract=None):
        state["calls"]["preview"].append(extract)
        if not articles:
            return {"status": "no_sources", "model": "fake"}
        return state["result"] or ready("preview", sources=[{k: a[k] for k in ("title", "url", "outlet", "published")}
                                                            for a in articles],
                                        extract={"storylines": [], "urls": sorted(a["url"] for a in articles)})

    def recap(page):
        state["calls"]["recap"] += 1
        return state["result"] or ready("recap")

    def one_liner(page):
        state["calls"]["one_liner"] += 1
        return state["result"] or ready("one_liner")

    monkeypatch.setattr(writer, "write_preview", preview)
    monkeypatch.setattr(writer, "write_recap", recap)
    monkeypatch.setattr(writer, "write_one_liner", one_liner)
    monkeypatch.setattr(sources, "find_articles", lambda page, news, **kw: (list(state["articles"]), []))
    state["ids"] = _ids(client)
    return state


def test_recap_written_on_open_then_instant(client, fake):  # noqa: F811
    gid = fake["ids"]["DAL"]
    first = client.get(f"/api/games/{gid}/ai").json()
    assert first["kind"] == "recap" and first["status"] == "ready" and first["body"]["recap"] == "r"
    assert client.get(f"/api/games/{gid}/ai").json()["status"] == "ready"
    assert fake["calls"]["recap"] == 1                                   # the second open reused it
    assert _sql("SELECT writer, checker, reason FROM ai_texts WHERE game_id = %s", gid) == [("fake", "fake-check", "open")]


def test_kind_follows_the_game_state(client, fake):  # noqa: F811
    ids = fake["ids"]
    assert client.get(f"/api/games/{ids['BUF']}/ai").json()["kind"] == "preview"
    assert client.get(f"/api/games/{ids['KC']}/ai").json()["kind"] == "one_liner"     # back on Adam's call (Oct 1)


def test_one_liner_reused_for_15_minutes_then_rewritten(client, fake):  # noqa: F811
    gid = fake["ids"]["KC"]
    client.get(f"/api/games/{gid}/ai")
    client.get(f"/api/games/{gid}/ai")
    assert fake["calls"]["one_liner"] == 1
    _sql("UPDATE ai_texts SET updated_at = now() - interval '16 minutes' WHERE game_id = %s", gid)
    client.get(f"/api/games/{gid}/ai")
    assert fake["calls"]["one_liner"] == 2


def test_a_rate_limited_one_liner_is_not_handed_to_the_worker(client, fake, monkeypatch):  # noqa: F811
    from app import main
    monkeypatch.setattr(main, "_hand_to_worker", lambda kind, row: pytest.fail("handed a one-liner to the worker"))
    fake["result"] = RATE_LIMITED
    assert client.get(f"/api/games/{fake['ids']['KC']}/ai").json()["status"] == "failed"   # the app: the template


def test_preview_without_fresh_articles_is_no_sources(client, fake):  # noqa: F811
    fake["articles"] = []
    assert client.get(f"/api/games/{fake['ids']['BUF']}/ai").json()["status"] == "no_sources"


def test_league_without_ai_text_writes_nothing(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, scope
    monkeypatch.setattr(scope, "AI_LEAGUES", {"ncaaf"})               # NFL off: the fixture's games are NFL
    monkeypatch.setattr(scope, "HEADLINE_LEAGUES", {"ncaaf"})         # headlines' own list (2026-10-02) off too
    out = client.get(f"/api/games/{fake['ids']['DAL']}/ai").json()
    assert out["kind"] is None and out["status"] == "none"
    assert jobs.write_for_game("recap", fake["ids"]["DAL"], "final")["status"] == "skipped"
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "skipped"
    assert fake["calls"]["recap"] == 0


def test_written_at_is_when_the_shown_text_was_written(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["BUF"]
    first = client.get(f"/api/games/{gid}/ai").json()
    assert first["status"] == "ready" and first["written_at"]
    _sql("UPDATE ai_texts SET written_at = now() - interval '2 days' WHERE game_id = %s", gid)
    fake["articles"] = [dict(ARTICLE, url="https://www.espn.com/nfl/story/_/id/2/y")]      # new articles: a refresh
    fake["result"] = {"status": "failed", "reason": "check failed twice", "model": "fake"}
    jobs.write_for_game("preview", gid, "refresh")
    kept = client.get(f"/api/games/{gid}/ai").json()
    assert kept["status"] == "ready" and kept["body"]["preview"] == "p"     # the last good preview stays
    assert kept["written_at"] < first["written_at"]                         # and says when it was written


def test_recap_rewritten_once_after_a_stat_correction(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["DAL"]
    client.get(f"/api/games/{gid}/ai")
    _sql("UPDATE games SET home_score = home_score + 1 WHERE id = %s", gid)
    _sql("UPDATE game_summaries SET payload = jsonb_set(payload, '{header,competitions,0,competitors,0,score}', "
         "to_jsonb((SELECT home_score FROM games WHERE id = %s)::text)) WHERE game_id = %s", gid, gid)
    assert jobs.write_for_game("recap", gid, "final")["status"] in ("ready", "current")
    basis = _sql("SELECT basis FROM ai_texts WHERE game_id = %s AND kind = 'recap'", gid)[0][0]
    row = _sql("SELECT away_score, home_score FROM games WHERE id = %s", gid)[0]
    assert basis == f"{row[0]}-{row[1]}"


def test_failed_text_is_left_alone_on_open_for_30_minutes_then_retried(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["DAL"]
    fake["result"] = {"status": "failed", "reason": "check failed twice", "model": "fake"}
    for _ in range(3):
        assert client.get(f"/api/games/{gid}/ai").json()["status"] == "failed"
    assert fake["calls"]["recap"] == 1                     # pulls don't pay for the same failure again
    fake["result"] = None
    assert jobs.write_for_game("recap", gid, "nightly")["status"] == "ready"      # the worker still retries
    _sql("UPDATE ai_texts SET status = 'failed', updated_at = now() - interval '31 minutes' WHERE game_id = %s", gid)
    assert client.get(f"/api/games/{gid}/ai").json()["status"] == "ready"
    assert fake["calls"]["recap"] == 3


def test_no_keys_means_fallback_not_a_crash(client, monkeypatch):  # noqa: F811
    from app.ai import sources
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(sources, "find_articles", lambda page, news, **kw: ([ARTICLE], []))
    ids = _ids(client)
    for team in ("DAL", "BUF", "KC"):
        out = client.get(f"/api/games/{ids[team]}/ai")
        assert out.status_code == 200 and out.json()["status"] == "failed"


def test_two_writers_one_claim(client, fake):  # noqa: F811
    from app import db
    from app.ai import store
    gid = fake["ids"]["DAL"]
    got = []

    def claim():
        with db.connect() as conn:
            got.append(store.claim(conn, gid, "recap", "27-20", reason="test"))
    threads = [threading.Thread(target=claim) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for g in got if g is not None) == 1


def test_stuck_writing_row_is_reclaimed_after_16_minutes(client, fake):  # noqa: F811
    from app import db
    from app.ai import store
    gid = fake["ids"]["DAL"]
    with db.connect() as conn:
        assert store.claim(conn, gid, "recap", "b") is not None
        assert store.claim(conn, gid, "recap", "b") is None
    _sql("UPDATE ai_texts SET updated_at = now() - interval '10 minutes' WHERE game_id = %s", gid)
    with db.connect() as conn:
        assert store.claim(conn, gid, "recap", "b") is None     # a worker can wait minutes for quota
    _sql("UPDATE ai_texts SET updated_at = now() - interval '17 minutes' WHERE game_id = %s", gid)
    with db.connect() as conn:
        assert store.claim(conn, gid, "recap", "b") is not None


def test_preview_refresh_reuses_the_extract_when_only_our_data_changed(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["BUF"]
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "ready"
    assert jobs.write_for_game("preview", gid, "refresh")["status"] == "current"      # nothing changed
    _sql("DELETE FROM game_summaries WHERE game_id = %s", gid)                         # injuries change on refetch
    _sql("UPDATE ai_texts SET fingerprint = 'old' WHERE game_id = %s", gid)
    assert jobs.write_for_game("preview", gid, "refresh")["status"] == "ready"
    assert fake["calls"]["preview"][0] is None
    assert fake["calls"]["preview"][-1] == {"storylines": [], "urls": [ARTICLE["url"]]}


def test_headlines_endpoint(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, writer
    assert client.get("/api/headlines").json() is None
    _sql("INSERT INTO news_items (league, espn_id, headline, published_at) VALUES ('nfl', '1', 'Big news', now())")
    monkeypatch.setattr(writer, "write_headlines", lambda news, finals: {
        "status": "ready", "body": {"items": [{"text": "Big news", "url": None}]}, "model": "fake"})
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready"
    out = client.get("/api/headlines").json()
    assert out["items"] == [{"text": "Big news", "url": None}] and out["updated_at"]


def test_upcoming_lists_games_without_a_current_preview(client, fake):  # noqa: F811
    from app.ai import jobs
    ids = fake["ids"]
    _sql("UPDATE games SET start_time = now() + interval '1 day' WHERE id IN (%s, %s)", ids["BUF"], ids["SEA"])
    todo = jobs.upcoming("nfl", timedelta(days=6))
    assert set(todo) == {ids["BUF"], ids["SEA"]}
    jobs.write_for_game("preview", ids["BUF"], "midweek")
    assert jobs.upcoming("nfl", timedelta(days=6)) == [ids["SEA"]]


# ---------- the shared quota (AI_QUOTA=db) ----------

def test_db_quota_shares_the_minute_and_cooling(client):  # noqa: F811
    from app.ai.quota import DbQuota
    q = DbQuota()
    a = q.reserve(["m1", "m2"], 5000, 8000, wait=False)
    b = q.reserve(["m1", "m2"], 5000, 8000, wait=False)
    assert a[0] == "m1" and b[0] == "m2"                   # m1's minute is full: the next call goes to m2
    assert q.reserve(["m1", "m2"], 5000, 8000, wait=False) is None
    q.cool("m3", 120)
    assert q.reserve(["m3"], 10, 8000, wait=False) is None
    q.used(a[1], 1234)
    assert _sql("SELECT used FROM ai_calls WHERE id = %s", a[1]) == [(1234,)]


def test_db_quota_counts_the_day(client):  # noqa: F811
    from app.ai.quota import DbQuota
    q = DbQuota()
    a = q.reserve(["t1"], 5000, 8000, wait=False)
    q.used(a[1], 1200)
    q.reserve(["t2"], 3000, 8000, wait=False)                           # not reported yet: its reservation counts
    assert q.spent_today("t1", False)[0] == 1200 and q.spent_today("t2", False)[0] == 3000
    q.reserve(["or:v/a:free"], 1, 20, wait=False)
    q.reserve(["or:v/b:free"], 1, 20, wait=False)
    assert q.spent_today("or:", True)[0] == 2                           # OpenRouter: one budget for the account
    assert q.spent_today("or:v/a:free", True)[0] == 1
    _sql("UPDATE ai_calls SET at = now() - interval '23 hours' WHERE model = 't1'")
    spent, frees_in = q.spent_today("t1", False)
    assert spent == 1200 and 3500 < frees_in <= 3600                   # back in about an hour
    _sql("UPDATE ai_calls SET at = now() - interval '25 hours' WHERE model = 't1'")
    assert q.spent_today("t1", False) == (0.0, 0.0)


# ---------- the activity's retry signal ----------

def favorite(monkeypatch, tmp_path, *teams):
    """Put teams on the pre-write list (the favorites file the worker reads)."""
    import json
    f = tmp_path / "favs.json"
    f.write_text(json.dumps({"nfl": list(teams)}))
    monkeypatch.setenv("FAVORITES_FILE", str(f))


def test_write_text_rate_limited_retries_after_groqs_wait(client, fake, monkeypatch, tmp_path):  # noqa: F811
    favorite(monkeypatch, tmp_path, "DAL")
    from temporalio.exceptions import ApplicationError
    from temporalio.testing import ActivityEnvironment
    from app.temporal import activities
    from app.temporal.models import TextJob
    fake["result"] = {"status": "failed", "reason": "rate limited", "retry_after": 409.0, "model": "fake"}
    espn_id = _sql("SELECT espn_id FROM games WHERE id = %s", fake["ids"]["DAL"])[0][0]
    with pytest.raises(ApplicationError) as err:
        ActivityEnvironment().run(activities.write_text, TextJob("recap", "nfl", espn_id, "final"))
    assert err.value.type == "RateLimited" and err.value.next_retry_delay == timedelta(seconds=409)
    fake["result"] = None
    assert ActivityEnvironment().run(activities.write_text, TextJob("recap", "nfl", espn_id, "final")) == "ready"


def test_preview_step_starts_the_refresh(client, monkeypatch, tmp_path):  # noqa: F811
    favorite(monkeypatch, tmp_path, "BUF")
    _ids(client)
    from temporalio.testing import ActivityEnvironment
    from app.temporal import activities
    started = []
    monkeypatch.setattr(activities, "start_text", started.append)
    ActivityEnvironment().run(activities.generate_preview, "nfl", "401900001")
    assert [(j.kind, j.espn_id, j.reason) for j in started] == [("preview", "401900001", "refresh")]


def test_no_key_in_the_repo():
    """The handoff's key-pattern scan: no Gemini or Groq key outside .env anywhere in the tree."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    pat = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-]{20,}|gsk_[0-9A-Za-z]{20,}")
    skip = {".git", "node_modules", ".venv", "dist", "__pycache__"}
    hits = [str(p) for p in root.rglob("*") if p.is_file() and not skip & set(p.parts) and p.name != ".env"
            and p.stat().st_size < 2_000_000 and pat.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert hits == []


# ---------- audit fixes (2026-09-29); each was a reproduced bug ----------

def test_late_writer_cannot_overwrite_newer_work(client, fake):  # noqa: F811
    from app import db
    from app.ai import store
    gid = fake["ids"]["DAL"]
    with db.connect() as conn:
        a = store.claim(conn, gid, "recap", "20-27", reason="final")
    _sql("UPDATE ai_texts SET updated_at = now() - interval '17 minutes' WHERE id = %s", a.id)
    with db.connect() as conn:
        b = store.claim(conn, gid, "recap", "20-27", reason="open")
        assert store.save(conn, b, ready("recap"))
        assert not store.save(conn, a, {"status": "failed", "reason": "check failed twice"})   # dropped
    assert _sql("SELECT status FROM ai_texts WHERE id = %s", a.id) == [("ready",)]


def test_text_is_stored_under_the_basis_it_was_written_from(client, fake):  # noqa: F811
    from app import db
    from app.ai import store
    gid = fake["ids"]["DAL"]
    with db.connect() as conn:
        a = store.claim(conn, gid, "recap", "20-27", reason="final")
    _sql("UPDATE ai_texts SET updated_at = now() - interval '17 minutes' WHERE id = %s", a.id)
    with db.connect() as conn:
        b = store.claim(conn, gid, "recap", "20-30", reason="open")          # a stat correction
        assert not store.save(conn, a, ready("recap"))                        # old-score text dropped
        assert store.save(conn, b, ready("recap"))
    assert _sql("SELECT status, basis FROM ai_texts WHERE id = %s", a.id) == [("ready", "20-30")]


def test_refresh_keeps_the_last_good_preview_on_screen_and_on_failure(client, fake):  # noqa: F811
    from app import db
    from app.ai import jobs, store
    gid = fake["ids"]["BUF"]
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "ready"
    with db.connect() as conn:
        c = store.claim(conn, gid, "preview", jobs.row_basis("preview", db.game_by_id(conn, gid)), "new-fp", "refresh")
    shown = client.get(f"/api/games/{gid}/ai").json()                        # mid-refresh: old text at once
    assert shown["status"] == "ready" and shown["body"]["preview"] == "p"
    with db.connect() as conn:
        assert store.save(conn, c, {"status": "failed", "reason": "check failed twice"})
    row = _sql("SELECT status, body->>'preview' FROM ai_texts WHERE id = %s", c.id)
    assert row == [("ready", "p")]                                            # a failed refresh erases nothing


def test_rate_limited_search_retries_instead_of_no_fresh_previews(client, fake, monkeypatch):  # noqa: F811
    from app.ai import client as ai_client, jobs, sources
    monkeypatch.setattr(sources, "find_articles", REAL_FIND_ARTICLES)    # the real one, with a rate-limited search

    def limited(query):
        raise ai_client.RateLimited("groq 429", 420.0)
    monkeypatch.setattr(ai_client, "groq_search", limited)
    monkeypatch.setattr(sources, "_check_all", lambda cands, *a: [])      # no ESPN articles pass
    out = jobs.write_for_game("preview", fake["ids"]["BUF"], "midweek")
    assert out == {"status": "failed", "id": None, "retry_after": 420.0}
    assert _sql("SELECT count(*) FROM ai_texts WHERE game_id = %s", fake["ids"]["BUF"]) == [(0,)]


def test_nightly_job_rewrites_a_recap_after_a_stat_correction(client, fake):  # noqa: F811
    from app import db
    from app.ai import jobs, store
    gid = fake["ids"]["DAL"]
    _sql("UPDATE games SET start_time = now() - interval '5 hours' WHERE id = %s", gid)
    with db.connect() as conn:
        c = store.claim(conn, gid, "recap", "0-0", reason="final")          # written from an old score
        store.save(conn, c, ready("recap"))
    assert gid in jobs.unwritten_recaps("nfl", timedelta(days=2))


def test_api_returns_at_once_when_no_row_will_come(client, fake, monkeypatch):  # noqa: F811
    import time
    import app.main as main
    monkeypatch.setattr(main.ai_jobs, "write_for_game", lambda *a, **k: {"status": "skipped", "id": None})
    t = time.monotonic()
    out = client.get(f"/api/games/{fake['ids']['BUF']}/ai").json()
    assert out["status"] == "missing" and time.monotonic() - t < 3


def test_busy_row_skips_the_article_fetch(client, fake, monkeypatch):  # noqa: F811
    from app import db
    from app.ai import jobs, sources, store
    gid = fake["ids"]["BUF"]
    with db.connect() as conn:
        store.claim(conn, gid, "preview", "x", reason="midweek")
    monkeypatch.setattr(sources, "find_articles", lambda *a, **k: pytest.fail("fetched articles while busy"))
    assert jobs.write_for_game("preview", gid, "open")["status"] == "busy"


def test_games_off_the_prewrite_list_are_left_for_page_opens(client, fake, monkeypatch, tmp_path):  # noqa: F811
    from temporalio.testing import ActivityEnvironment
    from app.temporal import activities
    from app.temporal.models import TextJob
    favorite(monkeypatch, tmp_path, "NE")                         # none of the fixture's teams
    started = []
    monkeypatch.setattr(activities, "start_text", started.append)
    dal = _sql("SELECT espn_id FROM games WHERE id = %s", fake["ids"]["DAL"])[0][0]
    env = ActivityEnvironment()
    assert env.run(activities.write_text, TextJob("recap", "nfl", dal, "final")) == "skipped"
    env.run(activities.generate_preview, "nfl", "401900001")
    assert started == [] and fake["calls"]["recap"] == 0
    _sql("UPDATE games SET start_time = now() + interval '1 day' WHERE id = %s", fake["ids"]["BUF"])
    assert env.run(activities.texts_to_write, "nfl", "previews", 144) == []
    favorite(monkeypatch, tmp_path, "BUF")
    assert env.run(activities.texts_to_write, "nfl", "previews", 144) == ["401900001"]


# ---------- worker-side savings (2026-10-01): preflight, a saved extract, unchanged headlines, recap_due ----------

def test_preflight_turns_a_text_away_before_it_is_claimed_or_drafted(client, fake, monkeypatch):  # noqa: F811
    from app.ai import client as ai_client, jobs
    gid = fake["ids"]["DAL"]
    monkeypatch.setattr(ai_client, "unavailable_for", lambda kind: 600.0)
    assert jobs.write_for_game("recap", gid, "final", preflight=True) == \
        {"status": "failed", "id": None, "retry_after": 600.0}
    assert fake["calls"]["recap"] == 0
    assert _sql("SELECT count(*) FROM ai_texts WHERE game_id = %s", gid) == [(0,)]       # no row claimed
    assert jobs.write_for_game("recap", gid, "open")["status"] == "ready"                 # the api never asks
    assert jobs.write_for_game("recap", gid, "final", preflight=True)["status"] == "current"   # nothing to write


def test_preflight_turns_a_preview_away_after_the_article_lookup_but_before_the_claim(client, fake, monkeypatch):  # noqa: F811
    from app.ai import client as ai_client, jobs, sources
    gid = fake["ids"]["BUF"]
    lookups = []

    def find(page, news, **kw):
        lookups.append(1)
        return [ARTICLE], []
    monkeypatch.setattr(sources, "find_articles", find)
    monkeypatch.setattr(ai_client, "unavailable_for", lambda kind: 600.0)
    assert jobs.write_for_game("preview", gid, "midweek", preflight=True) == \
        {"status": "failed", "id": None, "retry_after": 600.0}
    assert lookups == [1]                                  # the fingerprint needs the articles, so they come first
    assert fake["calls"]["preview"] == []                  # nothing drafted
    assert _sql("SELECT count(*) FROM ai_texts WHERE game_id = %s", gid) == [(0,)]       # nothing claimed


def test_preflight_lets_a_sourceless_preview_through(client, fake, monkeypatch):  # noqa: F811
    from app.ai import client as ai_client, jobs
    fake["articles"] = []
    monkeypatch.setattr(ai_client, "unavailable_for", lambda kind: 600.0)       # no model is needed for "no sources"
    assert jobs.write_for_game("preview", fake["ids"]["BUF"], "midweek", preflight=True)["status"] == "no_sources"


def test_write_text_waits_for_a_limited_model_instead_of_claiming(client, fake, monkeypatch, tmp_path):  # noqa: F811
    favorite(monkeypatch, tmp_path, "DAL")
    from temporalio.exceptions import ApplicationError
    from temporalio.testing import ActivityEnvironment
    from app.ai import client as ai_client
    from app.temporal import activities
    from app.temporal.models import TextJob
    monkeypatch.setattr(ai_client, "unavailable_for", lambda kind: 900.0)
    espn_id = _sql("SELECT espn_id FROM games WHERE id = %s", fake["ids"]["DAL"])[0][0]
    with pytest.raises(ApplicationError) as err:
        ActivityEnvironment().run(activities.write_text, TextJob("recap", "nfl", espn_id, "final"))
    assert err.value.type == "RateLimited" and err.value.next_retry_delay == timedelta(seconds=900)
    assert fake["calls"]["recap"] == 0 and _sql("SELECT count(*) FROM ai_texts") == [(0,)]


def test_a_rate_limited_preview_keeps_its_extract_for_the_retry(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["BUF"]
    ex = {"storylines": [], "edges": {"home": [], "away": []}, "picks": [], "urls": [ARTICLE["url"]]}
    fake["result"] = {"status": "failed", "reason": "rate limited", "retry_after": 300.0, "model": "fake",
                      "extract": ex}
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "failed"
    assert _sql("SELECT status, body IS NULL, extract IS NOT NULL FROM ai_texts WHERE game_id = %s",
                gid) == [("failed", True, True)]
    fake["result"] = None
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "ready"
    assert fake["calls"]["preview"][-1] == ex                                     # the retry skipped the extract call


def test_a_saved_extract_is_not_reused_for_other_articles(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["BUF"]
    ex = {"storylines": [], "edges": {"home": [], "away": []}, "picks": [], "urls": [ARTICLE["url"]]}
    fake["result"] = {"status": "failed", "reason": "rate limited", "retry_after": 300.0, "model": "fake",
                      "extract": ex}
    jobs.write_for_game("preview", gid, "midweek")
    fake["result"] = None
    fake["articles"] = [dict(ARTICLE, url="https://www.espn.com/nfl/story/_/id/9/z")]
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "ready"
    assert fake["calls"]["preview"][-1] is None


def test_a_check_failure_keeps_no_new_extract(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["BUF"]
    fake["result"] = {"status": "failed", "reason": "check failed twice", "model": "fake"}
    jobs.write_for_game("preview", gid, "midweek")
    assert _sql("SELECT extract IS NULL FROM ai_texts WHERE game_id = %s", gid) == [(True,)]


def test_headlines_with_nothing_new_are_not_written_again(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, writer
    calls = []

    def write(news, finals):
        calls.append(1)
        return {"status": "ready", "body": {"items": []}, "model": "fake"}
    monkeypatch.setattr(writer, "write_headlines", write)
    _sql("INSERT INTO news_items (league, espn_id, headline, published_at) VALUES ('nfl', '1', 'Big news', now())")
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready"
    first = _sql("SELECT id FROM ai_texts WHERE kind = 'headlines'")
    assert jobs.write_headlines(["nfl"], "schedule") == {"status": "current", "id": first[0][0]}
    assert jobs.write_headlines(["nfl"], "manual")["status"] == "ready"                    # a manual run always writes
    _sql("INSERT INTO news_items (league, espn_id, headline, published_at) VALUES ('nfl', '2', 'More news', now())")
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready"
    assert len(calls) == 3


def test_a_failed_headlines_run_is_tried_again_with_the_same_inputs(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, writer
    results = iter([{"status": "failed", "reason": "check failed twice", "model": "fake"},
                    {"status": "ready", "body": {"items": []}, "model": "fake"}])
    monkeypatch.setattr(writer, "write_headlines", lambda news, finals: next(results))
    _sql("INSERT INTO news_items (league, espn_id, headline, published_at) VALUES ('nfl', '1', 'Big news', now())")
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "failed"
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready"


def test_headlines_preflight_turns_the_run_away_when_the_models_are_limited(client, fake, monkeypatch):  # noqa: F811
    from app.ai import client as ai_client, jobs, writer
    monkeypatch.setattr(ai_client, "unavailable_for", lambda kind: 120.0)
    monkeypatch.setattr(writer, "write_headlines", lambda *a: pytest.fail("wrote while limited"))
    _sql("INSERT INTO news_items (league, espn_id, headline, published_at) VALUES ('nfl', '1', 'Big news', now())")
    assert jobs.write_headlines(["nfl"], "schedule", preflight=True) == \
        {"status": "failed", "id": None, "retry_after": 120.0}
    assert _sql("SELECT count(*) FROM ai_texts WHERE kind = 'headlines'") == [(0,)]


def test_recap_due_follows_the_prewrite_list(client, fake, monkeypatch, tmp_path):  # noqa: F811
    from temporalio.testing import ActivityEnvironment
    from app.ai import scope
    from app.temporal import activities
    dal = _sql("SELECT espn_id FROM games WHERE id = %s", fake["ids"]["DAL"])[0][0]
    env = ActivityEnvironment()
    favorite(monkeypatch, tmp_path, "DAL")
    assert env.run(activities.recap_due, "nfl", dal) is True
    favorite(monkeypatch, tmp_path, "NE")                          # none of the fixture's teams
    assert env.run(activities.recap_due, "nfl", dal) is False
    favorite(monkeypatch, tmp_path, "DAL")
    monkeypatch.setattr(scope, "AI_LEAGUES", {"ncaaf"})            # a favorite in a league without AI text
    assert env.run(activities.recap_due, "nfl", dal) is False


# ---------- the rejection cap (Adam, 2026-10-01): REJECTION_CAP failed writes for the same inputs, then wait ----------

REJECTED = {"status": "failed", "reason": "check failed twice: x", "model": "fake"}


def rejections(gid, kind):
    return _sql("SELECT rejections FROM ai_texts WHERE game_id = %s AND kind = %s", gid, kind)[0][0]


def cap():
    from app.ai import store
    return store.REJECTION_CAP


def test_a_text_rejected_cap_times_is_not_written_again(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["DAL"]
    fake["result"] = REJECTED
    for _ in range(cap()):
        assert jobs.write_for_game("recap", gid, "nightly")["status"] == "failed"
    assert rejections(gid, "recap") == cap()
    out = jobs.write_for_game("recap", gid, "nightly")
    assert out["status"] == "capped" and fake["calls"]["recap"] == cap()             # nothing was written
    assert jobs.write_for_game("recap", gid, "refresh")["status"] == "capped"        # no caller gets past it
    assert jobs.write_for_game("recap", gid, "manual")["status"] == "failed"         # the runbook forces it
    assert fake["calls"]["recap"] == cap() + 1


def test_rate_limits_and_a_missing_setup_never_count_as_rejections(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["DAL"]
    fake["result"] = {"status": "failed", "reason": "rate limited", "retry_after": 60.0, "model": "fake"}
    for _ in range(3):
        assert jobs.write_for_game("recap", gid, "nightly")["status"] == "failed"
    fake["result"] = {"status": "failed", "reason": "no key", "unconfigured": True, "model": "fake"}
    for _ in range(3):
        assert jobs.write_for_game("recap", gid, "nightly")["status"] == "failed"
    assert fake["calls"]["recap"] == 6 and rejections(gid, "recap") == 0


def test_a_ready_text_clears_the_count(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["DAL"]
    fake["result"] = REJECTED
    jobs.write_for_game("recap", gid, "nightly")
    assert rejections(gid, "recap") == 1
    fake["result"] = None
    assert jobs.write_for_game("recap", gid, "nightly")["status"] == "ready"
    assert rejections(gid, "recap") == 0


def test_new_inputs_lift_the_cap(client, fake):  # noqa: F811
    from app.ai import jobs
    gid = fake["ids"]["BUF"]
    fake["result"] = REJECTED
    for _ in range(cap()):
        jobs.write_for_game("preview", gid, "midweek")
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "capped"
    # other articles: a different fingerprint, so it is written (and counted afresh)
    fake["articles"] = [dict(ARTICLE, url="https://www.espn.com/nfl/story/_/id/9/z")]
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "failed"
    assert rejections(gid, "preview") == 1 and len(fake["calls"]["preview"]) == cap() + 1
    # a moved kickoff: a different game day
    fake["result"] = None
    _sql("UPDATE games SET start_time = start_time + interval '1 day' WHERE id = %s", gid)
    assert jobs.write_for_game("preview", gid, "midweek")["status"] == "ready"


def test_the_api_does_not_rewrite_a_text_rejected_cap_times_for_this_score(client, fake):  # noqa: F811
    gid = fake["ids"]["DAL"]
    fake["result"] = REJECTED
    for expected in range(1, cap() + 1):
        assert client.get(f"/api/games/{gid}/ai").json()["status"] == "failed"
        assert fake["calls"]["recap"] == expected
        _sql("UPDATE ai_texts SET updated_at = now() - interval '40 minutes' WHERE game_id = %s", gid)
    assert client.get(f"/api/games/{gid}/ai").json()["status"] == "failed"          # past the 30 quiet minutes ...
    assert fake["calls"]["recap"] == cap()                                            # ... and still no more writes


def test_the_cap_reaches_the_worker_as_a_result_not_an_error(client, fake, monkeypatch, tmp_path):  # noqa: F811
    favorite(monkeypatch, tmp_path, "DAL")
    from temporalio.testing import ActivityEnvironment
    from app.temporal import activities
    from app.temporal.models import TextJob
    espn_id = _sql("SELECT espn_id FROM games WHERE id = %s", fake["ids"]["DAL"])[0][0]
    fake["result"] = REJECTED
    env = ActivityEnvironment()
    assert [env.run(activities.write_text, TextJob("recap", "nfl", espn_id, "nightly")) for _ in range(cap() + 1)] == \
        ["failed"] * cap() + ["capped"]                      # "capped" ends the workflow: no Temporal retry


def test_headlines_rejected_cap_times_wait_for_new_news(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, writer
    results = iter([dict(REJECTED) for _ in range(cap())]
                   + [{"status": "ready", "body": {"items": []}, "model": "fake"}, dict(REJECTED)])
    calls = []

    def write(news, finals):
        calls.append(1)
        return next(results)
    monkeypatch.setattr(writer, "write_headlines", write)
    news = "INSERT INTO news_items (league, espn_id, headline, published_at) VALUES ('nfl', '%s', '%s', now())"
    _sql(news % ("1", "Big news"))
    assert [jobs.write_headlines(["nfl"], "schedule")["status"] for _ in range(cap())] == ["failed"] * cap()
    assert jobs.write_headlines(["nfl"], "schedule") == {"status": "capped", "id": None}
    assert len(calls) == cap()
    _sql(news % ("2", "More news"))                          # new inputs: written (a ready set), which clears it
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready"
    _sql(news % ("3", "Even more"))
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "failed"          # counted afresh, not capped
    assert len(calls) == cap() + 2


def test_a_manual_headlines_run_ignores_the_cap(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, writer
    monkeypatch.setattr(writer, "write_headlines", lambda news, finals: dict(REJECTED))
    _sql("INSERT INTO news_items (league, espn_id, headline, published_at) VALUES ('nfl', '1', 'Big news', now())")
    for _ in range(cap()):
        jobs.write_headlines(["nfl"], "schedule")
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "capped"
    assert jobs.write_headlines(["nfl"], "manual")["status"] == "failed"


# ---------- review fixes (2026-10-01) ----------

def test_claim_resets_the_count_when_the_inputs_change(client, fake):  # noqa: F811
    from app import db
    from app.ai import store
    gid = fake["ids"]["DAL"]

    def fail(basis, fingerprint=None):
        with db.connect() as conn:
            c = store.claim(conn, gid, "recap", basis, fingerprint)
            assert store.save(conn, c, {"status": "failed", "reason": "check failed twice"})
        return rejections(gid, "recap")
    assert [fail("1-0") for _ in range(cap())] == list(range(1, cap() + 1))
    assert fail("2-0") == 1                                # a corrected score: new inputs, counted afresh
    assert fail("2-0") == 2
    assert fail("2-0", "other-fingerprint") == 1           # a changed fact sheet: new inputs too


def test_headlines_rejections_are_counted_since_the_last_ready_set(client, fake):  # noqa: F811
    from app import db
    from app.ai import store
    failed = {"status": "failed", "reason": "check failed twice"}
    ready_set = {"status": "ready", "body": {"items": []}, "model": "fake"}
    with db.connect() as conn:
        for n in range(store.REJECTION_CAP):
            assert store.headlines_rejected_out(conn, "fp-x") is False       # not yet
            store.save_headlines(conn, failed, "schedule", "fp-x")
        assert store.headlines_rejected_out(conn, "fp-x") is True
        assert store.headlines_rejected_out(conn, "fp-y") is False          # other inputs
        store.save_headlines(conn, ready_set, "schedule", "fp-z")
        assert store.headlines_rejected_out(conn, "fp-x") is False          # a ready set since: counted afresh
        store.save_headlines(conn, dict(failed, retry_after=60.0), "schedule", "fp-x")      # a rate limit isn't one
        assert store.headlines_rejected_out(conn, "fp-x") is False


def test_headlines_are_written_again_when_a_final_changes(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, writer
    calls = []
    monkeypatch.setattr(writer, "write_headlines", lambda news, finals: (
        calls.append(finals), {"status": "ready", "body": {"items": []}, "model": "fake"})[1])
    gid = fake["ids"]["DAL"]
    _sql("UPDATE games SET start_time = now() - interval '2 hours' WHERE id = %s", gid)    # a final inside the window
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready" and len(calls[0]) >= 1
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "current"
    _sql("UPDATE games SET home_score = home_score + 1 WHERE id = %s", gid)                # a stat correction
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready" and len(calls) == 2


def test_a_manual_job_writes_any_game_and_lifts_the_cap(client, fake, monkeypatch, tmp_path):  # noqa: F811
    favorite(monkeypatch, tmp_path, "NE")                                  # DAL is not on the pre-write list
    from temporalio.testing import ActivityEnvironment
    from app.ai import jobs
    from app.temporal import activities
    from app.temporal.models import TextJob
    gid = fake["ids"]["DAL"]
    espn_id = _sql("SELECT espn_id FROM games WHERE id = %s", gid)[0][0]
    env = ActivityEnvironment()
    assert env.run(activities.write_text, TextJob("recap", "nfl", espn_id, "nightly")) == "skipped"
    fake["result"] = REJECTED
    assert [jobs.write_for_game("recap", gid, "open")["status"] for _ in range(cap() + 1)] ==         ["failed"] * cap() + ["capped"]
    fake["result"] = None
    assert env.run(activities.write_text, TextJob("recap", "nfl", espn_id, "manual")) == "ready"      # the runbook
    assert rejections(gid, "recap") == 0


def test_write_text_and_the_worker_are_wired_for_preflight(client, fake, monkeypatch):  # noqa: F811
    from temporalio.testing import ActivityEnvironment
    from app.temporal import activities
    from app.temporal.models import TextJob
    seen = {}

    def headlines(leagues, reason, preflight=False):
        seen.update(leagues=leagues, reason=reason, preflight=preflight)
        return {"status": "current", "id": None}
    monkeypatch.setattr(activities.ai_jobs, "write_headlines", headlines)
    assert ActivityEnvironment().run(activities.write_text, TextJob("headlines", "nfl,ncaaf", None, "schedule")) == "current"
    assert seen == {"leagues": ["nfl", "ncaaf"], "reason": "schedule", "preflight": True}
    assert activities.recap_due in activities.ALL and activities.write_text in activities.AI


def test_unavailable_for_with_the_shared_quota(client, monkeypatch):  # noqa: F811
    from app.ai import client as ai_client
    from app.ai.quota import DbQuota
    q = DbQuota()
    monkeypatch.setattr(ai_client, "quota", q)
    monkeypatch.setattr(ai_client, "WRITERS", ["openai/w"])
    monkeypatch.setattr(ai_client, "CHECKERS", ["qwen/q"])
    monkeypatch.setattr(ai_client, "GROQ_TPD", 151000)
    assert ai_client.unavailable_for("recap") is None
    q.cool("openai/w", 300)
    assert 290 < ai_client.unavailable_for("recap") <= 300                  # the writer is cooling (in Postgres)
    _sql("DELETE FROM ai_cooling")                                           # the writer is back
    q.reserve(["qwen/q"], 150000, 200000, wait=False)                        # the checker has spent its day
    wait = ai_client.unavailable_for("recap")
    assert wait is not None and 80000 < wait <= 86400


# ---------- a page open that runs out of quota hands the text to the worker (Oct 1) ----------

RATE_LIMITED = {"status": "failed", "reason": "rate limited: no room this minute", "retry_after": 60.0,
                "model": "fake"}


def test_a_rate_limited_open_is_handed_to_the_worker(client, fake, monkeypatch):  # noqa: F811
    from app import main
    handed = []
    monkeypatch.setattr(main, "_hand_to_worker", lambda kind, row: handed.append((kind, row["espn_id"])) or True)
    gid = fake["ids"]["DAL"]
    fake["result"] = RATE_LIMITED
    first = client.get(f"/api/games/{gid}/ai").json()
    assert first["status"] == "queued" and first["body"] is None
    assert handed == [("recap", _sql("SELECT espn_id FROM games WHERE id = %s", gid)[0][0])]
    assert _sql("SELECT status, reason FROM ai_texts WHERE game_id = %s", gid) == [("failed", "queued")]
    assert client.get(f"/api/games/{gid}/ai").json()["status"] == "queued"       # the next pull says so at once
    assert fake["calls"]["recap"] == 1 and len(handed) == 1


def test_without_the_worker_a_rate_limited_open_falls_back_as_before(client, fake, monkeypatch):  # noqa: F811
    from app import main
    monkeypatch.setattr(main, "TEMPORAL_ADDRESS", None)
    fake["result"] = RATE_LIMITED
    assert client.get(f"/api/games/{fake['ids']['DAL']}/ai").json()["status"] == "failed"
    assert _sql("SELECT reason FROM ai_texts WHERE game_id = %s", fake["ids"]["DAL"]) == [("open",)]


def test_a_rejected_text_is_not_handed_over(client, fake, monkeypatch):  # noqa: F811
    from app import main
    monkeypatch.setattr(main, "_hand_to_worker", lambda kind, row: pytest.fail("handed over a rejected text"))
    fake["result"] = {"status": "failed", "reason": "check failed twice: x", "model": "fake"}
    assert client.get(f"/api/games/{fake['ids']['DAL']}/ai").json()["status"] == "failed"


def test_the_worker_writes_a_handed_over_text_off_the_prewrite_list(client, fake, monkeypatch, tmp_path):  # noqa: F811
    favorite(monkeypatch, tmp_path, "NE")                                  # DAL is not on the pre-write list
    from temporalio.testing import ActivityEnvironment
    from app.temporal import activities
    from app.temporal.models import TextJob
    espn_id = _sql("SELECT espn_id FROM games WHERE id = %s", fake["ids"]["DAL"])[0][0]
    env = ActivityEnvironment()
    assert env.run(activities.write_text, TextJob("recap", "nfl", espn_id, "nightly")) == "skipped"
    assert env.run(activities.write_text, TextJob("recap", "nfl", espn_id, "open")) == "ready"


def test_hand_to_worker_starts_the_text_workflow_and_survives_temporal_down(monkeypatch):
    from temporalio.client import Client
    from app import main
    from app.temporal import starter
    from app.temporal.models import TextJob
    monkeypatch.setattr(main, "TEMPORAL_ADDRESS", "temporal:7233")
    started = []

    async def connect(addr, namespace="default"):
        return "client"

    async def start(client_, job):
        started.append(job)
        return True
    monkeypatch.setattr(Client, "connect", staticmethod(connect))
    monkeypatch.setattr(starter, "start_text", start)
    row = {"id": 1, "league": "nfl", "espn_id": "401"}
    assert main._hand_to_worker("preview", row) is True
    assert started == [TextJob("preview", "nfl", "401", "open")]

    async def down(addr, namespace="default"):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(Client, "connect", staticmethod(down))
    assert main._hand_to_worker("preview", row) is False                   # the page keeps its fallback
    monkeypatch.setattr(main, "TEMPORAL_ADDRESS", None)
    assert main._hand_to_worker("preview", row) is False


def test_headlines_read_every_outlets_stored_news_and_the_league_goes_to_the_writer(client, fake, monkeypatch):  # noqa: F811
    from app.ai import jobs, writer
    seen = {}

    def write(news, finals):
        seen["news"] = news
        return {"status": "ready", "body": {"items": [{"text": "x", "url": news[0]["url"], "league": news[0]["league"],
                                                        "outlet": "Yahoo Sports", "final": False}]}}
    monkeypatch.setattr(writer, "write_headlines", write)
    ins = "INSERT INTO news_items (league, espn_id, headline, url, published_at) VALUES (%s)"
    for league, key, url, ago in (("nfl", "1", "https://www.espn.com/nfl/story/_/id/1/a", 3),
                                  ("nfl", "x:aa", "https://sports.yahoo.com/articles/b-1.html", 1),
                                  ("nfl", "x:bb", "https://www.cbssports.com/nfl/news/c/", 2),
                                  ("nfl", "x:cc", "https://sports.yahoo.com/articles/old-1.html", 80)):
        _sql(ins % f"'{league}', '{key}', 'h {key}', '{url}', now() - interval '{ago} hours'")
    assert jobs.write_headlines(["nfl"], "schedule")["status"] == "ready"
    assert [n["headline"] for n in seen["news"]] == ["h x:aa", "h x:bb", "h 1"]              # newest first, two days only
    assert {n["league"] for n in seen["news"]} == {"nfl"}
    out = client.get("/api/headlines").json()
    assert out["items"][0]["league"] == "nfl" and out["items"][0]["outlet"] == "Yahoo Sports"


def test_previews_still_read_only_espn_news_first(client, fake):  # noqa: F811
    from app import db
    from app.ai import store
    _sql("INSERT INTO news_items (league, espn_id, headline, url, published_at) VALUES "
         "('nfl', '77', 'h espn', 'https://www.espn.com/nfl/story/_/id/77/a', now()), "
         "('nfl', 'x:ab', 'h yahoo', 'https://sports.yahoo.com/articles/b-1.html', now())")
    with db.connect() as conn:
        assert [r["headline"] for r in store.news(conn, ["nfl"], espn_only=True)] == ["h espn"]
        assert sorted(r["headline"] for r in store.news(conn, ["nfl"])) == ["h espn", "h yahoo"]
