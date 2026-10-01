"""Write each AI text from stored facts, and check it in code before anyone sees it.

Every output: facts first, then WRITE (facts -> text). A recap's facts are built by code (facts.py); a preview's
game facts too, with the model extracting only from the articles. Checks, enforced here, not trusted to the prompt:
  * numbers: every number in an extract appears in its inputs, and every number in the text appears in the facts
    (the PRD's "facts only from inputs");
  * no betting advice words;
  * no run of COPY_WORDS words copied from an article;
  * edges and picks point at an article we actually passed in, and a pick's writer is named in that article;
  * recaps and one-liners: a player's numbers come from that player's line, "X favored / outgained" and home or
    road match the box score, and claims a box score can't support are refused (claims_ok);
  * a preview's point spread is our own line (line_ok).
A failed check reruns that step once; a second failure returns status "failed" and the app shows fallback text.

Stage 1 works on plain dicts (the /api/games/{id} payload); Stage 2 adds the ai_texts claim and storage.
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

from . import client, facts, prompts
from .sources import mentions, team_terms

COPY_WORDS = 8
TRANSIENT_RETRY = 300.0     # seconds before the worker retries a text that failed on a 5xx or a timeout
WHY_CHARS = 200             # a fact-check reason longer than this is the checker's reasoning, not a reason
NUM = re.compile(r"\d+(?:\.\d+)?")
ADVICE = re.compile(r"\b(you should|should bet|take the (over|under|points)|hammer|lock of|best bet|smash|fade|"
                    r"we like|bet on|expect)\b", re.I)     # "Expect a low total" is a prediction
# Football has four quarters; number words escape the digit check ("a seventh-quarter touchdown", 2026-09-29).
BAD_PERIOD = re.compile(r"\b(fifth|sixth|seventh|eighth|ninth|tenth)[\s\-‐-—]quarter", re.I)  # any hyphen: the model writes U+2011
# Recaps leave bet results to code (bets_line), so betting words in the prose mean the model restated them.
BET_TALK = re.compile(r"\b(spread|moneyline|covered|covering|cover|over/under|the (over|under)|push|bets?)\b", re.I)
# Claims a box score can't support, so code rejects them instead of hoping the checker does (2026-09-30 eval:
# "dominated possession", "never relinquishing the lead", "the game's only touchdown", "kept them off balance").
# Text is normalized to straight apostrophes first (_norm).
UNSUPPORTED = re.compile(
    r"\b(dominat\w*|relinquish\w*|wire[\s\-‐-—]to[\s\-‐-—]wire|the game's (only|lone)|"
    r"(two|three|four|five) (more )?scores|because|buoyed|fueled|powered by|off balance|momentum|"
    r"erased|proved (costly|decisive)|the difference)\b", re.I)
HYPHEN = "[\\s\\-‐-—]"      # the model writes U+2011 non-breaking hyphens
TEAM_WORDS = r"[A-Z][\w.'’]*(?:\s[A-Z0-9][\w.'’]*)*"
CLAUSE = re.compile(r"[,;–—]|:(?!\d)|\b(?:and|while|as|but|with|whereas|despite|after|before)\b")
FAVORED = re.compile(rf"\b(?:favou?red|tilted(?: \w+)? (?:toward|to)|in favor of)\s+(?:the\s+)?({TEAM_WORDS})")
OUTGAINED = re.compile(rf"\bout{HYPHEN}?gain(?:ed|ing|s)?\s+(?:the\s+)?({TEAM_WORDS})?")
LONGER = re.compile(r"\b(?:held|kept|had) (?:the ball|possession)\b[\w\s:]{0,30}?\blonger\b", re.I)
ROAD = re.compile(r"\b(on the road|road (win|loss|victory|team)|away from home)\b", re.I)
AT_HOME = re.compile(r"\b(at home|home (win|loss|victory|crowd|fans))\b", re.I)
# Which team stat a "favored X" clause is about; the first match wins, so rushing/passing come before yards.
STAT_WORDS = [(re.compile(r"possession|the ball|clock", re.I), "possessionTime"),
              (re.compile(r"rush|on the ground", re.I), "rushingYards"),
              (re.compile(r"pass|through the air", re.I), "netPassingYards"),
              (re.compile(r"turnover|giveaway|takeaway", re.I), "takeaways"),
              (re.compile(r"yard", re.I), "totalYards")]
NAME_SUFFIX = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv)$", re.I)
# A preview quoted an article's older line ("a 14-point favorite") when FACTS had 14.5 (2026-09-30).
FAVORITE = re.compile(rf"(\d+(?:\.\d+)?){HYPHEN}?point (?:favou?rite|underdog)|favou?red by (\d+(?:\.\d+)?)", re.I)


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
    writer rewrites once with the problems quoted. On 23 known cases (python -m app.ai.check_eval, 2026-09-30) it
    caught 11 of 15 errors with no false alarms; claims_ok catches the commonest kinds first. If the checker can't
    be reached the text fails: never unchecked."""
    stats["checks"] = stats.get("checks", 0) + 1
    try:
        probs = checker_problems(client.check(prompts.FACT_CHECK.format(facts=facts_json, text="\n\n".join(texts))))
    except client.BadReply:
        probs = None
    if probs is None:
        raise client.AIError("fact check: unreadable reply")
    if probs:
        said = "; ".join(f"{p.get('quote', '')!r} ({str(p.get('why', ''))[:WHY_CHARS]})" if isinstance(p, dict)
                         else str(p)[:WHY_CHARS] for p in probs)
        raise CheckFailed(f"fact check: {said}")


