"""Write each AI text from stored facts, and check it in code before anyone sees it.

Every output: facts first, then WRITE (facts -> text). A recap's facts are built by code (facts.py); a preview's
game facts too, with the model extracting only from the articles. Checks, enforced here, not trusted to the prompt:
  * numbers: every number in an extract appears in its inputs, and every number in the text appears in the facts
    (the PRD's "facts only from inputs");
  * no betting advice words;
  * no run of COPY_WORDS words copied from an article;
  * edges and picks point at an article we actually passed in, and a pick's writer is named in that article.
A failed check reruns that step once; a second failure returns status "failed" and the app shows fallback text.

Stage 1 works on plain dicts (the /api/games/{id} payload); Stage 2 adds the ai_texts claim and storage.
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

from . import client, facts, prompts

COPY_WORDS = 8
NUM = re.compile(r"\d+(?:\.\d+)?")
ADVICE = re.compile(r"\b(you should|should bet|take the (over|under|points)|hammer|lock of|best bet|smash|fade|"
                    r"we like|bet on|expect)\b", re.I)     # "Expect a low total" is a prediction
# Football has four quarters; number words escape the digit check ("a seventh-quarter touchdown", 2026-09-29).
BAD_PERIOD = re.compile(r"\b(fifth|sixth|seventh|eighth|ninth|tenth)[\s\-‐-—]quarter", re.I)  # any hyphen: the model writes U+2011
# Recaps leave bet results to code (bets_line), so betting words in the prose mean the model restated them.
BET_TALK = re.compile(r"\b(spread|moneyline|covered|covering|cover|over/under|the (over|under)|push|bets?)\b", re.I)
class CheckFailed(Exception):
    pass


def numbers_ok(text: str, allowed_from: str) -> bool:
    allowed = set(NUM.findall(allowed_from))
    return all(n in allowed for n in NUM.findall(text))


def copied(text: str, articles: list[dict], n: int = COPY_WORDS) -> str | None:
    """The first n-word run of `text` that also appears in an article, or None. Runs containing a number are
    stat lines ("12 passes for 217 yards and four touchdowns"), not prose, so they don't count."""
    norm = lambda s: re.findall(r"[a-z0-9']+", s.lower())
    words = norm(text)
    grams = {g for g in (" ".join(words[i:i + n]) for i in range(len(words) - n + 1)) if not re.search(r"\d", g)}
    for a in articles:
        aw = norm(a["text"])
        for i in range(len(aw) - n + 1):
            g = " ".join(aw[i:i + n])
            if g in grams:
                return g
    return None


def _json(prompt: str) -> dict:
    try:
        out = json.loads(client.write(prompt, json_out=True))
    except (ValueError, client.BadReply):
        raise CheckFailed("reply was not JSON") from None
    if not isinstance(out, dict):
        raise CheckFailed("reply was not a JSON object")
    return out


def _step(prompt: str, check: Callable[[dict], None], stats: dict) -> dict:
    """One writer JSON call plus its check, rerun once if the check fails, told what was wrong.
    RateLimited/AIError propagate."""
    ask = prompt
    for attempt in (1, 2):
        stats["calls"] += 1
        try:
            out = _json(ask)
            try:
                check(out)
            except (AttributeError, KeyError, TypeError):    # JSON of the wrong shape, e.g. an edge that's a string
                raise CheckFailed("reply had the wrong shape") from None
            return out
        except CheckFailed as exc:
            stats.setdefault("rejected", []).append(str(exc))
            if attempt == 2:
                raise
            ask = f"{prompt}\n\nYour previous draft was rejected: {exc}. Write it again without that problem."


def _fact_check(texts: list[str], facts_json: str, stats: dict) -> None:
    """A second model (client.check_model) lists every claim the facts don't support. Any -> CheckFailed, so the
    writer rewrites once with the problems quoted. On 12 known cases from 2026-09-29 it caught 8/8 errors with no
    false alarms (python -m app.ai.check_eval). If the checker can't be reached the text fails: never unchecked."""
    stats["checks"] = stats.get("checks", 0) + 1
    try:
        out = json.loads(client.check(prompts.FACT_CHECK.format(facts=facts_json, text="\n\n".join(texts))))
        probs = (out.get("problems") or []) if isinstance(out, dict) else None
    except (ValueError, client.BadReply):
        probs = None
    if probs is None or not isinstance(probs, list):
        raise client.AIError("fact check: unreadable reply")
    if probs:
        said = "; ".join(f"{p.get('quote', '')!r} ({p.get('why', '')})" if isinstance(p, dict) else str(p)
                         for p in probs)
        raise CheckFailed(f"fact check: {said}")


