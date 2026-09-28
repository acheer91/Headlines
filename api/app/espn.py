"""ESPN public site API adapter.

Unofficial and undocumented, so every field is read defensively: a missing field
becomes None, never an exception. The only hard requirements per game are an id,
a start time and two teams; anything else missing still yields a usable game.

Phase 1 turns on NFL only. The other leagues are mapped so Phase 5 is a config change.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports"
CORE = "https://sports.core.api.espn.com/v2/sports"   # per-team season stats and leaders

LEAGUES: dict[str, dict[str, Any]] = {
    "nfl":   {"path": "football/nfl", "core": "football/leagues/nfl", "params": {}},
    "ncaaf": {"path": "football/college-football", "core": "football/leagues/college-football",
              "params": {"groups": "80"}},  # 80 = all FBS
    "nba":   {"path": "basketball/nba", "core": "basketball/leagues/nba", "params": {}},
    "epl":   {"path": "soccer/eng.1", "core": "soccer/leagues/eng.1", "params": {}},
    "mls":   {"path": "soccer/usa.1", "core": "soccer/leagues/usa.1", "params": {}},
}
BETTING_LEAGUES = {"nfl", "ncaaf", "nba"}  # soccer is scores only


class ESPNError(Exception):
    pass


@dataclass
class Team:
    espn_id: str
    abbr: str
    name: str
    short_name: str | None = None
    logo_url: str | None = None
    color: str | None = None


@dataclass
class Odds:
    provider: str | None
    details: str | None
    home_spread: float | None   # negative = home favored
    total: float | None
    home_ml: int | None
    away_ml: int | None


@dataclass
class Game:
    league: str
    espn_id: str
    start_time: datetime
    state: str                   # pre | in | post
    status_detail: str | None
    period: int | None
    clock: str | None
    home: Team
    away: Team
    home_score: int | None
    away_score: int | None
    venue: str | None
    broadcast: str | None
    season: int | None
    week: int | None
    odds: Odds | None = None
    # ESPN numbers weeks per season type: 1 preseason, 2 regular season, 3 postseason.
    season_type: int | None = None
    # A canceled or postponed game is state 'post' but not completed; only completed games are graded.
    completed: bool = False
    # False for a flexed game whose kickoff isn't set: start_time is a placeholder (midnight Eastern).
    time_valid: bool = True
    warnings: list[str] = field(default_factory=list)


# ---------- small defensive helpers ----------

def _get(d: Any, *path: Any, default: Any = None) -> Any:
    cur = d
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int):
            cur = cur[key] if -len(cur) <= key < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ml(v: Any) -> int | None:
    """Moneyline from 150, '-175', '+150' or 'EVEN'."""
    if isinstance(v, str) and v.strip().upper() == "EVEN":
        return 100
    return _int(v.replace("+", "") if isinstance(v, str) else v)


def _parse_time(s: str) -> datetime:
    # ESPN sends "2026-09-28T17:00Z" (no seconds) or with seconds.
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M%z")


def _team(comp: dict) -> Team:
    t = comp.get("team") or {}
    return Team(
        espn_id=str(t.get("id", "")),
        abbr=t.get("abbreviation") or t.get("shortDisplayName") or "?",
        name=t.get("displayName") or t.get("name") or "Unknown",
        short_name=t.get("shortDisplayName") or t.get("name"),
        logo_url=t.get("logo") or _get(t, "logos", 0, "href"),
        color=t.get("color"),
    )


_DETAILS_RE = re.compile(r"^\s*([A-Za-z0-9&.]+)\s+([+-]?\d+(?:\.\d+)?)\s*$")


def _home_spread(o: dict, home_abbr: str, away_abbr: str) -> float | None:
    """Home-perspective spread. ESPN's 'details' string ("BUF -3.5") names the
    favorite, which is unambiguous, so it wins; the bare 'spread' field is the fallback."""
    details = (o.get("details") or "").strip()
    if details.upper() in ("EVEN", "PK", "PICK"):
        return 0.0
    m = _DETAILS_RE.match(details)
    if m:
        abbr, num = m.group(1).upper(), float(m.group(2))
        if abbr == home_abbr.upper():
            return num
        if abbr == away_abbr.upper():
            return -num
    return _float(o.get("spread"))


def _odds(comp: dict, home: Team, away: Team) -> Odds | None:
    o = _get(comp, "odds", 0)
    if not isinstance(o, dict):
        return None
    home_ml = _ml(_get(o, "homeTeamOdds", "moneyLine"))
    away_ml = _ml(_get(o, "awayTeamOdds", "moneyLine"))
    # Newer response shape nests the moneyline under open/current/close.
    if home_ml is None:
        home_ml = _ml(_get(o, "moneyline", "home", "close", "odds") or _get(o, "moneyline", "home", "current", "odds"))
    if away_ml is None:
        away_ml = _ml(_get(o, "moneyline", "away", "close", "odds") or _get(o, "moneyline", "away", "current", "odds"))
    odds = Odds(
        provider=_get(o, "provider", "name"),
        details=o.get("details"),
        home_spread=_home_spread(o, home.abbr, away.abbr),
        total=_float(o.get("overUnder")),
        home_ml=home_ml,
        away_ml=away_ml,
    )
    if all(v is None for v in (odds.home_spread, odds.total, odds.home_ml, odds.away_ml)):
        return None
    return odds


# ---------- parsing ----------

_NOT_PLAYED = re.compile(r"CANCEL|POSTPON|SUSPEND|DELAY|FORFEIT", re.I)


def is_completed(status: dict) -> bool:
    """True only for a game that was played to a final. Canceled and postponed games are
    state 'post' with completed false (and a 0-0 score); never grade those."""
    c = _get(status, "type", "completed")
    if isinstance(c, bool):
        return c
    return _get(status, "type", "state") == "post" and not _NOT_PLAYED.search(_get(status, "type", "name") or "")


def parse_scoreboard(payload: dict, league: str) -> tuple[list[Game], list[str]]:
    """Pure function: ESPN scoreboard JSON -> games. Returns (games, errors).
    A game that can't be parsed is skipped and reported, never fatal."""
    games: list[Game] = []
    errors: list[str] = []
    season = _int(_get(payload, "season", "year"))
    season_type = _int(_get(payload, "season", "type"))
    week = _int(_get(payload, "week", "number"))

    for ev in payload.get("events") or []:
        ev_id = str(ev.get("id", "?"))
        try:
            comp = _get(ev, "competitions", 0) or {}
            competitors = comp.get("competitors") or []
            home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home_c or not away_c:
                raise ValueError("missing home/away competitor")
            home, away = _team(home_c), _team(away_c)

            status = comp.get("status") or ev.get("status") or {}
            state = _get(status, "type", "state") or "pre"
            if state not in ("pre", "in", "post"):
                state = "pre"

            warnings: list[str] = []
            odds = _odds(comp, home, away) if league in BETTING_LEAGUES else None
            if league in BETTING_LEAGUES and odds is None and state == "pre":
                warnings.append("no line")

            games.append(Game(
                league=league,
                espn_id=ev_id,
                start_time=_parse_time(ev.get("date") or comp["date"]),
                state=state,
                status_detail=_get(status, "type", "shortDetail") or _get(status, "type", "detail"),
                period=_int(status.get("period")),
                clock=status.get("displayClock"),
                home=home,
                away=away,
                home_score=_int(home_c.get("score")) if state != "pre" else None,
                away_score=_int(away_c.get("score")) if state != "pre" else None,
                venue=_get(comp, "venue", "fullName"),
                broadcast=_get(comp, "broadcasts", 0, "names", 0) or _get(comp, "geoBroadcasts", 0, "media", "shortName"),
                season=season or _int(_get(ev, "season", "year")),
                week=week or _int(_get(ev, "week", "number")),
                season_type=season_type or _int(_get(ev, "season", "type")),
                completed=is_completed(status),
                time_valid=comp.get("timeValid") is not False and ev.get("timeValid") is not False,
                odds=odds,
                warnings=warnings,
            ))
        except Exception as exc:  # noqa: BLE001 — one bad game never kills the board
            errors.append(f"{league}:{ev_id}: {exc!r}")
    return games, errors