def checker_problems(reply: str) -> list | None:
    """The problems in a fact-check reply, or None if it can't be read. Only the verdict counts: Qwen wrote its
    reasoning into "why" and once rejected a draft whose reasoning ended "I see no problems" (2026-09-30), so a
    problem it calls fine is dropped (and _fact_check cuts the reason short)."""
    try:
        out = json.loads(reply)
    except ValueError:
        return None
    probs = out.get("problems") if isinstance(out, dict) else None
    if probs is None:
        return [] if isinstance(out, dict) and "problems" in out else None
    if not isinstance(probs, list):
        return None
    return [p for p in probs if not (isinstance(p, dict) and str(p.get("verdict", "")).lower() in ("fine", "ok"))]


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


# ---------------------------------------------------------------- box-score claims, checked in code

SENTENCE = re.compile(r"(?<!\bJr\.)(?<!\bSr\.)(?<!\b[A-Z]\.)(?<=[.!?])\s+")


def _norm(s: str) -> str:
    return s.replace("’", "'")


def _team_patterns(teams: dict) -> dict[str, re.Pattern]:
    """One pattern per side matching any name the writer may use for that team."""
    out = {}
    for side, t in teams.items():
        names = {t[k] for k in ("name", "short", "nickname", "place") if t.get(k)}
        alts = "|".join(re.escape(x) for x in sorted(names, key=len, reverse=True))
        abbr = rf"|\b{re.escape(t['abbr'])}\b" if t.get("abbr") else ""
        out[side] = re.compile(rf"\b(?:{alts})\b{abbr}" if alts else abbr.lstrip("|") or r"(?!)")
    return out


def _sides_in(text: str, pats: dict) -> list[tuple[int, str]]:
    """(position, side) of every team mention, in order."""
    return sorted((m.start(), side) for side, p in pats.items() for m in p.finditer(text))


def _side_of(phrase: str, pats: dict) -> str | None:
    hits = {s for _, s in _sides_in(phrase, pats)}
    return hits.pop() if len(hits) == 1 else None


def _player_patterns(players: list[dict], pats: dict) -> list[tuple[re.Pattern, dict]]:
    """Full name, name without Jr./III, and the last name alone when no other leader or team shares it."""
    lasts = [NAME_SUFFIX.sub("", _norm(p.get("last_name") or p["name"].split()[-1])) for p in players]
    out = []
    for p, last in zip(players, lasts):
        full = _norm(p["name"])
        names = {full, NAME_SUFFIX.sub("", full)}
        if lasts.count(last) == 1 and not any(pt.search(last) for pt in pats.values()):
            names.add(last)
        alts = "|".join(re.escape(x) for x in sorted(names, key=len, reverse=True))
        out.append((re.compile(rf"\b(?:{alts})\b"), p))
    return out


def _stat_winner(key: str, st: dict) -> str | None:
    """The side with more of a team stat (takeaways: the side the other one gave the ball to), None if even."""
    if key == "takeaways":
        row = st.get("turnovers") or {}
        h, a = facts._int(row.get("away")), facts._int(row.get("home"))
    else:
        row = st.get(key) or {}
        conv = facts.clock_seconds if key == "possessionTime" else facts._int
        h, a = conv(row.get("home")), conv(row.get("away"))
    if h is None or a is None or h == a:
        return None
    return "home" if h > a else "away"


def _stat_key(clause: str) -> str:
    return next((k for p, k in STAT_WORDS if p.search(clause)), "totalYards")


