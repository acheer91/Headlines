"""News from the outlets besides ESPN, for the Home headlines (Adam, 2026-10-02: "in addition to ESPN, the other
publications we discussed"): Yahoo Sports, CBS Sports, FOX Sports and The Athletic by their own league RSS feeds, and
AP, Sports Illustrated and The Ringer (no feed of their own that answers a script) by Bing News RSS limited to the
site. Keyless and free: no search model, no tokens (the PRD's "no Google search" for headlines still holds).

Stored in `news_items` next to ESPN's: the dedupe key (the `espn_id` column) is "x:" plus a hash of the canonical
URL, so one article that two feeds carry is stored once. A feed that is down is skipped, never an error: ESPN's news
and the other feeds still land. Whatever a feed says is untrusted text: stored and shown as data, never read as
instructions; the writer's checks hold on every headline whichever outlet it came from.
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from .ai import dates, sources

log = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
PER_FEED = 15              # newest items kept from one feed
FEED_TIMEOUT = 6.0         # seconds a feed gets to answer
DEADLINE = 10.0            # seconds for all of a league's feeds together: the activity has 30 s, ESPN's call comes first
MAX_BYTES = 2_000_000      # a feed bigger than this is not a headline feed
DESCRIPTION_CHARS = 240    # the writer sees a headline and a snippet, not an article
FUTURE = timedelta(hours=1)

# Betting promotions are ads, not news ("Use DraftKings promo code to get $150 in bonus bets"): never stored.
PROMO = re.compile(r"promo(?:tion(?:al)?)?\s+code|bonus\s+(?:bets?|code)|sign[\s-]?up\s+bonus|\bkalshi\b|\bfanatics\b"
                   r".*\bcode\b|\bpromo\b", re.I)
PROMO_PATH = ("/betting/", "/promo", "/sportsbook")

# What a league's story says, for the feeds that cover every sport (a Bing site search). Pro leagues also match a
# team's nickname (read from our own teams table by the caller).
LEAGUE_TEXT = {
    "nfl": re.compile(r"\bNFL\b"),
    "ncaaf": re.compile(r"college football|\bCFB\b|\bNCAA\b|\bFBS\b|\bHeisman\b|College Football Playoff|\bTop 25\b|"
                        r"\bAP poll\b", re.I),
    "nba": re.compile(r"\bNBA\b"),
}
BING_WORDS = {"nfl": "NFL", "ncaaf": "college football", "nba": "NBA"}


@dataclass(frozen=True)
class Feed:
    outlet: str                  # sources.OUTLETS' name for it
    url: str
    kind: str = "rss"            # "rss": the outlet's own league feed; "bing": a Bing News site search
    path: str | None = None      # keep only article URLs whose path has this (a feed that covers every sport)
    query: str | None = None     # bing: the search


def feeds(league: str) -> list[Feed]:
    """Every feed for a league (none for a league not listed: soccer has no outlet feed yet)."""
    if league not in BING_WORDS:
        return []
    slug = {"nfl": "nfl", "ncaaf": "college-football", "nba": "nba"}[league]
    out = [Feed("Yahoo Sports", f"https://sports.yahoo.com/{slug}/rss/"),
           # CBS's college football feed carries basketball and betting pages too: its stories live under the sport.
           Feed("CBS Sports", f"https://www.cbssports.com/rss/headlines/{slug}/", path=f"/{slug}/"),
           # FOX's feed ignores its category and sends every sport: the article's path says which.
           Feed("FOX Sports", "https://www.foxsports.com/feedout/syndicatedContent?categories=syndication_" +
                slug.replace("-", "_"), path=f"/stories/{slug}/"),
           Feed("The Athletic", f"https://www.nytimes.com/athletic/rss/{slug}/", path="/athletic/")]
    for domain, name in (("apnews.com", "AP"), ("si.com", "Sports Illustrated"), ("theringer.com", "The Ringer")):
        out.append(Feed(name, "https://www.bing.com/news/search", kind="bing",
                        query=f"{BING_WORDS[league]} site:{domain}"))
    return out


def canonical(url: str) -> str:
    """The article's address without query or fragment, lower-case host, no trailing slash: the dedupe identity."""
    p = urlsplit(url.strip())
    return urlunsplit(("https", (p.hostname or "").lower(), p.path.rstrip("/") or "/", "", ""))


def item_key(url: str) -> str:
    return "x:" + hashlib.sha1(canonical(url).encode()).hexdigest()[:20]


