"""Which games get AI text written ahead (Adam, 2026-09-29): only games with a favorite, or college games between
two ranked teams. Everything else is written the first time its page is opened (~15 s), so nothing is spent on
games nobody opens.

Checked when the text is due (the midweek batch, the 8 AM refresh, the final, the nightly job), always against
that moment's favorites file and poll ranks, so the list keeps itself current. Decided in activities, never in
workflow code, so changing it needs no workflow versioning.
"""
from __future__ import annotations

import os

from .. import favorites

# Leagues that get AI text at all (CTO, 2026-10-01: NFL first, college once we have real token numbers). Separate
# from ENABLED_LEAGUES, which turns on scores: NCAAF scores go live without AI text.
AI_LEAGUES = {x.strip() for x in os.environ.get("AI_LEAGUES", "nfl").split(",") if x.strip()}


def ai_league(league: str) -> bool:
    return league in AI_LEAGUES


def is_prewritten(row: dict, favs: dict[str, list[str]] | None = None) -> bool:
    """row: a games row (db._GAMES_SQL: league, home_abbr, away_abbr, home_rank, away_rank)."""
    if not ai_league(row["league"]):
        return False
    favs = favorites.load() if favs is None else favs
    mine = {a.upper() for a in favs.get(row["league"], [])}
    if (row.get("home_abbr") or "").upper() in mine or (row.get("away_abbr") or "").upper() in mine:
        return True
    return row["league"] == "ncaaf" and row.get("home_rank") is not None and row.get("away_rank") is not None
