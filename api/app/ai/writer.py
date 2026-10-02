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
import os
import re
import time
from typing import Callable

from . import client, facts, prompts
from .sources import mentions, team_terms

COPY_WORDS = 8
RECAP_VOICE = True          # Adam's house style and example recaps in the recap prompt; False = the plain prompt (Oct 1)
EXAMPLE_COPY_WORDS = 6      # a run this long (no numbers) copied from one of the prompt's example recaps is refused
TRANSIENT_RETRY = 300.0     # seconds before the worker retries a text that failed on a 5xx or a timeout
WHY_CHARS = 200             # a fact-check reason longer than this is the checker's reasoning, not a reason
NUM = re.compile(r"\d+(?:\.\d+)?")
ADVICE = re.compile(r"\b(you should|should bet|take the (over|under|points)|hammer|lock of|best bet|smash|fade|"
                    r"we like|bet on|expect)\b", re.I)     # "Expect a low total" is a prediction
# Football has four quarters; number words escape the digit check ("a seventh-quarter touchdown", 2026-09-29).
BAD_PERIOD = re.compile(r"\b(fifth|sixth|seventh|eighth|ninth|tenth)[\s\-‐-—]quarter", re.I)  # any hyphen: the model writes U+2011
# Recaps leave bet results to code (bets_line), so betting words in the prose mean the model restated them.
BET_TALK = re.compile(r"\b(spread|moneyline|covered|covering|cover|over/under|the (over|under)|bets?)\b", re.I)
# A bet's push, not "Seattle's fourth-quarter push" (Adam's sample, 2026-10-01): only with another betting word.
BET_PUSH = re.compile(r"\bpush\b(?=.*\b(?:line|total|odds|wager|bets?|spread)\b)|"
                      r"\b(?:line|total|odds|wager|bets?|spread)\b.*\bpush\b", re.I | re.S)


def bet_talk(texts: list[str]) -> str | None:
    """The first betting word in the prose, or None."""
    for t in texts:
        m = BET_TALK.search(t)
        if m:
            return m[0]
        if BET_PUSH.search(t):
            return "push"
    return None
# Claims a box score can't support, so code rejects them instead of hoping the checker does (2026-09-30 eval:
# "dominated possession", "never relinquishing the lead", "the game's only touchdown", "kept them off balance").
# Text is normalized to straight apostrophes first (_norm).
UNSUPPORTED = re.compile(
    r"\b(dominat\w*|relinquish\w*|wire[\s\-‐-—]to[\s\-‐-—]wire|the game's (only|lone)|"
    r"(two|three|four|five) (more )?scores|because|buoyed|fueled|powered by|off balance|momentum|"
    r"erased|proved (costly|decisive)|"
    # "The difference was the turnover column" (Adam's sample) restates a stat; any other "difference" is a cause.
    r"the difference(?!\s+(?:was|is|came down to)\s+the\s+(?:turnover|takeaway|giveaway|penalt|yardage|possession)\w*)|"
    # Causes (2026-10-01, from the rerun: "bolstered by a clean ball", "capitalized on key opportunities", "Denver
    # checked out ... allowing Los Angeles to establish", "to control the game"). A box score shows what happened,
    # never why. "Thanks to 7 points in the first" only restates the score, so a number after it is fine.
    r"bolster\w*|boost(?:ed|ing)|propell\w*|spurr\w*|spark(?:ed|ing)|driven by|due to|thanks to(?!\s+\d)|"
    r"capitaliz\w*|took advantage|checked out|woke up|led to|resulted in|"
    # "allowing Los Angeles to establish" (a cause), not "allowed 17 points to Atlanta" (a stat): a verb follows "to".
    r"allow(?:ed|ing)\b[^.,;]{0,40}?\bto\s+(?-i:(?!the\b|an?\b|their\b|its\b|his\b)[a-z]+)|"
    r"as a result|paid off|turned the tide|"
    r"control(?:led|ling|s)? (?:the )?(?:game|pace|tempo|contest|flow|action|early)|to control|"
    # Streaks and records going in: FACTS has only the record after this game ("winless in four games").
    r"winless|unbeaten|undefeated|streak\w*|in a row|consecutive|straight (?:win|loss|game)s?|"
    # The whole game: a box score has only the score at each quarter break ("trailing all game", "from start to
    # finish", "preserving the lead through the end", "kept a 7-point edge through the third and fourth").
    r"all (?:game|night|afternoon)|start[\s\-‐-—]to[\s\-‐-—]finish|beginning to end|"
    r"(?:the )?(?:entire|whole) (?:game|contest)|from the opening (?:whistle|kickoff|snap)|"
    r"(?:through|until|to) the (?:end(?!\s+of\s+(?:the\s+)?(?:first|second|third|fourth|1st|2nd|3rd|4th|half|q[1-4]))|"
    r"final whistle)|the rest of the way|"
    r"(?:lead|edge|advantage|margin|cushion)\s+(?:through|throughout)\s+(?:the\s+)?(?:first|second|third|fourth|"
    r"half|rest|game|contest|end)|"
    # Inside a quarter: "a late field goal", "controlled the early minutes", "scored first".
    r"(?:late|early) (?:field goals?|touchdowns?|scores?|points?|drives?|rally|surge|push|run|minutes|moments|"
    r"stages|going)|(?:scored|struck) first)\b", re.I)
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
              (re.compile(r"rush|on the ground|ground game", re.I), "rushingYards"),
              (re.compile(r"pass|through the air", re.I), "netPassingYards"),
              (re.compile(r"turnover|giveaway|takeaway", re.I), "takeaways"),
              (re.compile(r"yard", re.I), "totalYards")]