def claims_ok(texts: list[str], game: dict, sheet: dict) -> None:
    """Box-score claims checked in code, before the model checker (2026-09-30 eval, 24 errors in 14 texts):
    a player's numbers come from that player's line (three recaps gave team totals to the leading rusher and
    passer); "X favored / outgained / held the ball longer" names the team that really had more; home and road
    are right; and phrases a box score can't support are refused."""
    pats = _team_patterns(sheet["teams"])
    ppats = _player_patterns(sheet.get("players") or [], pats)
    st = sheet.get("stats") or {}
    hdr = game.get("header") or {}
    shared = set(NUM.findall(" ".join(str(x) for x in (
        game["home"].get("score"), game["away"].get("score"),
        (hdr.get("home") or {}).get("record"), (hdr.get("away") or {}).get("record")) if x is not None)))
    short = {s: sheet["teams"][s]["short"] or sheet["teams"][s]["name"] for s in ("home", "away")}
    probs = []
    for text in texts:
        text = _norm(text)
        probs += [f"{m[0]!r}: a box score can't show that; leave it out" for m in UNSUPPORTED.finditer(text)]
        for sent in SENTENCE.split(text):
            for clause in CLAUSE.split(sent):
                who = [p for pt, p in ppats if pt.search(clause)]
                if who:
                    allowed = shared.union(*(NUM.findall(p["value"]) for p in who))
                    extra = [n for n in NUM.findall(clause) if n not in allowed]
                    if extra:
                        lines = "; ".join(f"{p['name']}: {p['value']}" for p in who)
                        probs.append(f"{clause.strip()!r}: {', '.join(extra)} isn't on the player's line "
                                     f"({lines}). Team totals belong to the team, not a player")
                m = FAVORED.search(clause)
                if m and _side_of(m[1], pats):
                    key, side = _stat_key(clause[:m.start()] + clause[m.end():]), _side_of(m[1], pats)
                    win = _stat_winner(key, st)
                    if win != side:
                        probs.append(f"{clause.strip()!r}: FACTS show "
                                     f"{'no edge' if win is None else short[win] + ' had more'} there")
                m = OUTGAINED.search(clause)
                if m:
                    obj = _side_of(m[1], pats) if m[1] else None
                    before = _sides_in(clause[:m.start()], pats)
                    side = ({"home": "away", "away": "home"}[obj] if obj else before[-1][1] if before else None)
                    win = _stat_winner(_stat_key(clause), st)
                    if side and win != side:
                        probs.append(f"{clause.strip()!r}: FACTS show "
                                     f"{'no edge' if win is None else short[win] + ' had more yards'}")
                m = LONGER.search(clause)
                if m:
                    before = _sides_in(clause[:m.start()], pats)
                    win = _stat_winner("possessionTime", st)
                    if before and win != before[-1][1]:
                        probs.append(f"{clause.strip()!r}: FACTS show "
                                     f"{'even' if win is None else short[win] + ' had the ball longer'}")
            sides = {s for _, s in _sides_in(sent, pats)}
            if len(sides) == 1 and not game.get("neutral_site"):
                side = sides.pop()
                if ROAD.search(sent) and side == "home" or AT_HOME.search(sent) and side == "away":
                    probs.append(f"{sent.strip()!r}: {sheet['teams'][side]['name']} were the "
                                 f"{'home' if side == 'home' else 'visiting'} team")
    if probs:
        raise CheckFailed("; ".join(probs))


def _run(fn: Callable[[dict], dict]) -> dict:
    """Wrap a writer: timing, call count and the failed status."""
    stats = {"calls": 0}
    t = time.monotonic()
    client.forget_last()
    try:
        out = fn(stats)
    except CheckFailed as exc:
        out = {"status": "failed", "reason": f"check failed twice: {exc}"}
    except client.RateLimited as exc:
        out = {"status": "failed", "reason": f"rate limited: {exc}", "retry_after": exc.retry_after}
    except (client.NoKey, client.TooLarge) as exc:
        out = {"status": "failed", "reason": str(exc)}                 # retrying can't help
    except client.AIError as exc:
        # A 5xx, a timeout, an unreadable check: worth another try later (the worker retries on retry_after).
        out = {"status": "failed", "reason": str(exc), "retry_after": TRANSIENT_RETRY}
    return out | {"calls": stats["calls"], "checks": stats.get("checks", 0), "rejected": stats.get("rejected", []),
                  "seconds": round(time.monotonic() - t, 1), "model": client.last_writer(),
                  "checker": client.last_checker()}


# ---------------------------------------------------------------- preview