# ---------- fetching ----------

# One shared client: keep-alive connections to ESPN, so a pull doesn't pay a new TCP + TLS handshake
# per request (httpx.Client is thread-safe). Keep httpx's default User-Agent: ESPN's Akamai edge 403s
# custom ones like "scores-app/0.1" (and bare "Mozilla/5.0"), while the default is accepted (2026-09-27).
# Tight timeouts: someone is waiting on the phone, and stored data is always there to fall back on.
_client = httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0),
                       limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))

# "ESPN is down" switch, per host: after a request fails every attempt, skip that host for
# DOWN_SECONDS and fail at once, so each pull serves stored data immediately instead of waiting out
# the retries again. Cleared by the next success.
DOWN_SECONDS = 30.0
_down_until: dict[str, float] = {}
_down_lock = threading.Lock()


def host_down(url: str) -> bool:
    with _down_lock:
        return _down_until.get(urlsplit(url).netloc, 0.0) > time.monotonic()


def reset_down() -> None:
    with _down_lock:
        _down_until.clear()


def _mark(url: str, ok: bool) -> None:
    host = urlsplit(url).netloc
    with _down_lock:
        if ok:
            _down_until.pop(host, None)
        else:
            _down_until[host] = time.monotonic() + DOWN_SECONDS