NAME_SUFFIX = re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv)$", re.I)
# A preview quoted an article's older line ("a 14-point favorite") when FACTS had 14.5 (2026-09-30).
FAVORITE = re.compile(rf"(\d+(?:\.\d+)?){HYPHEN}?point (?:favou?rite|underdog)|favou?red by (\d+(?:\.\d+)?)", re.I)

# Order inside a quarter is unknowable: the sheet has the score at each quarter break only (2026-09-30 eval and the
# Oct 1 rerun: "added 3 in the second before the Eagles tied", "put three points on the board in the second while the
# Browns responded with ten"). A sentence naming a quarter may not say what came "before" something or who "responded"
# (a team can respond to the break score before, so only the same sentence's quarter counts), and "tied" must be a
# tie the sheet shows at a quarter break.
QUARTER_NO = {"first": 0, "1st": 0, "q1": 0, "second": 1, "2nd": 1, "q2": 1, "third": 2, "3rd": 2, "q3": 2,
              "fourth": 3, "4th": 3, "q4": 3}
# A quarter is "in the second", "the second quarter", "Q4": not "a second touchdown" or "a 14-second drive".
Q_THE = re.compile(r"\b(?:in|of|during|through|into|after|by|until|before|to)\s+the\s+(first|second|third|fourth)\b"
                   r"(?!\s+(?:touchdown|field|score|time|down|straight|interception|possession|drive|team|half))", re.I)
Q_NAMED = re.compile(r"\b(?:(first|second|third|fourth|1st|2nd|3rd|4th)[\s-]+(?:quarter|period|stanza|frame)|"
                     r"(q[1-4]))\b", re.I)
QUARTER_WORD = re.compile(r"\b(?:quarter|period|stanza)\b", re.I)


def _quarters(text: str) -> list[int]:
    """The quarters a text names, in order (0 = first), none for 'a second touchdown' or 'a 14-second drive'."""
    hits = [(m.start(), m[1]) for m in Q_THE.finditer(text)] + [(m.start(), m[1] or m[2]) for m in Q_NAMED.finditer(text)]
    return [QUARTER_NO[w.lower()] for _, w in sorted(hits)]


def _has_quarter(text: str) -> bool:
    return bool(_quarters(text) or QUARTER_WORD.search(text))
BEFORE = re.compile(r"\bbefore\b(?!\s+(?:the\s+)?(?:half|halftime|break|intermission|final|end|clock|game|quarter|"
                    r"(?:first|second|third|fourth|1st|2nd|3rd|4th)\b))", re.I)
RESPONDED = re.compile(r"\brespond(?:ed|ing|s)?\b", re.I)
TIED = re.compile(r"\b(?:tied|ties|tie|tying)\b", re.I)
EQUAL_SCORE = re.compile(r"(\d+)\s*-\s*(\d+)")
BREAK_WORDS = [(re.compile(r"half|halftime"), 1), (re.compile(r"end of (?:the )?(?:first|1st|q1)|after (?:the )?(?:first|1st|q1)"), 0),
               (re.compile(r"end of (?:the )?(?:third|3rd|q3)|after (?:the )?(?:third|3rd|q3)"), 2),
               (re.compile(r"end of (?:the )?(?:fourth|4th|q4|regulation)|after (?:the )?(?:fourth|4th|q4)"), 3)]
