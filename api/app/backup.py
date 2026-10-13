"""Live-score backup: used only when an ESPN refresh fails and some games are live.

Display-only: never written to the database, never used for lines or grading. Each fetch is one
attempt with a short timeout; any failure returns [] and the board falls back to stored data.
Field names come from samples captured 2026-09-29 (tests/fixtures/backup_*.json)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
TIMEOUT = httpx.Timeout(3.0, connect=2.0)
ET = ZoneInfo("America/New_York")

YAHOO_URL = "https://api-secure.sports.yahoo.com/v1/editorial/s/scoreboard"
NCAA_URL = "https://ncaa-api.henrygd.me/scoreboard/football/fbs/{season}/{week:02d}/all-conf"
PREMIER_LEAGUE_URL = "https://footballapi.pulselive.com/football/fixtures"


@dataclass
class BackupScore:
    home: str            # team name as the source spells it
    away: str
    home_score: int | None
    away_score: int | None
    status: str          # e.g. "19:00 1st", "3rd 4:12", "63'", "Final"


def _get(url: str, params: dict | None = None) -> dict:
    r = httpx.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _yahoo_parse(payload: dict) -> list[BackupScore]:
    board = payload["service"]["scoreboard"]
    teams = board.get("teams") or {}
    out = []
    for g in (board.get("games") or {}).values():
        home, away = teams.get(g.get("home_team_id")) or {}, teams.get(g.get("away_team_id")) or {}
        out.append(BackupScore(home=home.get("full_name") or "", away=away.get("full_name") or "",
                               home_score=_int(g.get("total_home_points")),
                               away_score=_int(g.get("total_away_points")),
                               status=g.get("status_display_name") or ""))
    return out


def _ncaa_parse(payload: dict) -> list[BackupScore]:
    out = []
    for item in payload.get("games") or []:
        g = item.get("game") or {}
        period, clock = g.get("currentPeriod") or "", g.get("contestClock") or ""
        if g.get("gameState") == "final":
            status = "Final"
        elif period and clock and period.upper() not in ("HALF", "HALFTIME"):
            status = f"{period} {clock}"
        else:
            status = period
        h, a = g.get("home") or {}, g.get("away") or {}
        out.append(BackupScore(home=(h.get("names") or {}).get("short") or "",
                               away=(a.get("names") or {}).get("short") or "",
                               home_score=_int(h.get("score")), away_score=_int(a.get("score")),
                               status=status))
    return out


def _premier_league_parse(payload: dict) -> list[BackupScore]:
    out = []
    for f in payload.get("content") or []:
        teams = f.get("teams") or []
        if len(teams) != 2:
            continue
        # The site lists the home side first.
        (home, away) = teams
        label = (f.get("clock") or {}).get("label") or ""       # "63'00", "90+4'00"
        if f.get("status") == "C":
            status = "FT"
        elif f.get("phase") == "H":
            status = "HT"
        elif label:
            status = label.split("'")[0] + "'"
        else:
            status = "Live"
        out.append(BackupScore(home=(home.get("team") or {}).get("name") or "",
                               away=(away.get("team") or {}).get("name") or "",
                               home_score=_int(home.get("score")), away_score=_int(away.get("score")),
                               status=status))
    return out


def _yahoo(league: str, day: date, **_) -> list[BackupScore]:
    return _yahoo_parse(_get(YAHOO_URL, {"leagues": league, "date": day.isoformat()}))


def _ncaa(season: int | None, week: int | None, season_type: int | None, **_) -> list[BackupScore]:
    # The wrapper's week numbers match ESPN's regular-season weeks (checked for 2026 weeks 1-5).
    # Postseason weeks are numbered differently, so no backup there.
    if not season or not week or season_type != 2:
        return []
    return _ncaa_parse(_get(NCAA_URL.format(season=season, week=week)))


def _premier_league(**_) -> list[BackupScore]:
    # Most recent first, live and complete only: any live match is in the first page.
    return _premier_league_parse(_get(PREMIER_LEAGUE_URL,
                                      {"comps": 1, "pageSize": 20, "sort": "desc", "statuses": "L,C"}))


SOURCES = {"nfl": lambda **kw: _yahoo("nfl", **kw), "nba": lambda **kw: _yahoo("nba", **kw),
           "ncaaf": _ncaa, "epl": _premier_league}      # no MLS


def us_date(start_iso: str) -> date:
    """The US (Eastern) date a game is listed under, from its start time."""
    return datetime.fromisoformat(start_iso).astimezone(ET).date()


def fetch(league: str, live: list[dict], season: int | None = None, week: int | None = None,
          season_type: int | None = None) -> list[BackupScore]:
    """Scores for the given live cards' league. Never raises. Games with no score yet are dropped."""
    src = SOURCES.get(league)
    if src is None or not live:
        return []
    try:
        # Yahoo lists games by US (ET) start date, so a game still live after midnight is under the day it began.
        # The latest live card decides: an older card stuck "in" must not send us to a past day.
        day = max(us_date(c["start_time"]) for c in live)
        scores = src(day=day, season=season, week=week, season_type=season_type)
        return [s for s in scores if s.home_score is not None and s.away_score is not None]
    except Exception as exc:  # noqa: BLE001
        log.warning("backup %s failed: %r", league, exc)
        return []