def _is_outage(exc: Exception) -> bool:
    """Does this failure mean the host is down or blocking us, rather than one bad request?
    Connection errors, timeouts, 5xx, 403 (Akamai block) and 429 (rate limit) are outages. A 404/400
    is about one resource (a bad event id, a team with no stats yet) and must not take every other
    request down with it; nor is it worth retrying."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in (403, 429)
    return False


def _get_json(url: str, params: dict, *, client: httpx.Client | None, attempts: int, what: str,
              backoff: float = 0.25) -> dict:
    """GET with retries (backoff 0.25 s, 0.5 s, ...). Raises ESPNError after the last try, or at once
    while the host is marked down."""
    use_switch = client is None and DOWN_SECONDS > 0
    if use_switch and host_down(url):
        raise ESPNError(f"{what} skipped: {urlsplit(url).netloc} failed in the last {DOWN_SECONDS:.0f}s")
    c = client or _client
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if use_switch:
                _mark(url, True)
            return data
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            if not _is_outage(exc) and not isinstance(exc, ValueError):
                break                                   # 404/400: asking again won't change it
            if i < attempts - 1:
                time.sleep(backoff * 2 ** i)
    if use_switch and last is not None and _is_outage(last):
        _mark(url, False)
    raise ESPNError(f"{what} failed after {attempts} attempts: {last!r}")


def disable_down_switch() -> None:
    """Batch commands (grade_week, check_summary) make many calls in a row and nobody is waiting on a
    phone: retry every call on its own instead of skipping the rest after one failure."""
    global DOWN_SECONDS
    DOWN_SECONDS = 0.0
    reset_down()


def fetch_scoreboard(league: str, *, week: int | None = None, dates: str | None = None,
                     season_type: int | None = None, client: httpx.Client | None = None,
                     attempts: int = 2, base_url: str = BASE) -> dict:
    """GET the scoreboard (retries in _get_json). Raises ESPNError when ESPN fails."""
    if league not in LEAGUES:
        raise ValueError(f"unknown league {league}")
    cfg = LEAGUES[league]
    params = dict(cfg["params"])
    if week is not None:
        params["week"] = str(week)
    if season_type is not None:
        params["seasontype"] = str(season_type)   # without it ESPN uses the current season type
    if dates:
        params["dates"] = dates
    return _get_json(f"{base_url}/{cfg['path']}/scoreboard", params,
                     client=client, attempts=attempts, what=f"{league} scoreboard")


def fetch_summary(league: str, event_id: str, *, client: httpx.Client | None = None,
                  attempts: int = 2, base_url: str = BASE) -> dict:
    """GET one game's summary (/summary?event=<id>): header, box score, leaders, injuries,
    pickcenter. Same retries as the scoreboard. Parsing lives in summary.py."""
    if league not in LEAGUES:
        raise ValueError(f"unknown league {league}")
    return _get_json(f"{base_url}/{LEAGUES[league]['path']}/summary", {"event": str(event_id)},
                     client=client, attempts=attempts, what=f"{league} summary {event_id}")


# ---------- team season leaders (core API) ----------

def fetch_team_leaders(league: str, team_id: str, season: int, season_type: int, *,
                       client: httpx.Client | None = None, attempts: int = 2, base: str = CORE) -> dict:
    """A team's season leaders by category, from ESPN's core API. Used for the one C1 leader the game
    summary doesn't carry: interceptions. Athletes come back as $ref links (see fetch_ref)."""
    if league not in LEAGUES:
        raise ValueError(f"unknown league {league}")
    url = f"{base}/{LEAGUES[league]['core']}/seasons/{season}/types/{season_type}/teams/{team_id}/leaders"
    return _get_json(url, {}, client=client, attempts=attempts, what=f"{league} team {team_id} leaders")