def write_preview(game: dict, articles: list[dict], extract: dict | None = None) -> dict:
    """extract: the article extract saved with an earlier version of this preview (keyed by article URL), for the
    same articles. The game-morning refresh passes it when only our own data changed, skipping the extract call."""
    if not articles:
        return {"status": "no_sources", "calls": 0, "rejected": [], "seconds": 0.0, "model": client.model()}
    saved = extract

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

        if saved is None:
            extract = _step(prompts.EXTRACT_PREVIEW.format(game=g, articles=arts, home=game["home"]["name"],
                                                           away=game["away"]["name"]), check_extract, stats)
        else:
            # Saved by URL: map onto today's numbering. The same articles can come back in another order, and
            # positions would then point an edge at the wrong article (audit, 2026-09-29).
            extract = _by_id(saved, {a["url"]: i for i, a in ids.items()})
        sheet = {"game": gf["facts"], "storylines": extract.get("storylines") or [],
                 "edges": extract.get("edges") or {}}
        fj = json.dumps(sheet, ensure_ascii=False)

        def check_write(x):
            edges = x.get("edges") or {}
            texts = [x.get("preview")] + [e.get("text") for s in ("home", "away") for e in edges.get(s) or []]
            _texts_ok(texts, fj, articles)
            line_ok(x.get("preview"), game)
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
            why = pick_problem(p, a, game)
            if why:
                stats.setdefault("rejected", []).append(f"pick dropped: {why}")
                continue
            picks.append({"writer": p["writer"].strip(), "outlet": a["outlet"], "pick": p["pick"].strip(),
                          "url": a["url"]})
        return {"status": "ready", "body": {"preview": out["preview"], "edges": edges, "picks": picks},
                "sources": [{k: a[k] for k in ("title", "url", "outlet", "published")} for a in articles],
                "extract": _by_url(extract, {i: a["url"] for i, a in ids.items()})}

    return _run(go)


def line_ok(text: str, game: dict) -> None:
    """The preview states the point spread only as our own line has it, never an article's older number."""
    sp = (game.get("line") or {}).get("home_spread")
    want = f"{abs(float(sp)):g}" if sp is not None else None
    bad = [m[0] for m in FAVORITE.finditer(text) if (m[1] or m[2]) != want]
    if bad:
        raise CheckFailed(f"{bad[0]!r}: the line in FACTS is {want + ' points' if want else 'not set'}; "
                          "use that number or leave the line out")


PICK_WINDOW = 40         # words: a pick's writer and a team must be named this close together in its article
PICK_MAX_WORDS = 15


def pick_problem(p: object, article: dict | None, game: dict) -> str | None:
    """Why a writer's pick can't be shown, or None. Picks go on screen as quoted from the extract, so they get their
    own checks (the audit published "You should hammer the Bears, lock of the year" from a coach's quote):
    a real article, short, no advice words, numbers only from that article, one of the two teams named, and the
    writer's name within PICK_WINDOW words of a team name in the article."""
    if not isinstance(p, dict) or article is None:
        return "no such article"
    who, pick = p.get("writer"), p.get("pick")
    if not isinstance(who, str) or not who.strip() or not isinstance(pick, str) or not pick.strip():
        return "missing writer or pick"
    if len(pick.split()) > PICK_MAX_WORDS:
        return "pick too long"
    if ADVICE.search(pick):
        return f"advice wording: {ADVICE.search(pick)[0]!r}"
    body = f"{article['title']} {article['text']}"
    if not numbers_ok(pick, body):
        return "numbers not in its article"
    terms = team_terms(game["home"]) + team_terms(game["away"])
    if not mentions(pick, terms):
        return "names neither team"
    words = body.split()
    first = who.split()[0].lower()
    last = who.split()[-1].lower().strip(".,")
    near = False
    for i, w in enumerate(words):
        if w.lower().strip(".,'\"") == last and (i == 0 or first in words[i - 1].lower() or first == last):
            window = " ".join(words[max(0, i - PICK_WINDOW): i + PICK_WINDOW])
            if mentions(window, terms):
                near = True
                break
    return None if near else "writer not named near a team in the article"


def _by_url(extract: dict, url_of: dict[int, str]) -> dict:
    """The extract with article numbers replaced by URLs, for storing."""
    def conv(item):
        return {k: v for k, v in item.items() if k != "article"} | {"url": url_of.get(item.get("article"))}
    return {"storylines": [conv(x) for x in extract.get("storylines") or [] if isinstance(x, dict)],
            "edges": {s: [conv(x) for x in (extract.get("edges") or {}).get(s) or [] if isinstance(x, dict)]
                      for s in ("home", "away")},
            "picks": [conv(x) for x in extract.get("picks") or [] if isinstance(x, dict)]}


