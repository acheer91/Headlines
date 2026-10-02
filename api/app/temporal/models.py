"""Data passed between workflows and activities. No imports beyond the standard library, so both the
workflow sandbox and the activities can use it. Times are UTC ISO strings, never datetime objects."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GameRef:
    """A game ScheduleSync found that isn't final yet."""
    league: str
    espn_id: str
    start_iso: str          # kickoff, UTC
    preview_iso: str        # 8:00 AM Pacific on the game's Pacific-time date, in UTC


@dataclass
class GameInput:
    league: str
    espn_id: str
    start_iso: str
    preview_iso: str


@dataclass
class Times:
    """Payload of the reschedule signal. preview_iso may be left out (runbook use): the workflow then
    keeps the same lead time before kickoff."""
    start_iso: str
    preview_iso: str | None = None


@dataclass
class GameState:
    state: str              # pre | in | post
    home_score: int | None
    away_score: int | None
    start_iso: str
    postponed: bool
    completed: bool         # a played final; a canceled game is 'post' but not completed


@dataclass
class TextJob:
    """One AI text for WriteTextWorkflow (Phase 4). kind: preview | recap | headlines. For headlines, league is
    a comma-separated list and espn_id is None. reason: midweek | refresh | final | nightly | schedule | manual | open (a page open
    handed over for lack of quota)."""
    kind: str
    league: str
    espn_id: str | None
    reason: str
