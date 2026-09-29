"""NCAAF board filter (PRD decision, Sep 27 2026; mid-major vs major shows, Adam Sep 29).

A game is on the board when either team is in the SEC, Big Ten, Big 12, ACC or Pac-12, or is Notre Dame,
and neither team is FCS. A favorite team's games always show, whatever the rules say.
Every FBS game is stored; this decides only what the board shows and which games get a workflow.
The API and the worker both call it, so they never disagree. Pure functions: no database, no network.
"""
from __future__ import annotations

from collections.abc import Collection

MAJOR_CONFERENCES = {1: "ACC", 4: "Big 12", 5: "Big Ten", 8: "SEC", 9: "Pac-12"}
NOTRE_DAME = "87"   # ESPN team id; Notre Dame is in group 18, FBS Independents

# Every FBS conference (ESPN parent group 80). A team in any other group is FCS or lower.
# 12 = Conference USA, 15 = MAC, 17 = Mountain West, 18 = FBS Independents, 37 = Sun Belt, 151 = American.
FBS_CONFERENCES = {1, 4, 5, 8, 9, 12, 15, 17, 18, 37, 151}


def is_featured(home_id: str, away_id: str, home_abbr: str, away_abbr: str,
                home_conf: int | None, away_conf: int | None, favorites: Collection[str]) -> bool:
    if home_abbr in favorites or away_abbr in favorites:
        return True
    # A missing conference counts as not FBS: the game is hidden, and the parser logged "no conference".
    if home_conf not in FBS_CONFERENCES or away_conf not in FBS_CONFERENCES:
        return False
    if home_conf in MAJOR_CONFERENCES or away_conf in MAJOR_CONFERENCES:
        return True
    return NOTRE_DAME in (home_id, away_id)


def is_featured_game(g, favorites: Collection[str]) -> bool:
    """For an espn.Game."""
    return is_featured(g.home.espn_id, g.away.espn_id, g.home.abbr, g.away.abbr,
                       g.home_conf, g.away_conf, favorites)


def is_featured_row(r: dict, favorites: Collection[str]) -> bool:
    """For a row from db._GAMES_SQL."""
    return is_featured(r["home_espn_id"], r["away_espn_id"], r["home_abbr"], r["away_abbr"],
                       r.get("home_conf"), r.get("away_conf"), favorites)