# "Kept the Chargers ahead 13-10" when the half was tied; "to close the gap" when the quarter began tied.
KEPT_LEAD = re.compile(r"\b(?:kept|held|maintained|preserv\w+|extended|stretch\w*|widened|retained|protected)\b"
                       r"[^.]{0,40}?\b(?:ahead|lead|edge|advantage|margin)\b", re.I)
CLOSE_GAP = re.compile(r"\b(?:clos\w+|narrow\w*|trim\w*|cut\w*)\b[^.]{0,20}?\b(?:gap|deficit)\b", re.I)
# Comparisons the sentence makes about both teams: "a passing advantage of 250 to 277", "turnovers were split".
ADVANTAGE = re.compile(r"\b(?:advantage|edge)\b([^.;]{0,30}?)(\d[\d:.,]*)\s+to\s+(\d[\d:.,]*)", re.I)
SYMMETRIC = re.compile(r"\b(?:each (?:side|team)|both (?:teams|sides|squads)|split|evenly|equal(?:ly)?|matched)\b", re.I)
TURNOVERS = re.compile(r"turnover|giveaway|takeaway", re.I)
# A number given to the wrong thing: a player's yards as the team's ("offense sputtered: 199 passing yards", Maye's
# number), "only 3 points" for a team that scored 6, "six at halftime" for a team that led 9-0 at the half.
TEAM_YARDS = re.compile(r"(\d[\d,]*)\s+(passing|rushing)\s+yards", re.I)
ONLY_POINTS = re.compile(r"\b(?:only|just|merely)\s+(\d+)\s+points?\b", re.I)
WORD_NUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                       "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
AT_HALF = re.compile(rf"(?<![\d-])\b(\d+|{'|'.join(WORD_NUM)})\s+(?:points?\s+)?(?:at|by)\s+(?:the\s+)?(?:half|halftime)\b",
                     re.I)
# "held the ball for over five minutes longer" when the gap was 4:56.
LONGER_BY = re.compile(r"\b(over|more than|just over|nearly|almost|just under|about|roughly|around)?\s*"
                       rf"(\d+|{'|'.join(WORD_NUM)})\s+(?:full\s+)?minutes?(?:\s+and\s+[\w-]+\s+seconds?)?\s+(?:longer|more)\b",
                       re.I)
LEAD_CHANGE = re.compile(r"\blead changes?\b|\bchanged hands\b", re.I)
# "Seattle surged ahead with 14 points in Q4" when Seattle trailed at the end of Q4.
TOOK_LEAD = re.compile(r"\b(?:surg\w*|pull\w*|mov\w*|went|vault\w*|jump\w*) ahead\b|"
                       r"\b(?:took|take|seiz\w+|grabb?\w*|regain\w*|retook|snatch\w*) (?:the |a )?lead\b", re.I)


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


def _json(prompt: str, light: bool = False, reasoning: str | None = None) -> dict:
    # reasoning only when set, so every other call is exactly what it was.
    effort = {"reasoning": reasoning} if reasoning else {}
    try:
        out = json.loads(client.write(prompt, json_out=True, light=light, **effort))
    except (ValueError, client.BadReply):
        raise CheckFailed("reply was not JSON") from None
    if not isinstance(out, dict):
        raise CheckFailed("reply was not a JSON object")
    return out


def _step(prompt: str, check: Callable[[dict], None], stats: dict, light: bool = False,
          reasoning: str | None = None) -> dict:
    """One writer JSON call plus its check, rerun once if the check fails, told what was wrong. light: a short
    structured reply (client.write's low-reasoning mode). reasoning: the writer's effort for both calls (None: the
    client's own). RateLimited/AIError propagate."""
    ask = prompt
    for attempt in (1, 2):
        stats["calls"] += 1
        try:
            out = _json(ask, light, reasoning)
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


APPROX = re.compile(r"\b(?:nearly|almost|about|roughly|around|just over|just under)\s+(\d+(?:\.\d+)?)\b", re.I)
CLOCK = re.compile(r"\b(\d+):(\d\d)\b")


def _rounded(t: str, facts: str) -> set[str]:
    """Numbers t rounds with a word ("nearly 37 minutes" for 36:53, "about 500 yards" for 498): within a unit, or
    3%, of a number in the facts (a clock counts as its minutes)."""
    vals = [float(n) for n in NUM.findall(facts)] + [int(m) + int(s) / 60 for m, s in CLOCK.findall(facts)]
    return {m[1] for m in APPROX.finditer(t)
            if any(abs(v - float(m[1])) <= max(1.0, 0.03 * float(m[1])) for v in vals)}