def _by_id(saved: dict, id_of: dict[str, int]) -> dict:
    """A stored extract mapped onto today's article numbers; items whose article isn't here any more are dropped."""
    def conv(items):
        return [{k: v for k, v in x.items() if k != "url"} | {"article": id_of[x["url"]]}
                for x in items or [] if isinstance(x, dict) and x.get("url") in id_of]
    return {"storylines": conv(saved.get("storylines")),
            "edges": {s: conv((saved.get("edges") or {}).get(s)) for s in ("home", "away")},
            "picks": conv(saved.get("picks"))}


# ---------------------------------------------------------------- recap

# A one-minute read (decided 2026-10-01): recap paragraph and each team paragraph, in words. The bets line (~16
# words, code) comes on top.
RECAP_WORDS = {"standard": (110, 45), "featured": (120, 55)}
LENGTH_SLACK = 1.1          # code rejects a recap screen more than 10% over its length


def _record(rec: str | None) -> tuple[int, int] | None:
    parts = [int(x) for x in re.findall(r"\d+", rec or "")]
    return (parts[0], parts[1]) if len(parts) >= 2 else None


def recap_length(game: dict) -> tuple[str, str]:
    """('featured' or 'standard', why). Featured, a little longer: a favorite team, ranked vs ranked, two NFL teams
    with winning records going in, or a great game (overtime, decided by 3 or less, or 2+ lead changes)."""
    h, a = game["home"], game["away"]
    if game.get("favorite"):
        return "featured", "a favorite team"
    if game.get("league") == "ncaaf" and h.get("rank") and a.get("rank"):
        return "featured", "ranked vs ranked"
    hs, as_ = h.get("score"), a.get("score")
    if game.get("league") == "nfl" and hs is not None and as_ is not None and hs != as_:
        hdr = game.get("header") or {}
        before = []
        for side, won in (("home", hs > as_), ("away", as_ > hs)):
            r = _record((hdr.get(side) or {}).get("record"))     # ESPN's record after this game
            before.append(r and (r[0] - won, r[1] - (not won)))
        if all(b and b[0] > b[1] for b in before):
            return "featured", "two winning teams"
    al, _ = facts._linescores(game)
    if len(al) > 4:
        return "featured", "overtime"
    if hs is not None and as_ is not None and abs(hs - as_) <= 3:
        return "featured", "decided by 3 or less"
    if facts.lead_changes(game) >= 2:
        return "featured", "lead changed 2+ times"
    return "standard", "standard"


def write_recap(game: dict) -> dict:
    """One call: the fact sheet comes from code (facts.recap_facts), so there is no extract step to misread."""
    tier, why = recap_length(game)
    recap_w, team_w = RECAP_WORDS[tier]
    cap = round((recap_w + 2 * team_w) * LENGTH_SLACK)

    def go(stats):
        sheet = facts.recap_facts(game)
        fj = json.dumps(sheet["facts"], ensure_ascii=False)

        def check_write(x):
            texts = [x.get("recap"), x.get("home"), x.get("away")]
            _texts_ok(texts, fj)
            claims_ok(texts, game, sheet)
            words = len((x.get("recap") or "").split())
            total = sum(len(t.split()) for t in texts)
            if words < 60 or total > cap:
                raise CheckFailed(f"recap is {words} words and {total} in all; keep the recap at about {recap_w} "
                                  f"words, each team at about {team_w}, {cap} in all at most")
            bet = next((BET_TALK.search(t) for t in texts if BET_TALK.search(t)), None)
            if bet:
                raise CheckFailed(f"bet talk in the prose: {bet[0]!r}")
            _fact_check(texts, fj, stats)

        out = _step(prompts.WRITE_RECAP.format(facts=fj, voice=prompts.VOICE, guardrails=prompts.GUARDRAILS,
                                               home=game["home"]["name"], away=game["away"]["name"],
                                               recap_words=recap_w, team_words=team_w),
                    check_write, stats)
        # Bet results are the graded text itself, added by code: the model once called a push a win (2026-09-29).
        return {"status": "ready", "body": {"recap": out["recap"], "bets": bets_line(game),
                                            "home": out["home"], "away": out["away"]},
                "length": {"tier": tier, "why": why,
                           "words": sum(len(out[k].split()) for k in ("recap", "home", "away"))}}

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
        sheet = facts.live_facts(game)
        fj = json.dumps(sheet["facts"], ensure_ascii=False)

        def check(x):
            line = x.get("line")
            _texts_ok([line], fj)
            claims_ok([line], game, sheet)
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