def fetch_ref(ref: str, *, client: httpx.Client | None = None, attempts: int = 2) -> dict:
    """Follow a core-API $ref. ESPN writes them as http://; the https host serves the same JSON."""
    return _get_json(ref.replace("http://", "https://", 1), {}, client=client, attempts=attempts, what="core ref")


def parse_category_leader(payload: dict, category: str) -> dict | None:
    """Top of one leader category: {'value': 2.0, 'display': '2', 'tied': 1, 'athlete_ref': url}.
    'tied' counts the other players on the same value. None when the category is missing or empty
    (e.g. no interceptions yet this season)."""
    cat = next((c for c in payload.get("categories") or [] if c.get("name") == category), None)
    leaders = [x for x in (cat or {}).get("leaders") or [] if isinstance(x, dict)]
    if not leaders:
        return None
    top = leaders[0]
    value = _float(top.get("value"))
    if not value:
        return None
    return {
        "value": value,
        "display": top.get("displayValue") or f"{value:g}",
        "tied": sum(1 for x in leaders[1:] if _float(x.get("value")) == value),
        "athlete_ref": _get(top, "athlete", "$ref"),
    }


# ---------- team season stats with league ranks (C1) ----------

def fetch_team_stats(league: str, team_id: str, season: int, season_type: int, *,
                     client: httpx.Client | None = None, attempts: int = 2, base: str = CORE) -> dict:
    """A team's season stats with league ranks (core API): the offense side of C1's ranks."""
    if league not in LEAGUES:
        raise ValueError(f"unknown league {league}")
    url = f"{base}/{LEAGUES[league]['core']}/seasons/{season}/types/{season_type}/teams/{team_id}/statistics"
    return _get_json(url, {}, client=client, attempts=attempts, what=f"{league} team {team_id} stats")


def fetch_team_opponent_stats(league: str, team_id: str, season: int, season_type: int, *,
                              client: httpx.Client | None = None, attempts: int = 2, base_url: str = BASE) -> dict:
    """The site API's team statistics. Its results.opponent block is what the team *allowed*, with
    league ranks where 1st = allowed the least (checked against espn.com's defense leaderboard)."""
    if league not in LEAGUES:
        raise ValueError(f"unknown league {league}")
    url = f"{base_url}/{LEAGUES[league]['path']}/teams/{team_id}/statistics"
    return _get_json(url, {"season": str(season), "seasontype": str(season_type)}, client=client,
                     attempts=attempts, what=f"{league} team {team_id} opponent stats")


# summary stat key (C1 row) -> (source, ESPN stat name)
RANKED_STATS = {
    "totalPointsPerGame": ("own", "totalPointsPerGame"),
    "yardsPerGame": ("own", "yardsPerGame"),
    "totalPointsPerGameAllowed": ("opponent", "totalPointsPerGame"),
    "yardsPerGameAllowed": ("opponent", "yardsPerGame"),
}


def _first_ranked(categories: Any, name: str) -> dict | None:
    for cat in categories if isinstance(categories, list) else []:
        for st in (cat or {}).get("stats") or []:
            if st.get("name") == name and st.get("rankDisplayValue"):
                return {"value": _float(st.get("value")), "rank": st["rankDisplayValue"]}
    return None


def parse_ranks(own_payload: dict | None, opponent_payload: dict | None) -> dict:
    """{summary stat key: {'value': 31.0, 'rank': 'Tied-4th'}} for whatever ESPN ranked.
    own_payload: core /statistics; opponent_payload: site /teams/{id}/statistics."""
    sources = {
        "own": _get(own_payload or {}, "splits", "categories"),
        "opponent": _get(opponent_payload or {}, "results", "opponent"),
    }
    out = {}
    for key, (src, name) in RANKED_STATS.items():
        r = _first_ranked(sources[src], name)
        if r:
            out[key] = r
    return out
