"""The fact sheet for a weekend column (Adam, 2026-10-06), built by code: the weekend's finals with tags the scores
decide (upset, one-score game, blowout, overtime), a note from each of the most interesting games' stored recaps (those
texts already passed their own fact check), and a few ESPN headlines. Pure: rows in, a dict out. The model reads this
and nothing else (not facts.py, which is pinned for the Z5 retest).
"""
from __future__ import annotations

import re

SENTENCE = re.compile(r"(?<=[.!?])\s+")
NOTE_SENTENCES = 2          # a recap's first two sentences: the result and what decided it
NOTE_GAMES = 6              # games that get a note (the most interesting that have a recap)
NEWS_ITEMS = 5
NEWS_CHARS = 160
ONE_SCORE = 8               # a margin of 8 or less is a one-score game
BLOWOUT = 21


def _team(name: str, rank: int | None) -> str:
    return f"No. {rank} {name}" if rank else name


def tags(row: dict) -> list[str]:
    """What the score and the ranks say about a game: only things code can prove from them."""
    hs, as_ = row["home_score"], row["away_score"]
    margin = abs(hs - as_)
    hr, ar = row.get("home_rank"), row.get("away_rank")
    if hs > as_:
        wr, lr = hr, ar
    else:
        wr, lr = ar, hr
    out = []
    if "OT" in (row.get("status_detail") or "").upper():        # "Final/OT", "Final/2OT"
        out.append("overtime")
    if margin == 0:
        out.append("tie")
    if lr and (not wr or wr > lr):
        out.append("upset")
    if hr and ar:
        out.append("ranked vs ranked")
    if 0 < margin <= ONE_SCORE:
        out.append("one-score game")
    if margin >= BLOWOUT:
        out.append("blowout")
    return out


def result_line(row: dict) -> str:
    """"No. 20 Utah 28, No. 5 Baylor 14 (upset; blowout; margin 14)": the winner first."""
    hs, as_ = row["home_score"], row["away_score"]
    home = (_team(row["home"], row.get("home_rank")), hs)
    away = (_team(row["away"], row.get("away_rank")), as_)
    first, second = (home, away) if hs >= as_ else (away, home)
    margin = abs(hs - as_)
    extra = tags(row) + ([f"margin {margin}"] if margin else [])
    return f"{first[0]} {first[1]}, {second[0]} {second[1]}" + (f" ({'; '.join(extra)})" if extra else "")


def interest(row: dict) -> int:
    """How much a game deserves a note: upsets and ranked matchups first, then overtime and close finishes."""
    t = tags(row)
    margin = abs(row["home_score"] - row["away_score"])
    return (3 * ("upset" in t) + 2 * ("ranked vs ranked" in t) + 2 * ("overtime" in t) + 2 * (margin <= 3)
            + 1 * ("blowout" in t) + 1 * bool(row.get("home_rank") or row.get("away_rank")))


def note_of(recap: str | None) -> str | None:
    """The first sentences of a stored recap."""
    if not recap or not recap.strip():
        return None
    return " ".join(SENTENCE.split(recap.strip())[:NOTE_SENTENCES])


def featured(rows: list[dict]) -> list[dict]:
    """The games that get a note and their leaders: the most interesting NOTE_GAMES, oldest first."""
    top = sorted(rows, key=lambda r: (-interest(r), r["start_time"]))[:NOTE_GAMES]
    return sorted(top, key=lambda r: r["start_time"])


def leader_lines(leaders: list[dict], home: str, away: str) -> list[str]:
    """summary.leaders(p, "game") -> "Chicago Bears passing: Caleb Williams 25/35, 312 YDS, 2 TD" (ESPN's own line)."""
    out = []
    for cat in leaders:
        for side, team in (("away", away), ("home", home)):
            who = cat.get(side)
            if who and who.get("name") and who.get("value"):
                out.append(f"{team} {cat['label'].lower()}: {who['name']} {who['value']}")
    return out


def build(league: str, rows: list[dict], news: list[dict], leaders: dict | None = None) -> dict:
    """rows: the weekend's completed games (home, away, scores, ranks, status_detail, start_time, optional recap and
    id), oldest first. news: stored ESPN items, newest first. leaders: game id -> leader_lines, for the featured games."""
    leaders = leaders or {}
    games = []
    for r in featured(rows):
        entry = {"game": result_line(r).split(" (")[0]}
        note = note_of(r.get("recap"))
        if note:
            entry["note"] = note
        if leaders.get(r.get("id")):
            entry["leaders"] = leaders[r["id"]]
        if len(entry) > 1:
            games.append(entry)
    stories = []
    for n in news[:NEWS_ITEMS]:
        text = n["headline"].strip()
        desc = (n.get("description") or "").strip()
        if desc:
            text = f"{text}: {desc}"
        stories.append(text[:NEWS_CHARS].rstrip())
    return {
        "league": league.upper(),
        "games_played": len(rows),
        "results": [result_line(r) for r in rows],
        "notes": games,
        "news": stories,
    }
