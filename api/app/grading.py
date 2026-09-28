"""Bet grading: plain arithmetic on a score and one saved line. No database, no network, no AI.

Rules (Phase 2 handoff):
- Moneyline: the team that won; a tie is a push. Margin = points the winner won by.
- Spread: home margin + home spread. > 0 home covers, < 0 away covers, 0 push.
- Total: combined points against the total. Equal is a push.
Every margin is >= 0 and says by how much the outcome won ("Under 43.5 by 3.5").

The same functions give the live "so far" status on C2; that is never stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

MARKETS = ("moneyline", "spread", "total")


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def fmt_num(v: Any) -> str:
    """-7.0 -> '-7', 3.5 -> '3.5'."""
    d = _d(v)
    if d is None:
        return "–"
    s = f"{d.normalize():f}"
    return "0" if s in ("-0", "0") else s


def fmt_signed(v: Any) -> str:
    d = _d(v)
    if d is None:
        return "–"
    return f"+{fmt_num(d)}" if d > 0 else fmt_num(d)


def fmt_ml(v: int | None) -> str:
    return "–" if v is None else (f"+{v}" if v > 0 else str(v))


# ---------- the line used for grading ----------

@dataclass(frozen=True)
class Line:
    source: str                  # pre_game | espn_close | in_game
    provider: str | None
    home_spread: Decimal | None  # negative = home favored
    total: Decimal | None
    home_ml: int | None
    away_ml: int | None
    snapshot_id: int | None = None


_FIELDS = {"moneyline": ("home_ml", "away_ml"), "spread": ("home_spread",), "total": ("total",)}


def _has(market: str, src: dict) -> bool:
    return any(src.get(f) is not None for f in _FIELDS[market])


def choose_lines(snapshots: Iterable[dict], espn_close: dict | None) -> dict[str, Line]:
    """Pick the grading line for each market separately (Adam, 2026-09-27):

    1. ESPN's closing line from the game summary: the book's line at kickoff, so results match the
       sportsbook. (Our own snapshots come only from pulls and can be days old.)
    2. The last line we captured before kickoff (odds snapshot taken while the game was 'pre').
    3. The newest line we have at all, tagged in-game.
    A market with none of these is left out: Ungraded. Per market, because a source can carry one
    market and not another (spread "OFF", moneyline only).

    `snapshots` are odds_snapshots rows in any order; `espn_close` has home_spread/total/
    home_ml/away_ml/provider keys (or is None).
    """
    snaps = sorted(snapshots, key=lambda s: (s["captured_at"], s["id"]))

    def from_snap(s: dict, source: str) -> Line:
        return Line(source, s.get("provider"), _d(s.get("home_spread")), _d(s.get("total")),
                    s.get("home_ml"), s.get("away_ml"), s["id"])

    out: dict[str, Line] = {}
    for market in MARKETS:
        if espn_close and _has(market, espn_close):
            out[market] = Line("espn_close", espn_close.get("provider"), _d(espn_close.get("home_spread")),
                               _d(espn_close.get("total")), espn_close.get("home_ml"), espn_close.get("away_ml"))
            continue
        pre = [s for s in snaps if s.get("game_state") == "pre" and _has(market, s)]
        if pre:
            out[market] = from_snap(pre[-1], "pre_game")
            continue
        later = [s for s in snaps if _has(market, s)]
        if later:
            out[market] = from_snap(later[-1], "in_game")
    return out


# ---------- grading ----------

@dataclass(frozen=True)
class Result:
    market: str
    outcome: str        # home | away | push (moneyline, spread) · over | under | push (total)
    margin: Decimal     # >= 0


def grade_moneyline(home_score: int, away_score: int) -> Result:
    diff = home_score - away_score
    outcome = "home" if diff > 0 else "away" if diff < 0 else "push"
    return Result("moneyline", outcome, Decimal(abs(diff)))


def grade_spread(home_score: int, away_score: int, home_spread: Any) -> Result:
    m = Decimal(home_score - away_score) + _d(home_spread)
    outcome = "home" if m > 0 else "away" if m < 0 else "push"
    return Result("spread", outcome, abs(m))


def grade_total(home_score: int, away_score: int, total: Any) -> Result:
    d = Decimal(home_score + away_score) - _d(total)
    outcome = "over" if d > 0 else "under" if d < 0 else "push"
    return Result("total", outcome, abs(d))


def grade(home_score: int, away_score: int, lines: dict[str, Line]) -> dict[str, Result]:
    """Grade every market that has a line (see choose_lines). A market without one is left out
    (Ungraded); the moneyline needs at least one price, since it's reported with its odds."""
    out: dict[str, Result] = {}
    ml, sp, tot = lines.get("moneyline"), lines.get("spread"), lines.get("total")
    if ml and (ml.home_ml is not None or ml.away_ml is not None):
        out["moneyline"] = grade_moneyline(home_score, away_score)
    if sp and sp.home_spread is not None:
        out["spread"] = grade_spread(home_score, away_score, sp.home_spread)
    if tot and tot.total is not None:
        out["total"] = grade_total(home_score, away_score, tot.total)
    return out