def _texts_ok(texts: list[str], facts: str, articles: list[dict] = ()) -> None:
    for t in texts:
        if not isinstance(t, str) or not t.strip():
            raise CheckFailed("empty text")
        bad = [n for n in NUM.findall(t) if n not in set(NUM.findall(facts)) | _rounded(t, facts)]
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
    """Straight apostrophes and plain hyphens: the model writes U+2019 and the non-breaking hyphen U+2011, so a score
    like 16‑0 would not read as one. En and em dashes stay: CLAUSE splits on them."""
    return s.replace("’", "'").translate({0x2010: "-", 0x2011: "-", 0x2012: "-"})


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


def _quarter_no(text: str) -> int | None:
    qs = _quarters(text)
    return qs[0] if qs else None


def _tie_at_break(sent: str, brks: list) -> bool:
    """The sentence's 'tied' matches a tie the sheet shows at a quarter break (an equal score, or a named break)."""
    ties = {i: at for i, at, ht, lead, _ in brks if lead is None}
    low = sent.lower()
    if any(m[1] == m[2] and int(m[1]) in ties.values() for m in EQUAL_SCORE.finditer(low)):
        return True
    return any(p.search(low) and i in ties for p, i in BREAK_WORDS)


def _timing_problems(sent: str, game: dict, sheet: dict, pats: dict, ppats: list, st: dict) -> list[tuple[str, str]]:
    """(span, what's wrong) for what a sentence says about order, who led when, and comparisons between the teams
    that the sheet contradicts or can't show. The sheet knows the score only at quarter breaks (Oct 1: the 24 + 15
    reviewed errors). The span is the exact words flagged, so a sentence with two errors reports each one."""
    out = []
    al, hl = facts._linescores(game)
    brks = facts._breaks(al, hl)
    sides = [s for _, s in _sides_in(sent, pats)]
    one = sides[0] if len(set(sides)) == 1 else None
    short = {s: sheet["teams"][s]["short"] or sheet["teams"][s]["name"] for s in ("home", "away")}
    if brks and _has_quarter(sent):
        # Order is unknowable only in a quarter where both teams scored; "before Tennessee's touchdown in the fourth"
        # after three Giants quarters is plain from the breaks.
        named = set(_quarters(sent))
        both = any(q < len(al) and al[q] and hl[q] for q in named)
        if BEFORE.search(sent) and both:
            out.append((sent, "'before' inside a quarter: the score is known only at quarter breaks, so say what "
                              "each quarter added, not what came first"))
        if RESPONDED.search(sent) and both:
            out.append((sent, "'responded' inside a quarter: the order of scores within a quarter isn't known"))
        if TIED.search(sent) and not _tie_at_break(sent, brks):
            out.append((sent, "'tied' is a tie the sheet doesn't show at a quarter break; say who led at each break"))
    for clause in CLAUSE.split(sent):
        cs = {s for _, s in _sides_in(clause, pats)}
        qn = _quarter_no(clause)
        if brks and qn is not None and len(cs) == 1 and KEPT_LEAD.search(clause) and 0 <= qn <= len(brks):
            side = next(iter(cs))
            if (brks[qn - 1][3] if qn else None) != side:
                out.append((clause, f"{short[side]} did not lead at the break before that quarter, so nothing was "
                                    f"kept or extended"))
        if brks and len(cs) == 1 and TOOK_LEAD.search(clause):
            # The quarter in the clause, else the first one named after it ("surged ahead with 14 points in Q4").
            tq = qn if qn is not None else _quarter_no(sent[sent.find(clause) + len(clause):])
            side = next(iter(cs))
            if tq is not None and tq < len(brks) and brks[tq][3] != side:
                out.append((clause, f"{short[side]} did not lead at the end of that quarter"))
        if SYMMETRIC.search(clause) and TURNOVERS.search(clause):
            row = st.get("turnovers") or {}
            h, a = facts._int(row.get("home")), facts._int(row.get("away"))
            if h is not None and a is not None and h != a:
                out.append((clause, f"turnovers were not even (giveaways: {short['away']} {a}, {short['home']} {h})"))
    if brks and CLOSE_GAP.search(sent):
        qn = _quarter_no(sent)
        if qn and qn <= len(brks) and brks[qn - 1][3] is None:
            out.append((CLOSE_GAP.search(sent)[0], "there was no gap to close: the score was tied at the break "
                                                   "before that quarter"))
    for m in ADVANTAGE.finditer(sent):
        # "advantage of 250 to 277" gives the advantage to whoever has the first number, whatever the sentence's subject.
        key = next((k for p, k in STAT_WORDS if p.search(sent[max(0, m.start() - 30):m.start()] + m[1])), None)
        row = st.get(key) or {}
        first = next((s for s in ("home", "away") if str(row.get(s)).replace(",", "") == m[2].replace(",", "")), None)
        if first and _stat_winner(key, st) != first:
            win = _stat_winner(key, st)
            out.append((sent[max(0, m.start() - 30):m.end()], f"{short[first]} had the {m[2]}, not the advantage: "
                        f"FACTS show {'no edge' if win is None else short[win] + ' had more'} there"))
    top = st.get("possessionTime") or {}
    gap = None
    h, a = facts.clock_seconds(top.get("home")), facts.clock_seconds(top.get("away"))
    if h is not None and a is not None:
        gap = abs(h - a)
    for m in LONGER_BY.finditer(sent):
        n = (int(m[2]) if m[2].isdigit() else WORD_NUM[m[2].lower()]) * 60
        qual = (m[1] or "").lower()
        if gap is None:
            continue
        ok = (gap > n if qual in ("over", "more than", "just over") else
              n - 60 < gap <= n if qual in ("nearly", "almost", "just under") else
              abs(gap - n) <= 60 if qual in ("about", "roughly", "around") else n <= gap < n + 60)
        if not ok:
            out.append((m[0], f"the possession gap was {gap // 60}:{gap % 60:02d}; use that"))
    if one:
        if LEAD_CHANGE.search(sent) and not re.search(r"\b(no|never|zero|without)\b", sent, re.I):
            movers = {lead for *_, lead, changed in brks if changed}
            if movers and one not in movers:
                out.append((sent, f"{short[one]} never took the lead from the other team at a quarter break "
                                  f"(FACTS name who did)"))
        if not any(pt.search(sent) for pt, _ in ppats):
            for m in TEAM_YARDS.finditer(sent):
                n = m[1].replace(",", "")
                row = st.get("netPassingYards" if m[2].lower() == "passing" else "rushingYards") or {}
                if n not in {str(facts._int(row.get("home"))), str(facts._int(row.get("away")))}:
                    owner = next((p for p in sheet.get("players") or [] if n in NUM.findall(p["value"])), None)
                    if owner:
                        out.append((m[0], f"{n} is {owner['name']}'s own line, not the team's"))
        pts = game[one].get("score")
        for m in ONLY_POINTS.finditer(sent):
            ok = {pts} | (set(hl if one == "home" else al) if _has_quarter(sent) else set())
            if int(m[1]) not in ok:
                out.append((m[0], f"{short[one]} scored {pts} in all ('only' is a claim: use FACTS' exact numbers)"))
        for m in AT_HALF.finditer(sent):
            n = int(m[1]) if m[1].isdigit() else WORD_NUM[m[1].lower()]
            if len(brks) > 1 and n != brks[1][2 if one == "home" else 1]:
                out.append((m[0], f"{short[one]} had {brks[1][2 if one == 'home' else 1]} at halftime in all"))
    return out


