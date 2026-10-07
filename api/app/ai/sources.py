"""Articles for a preview: stored ESPN news first, a Groq search only to fill gaps (Adam, 2026-09-29).

Every article, whatever found it, is fetched and passes the same checks before Gemini sees it:
  * the 8-day rule (dates.py): no confirmable date or older than 8 days -> dropped;
  * relevance: the page names both teams (Phase 0: search returns plenty of unrelated items);
  * readable: enough article text (a paywall or a video page leaves almost none).
At most MAX_ARTICLES are kept, newest first. None left -> the preview shows "No fresh previews".
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from . import client, dates

log = logging.getLogger(__name__)

# Major outlets only (Adam, 2026-09-29): keeps small SEO sites out, and a site-limited search that opens no
# pages costs ~1,600 Groq tokens instead of 30-60K.
OUTLETS = {"espn.com": "ESPN", "apnews.com": "AP", "si.com": "Sports Illustrated", "cbssports.com": "CBS Sports",
           "theathletic.com": "The Athletic", "nytimes.com": "The Athletic", "theringer.com": "The Ringer",
           "foxsports.com": "FOX Sports", "sports.yahoo.com": "Yahoo Sports"}
SEARCH_SITES = ["espn.com", "apnews.com", "si.com", "cbssports.com", "theathletic.com", "theringer.com",
                "foxsports.com", "sports.yahoo.com"]
MAX_ARTICLES = 4          # Adam, 2026-09-29
ENOUGH_FROM_ESPN = 2      # fewer fresh ESPN articles than this -> search
MAX_FETCH = 8             # pages fetched per preview, to stay inside the on-open time budget
MIN_WORDS = 150
# Per article sent to the writer, after relevant_text keeps only the paragraphs about this game (Oct 1: was 700
# words of the page as it came; an extract of 4 such articles took 9,029 of the writer's 8,000 tokens a minute).
MAX_WORDS = 500
MIN_RELEVANT_WORDS = 80   # fewer relevant words than this: the filter missed how the article names things; keep it all
FETCH_TIMEOUT = 8
LEAGUE_WORDS = {"nfl": "NFL", "ncaaf": "college football", "nba": "NBA", "epl": "Premier League", "mls": "MLS"}


def outlet(url: str) -> str | None:
    host = urlsplit(url).hostname or ""
    return next((name for dom, name in OUTLETS.items() if host == dom or host.endswith("." + dom)), None)


class _Text(HTMLParser):
    """Paragraph text and title. <p> inside <script>/<style>/<nav>/<footer>/<aside> is skipped."""
    SKIP = {"script", "style", "nav", "footer", "aside", "noscript"}

    def __init__(self):
        super().__init__()
        self.paras: list[str] = []
        self.title = ""
        self._p: list[str] | None = None
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._p = []
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = dict(attrs)
            if (a.get("property") or "").lower() == "og:title" and a.get("content"):
                self.title = a["content"]

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "p" and self._p is not None:
            t = " ".join("".join(self._p).split())
            if t:
                self.paras.append(t)
            self._p = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._p is not None:
            self._p.append(data)
        elif self._in_title and not self.title:
            self.title = data.strip()


def page_text(html: str) -> tuple[str, str]:
    p = _Text()
    try:
        p.feed(html)
    except Exception:   # malformed markup: keep what was read
        pass
    return p.title, "\n".join(p.paras)


def team_terms(team: dict) -> list[str]:
    """Words that identify a team in text: 'Bears', 'Chicago'; 'Ohio State', 'Buckeyes'."""
    name, short = team.get("name") or "", team.get("short") or ""
    terms = {short} if short else set()
    if name:
        words = name.split()
        terms.add(words[-1])                       # nickname: "Bears", "Buckeyes"
        if len(words) > 1:
            terms.add(" ".join(words[:-1]))        # place: "Chicago", "Ohio State"
    return [t for t in terms if len(t) >= 3]


def mentions(text: str, terms: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(t)}\b", text) for t in terms)


_SUFFIX = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$", re.I)


def player_terms(game: dict) -> list[str]:
    """Last names of the players our own data names for this game (the injury report, the leaders): a paragraph
    about one of them is about this game even when it names neither team."""
    names = [p.get("name") for side in ("home", "away") for p in (game.get("injuries") or {}).get(side) or []]
    names += [(r.get(side) or {}).get("name") for r in (game.get("leaders") or {}).get("rows") or []
              for side in ("home", "away")]
    out = set()
    for n in names:
        words = _SUFFIX.sub("", n or "").split()
        if words and len(words[-1]) >= 4:
            out.add(words[-1])
    return sorted(out)


def relevant_text(text: str, terms: list[str]) -> str:
    """Only the paragraphs that name a team or one of the game's players (Oct 1, to fit the writer's minute): the
    rest of a page (other games, the league, ads) is tokens the extract never uses. Too little left: the whole text."""
    kept = [p for p in text.split("\n") if mentions(p, terms)]
    out = " ".join(kept)
    return out if len(out.split()) >= MIN_RELEVANT_WORDS else " ".join(text.split())


def _fetch(url: str) -> tuple[str, str | None]:
    """(final url, html) or (url, None). Follows redirects."""
    try:
        r = httpx.get(url, headers={"User-Agent": dates.UA}, timeout=FETCH_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError:
        return url, None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return str(r.url), None
    return str(r.url), r.text


def check(cand: dict, home: dict, away: dict, now: datetime, players: list[str] = ()) -> tuple[dict | None, str]:
    """Fetch one candidate {url, title, published?} and apply every rule. Returns (article, reason). Its text is the
    paragraphs about this game (relevant_text: the teams, `players`), cut at MAX_WORDS."""
    url, html = _fetch(cand["url"])
    name = outlet(url)
    if not name:
        return None, "not a listed outlet"
    if html is None:
        return None, "fetch failed"
    rng, used = dates.article_age(now, cand.get("published"), html, url)
    if rng is None:
        return None, "no confirmable date"
    if not dates.is_fresh(rng):
        return None, f"older than 8 days ({rng[1].total_seconds() / 86400:.1f}d)"
    title, text = page_text(html)
    words = text.split()
    if len(words) < MIN_WORDS:
        return None, "too little text (paywall or video)"
    body = f"{title}\n{text}"
    if not (mentions(body, team_terms(home)) and mentions(body, team_terms(away))):
        return None, "doesn't name both teams"
    published = now - rng[1]    # the worst case, i.e. the oldest the article can be
    return {"url": url, "outlet": name, "title": cand.get("title") or title,
            "published": published.isoformat(timespec="minutes"), "date_source": used,
            "text": " ".join(relevant_text(text, team_terms(home) + team_terms(away) + list(players))
                             .split()[:MAX_WORDS])}, "kept"


def search_query(game: dict) -> str:
    away, home = game["away"], game["home"]
    when = datetime.fromisoformat(game["start_time"]).strftime("%B %Y")
    sites = " OR ".join(f"site:{s}" for s in SEARCH_SITES)
    return f"{away['name']} vs {home['name']} {LEAGUE_WORDS.get(game['league'], '')} preview {when} {sites}"


def find_articles(game: dict, news: list[dict], *, now: datetime | None = None,
                  search: bool = True) -> tuple[list[dict], list[dict]]:
    """game: the /api/games/{id} payload. news: stored news_items rows for the league.
    Returns (kept articles, log of every candidate and why it was kept or dropped)."""
    now = now or datetime.now(timezone.utc)
    home, away = game["home"], game["away"]
    hs, as_ = team_terms(home), team_terms(away)
    espn = [{"url": n["url"], "title": n["headline"],
             "published": n["published_at"].isoformat() if n.get("published_at") else None}
            for n in news if n.get("url")
            and mentions(f"{n['headline']} {n.get('description') or ''}", hs + as_)]
    trail: list[dict] = []
    players = player_terms(game)
    kept = _check_all(espn[:MAX_FETCH], home, away, now, trail, "espn", players)
    if search and len(kept) < ENOUGH_FROM_ESPN:
        try:
            found = client.groq_search(search_query(game))
        except client.AIError as exc:
            if not kept:
                # A search that couldn't run is not "no fresh previews": raise so the text is retried later
                # instead of being stored as sourceless (audit, 2026-09-29).
                raise
            log.warning("preview search failed, writing from %d ESPN article(s): %s", len(kept), exc)
            found = []
        seen = {a["url"] for a in kept} | {c["url"] for c in espn}
        found = [f for f in found if f["url"] not in seen and outlet(f["url"])]
        kept += _check_all(found[:MAX_FETCH], home, away, now, trail, "search", players)
    uniq = {a["url"]: a for a in kept}
    # Newest first, URL as the tie-break, so the same articles always come back in the same order.
    ranked = sorted(uniq.values(), key=lambda a: (a["published"], a["url"]), reverse=True)
    return ranked[:MAX_ARTICLES], trail


def _check_all(cands: list[dict], home, away, now, trail: list[dict], via: str, players: list[str] = ()) -> list[dict]:
    if not cands:
        return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda c: check(c, home, away, now, players), cands))
    out = []
    for c, (art, why) in zip(cands, results):
        trail.append({"via": via, "url": c["url"], "result": why})
        if art:
            out.append(art)
    return out
