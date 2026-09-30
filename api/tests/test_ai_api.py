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
                                                            for a in articles], extract={"storylines": []})

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
    assert client.get(f"/api/games/{ids['KC']}/ai").json()["kind"] == "one_liner"


def test_preview_without_fresh_articles_is_no_sources(client, fake):  # noqa: F811
    fake["articles"] = []
    assert client.get(f"/api/games/{fake['ids']['BUF']}/ai").json()["status"] == "no_sources"


def test_one_liner_reused_for_15_minutes_then_rewritten(client, fake):  # noqa: F811
    gid = fake["ids"]["KC"]
    client.get(f"/api/games/{gid}/ai")
    client.get(f"/api/games/{gid}/ai")
    assert fake["calls"]["one_liner"] == 1
    _sql("UPDATE ai_texts SET updated_at = now() - interval '16 minutes' WHERE game_id = %s", gid)
    client.get(f"/api/games/{gid}/ai")
    assert fake["calls"]["one_liner"] == 2


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
    assert fake["calls"]["preview"][0] is None and fake["calls"]["preview"][-1] == {"storylines": []}


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