def _texts_ok(texts: list[str], facts: str, articles: list[dict] = ()) -> None:
    for t in texts:
        if not isinstance(t, str) or not t.strip():
            raise CheckFailed("empty text")
        bad = [n for n in NUM.findall(t) if n not in set(NUM.findall(facts))]
        if bad:
            raise CheckFailed(f"numbers not in the facts: {bad}")
        if BAD_PERIOD.search(t):
            raise CheckFailed(f"no such quarter: {BAD_PERIOD.search(t)[0]!r}")
        if ADVICE.search(t):
            raise CheckFailed(f"advice wording: {ADVICE.search(t)[0]!r}")
        c = copied(t, list(articles))
        if c:
            raise CheckFailed(f"copied from an article: {c!r}")


def _run(fn: Callable[[dict], dict]) -> dict:
    """Wrap a writer: timing, call count and the failed status."""
    stats = {"calls": 0}
    t = time.monotonic()
    try:
        out = fn(stats)
    except CheckFailed as exc:
        out = {"status": "failed", "reason": f"check failed twice: {exc}"}
    except client.RateLimited as exc:
        out = {"status": "failed", "reason": f"rate limited: {exc}"}
    except client.AIError as exc:
        out = {"status": "failed", "reason": str(exc)}
    return out | {"calls": stats["calls"], "checks": stats.get("checks", 0), "rejected": stats.get("rejected", []),
                  "seconds": round(time.monotonic() - t, 1), "model": client.last_writer()}


# ---------------------------------------------------------------- preview

def write_preview(game: dict, articles: list[dict]) -> dict:
    if not articles:
        return {"status": "no_sources", "calls": 0, "rejected": [], "seconds": 0.0, "model": client.model()}

    def go(stats):
        ids = {i + 1: a for i, a in enumerate(articles)}
        arts = "\n\n".join(f"[{i}] {a['outlet']} | {a['published']} | {a['url']}\n{a['title']}\n{a['text']}"
                           for i, a in ids.items())
        gf = facts.preview_facts(game)
        g = "\n".join(gf["facts"])

        def edge_ok(text, article):
            """An edge comes from the article it links to: a real id, and its numbers are in that article."""
            if article not in ids:
                raise CheckFailed("edge without a real article")
            if not numbers_ok(text or "", ids[article]["title"] + " " + ids[article]["text"]):
                raise CheckFailed("edge has numbers its article doesn't")

        def check_extract(x):
            if not numbers_ok(json.dumps(x, ensure_ascii=False), arts):
                raise CheckFailed("extract has numbers not in the articles")
            for s in x.get("storylines") or []:
                if s.get("article") not in ids:
                    raise CheckFailed("storyline without a real article")
            for side in ("home", "away"):
                for e in (x.get("edges") or {}).get(side) or []:
                    edge_ok(e.get("fact"), e.get("article"))

        extract = _step(prompts.EXTRACT_PREVIEW.format(game=g, articles=arts, home=game["home"]["name"],
                                                       away=game["away"]["name"]), check_extract, stats)
        sheet = {"game": gf["facts"], "storylines": extract.get("storylines") or [],
                 "edges": extract.get("edges") or {}}
        fj = json.dumps(sheet, ensure_ascii=False)

        def check_write(x):
            edges = x.get("edges") or {}
            texts = [x.get("preview")] + [e.get("text") for s in ("home", "away") for e in edges.get(s) or []]
            _texts_ok(texts, fj, articles)
            words = len((x.get("preview") or "").split())
            if not 60 <= words <= 200:
                raise CheckFailed(f"preview is {words} words")
            for s in ("home", "away"):
                for e in edges.get(s) or []:
                    edge_ok(e.get("text"), e.get("article"))
            _fact_check(texts, fj, stats)

        out = _step(prompts.WRITE_PREVIEW.format(facts=fj, voice=prompts.VOICE, guardrails=prompts.GUARDRAILS),
                    check_write, stats)
        edges = {s: [{"text": e["text"], "url": ids[e["article"]]["url"], "outlet": ids[e["article"]]["outlet"]}
                     for e in (out.get("edges") or {}).get(s) or []] for s in ("home", "away")}
        picks = []
        for p in extract.get("picks") or []:
            a = ids.get(p.get("article")) if isinstance(p, dict) else None
            # Attributed and linked, and the writer really appears in that article.
            if (a and isinstance(p.get("writer"), str) and isinstance(p.get("pick"), str) and p["writer"].strip()
                    and p["writer"] in a["text"]):
                picks.append({"writer": p["writer"], "outlet": a["outlet"], "pick": p["pick"], "url": a["url"]})
        return {"status": "ready", "body": {"preview": out["preview"], "edges": edges, "picks": picks},
                "sources": [{k: a[k] for k in ("title", "url", "outlet", "published")} for a in articles]}

    return _run(go)


