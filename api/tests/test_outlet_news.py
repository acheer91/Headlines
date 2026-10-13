"""The other outlets' news (app/outlet_news.py): parsing, the filters, the dedupe key, and a league fetch over a fake
transport. No network."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import outlet_news as on

NOW = datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc)


def rss(*items: tuple) -> bytes:
    """items: (title, link, pubDate, description)"""
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{d}</pubDate><description>{x}</description></item>"
                   for t, l, d, x in items)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


YAHOO = on.Feed("Yahoo Sports", "https://sports.yahoo.com/nfl/rss/")
CBS = on.Feed("CBS Sports", "https://www.cbssports.com/rss/headlines/nfl/", path="/nfl/")
FOX = on.Feed("FOX Sports", "https://www.foxsports.com/feedout/syndicatedContent?categories=syndication_nfl",
              path="/stories/nfl/")
ATHLETIC = on.Feed("The Athletic", "https://www.nytimes.com/athletic/rss/nfl/", path="/athletic/")
BING_AP = on.Feed("AP", "https://www.bing.com/news/search", kind="bing", query="NFL site:apnews.com")


def test_an_item_is_cleaned_dated_and_keyed():
    body = rss(("Chiefs &amp; Bills: &lt;b&gt;big&lt;/b&gt; win", "https://sports.yahoo.com/articles/a-1.html?x=1#top",
                "Fri, 02 Oct 2026 22:30:15 +0000", "&lt;p&gt;Patriots   make a\n decision&lt;/p&gt;"))
    [item] = on.parse_feed(body, YAHOO, now=NOW)
    assert item["headline"] == "Chiefs & Bills: big win"
    assert item["description"] == "Patriots make a decision"
    assert item["url"] == "https://sports.yahoo.com/articles/a-1.html"            # no query or fragment
    assert item["published_at"] == datetime(2026, 10, 2, 22, 30, 15, tzinfo=timezone.utc)
    assert item["key"] == on.item_key("http://SPORTS.yahoo.com/articles/a-1.html/")  # same article, same key
    assert item["key"].startswith("x:")


def test_descriptions_are_cut():
    [item] = on.parse_feed(rss(("T", "https://sports.yahoo.com/articles/a-1.html", "Fri, 02 Oct 2026 22:30:15 +0000",
                                "word " * 200)), YAHOO, now=NOW)
    assert len(item["description"]) <= on.DESCRIPTION_CHARS


def test_ads_other_sites_other_sports_and_undated_items_are_dropped():
    d = "Fri, 02 Oct 2026 22:30:15 +0000"
    body = rss(
        ("Use DraftKings promo code to get $150 in bonus bets", "https://www.cbssports.com/nfl/news/ok/", d, ""),
        ("A story", "https://www.cbssports.com/betting/news/a-story/", d, ""),
        ("College hoops", "https://www.cbssports.com/college-basketball/news/x/", d, ""),
        ("Elsewhere", "https://example.com/nfl/news/y/", d, ""),
        ("No date", "https://www.cbssports.com/nfl/news/z/", "", ""),
        ("Future", "https://www.cbssports.com/nfl/news/f/", "Sat, 03 Oct 2026 22:30:15 +0000", ""),
        ("Real one", "https://www.cbssports.com/nfl/news/real/", d, ""))
    assert [i["headline"] for i in on.parse_feed(body, CBS, now=NOW)] == ["Real one"]


def test_fox_keeps_only_its_leagues_stories_and_the_athletic_only_its_own():
    d = "Fri, 02 Oct 2026 18:09:57 -0400"
    fox = rss(("Braves", "https://www.foxsports.com/stories/mlb/braves", d, ""),
              ("Bills", "https://www.foxsports.com/stories/nfl/bills-win", d, ""))
    assert [i["headline"] for i in on.parse_feed(fox, FOX, now=NOW)] == ["Bills"]
    nyt = rss(("Eagles", "https://www.nytimes.com/athletic/7653620/2026/10/02/eagles/", d, ""),
              ("Election", "https://www.nytimes.com/2026/10/02/us/politics/x.html", d, ""))
    [item] = on.parse_feed(nyt, ATHLETIC, now=NOW)
    assert (item["headline"], on.sources.outlet(item["url"])) == ("Eagles", "The Athletic")
    assert item["published_at"] == datetime(2026, 10, 2, 22, 9, 57, tzinfo=timezone.utc)      # -0400 -> UTC


def test_bing_results_use_the_real_address_and_pacific_dates():
    link = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fapnews.com%2farticle%2fcolts-1&amp;c=1"
    other = "http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fwww.example.com%2fa&amp;c=1"
    body = rss(("Colts story", link, "Fri, 02 Oct 2026 13:07:00 GMT", "snippet"),
               ("Not AP", other, "Fri, 02 Oct 2026 13:07:00 GMT", ""))
    [item] = on.parse_feed(body, BING_AP, now=NOW)
    assert item["url"] == "https://apnews.com/article/colts-1"
    # Bing's "GMT" is Pacific time: 13:07 PDT is 20:07 UTC.
    assert item["published_at"] == datetime(2026, 10, 2, 20, 7, tzinfo=timezone.utc)


def test_a_feed_is_newest_first_capped_and_one_item_per_article():
    items = [(f"Story {n}", f"https://sports.yahoo.com/articles/s-{n}.html",
              (datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=n)).strftime("%a, %d %b %Y %H:%M:%S +0000"),
              "") for n in range(30)]
    items.append(items[3])                                                       # the same article twice
    out = on.parse_feed(rss(*items), YAHOO, now=NOW)
    assert len(out) == on.PER_FEED
    assert out[0]["headline"] == "Story 29" and len({i["key"] for i in out}) == on.PER_FEED


@pytest.mark.parametrize("bad", [b"<!DOCTYPE rss [<!ENTITY a 'b'>]><rss/>", b"<rss><!ENTITY x 'y'></rss>"])
def test_a_feed_that_declares_entities_is_refused(bad):
    with pytest.raises(ValueError):
        on.parse_feed(bad, YAHOO, now=NOW)


def test_off_topic_stories_from_a_search_are_dropped():
    nfl = {"headline": "Sean McVay says NFL agrees officials erred", "description": None}
    team = {"headline": "Bills emerge as favorites", "description": ""}
    mlb = {"headline": "What Would a Dodgers Three-peat Mean for MLB's Labor Battle?", "description": "Baseball"}
    assert on.about_league(nfl, "nfl", []) and on.about_league(team, "nfl", ["Bills"])
    assert not on.about_league(mlb, "nfl", ["Bills"])
    cfb = {"headline": "College Football Week 5: No. 8 Florida", "description": None}
    assert on.about_league(cfb, "ncaaf", []) and not on.about_league(nfl, "ncaaf", [])


def test_a_league_without_outlet_feeds_has_none():
    assert on.feeds("epl") == [] and on.fetch_league("epl") == []
    assert {f.outlet for f in on.feeds("nfl")} == {"Yahoo Sports", "CBS Sports", "FOX Sports", "The Athletic", "AP",
                                                   "Sports Illustrated", "The Ringer"}
    assert [f.path for f in on.feeds("ncaaf") if f.outlet == "CBS Sports"] == ["/college-football/"]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_league_merges_the_feeds_and_skips_the_ones_that_fail():
    d = "Fri, 02 Oct 2026 22:30:15 +0000"

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "sports.yahoo.com":
            return httpx.Response(200, content=rss(("Bills win", "https://sports.yahoo.com/articles/bills-1.html", d, "")))
        if host == "www.cbssports.com":
            return httpx.Response(503)
        if host == "www.foxsports.com":
            return httpx.Response(200, content=b"<rss><not xml")
        if host == "www.bing.com":
            if "apnews" in request.url.params["q"]:
                link = "http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fapnews.com%2farticle%2fap-1"
                return httpx.Response(200, content=rss(("Colts NFL story", link, "Fri, 02 Oct 2026 15:00:00 GMT", ""),
                                                       ("Baseball labor", link + "2", "Fri, 02 Oct 2026 15:00:00 GMT", "")))
            return httpx.Response(200, content=rss())
        return httpx.Response(404)
    out = on.fetch_league("nfl", ["Bills"], now=NOW, client=_client(handler))
    assert [(i["outlet"], i["headline"]) for i in out] == [("Yahoo Sports", "Bills win"), ("AP", "Colts NFL story")]


def test_fetch_league_leaves_a_slow_feed_behind(monkeypatch):
    import time
    monkeypatch.setattr(on, "DEADLINE", 0.3)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sports.yahoo.com":
            time.sleep(1.0)
        return httpx.Response(200, content=rss())
    t0 = time.monotonic()
    assert on.fetch_league("nfl", now=NOW, client=_client(handler)) == []
    assert time.monotonic() - t0 < 0.9


def test_balanced_takes_stories_in_turn_across_outlets_and_leagues():
    from app.ai import jobs
    t0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    def row(n, league, url):
        return {"league": league, "headline": f"h{n}", "url": url, "description": None,
                "published_at": t0 + timedelta(minutes=n)}
    # Yahoo posts 20 an hour; the others a few. Newest first, as the table query returns them.
    rows = [row(100 - i, "nfl", f"https://sports.yahoo.com/articles/y-{i}.html") for i in range(20)]
    rows += [row(50, "nfl", "https://www.cbssports.com/nfl/news/c1/"), row(40, "nfl", "https://www.espn.com/nfl/story/_/id/1/a"),
             row(30, "ncaaf", "https://www.espn.com/college-football/story/_/id/2/b"),
             row(20, "nfl", "https://www.nytimes.com/athletic/1/2026/10/02/x/")]
    rows.sort(key=lambda r: r["published_at"], reverse=True)
    out = jobs.balanced(rows, 8)
    assert len(out) == 8
    outlets = {on.sources.outlet(r["url"]) for r in out}
    assert {"ESPN", "CBS Sports", "The Athletic", "Yahoo Sports"} <= outlets and any(r["league"] == "ncaaf" for r in out)
    assert sum(on.sources.outlet(r["url"]) == "Yahoo Sports" for r in out) <= 4        # not the whole feed
    assert out == jobs.balanced(rows, 8)                                               # the fingerprint reads it: stable
    assert [r["published_at"] for r in out] == sorted((r["published_at"] for r in out), reverse=True)
    assert jobs.balanced([], 8) == [] and len(jobs.balanced(rows, 100)) == len(rows)


def test_a_video_clip_is_never_the_link_under_a_headline():
    from app.ai import jobs
    assert jobs.is_video("https://www.espn.com/video/clip/_/id/50087132/mark-keenum-thrilled")
    assert jobs.is_video("https://apnews.com/video/blood-brothers-and-fake-urination")
    assert not jobs.is_video("https://www.espn.com/nfl/story/_/id/50086883/49ers-wr-mike-evans") and not jobs.is_video(None)
