"""Stage 1 voice samples for Adam (handoff 1.6 and 1.7): python -m app.ai.samples

Laptop only. Reads games through the local API (which refreshes and grades the summary, as opening the game
would) and news from the local database, then writes docs/phase4-samples.md with every text, its sources and
their dates, and how long it took. Keys come from the environment, or from GEMINI_*/GROQ_* lines in ../.env.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

from . import client, facts, sources, writer

ROOT = Path(__file__).resolve().parents[3]
API = os.environ.get("SAMPLES_API", "http://127.0.0.1:8000")
OUT = ROOT / "docs" / "phase4-samples.md"

# (league, away, home). The last preview is an obscure game, expected to have no fresh articles.
PREVIEWS = [("nfl", "NE", "BUF"), ("nfl", "PIT", "CLE"), ("ncaaf", "OSU", "IOWA"), ("ncaaf", "MIA", "CLEM"),
            ("ncaaf", "TXSO", "FAU")]
# All 16 NFL finals of week 4 (Sep 25-28), for the accuracy evaluation.
RECAPS = [("nfl", "PHI", "CHI"), ("nfl", "NE", "JAX"), ("nfl", "SEA", "WSH"), ("nfl", "BAL", "DAL"),
          ("nfl", "CIN", "PIT"), ("nfl", "ATL", "GB"), ("nfl", "LAC", "BUF"), ("nfl", "CAR", "CLE"),
          ("nfl", "NYJ", "DET"), ("nfl", "HOU", "IND"), ("nfl", "KC", "MIA"), ("nfl", "TEN", "NYG"),
          ("nfl", "ARI", "SF"), ("nfl", "MIN", "TB"), ("nfl", "LV", "NO"), ("nfl", "LAR", "DEN")]


def _load_keys() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() in ("GEMINI_API_KEY", "GEMINI_MODEL", "GROQ_API_KEY", "POSTGRES_PASSWORD")                 and not os.environ.get(k.strip()):
            os.environ[k.strip()] = v.strip()


def db_url() -> str:
    """127.0.0.1, not localhost: on Windows localhost tries ::1 first and hangs."""
    pw = os.environ.get("POSTGRES_PASSWORD", "scores")
    return os.environ.get("SAMPLES_DATABASE_URL", f"postgresql://scores:{pw}@127.0.0.1:5432/scores")


def _game_id(conn, league: str, away: str, home: str) -> int:
    row = conn.execute("""
        SELECT g.id FROM games g JOIN teams h ON h.id = g.home_team_id JOIN teams a ON a.id = g.away_team_id
        WHERE g.league = %s AND a.abbr = %s AND h.abbr = %s AND g.start_time > now() - interval '10 days'
        ORDER BY g.start_time LIMIT 1""", (league, away, home)).fetchone()
    if not row:
        sys.exit(f"no game {league} {away}@{home}")
    return row["id"]


def _game(gid: int) -> dict:
    r = httpx.get(f"{API}/api/games/{gid}", timeout=90)
    r.raise_for_status()
    return r.json()


def _news(conn, league: str, before: datetime | None = None, limit: int = 60) -> list[dict]:
    return conn.execute("""
        SELECT headline, description, url, published_at FROM news_items
        WHERE league = ANY(%s) AND published_at IS NOT NULL AND (%s::timestamptz IS NULL OR published_at < %s)
        ORDER BY published_at DESC LIMIT %s""",
                        ([league] if isinstance(league, str) else league, before, before, limit)).fetchall()


def _finals(conn, since: datetime, before: datetime) -> list[str]:
    rows = conn.execute("""
        SELECT g.league, a.name AS away, g.away_score, h.name AS home, g.home_score FROM games g
        JOIN teams h ON h.id = g.home_team_id JOIN teams a ON a.id = g.away_team_id
        WHERE g.state = 'post' AND g.start_time BETWEEN %s AND %s ORDER BY g.start_time""",
                        (since, before)).fetchall()
    return [f"{r['league'].upper()}: {r['away']} {r['away_score']}, {r['home']} {r['home_score']} (final)" for r in rows]


def _timed(fn, *args):
    t, p, k = time.monotonic(), client.paced_seconds, client.tokens_used
    out = fn(*args)
    if isinstance(out, dict):
        out["tokens"] = client.tokens_used - k
    return out, round(time.monotonic() - t - (client.paced_seconds - p), 1)


def main() -> None:
    _load_keys()
    md = [f"# Phase 4 voice samples\n\nGenerated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by "
          f"`python -m app.ai.samples` · writer `{client.model()}` · search: stored ESPN news, then Groq "
          f"(`{client.GROQ_SEARCH_MODEL}`) limited to {', '.join(sources.SEARCH_SITES)}.\n\n"
          "Times exclude waiting for the free tier's per-minute limit. Targets (Adam): preview 15 s, recap 8 s.\n"]
    raw = []
    with psycopg.connect(db_url(), row_factory=dict_row) as conn:
        md.append("## Previews\n")
        for league, away, home in PREVIEWS:
            game = _game(_game_id(conn, league, away, home))
            (arts, trail), t_src = _timed(sources.find_articles, game, _news(conn, league))
            res, t_write = _timed(writer.write_preview, game, arts)
            raw.append({"kind": "preview", "game": f"{away}@{home}", "result": res, "trail": trail})
            md += _preview_md(game, res, trail, t_src, t_write)
        md.append("## Recaps\n")
        for league, away, home in RECAPS:
            game = _game(_game_id(conn, league, away, home))
            res, t = _timed(writer.write_recap, game)
            raw.append({"kind": "recap", "game": f"{away}@{home}", "result": res})
            md += _recap_md(game, res, t)
        md.append("## Headlines\n")
        now = datetime.now(timezone.utc)
        for label, before in (("Now", now), ("As of Sep 28, 11:00 AM PT", datetime(2026, 9, 28, 18, tzinfo=timezone.utc))):
            news = _news(conn, ["nfl", "ncaaf"], before, 30)
            res, t = _timed(writer.write_headlines, news, _finals(conn, before - timedelta(days=4), before))
            raw.append({"kind": "headlines", "as_of": label, "result": res})
            md += _headlines_md(label, res, t)
    OUT.write_text("\n".join(md), encoding="utf-8", newline="\n")
    Path(os.environ.get("SAMPLES_RAW", Path(tempfile.gettempdir()) / "phase4-samples.raw.json")).write_text(
        json.dumps(raw, indent=1, default=str), encoding="utf-8")
    print(f"wrote {OUT}")


def _title(game: dict) -> str:
    h, a = game["home"], game["away"]
    rank = lambda t: f"No. {t['rank']} " if t.get("rank") else ""
    return f"{rank(a)}{a['name']} at {rank(h)}{h['name']}"


def _status(res: dict, secs: str) -> str:
    extra = f" · rejected then rewritten: {'; '.join(res['rejected'])}" if res.get("rejected") else ""
    return f"*{res['status']} · {secs} · {res['calls']} writes + {res.get('checks', 0)} fact checks, {res.get('tokens', 0):,} tokens{extra}*\n"


def _preview_md(game, res, trail, t_src, t_write) -> list[str]:
    out = [f"### {_title(game)}\n", _status(res, f"sources {t_src}s + writing {t_write}s = {t_src + t_write:.1f}s")]
    if res["status"] == "no_sources":
        out.append("> No fresh previews\n")
    elif res["status"] == "failed":
        out.append(f"> Preview unavailable right now. ({res.get('reason')})\n")
    else:
        b = res["body"]
        out.append(b["preview"] + "\n")
        for side in ("away", "home"):
            if b["edges"][side]:
                out.append(f"**{game[side]['short']} edges**\n")
                out += [f"- {e['text']} ([{e['outlet']}]({e['url']}))" for e in b["edges"][side]]
                out.append("")
        out.append("**Writers' picks**\n")
        out += ([f"- {p['outlet']}'s {p['writer']} picks {p['pick']} ([link]({p['url']}))" for p in b["picks"]]
                or ["- None in these articles"])
        out.append("\n**Sources used**\n")
        out += [f"- {s['outlet']}, {s['published'][:16].replace('T', ' ')} UTC: [{s['title']}]({s['url']})"
                for s in res["sources"]]
        out.append("")
        out += _sheet(facts.preview_facts(game))
    dropped = [t for t in trail if t["result"] != "kept"]
    if dropped:
        out.append(f"\n<details><summary>{len(dropped)} candidate article(s) dropped</summary>\n")
        out += [f"- {t['via']}: {t['url']} — {t['result']}" for t in dropped]
        out.append("\n</details>")
    return out + [""]


def _recap_md(game, res, t) -> list[str]:
    h, a = game["home"], game["away"]
    out = [f"### Final: {a['name']} {a.get('score')}, {h['name']} {h.get('score')}\n", _status(res, f"{t}s")]
    if res["status"] != "ready":
        return out + [f"> {writer.recap_fallback(game)} ({res.get('reason')})\n"]
    b = res["body"]
    return out + [b["recap"] + "\n", f"*{b['bets']}* (added by code from the graded results)\n",
                  f"**{a['short']}:** {b['away']}\n", f"**{h['short']}:** {b['home']}\n"] + _sheet(facts.recap_facts(game))


def _sheet(f: dict) -> list[str]:
    """The fact sheet the text was written from, so a reviewer can check every claim on the same page."""
    return ["<details><summary>Fact sheet (built by code from the box score)</summary>\n"] + \
        [f"- {line}" for line in f["facts"]] + ["\n</details>\n"]


def _headlines_md(label, res, t) -> list[str]:
    out = [f"### {label}\n", _status(res, f"{t}s")]
    if res["status"] != "ready":
        return out + [f"> No headlines ({res.get('reason')})\n"]
    return out + [f"- {i['text']}" + (f" ([ESPN]({i['url']}))" if i["url"] else "") for i in res["body"]["items"]] + [""]


if __name__ == "__main__":
    main()