def claims_ok(texts: list[str], game: dict, sheet: dict, final: bool = True) -> None:
    probs = claim_problems(texts, game, sheet, final)
    if probs:
        raise CheckFailed("; ".join(msg for _, msg in probs))


def claim_problems(texts: list[str], game: dict, sheet: dict, final: bool = True) -> list[tuple[str, str]]:
    """(the words flagged, what's wrong) for every box-score claim the code refuses, before the model checker (2026-09-30
    eval, 24 errors in 14 texts): a player's numbers come from that player's line (three recaps gave team totals
    to the leading rusher and passer); "X favored / outgained / held the ball longer" names the team that really
    had more; home and road are right; and phrases a box score can't support are refused. `final` is False for the
    live one-liner: the order, lead and tie rules (_timing_problems) are about a finished game's quarter breaks, and
    "tied 14-14 early in the third" is a true statement of a game in progress."""
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
        for sent in SENTENCE.split(text):
            probs += [(m[0], f"{m[0]!r}: a box score can't show that; leave it out") for m in UNSUPPORTED.finditer(sent)]
            for clause in CLAUSE.split(sent):
                who = [p for pt, p in ppats if pt.search(clause)]
                if who:
                    allowed = shared.union(*(NUM.findall(p["value"]) for p in who))
                    extra = [n for n in NUM.findall(clause) if n not in allowed]
                    if extra:
                        lines = "; ".join(f"{p['name']}: {p['value']}" for p in who)
                        probs.append((clause, f"{clause.strip()!r}: {', '.join(extra)} isn't on the player's line "
                                           f"({lines}). Team totals belong to the team, not a player"))
                m = FAVORED.search(clause)
                if m and _side_of(m[1], pats):
                    key, side = _stat_key(clause[:m.start()] + clause[m.end():]), _side_of(m[1], pats)
                    win = _stat_winner(key, st)
                    if win != side:
                        probs.append((clause, f"{clause.strip()!r}: FACTS show "
                                           f"{'no edge' if win is None else short[win] + ' had more'} there"))
                m = OUTGAINED.search(clause)
                if m:
                    obj = _side_of(m[1], pats) if m[1] else None
                    before = _sides_in(clause[:m.start()], pats)
                    side = ({"home": "away", "away": "home"}[obj] if obj else before[-1][1] if before else None)
                    win = _stat_winner(_stat_key(clause), st)
                    if side and win != side:
                        probs.append((clause, f"{clause.strip()!r}: FACTS show "
                                           f"{'no edge' if win is None else short[win] + ' had more yards'}"))
                m = LONGER.search(clause)
                if m:
                    before = _sides_in(clause[:m.start()], pats)
                    win = _stat_winner("possessionTime", st)
                    if before and win != before[-1][1]:
                        probs.append((clause, f"{clause.strip()!r}: FACTS show "
                                           f"{'even' if win is None else short[win] + ' had the ball longer'}"))
            if final:
                probs += [(span, f"{span.strip()!r}: {msg}")
                          for span, msg in _timing_problems(sent, game, sheet, pats, ppats, st)]
            sides = {s for _, s in _sides_in(sent, pats)}
            if len(sides) == 1 and not game.get("neutral_site"):
                side = sides.pop()
                if ROAD.search(sent) and side == "home" or AT_HOME.search(sent) and side == "away":
                    probs.append((sent, f"{sent.strip()!r}: {sheet['teams'][side]['name']} were the "
                                       f"{'home' if side == 'home' else 'visiting'} team"))
    return probs