def is_live(card: dict, now: datetime | None = None) -> bool:
    """In progress, or still 'pre' although its (real) kickoff time has passed."""
    if card["state"] == "in":
        return True
    if card["state"] != "pre" or not card["time_valid"]:
        return False
    # Compare as datetimes, not strings: the database may hand back times in another zone.
    return datetime.fromisoformat(card["start_time"]) <= (now or datetime.now(timezone.utc))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# Only where a source spells a team differently from ESPN (source name -> ESPN full name, both normalized).
# NCAA entries: every game of 2026 weeks 1-5 (390) paired with ESPN's, each matched exactly once.
ALIASES: dict[str, str] = {
    # NBA (Yahoo)
    "losangelesclippers": "laclippers",
    # NCAAF (NCAA.com short names)
    "abilenechristian": "abilenechristianwildcats",
    "alcorn": "alcornstatebraves",
    "arkpinebluff": "arkansaspinebluffgoldenlions",
    "armywestpoint": "armyblackknights",
    "ballst": "ballstatecardinals",
    "bethunecookman": "bethunecookmanwildcats",
    "centralark": "centralarkansasbears",
    "centralconnst": "centralconnecticutbluedevils",
    "centralmich": "centralmichiganchippewas",
    "coastalcarolina": "coastalcarolinachanticleers",
    "easternill": "easternillinoispanthers",
    "easternky": "easternkentuckycolonels",
    "easternmich": "easternmichiganeagles",
    "easternwash": "easternwashingtoneagles",
    "fiu": "floridainternationalpanthers",
    "flaatlantic": "floridaatlanticowls",
    "fresnost": "fresnostatebulldogs",
    "houstonchristian": "houstonchristianhuskies",
    "iowast": "iowastatecyclones",
    "jacksonvillest": "jacksonvillestategamecocks",
    "kentst": "kentstategoldenflashes",
    "lamaruniversity": "lamarcardinals",
    "liu": "longislanduniversitysharks",
    "massachusetts": "massachusettsminutemen",
    "miamifl": "miamihurricanes",
    "middletenn": "middletennesseeblueraiders",
    "mississippival": "mississippivalleystatedeltadevils",
    "niu": "northernillinoishuskies",
    "northala": "northalabamalions",
    "northdakotast": "northdakotastatebison",
    "northernariz": "northernarizonalumberjacks",
    "northerncolo": "northerncoloradobears",
    "northwesternst": "northwesternstatedemons",
    "ohiost": "ohiostatebuckeyes",
    "pennst": "pennstatenittanylions",
    "pittsburgh": "pittsburghpanthers",
    "sanjosest": "sanjosstatespartans",
    "southdakotast": "southdakotastatejackrabbits",
    "southeasternla": "selouisianalions",
    "southeastmost": "southeastmissouristateredhawks",
    "southerncalifornia": "usctrojans",
    "southernill": "southernillinoissalukis",
    "southernu": "southernjaguars",
    "southfla": "southfloridabulls",
    "uiw": "incarnatewordcardinals",
    "ulm": "ulmonroewarhawks",
    "uni": "northerniowapanthers",
    "utahst": "utahstateaggies",
    "utrgv": "utriograndevalleyvaqueros",
    "westerncaro": "westerncarolinacatamounts",
    "westernill": "westernillinoisleathernecks",
    "westernmich": "westernmichiganbroncos",
    "westga": "westgeorgiawolves",
}


def _key(name: str) -> str:
    n = _norm(name)
    return ALIASES.get(n, n)


def match(home_names: set[str], away_names: set[str], scores: list[BackupScore]) -> BackupScore | None:
    """The backup game whose home AND away teams match one of the card's names (full or short)."""
    h, a = {_key(x) for x in home_names if x}, {_key(x) for x in away_names if x}
    for s in scores:
        if _key(s.home) in h and _key(s.away) in a:
            return s
    return None
