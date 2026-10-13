"""Which stories the headlines are written from (jobs.balanced, jobs.is_video): pure, no database."""
from datetime import datetime, timedelta, timezone

from app.ai import jobs

T0 = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def story(league, host, i, minutes_ago):
    return {"league": league, "url": f"https://{host}/{league}/{i}", "headline": f"{league} {host} {i}",
            "published_at": T0 - timedelta(minutes=minutes_ago)}


def test_balanced_takes_stories_in_turn_across_league_and_outlet():
    # ESPN posts the most; a plain newest-first cut would be all ESPN NFL.
    news = [story("nfl", "www.espn.com", i, i) for i in range(30)]
    news += [story("nfl", "sports.yahoo.com", i, 40 + i) for i in range(5)]
    news += [story("ncaaf", "www.espn.com", i, 60 + i) for i in range(5)]
    news += [story("ncaaf", "www.cbssports.com", i, 70 + i) for i in range(5)]
    news.sort(key=lambda n: n["published_at"], reverse=True)
    out = jobs.balanced(news, 12)
    assert len(out) == 12
    groups = {(n["league"], n["url"].split("/")[2]) for n in out}
    assert groups == {("nfl", "www.espn.com"), ("nfl", "sports.yahoo.com"), ("ncaaf", "www.espn.com"),
                      ("ncaaf", "www.cbssports.com")}
    assert [n["published_at"] for n in out] == sorted((n["published_at"] for n in out), reverse=True)


def test_balanced_is_deterministic_and_handles_a_short_or_empty_pool():
    news = [story("nfl", "www.espn.com", i, i) for i in range(3)]
    assert jobs.balanced(news, 30) == jobs.balanced(list(news), 30) and len(jobs.balanced(news, 30)) == 3
    assert jobs.balanced([], 30) == []


def test_video_clip_pages_are_never_headline_links():
    assert jobs.is_video("https://www.espn.com/video/clip/_/id/1")
    assert jobs.is_video("https://apnews.com/VIDEO/abc")
    assert not jobs.is_video("https://www.espn.com/nfl/story/_/id/1/x")
    assert not jobs.is_video(None)