def _run(kind: str, fn: Callable[[dict], dict]) -> dict:
    """Wrap a writer: its kind's models (client.ROUTES), timing, call count and the failed status."""
    stats = {"calls": 0}
    t = time.monotonic()
    client.begin(kind)
    try:
        out = fn(stats)
    except CheckFailed as exc:
        out = {"status": "failed", "reason": f"check failed twice: {exc}"}
    except client.RateLimited as exc:
        out = {"status": "failed", "reason": f"rate limited: {exc}", "retry_after": exc.retry_after}
        if stats.get("extract"):
            out["extract"] = stats["extract"]                          # the retry skips the extract call
    except (client.NoKey, client.NoChecker) as exc:
        # Retrying can't help, but the text isn't at fault: fix the setup (a key back, a checker). Not counted as a
        # rejection (store.counts_as_rejection), so the text isn't held off once the setup is fixed.
        out = {"status": "failed", "reason": str(exc), "unconfigured": True}
    except client.TooLarge as exc:
        out = {"status": "failed", "reason": str(exc)}                 # retrying can't help
    except client.AIError as exc:
        # A 5xx, a timeout, an unreadable check: worth another try later (the worker retries on retry_after).
        out = {"status": "failed", "reason": str(exc), "retry_after": TRANSIENT_RETRY}
        if stats.get("extract"):
            out["extract"] = stats["extract"]
    return out | {"calls": stats["calls"], "checks": stats.get("checks", 0), "rejected": stats.get("rejected", []),
                  "seconds": round(time.monotonic() - t, 1), "model": client.last_writer(),
                  "checker": client.last_checker()}


# ---------------------------------------------------------------- preview

def write_preview(game: dict, articles: list[dict], extract: dict | None = None) -> dict:
    """extract: a saved article extract (keyed by article URL) for exactly these articles: from an earlier version of
    this preview (the game-morning refresh, when only our own data changed) or from an attempt that was rate limited
    after its extract call (jobs._reusable_extract). Skips the extract call."""
    if not articles:
        return {"status": "no_sources", "calls": 0, "rejected": [], "seconds": 0.0,
                "model": client.route("preview")[0][0]}
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
                                                           away=game["away"]["name"]), check_extract, stats,
                            light=True)
        else:
            # Saved by URL: map onto today's numbering. The same articles can come back in another order, and
            # positions would then point an edge at the wrong article (audit, 2026-09-29).
            extract = _by_id(saved, {a["url"]: i for i, a in ids.items()})
        # Kept for the caller even if the write below is rate limited: the retry reuses it (jobs._reusable_extract).
        stats["extract"] = (_by_url(extract, {i: a["url"] for i, a in ids.items()})
                            | {"urls": sorted(a["url"] for a in articles)})
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
                "extract": stats["extract"]}

    return _run("preview", go)


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