def clean(text: str | None, limit: int | None = None) -> str:
    """Feed text -> one line of plain words: tags dropped, entities decoded, whitespace squeezed."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    t = " ".join(t.split())
    return t[:limit].rstrip() if limit else t


def parse_date(value: str | None, *, bing: bool = False) -> datetime | None:
    """An RSS pubDate as a UTC datetime, None when it can't be read. Bing says GMT but means US Pacific time (Phase 0,
    checked 2026-09-27 against the pages' own dates: every one was exactly the Pacific offset later)."""
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if bing:
        dt = dt.replace(tzinfo=PACIFIC)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _real_url(link: str, kind: str) -> str:
    if kind != "bing":
        return link.strip()
    return parse_qs(urlsplit(link).query).get("url", [""])[0].strip()


def parse_feed(body: bytes, feed: Feed, *, now: datetime) -> list[dict]:
    """RSS -> [{key, headline, description, url, published_at, outlet}], newest first, at most PER_FEED. Items
    without a readable date, an address on the outlet's own site, or that are an ad or off the feed's path are
    dropped. A body with a DOCTYPE or entities is refused (nothing here needs them)."""
    if b"<!DOCTYPE" in body[:2000].upper() or b"<!ENTITY" in body.upper():
        raise ValueError("feed declares entities")
    root = ElementTree.fromstring(body)
    out: dict[str, dict] = {}
    for el in root.iter("item"):
        url = _real_url(el.findtext("link") or "", feed.kind)
        title = clean(el.findtext("title"))
        when = parse_date(el.findtext("pubDate"), bing=feed.kind == "bing")
        if not (url.startswith("http") and title and when) or when > now + FUTURE:
            continue
        # The address must be the outlet's own (a Bing result can be anyone's) and the page a story, not an ad.
        if sources.outlet(url) != feed.outlet:
            continue
        path = urlsplit(url).path
        if feed.path and feed.path not in path:
            continue
        if PROMO.search(title) or any(p in path.lower() for p in PROMO_PATH):
            continue
        key = item_key(url)
        out.setdefault(key, {"key": key, "headline": title, "url": canonical(url), "outlet": feed.outlet,
                             "description": clean(el.findtext("description"), DESCRIPTION_CHARS) or None,
                             "published_at": when})
    return sorted(out.values(), key=lambda i: i["published_at"], reverse=True)[:PER_FEED]


def about_league(item: dict, league: str, nicknames: list[str]) -> bool:
    """A story from a feed that covers every sport (Bing's site search) is kept only when it names the league or one
    of its teams: the same search returns a baseball labor story for "NFL" (checked 2026-10-02)."""
    text = f"{item['headline']} {item.get('description') or ''}"
    pat = LEAGUE_TEXT.get(league)
    if pat and pat.search(text):
        return True
    return any(re.search(rf"\b{re.escape(n)}\b", text) for n in nicknames)


def _fetch(feed: Feed, client: httpx.Client) -> bytes:
    params = {"q": feed.query, "format": "rss"} if feed.kind == "bing" else None
    r = client.get(feed.url, params=params, follow_redirects=True)
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise ValueError(f"{len(r.content)} bytes")
    return r.content


def fetch_league(league: str, nicknames: list[str] = (), *, now: datetime | None = None,
                 client: httpx.Client | None = None) -> list[dict]:
    """Every outlet's recent stories for a league, newest first. A feed that fails is logged and skipped; the
    feeds that answer inside DEADLINE are kept, a slow one is left behind."""
    now = now or datetime.now(timezone.utc)
    todo = feeds(league)
    if not todo:
        return []
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(FEED_TIMEOUT), headers={"User-Agent": dates.UA})

    def one(feed: Feed) -> list[dict]:
        items = parse_feed(_fetch(feed, client), feed, now=now)
        if feed.kind == "bing":
            items = [i for i in items if about_league(i, league, nicknames)]
        return items

    pool = ThreadPoolExecutor(max_workers=len(todo))
    try:
        futures = {pool.submit(one, f): f for f in todo}
        done, late = wait(futures, timeout=DEADLINE)
        found: list[dict] = []
        for fut in done:
            feed = futures[fut]
            try:
                found += fut.result()
            except (httpx.HTTPError, ElementTree.ParseError, ValueError) as exc:
                log.warning("%s news from %s failed: %s", league, feed.outlet, exc)
        for fut in late:
            log.warning("%s news from %s too slow, skipped", league, futures[fut].outlet)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if own:
            client.close()
    return sorted(found, key=lambda i: i["published_at"], reverse=True)