# ---------- words for the screens (report, never advise) ----------

def spread_label(home_spread: Any, home: str, away: str) -> str:
    """The favorite, the way books show it: 'BUF -7', or 'PK'."""
    hs = _d(home_spread)
    if hs is None:
        return "–"
    if hs == 0:
        return "PK"
    return f"{home} {fmt_signed(hs)}" if hs < 0 else f"{away} {fmt_signed(-hs)}"


def describe(r: Result, line: Line, home: str, away: str, *, live: bool = False) -> str:
    """'BUF won', 'BUF -7 covered by 1', 'Under 50.5 by 10.5'. With live=True the same facts
    in the present tense, e.g. 'LAR +1.5 covering by 14.5'."""
    if r.market == "moneyline":
        if r.outcome == "push":
            return "Tied" if live else "Tie · push"
        team = home if r.outcome == "home" else away
        return f"{team} leads by {fmt_num(r.margin)}" if live else f"{team} won by {fmt_num(r.margin)}"
    if r.market == "spread":
        if r.outcome == "push":
            return f"{spread_label(line.home_spread, home, away)} · push" + (" right now" if live else "")
        team, num = (home, line.home_spread) if r.outcome == "home" else (away, -line.home_spread)
        verb = "covering" if live else "covered"
        return f"{team} {fmt_signed(num) if num != 0 else 'PK'} {verb} by {fmt_num(r.margin)}"
    # total
    if r.outcome == "push":
        return f"Push at {fmt_num(line.total)}"
    word = "Over" if r.outcome == "over" else "Under"
    return f"{word} {fmt_num(line.total)} by {fmt_num(r.margin)}"


def points_needed_for_over(home_score: int, away_score: int, total: Any) -> int:
    """Points still needed to go over (not push) the total; 0 if already over."""
    t, pts = _d(total), Decimal(home_score + away_score)
    if pts > t:
        return 0
    return int((t - pts).to_integral_value(rounding="ROUND_FLOOR")) + 1


def live_status(home_score: int, away_score: int, lines: dict[str, Line], home: str, away: str) -> list[dict]:
    """Where each bet stands right now, labelled 'so far'. Never stored, never advice."""
    rows: list[dict] = []
    for market, r in grade(home_score, away_score, lines).items():
        line = lines[market]
        text = describe(r, line, home, away, live=True)
        if market == "total":
            pts = home_score + away_score
            need = points_needed_for_over(home_score, away_score, line.total)
            text = (f"{pts} pts so far vs O/U {fmt_num(line.total)} · "
                    + (f"over by {fmt_num(r.margin)}" if need == 0 else f"{need} more to go over"))
        rows.append({"market": market, "outcome": r.outcome, "margin": float(r.margin), "text": text})
    return rows