# A one-minute read (8d2066a, 2026-10-01): recap paragraph and each team paragraph, in words. The bets line (~16
# words, code) comes on top. OFF until Adam confirms 200-230 words was his call (2026-10-01): the recap goes back to
# about 120 words and two or three sentences a team. Set True to turn the tiers back on (recap_length picks one).
ONE_MINUTE_READ = False
FLAT_RECAP_WORDS = 120
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


# M3 (2026-10-02), both off until T3 (python -m app.ai.outline_eval) says otherwise. RECAP_OUTLINE=1 adds code's
# outline (facts.recap_outline) to the recap prompt; RECAP_REASONING=low or medium is the recap writer's reasoning
# effort ("low" made factual slips before: client.REASONING). Unset, the prompt and the request are exactly what they
# were (the client's GROQ_REASONING, medium). A bad value stops the process at start, like AI_WRITERS.
RECAP_OUTLINE = os.environ.get("RECAP_OUTLINE", "") == "1"
RECAP_EFFORTS = ("low", "medium")


def _effort(value: str | None) -> str | None:
    v = (value or "").strip().lower()
    if v and v not in RECAP_EFFORTS:
        raise ValueError(f"RECAP_REASONING={value!r}: low or medium")
    return v or None


RECAP_REASONING = _effort(os.environ.get("RECAP_REASONING"))


def _recap_size(game: dict) -> tuple[str, str, int, int | None, int | None]:
    """(tier, why, recap words, words a team or None, words in all at most or None)."""
    if ONE_MINUTE_READ:
        tier, why = recap_length(game)
        recap_w, team_w = RECAP_WORDS[tier]
        return tier, why, recap_w, team_w, round((recap_w + 2 * team_w) * LENGTH_SLACK)
    return "standard", "about 120 words", FLAT_RECAP_WORDS, None, None


def outline_text(plan: dict | None) -> str:
    """facts.recap_outline as the prompt shows it; '' for none."""
    if not plan:
        return ""
    return prompts.OUTLINE_NOTE.format(frame=plan["frame"],
                                       lines="\n".join(f"{i}. {line}" for i, line in enumerate(plan["lines"], 1)))


def recap_prompt(game: dict, sheet: dict | None = None, outline: bool | None = None) -> str:
    """The recap writer's prompt. The style guide sits in the fixed text up top, the examples after it with the game's
    own parts (M2), and the outline with those. outline: add facts.recap_outline (None: RECAP_OUTLINE; outline_eval
    sets it per arm)."""
    sheet = sheet or facts.recap_facts(game)
    _, _, recap_w, team_w, _ = _recap_size(game)
    plan = facts.recap_outline(game, sheet) if (RECAP_OUTLINE if outline is None else outline) else None
    return prompts.WRITE_RECAP.format(facts=json.dumps(sheet["facts"], ensure_ascii=False), voice=prompts.VOICE,
                                      guardrails=prompts.GUARDRAILS,
                                      home=game["home"]["name"], away=game["away"]["name"], recap_words=recap_w,
                                      style=prompts.RECAP_STYLE + "\n\n" if RECAP_VOICE else "",
                                      examples=("Examples, from other games (their facts are not yours):\n\n"
                                                + prompts.recap_examples(game["home"]["name"],
                                                                         game["away"]["name"]) + "\n\n"
                                                if RECAP_VOICE else ""),
                                      team_len=f"2-3 sentences, about {team_w} words" if team_w else "2-3 sentences",
                                      length_note=prompts.ONE_MINUTE_NOTE if team_w else "",
                                      outline=outline_text(plan))


