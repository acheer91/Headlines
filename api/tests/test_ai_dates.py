"""The 8-day rule: the Phase 0 step 0.3 fixtures, now against app.ai.dates."""
import json
from datetime import datetime, timezone

import pytest

from app.ai.dates import filter_fresh, is_fresh, needs_page_fetch, parse_age_range

NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
CASES = [
    # (input, expected keep?)
    ("2026-09-27T12:00:00Z", True),
    ("2026-09-19T18:00:00Z", True),            # exactly 8 days
    ("2026-09-19T17:59:00Z", False),           # 8 days + 1 minute
    ("2026-09-20T10:00:00-04:00", True),
    ("2026-09-21", True),
    ("2026-09-19", False),                     # date-only, could be > 8d
    ("Sun, 20 Sep 2026 14:03:00 GMT", True),
    ("September 22, 2026", True),              # Claude web search page_age style
    ("Sep 18, 2026", False),
    ("20 September 2026", False),              # date-only 7.75d + up to 14h tz slack -> 8.3d worst case
    ("21 September 2026", True),
    ("September 21st, 2026", True),
    ("09/21/2026", True),
    ("3 hours ago", True),                     # Brave `age` style
    ("7 days ago", True),
    ("8 days ago", False),                     # worst case 8.99d
    ("1 week ago", False),                     # could be 13 days
    ("a day ago", True),
    ("yesterday", True),
    ("2 months ago", False),
    ("Sep 20", False),                         # no year -> not confirmable
    ("", False),
    (None, False),
    ("recently", False),
    ("2026-10-05T00:00:00Z", False),           # future -> bad metadata
]


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


combo = [
    # (result, expected keep, why)
    ({"url": "a", "age": "2 days ago",
      "html": '<meta property="article:published_time" content="2026-09-10T00:00:00Z">'},
     False, "API says fresh, page says 17d old -> conflict, oldest wins"),
    ({"url": "b", "html": ld({"@type": "NewsArticle", "datePublished": "2026-09-25T09:00:00Z"})},
     True, "JSON-LD only"),
    ({"url": "c", "html": "<p>no dates</p>"}, False, "no date anywhere"),
    ({"url": "d", "age": "September 24, 2026"}, True, "API age only"),
    # Fix 1: a coarse API age is resolved by an exact page date instead of being dropped
    ({"url": "e", "age": "1 week ago", "html": ld({"@type": "NewsArticle", "datePublished": "2026-09-20T09:00:00Z"})},
     True, "'1 week ago' + page 7.4d -> intersect -> keep"),
    ({"url": "f", "age": "1 week ago", "html": ld({"@type": "NewsArticle", "datePublished": "2026-09-16T09:00:00Z"})},
     False, "'1 week ago' + page 11.4d -> drop"),
    ({"url": "g", "age": "1 week ago"}, False, "'1 week ago' with no page date -> still dropped"),
    # Fix 2: feed timestamp later than page's datePublished (the ESPN +17h case) -> oldest wins
    ({"url": "h", "age": "2026-09-19T20:00:00Z", "html": ld({"@type": "NewsArticle", "datePublished": "2026-09-19T03:00:00Z"})},
     False, "feed 7.9d but page 8.6d -> drop"),
    # Fix 3: video pages (uploadDate), @graph, related items ignored, URL dates
    ({"url": "i", "html": ld({"@type": "VideoObject", "uploadDate": "2026-09-26T19:28:07Z"})},
     True, "VideoObject uploadDate"),
    ({"url": "j", "html": ld({"@graph": [{"@type": "WebSite"}, {"@type": "NewsArticle", "datePublished": "2026-09-24T00:00:00Z"}]})},
     True, "JSON-LD @graph"),
    ({"url": "k", "html": ld({"@type": "NewsArticle", "datePublished": "2026-09-01T00:00:00Z",
                              "relatedLink": [{"@type": "NewsArticle", "datePublished": "2026-09-26T00:00:00Z"}]})},
     False, "nested related-article date ignored"),
    ({"url": "l", "html": ld([{"@type": "NewsArticle", "datePublished": "2026-09-27T14:57:00+00:00"},
                              {"@type": "VideoObject", "uploadDate": "2026-09-17T18:43:56+00:00"}])},
     True, "article date beats older embedded video (CBS case)"),
    ({"url": "https://example.com/2026/09/24/team-preview", "html": "<p>no tags</p>"}, True, "date in URL"),
    ({"url": "https://example.com/2026/09/02/old-story"}, False, "old date in URL"),
]


@pytest.mark.parametrize("value,want", CASES)
def test_date_strings(value, want):
    assert is_fresh(parse_age_range(value, NOW)) == want


@pytest.mark.parametrize("result,want,why", combo)
def test_page_and_search_dates(result, want, why):
    kept, _ = filter_fresh([result], NOW)
    assert bool(kept) == want, why


@pytest.mark.parametrize("age,want", [("1 week ago", True), ("3 hours ago", False), (None, True),
                                      ("2026-09-25T10:00:00Z", False)])
def test_needs_page_fetch(age, want):
    assert needs_page_fetch(age, NOW) == want
