"""The 8-day rule (PRD, hard): an article older than 8 days, or with no date we can confirm, is dropped.

Ported unchanged from Phase 0 step 0.3 (43 fixture tests, live-tested against four search providers).
  * Every date source yields an age *range* (min, max). Keep only if the WORST case (max age) <= 8 days.
  * Sources are intersected; if they conflict, the oldest wins.
  * Page date priority: JSON-LD (article > webpage > video) > meta tags > <time> > date in the URL
    (Adam, 2026-09-29: a date in the URL counts as confirmed).
  * No year, unparseable, or more than a day in the future -> dropped.
"""
from __future__ import annotations

import email.utils
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser


MAX_AGE = timedelta(days=8)
# www.espn.com returns 403 to the default python-requests UA (the site.api host does not).
UA = "headliners/0.4 (personal scores app)"
FUTURE_TOLERANCE = timedelta(days=1)

UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
        "month": 31 * 86400, "year": 366 * 86400}
RELATIVE = re.compile(r"^\s*(\d+|an?|one)\s+(second|minute|hour|day|week|month|year)s?\s+ago\s*$", re.I)
ABS_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d",
               "%B %d, %Y %I:%M %p", "%b %d, %Y, %I:%M %p", "%m/%d/%Y"]


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _date_only(d, now):
    """Date with no time or zone: published between local midnight in UTC+14 and end of day in UTC-12."""
    return now - d - timedelta(hours=36), now - d + timedelta(hours=14)


def parse_age_range(value, now):
    """Return (min_age, max_age) as timedeltas, or None if the date can't be confirmed."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    low = s.lower()

    if low in ("today", "just now"):
        return timedelta(0), timedelta(days=1)
    if low == "yesterday":
        return timedelta(days=1), timedelta(days=2)
    m = RELATIVE.match(s)
    if m:
        n = 1 if m[1].lower() in ("a", "an", "one") else int(m[1])
        unit = UNIT[m[2].lower()]
        return timedelta(seconds=n * unit), timedelta(seconds=(n + 1) * unit)

    dt = None
    try:  # ISO 8601 ("2026-09-20T14:03:00Z", "2026-09-20T14:03:00-04:00")
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if len(s) == 10:
            return _date_only(_utc(dt), now)
    except ValueError:
        pass
    if dt is None:
        try:  # RFC 2822 ("Sun, 20 Sep 2026 14:03:00 GMT")
            dt = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            pass
    if dt is None:
        cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
        for fmt in ABS_FORMATS:
            try:
                d = _utc(datetime.strptime(cleaned, fmt))
            except ValueError:
                continue
            if "%H" in fmt or "%I" in fmt:
                return now - d, now - d
            return _date_only(d, now)
        return None
    age = now - _utc(dt)
    return age, age


def is_fresh(age_range):
    if age_range is None:
        return False
    lo, hi = age_range
    return hi <= MAX_AGE and lo >= -FUTURE_TOLERANCE


# JSON-LD entity types, best first. An article page often also embeds an older VideoObject (seen on CBS:
# article 0.3d old, embedded video 5d old), so video dates only count when the page has no article entity.
LD_RANK = [re.compile(r"Article|BlogPosting|Report", re.I), re.compile(r"WebPage", re.I),
           re.compile(r"VideoObject", re.I)]
LD_DATE_KEYS = ("datePublished", "uploadDate", "dateCreated")


class _MetaDates(HTMLParser):
    KEYS = {"article:published_time", "og:article:published_time", "datepublished", "uploaddate",
            "parsely-pub-date", "sailthru.date", "pubdate", "publish-date", "publish_date", "dc.date",
            "dc.date.issued", "date"}

    def __init__(self):
        super().__init__()
        self.found = {"jsonld": [], "meta": [], "time": []}
        self._ld = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if k in self.KEYS and a.get("content"):
                self.found["meta"].append(a["content"])
        elif tag == "time" and a.get("datetime"):
            self.found["time"].append(a["datetime"])
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._ld = []

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.found["jsonld"] += _jsonld_dates("".join(self._ld))
            self._ld = None


def _jsonld_dates(text):
    """Publish dates of the page's own entities: top-level objects and @graph members only,
    so related-content items nested deeper don't leak in."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    items = [x for i in items if isinstance(i, dict) for x in ([i] + list(i.get("@graph", [])))]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = it.get("@type", "")
        t = " ".join(t) if isinstance(t, list) else str(t)
        rank = next((i for i, rx in enumerate(LD_RANK) if rx.search(t)), None)
        v = next((it[k] for k in LD_DATE_KEYS if isinstance(it.get(k), str)), None)
        if rank is not None and v:
            out.append((rank, v))
    return out


URL_DATE = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?:/|-|$)")


def url_date(url):
    m = URL_DATE.search(url or "")
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def page_dates(html):
    """Return {tier: [date strings]} for jsonld / meta / time."""
    p = _MetaDates()
    p.feed(html)
    return p.found


def _page_range(html, url, now):
    """Best page-side range: first tier (jsonld > meta > time > url) with a parseable date.
    Several dates in one tier -> oldest (conservative)."""
    tiers = page_dates(html) if html else {}
    ld = tiers.get("jsonld", [])
    if ld:  # keep only the best-ranked entity type
        best = min(rank for rank, _ in ld)
        tiers["jsonld"] = [v for rank, v in ld if rank == best]
    for tier in ("jsonld", "meta", "time"):
        rs = [r for r in (parse_age_range(v, now) for v in tiers.get(tier, [])) if r]
        if rs:
            return max(rs, key=lambda r: r[1]), tier
    u = url_date(url)
    if u:
        return parse_age_range(u, now), "url"
    return None, None


def combine(ranges):
    """Intersect independent estimates of the same publish time. If they conflict, the oldest wins."""
    lo, hi = max(r[0] for r in ranges), min(r[1] for r in ranges)
    if lo <= hi:
        return lo, hi
    return max(r[0] for r in ranges), max(r[1] for r in ranges)


def needs_page_fetch(api_age, now):
    """Fetch the page when the API gives no date or a coarse one (wider than a day)."""
    r = parse_age_range(api_age, now)
    return r is None or (r[1] - r[0]) > timedelta(days=1)


def article_age(now, api_age=None, html=None, url=None):
    """Combine page metadata and the search API's age. Returns ((min, max), [sources]) or (None, [])."""
    ranges, used = [], []
    pr, tier = _page_range(html, url, now)
    if pr:
        ranges.append(pr)
        used.append(tier)
    if api_age is not None:
        r = parse_age_range(api_age, now)
        if r:
            ranges.append(r)
            used.append("api")
    if not ranges:
        return None, used
    return combine(ranges), used


def filter_fresh(results, now):
    """results: dicts with 'url' and optional 'age' (API string) / 'html'. Returns (kept, dropped_with_reason)."""
    kept, dropped = [], []
    for r in results:
        rng, used = article_age(now, r.get("age"), r.get("html"), r.get("url"))
        if rng is None:
            dropped.append((r, "no confirmable date"))
        elif is_fresh(rng):
            kept.append(r | {"age_range_days": [round(x.total_seconds() / 86400, 2) for x in rng], "date_source": used})
        else:
            dropped.append((r, f"too old / bad date (max age {rng[1].total_seconds()/86400:.1f}d via {used})"))
    return kept, dropped