# ---------------------------------------------------------------- recap

def write_recap(game: dict) -> dict:
    """One call: the fact sheet comes from code (facts.recap_facts), so there is no extract step to misread."""
    def go(stats):
        fj = json.dumps(facts.recap_facts(game)["facts"], ensure_ascii=False)

        def check_write(x):
            texts = [x.get("recap"), x.get("home"), x.get("away")]
            _texts_ok(texts, fj)
            words = len((x.get("recap") or "").split())
            if not 60 <= words <= 200:
                raise CheckFailed(f"recap is {words} words")
            bet = next((BET_TALK.search(t) for t in texts if BET_TALK.search(t)), None)
            if bet:
                raise CheckFailed(f"bet talk in the prose: {bet[0]!r}")
            _fact_check(texts, fj, stats)

        out = _step(prompts.WRITE_RECAP.format(facts=fj, voice=prompts.VOICE, guardrails=prompts.GUARDRAILS,
                                               home=game["home"]["name"], away=game["away"]["name"]),
                    check_write, stats)
        # Bet results are the graded text itself, added by code: the model once called a push a win (2026-09-29).
        return {"status": "ready", "body": {"recap": out["recap"], "bets": bets_line(game),
                                            "home": out["home"], "away": out["away"]}}

    return _run(go)


def bets_line(game: dict) -> str:
    """The graded results in the screen's own words: 'Moneyline: CHI won by 20. Spread: CHI +3 covered by 23.'"""
    return " ".join(f"{b['label']}: {b['text']}." for b in game.get("bets") or [] if b.get("text"))


def recap_fallback(game: dict) -> str:
    """PRD stats-only template, e.g. 'Final: Bears 27, Eagles 7. Moneyline: CHI won by 20.'"""
    h, a = game["home"], game["away"]
    return " ".join(x for x in (f"Final: {h['short']} {h.get('score')}, {a['short']} {a.get('score')}.",
                                bets_line(game)) if x)


# ---------------------------------------------------------------- live one-liner

def write_one_liner(game: dict) -> dict:
    def go(stats):
        fj = json.dumps(facts.live_facts(game)["facts"], ensure_ascii=False)

        def check(x):
            line = x.get("line")
            _texts_ok([line], fj)
            if isinstance(line, str) and len(line.split()) > 40:
                raise CheckFailed(f"one-liner is {len(line.split())} words")
            _fact_check([line], fj, stats)

        out = _step(prompts.ONE_LINER.format(facts=fj, voice=prompts.VOICE), check, stats)
        return {"status": "ready", "body": {"line": out["line"]}}

    return _run(go)


# ---------------------------------------------------------------- headlines

def write_headlines(news: list[dict], finals: list[str]) -> dict:
    def go(stats):
        ids = {i + 1: n for i, n in enumerate(news)}
        nj = "\n".join(f"[{i}] {n['published_at']:%Y-%m-%d} | {n['headline']} | {n.get('description') or ''}"
                       for i, n in ids.items())
        fin = "\n".join(finals)
        inputs = nj + "\n" + fin

        def check_extract(x):
            if not numbers_ok(json.dumps(x, ensure_ascii=False), inputs):
                raise CheckFailed("extract has numbers not in the inputs")

        facts = _step(prompts.EXTRACT_HEADLINES.format(news=nj, finals=fin), check_extract, stats)
        fj = json.dumps(facts, ensure_ascii=False)

        def check_write(x):
            items = x.get("items") or []
            if not 5 <= len(items) <= 8:
                raise CheckFailed(f"{len(items)} headlines")
            _texts_ok([i.get("text") for i in items], fj)
            _fact_check([i.get("text") for i in items], fj, stats)

        out = _step(prompts.WRITE_HEADLINES.format(facts=fj, voice=prompts.VOICE, guardrails=prompts.GUARDRAILS),
                    check_write, stats)
        items = [{"text": i["text"], "url": ids[i["news"]]["url"] if i.get("news") in ids else None}
                 for i in out["items"]]
        return {"status": "ready", "body": {"items": items}}

    return _run(go)