def recap_code_check(x: dict, game: dict, sheet: dict) -> None:
    """Every code check on a recap draft, in order, before the model's fact check: CheckFailed with what is wrong.
    outline_eval runs it on first drafts too (log only)."""
    _, _, recap_w, team_w, cap = _recap_size(game)
    texts = [x.get("recap"), x.get("home"), x.get("away")]
    _texts_ok(texts, json.dumps(sheet["facts"], ensure_ascii=False))
    claims_ok(texts, game, sheet)
    words = len((x.get("recap") or "").split())
    total = sum(len(t.split()) for t in texts)
    if cap is None:
        if not 60 <= words <= 200:
            raise CheckFailed(f"recap is {words} words")
    elif words < 60 or total > cap:
        raise CheckFailed(f"recap is {words} words and {total} in all; keep the recap at about {recap_w} "
                          f"words, each team at about {team_w}, {cap} in all at most")
    joined = " ".join(t or "" for t in texts)
    copy = RECAP_VOICE and copied(joined, [{"text": ex} for _, ex in prompts.RECAP_EXAMPLES], EXAMPLE_COPY_WORDS)
    joke = RECAP_VOICE and re.search(prompts.EXAMPLE_JOKES, joined, re.I)
    if copy or joke:
        raise CheckFailed(f"reused the examples: {(copy or joke[0])!r}; write your own lines and comparisons")
    bet = bet_talk(texts)
    if bet:
        raise CheckFailed(f"bet talk in the prose: {bet!r}")


def write_recap(game: dict) -> dict:
    """One call: the fact sheet comes from code (facts.recap_facts), so there is no extract step to misread."""
    tier, why, *_ = _recap_size(game)

    def go(stats):
        sheet = facts.recap_facts(game)
        fj = json.dumps(sheet["facts"], ensure_ascii=False)

        def check_write(x):
            recap_code_check(x, game, sheet)
            _fact_check([x.get("recap"), x.get("home"), x.get("away")], fj, stats)

        out = _step(recap_prompt(game, sheet), check_write, stats, reasoning=RECAP_REASONING)
        # Bet results are the graded text itself, added by code: the model once called a push a win (2026-09-29).
        return {"status": "ready", "body": {"recap": out["recap"], "bets": bets_line(game),
                                            "home": out["home"], "away": out["away"]},
                "length": {"tier": tier, "why": why,
                           "words": sum(len(out[k].split()) for k in ("recap", "home", "away"))}}

    return _run("recap", go)


def bets_line(game: dict) -> str:
    """The graded results in the screen's own words: 'Moneyline: CHI won by 20. Spread: CHI +3 covered by 23.'"""
    return " ".join(f"{b['label']}: {b['text']}." for b in game.get("bets") or [] if b.get("text"))


def recap_fallback(game: dict) -> str:
    """PRD stats-only template, e.g. 'Final: Bears 27, Eagles 7. Moneyline: CHI won by 20.'"""
    h, a = game["home"], game["away"]
    return " ".join(x for x in (f"Final: {h['short']} {h.get('score')}, {a['short']} {a.get('score')}.",
                                bets_line(game)) if x)


# ---------------------------------------------------------------- live one-liner

ONE_LINER_MAX_WORDS = 30       # the prompt asks for two sentences under 20


def write_one_liner(game: dict) -> dict:
    """One checked sentence on the live game (Adam, Oct 1: back after the CTO cut it). Failure: the app shows the
    box-score template (summary.one_liner), never unchecked text."""
    def go(stats):
        # One sheet, trimmed to the prompt's hooks (M4): the writer, claims_ok and the fact-checker see the same facts.
        sheet = facts.live_facts(game)
        fj = json.dumps(sheet["facts"], ensure_ascii=False)

        def check(x):
            line = x.get("line")
            _texts_ok([line], fj)
            claims_ok([line], game, sheet, final=False)
            if isinstance(line, str) and len(line.split()) > ONE_LINER_MAX_WORDS:
                raise CheckFailed(f"one-liner is {len(line.split())} words")
            copy = copied(line, [{"text": ex} for ex in prompts.ONE_LINER_EXAMPLES], EXAMPLE_COPY_WORDS)
            if copy:
                raise CheckFailed(f"reused an example: {copy!r}; write your own line")
            bet = bet_talk([line])
            if bet:
                raise CheckFailed(f"bet talk in the line: {bet!r}")
            _fact_check([line], fj, stats)

        out = _step(prompts.ONE_LINER.format(facts=fj, examples="\n".join(f"- {ex}" for ex in prompts.ONE_LINER_EXAMPLES)),
                    check, stats, light=True)
        return {"status": "ready", "body": {"line": out["line"]}}

    return _run("one_liner", go)


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

        facts = _step(prompts.EXTRACT_HEADLINES.format(news=nj, finals=fin), check_extract, stats, light=True)
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

    return _run("headlines", go)
