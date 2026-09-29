"""NCAAF board filter (PRD decision, Sep 27 2026; mid-major vs major shows, Adam Sep 29).

The rule, in order:
1. A favorite team on either side: show, whatever the rules below say.
2. Either team not FBS (FCS, or no conference id): hide.
3. Either team in the SEC, Big Ten, Big 12, ACC or Pac-12: show.
4. Either team ranked 1-25: show (PRD scope row, added Sep 29 pre-deploy review).
5. Either team is Notre Dame: show.
6. Otherwise: hide.
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
                home_conf: int | None, away_conf: int | None, favorites: Collection[str],
                home_rank: int | None = None, away_rank: int | None = None) -> bool:
    if home_abbr in favorites or away_abbr in favorites:
        return True
    # A missing conference counts as not FBS: the game is hidden, and the parser logged "no conference".
    if home_conf not in FBS_CONFERENCES or away_conf not in FBS_CONFERENCES:
        return False                                   # FCS never shows, ranked or not
    if home_conf in MAJOR_CONFERENCES or away_conf in MAJOR_CONFERENCES:
        return True
    if home_rank is not None or away_rank is not None:
        return True                                    # PRD: ranked teams playing any FBS opponent
    return NOTRE_DAME in (home_id, away_id)


def is_featured_game(g, favorites: Collection[str]) -> bool:
    """For an espn.Game."""
    return is_featured(g.home.espn_id, g.away.espn_id, g.home.abbr, g.away.abbr,
                       g.home_conf, g.away_conf, favorites, g.home_rank, g.away_rank)


def is_featured_row(r: dict, favorites: Collection[str]) -> bool:
    """For a row from db._GAMES_SQL."""
    return is_featured(r["home_espn_id"], r["away_espn_id"], r["home_abbr"], r["away_abbr"],
                       r.get("home_conf"), r.get("away_conf"), favorites,
                       r.get("home_rank"), r.get("away_rank"))
