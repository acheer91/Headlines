"""Every Gemini prompt, one constant per step (handoff 1.4). Every output is two calls: EXTRACT pulls facts from
the inputs into JSON, WRITE writes from those facts only. Filled with str.format: {game}, {articles}, {facts}.

Voice (PRD): the Ringer house style is a guide to tone, diction and rhythm; the writer has free rein within it.
An original voice, never a named writer or byline.
"""

VOICE = """Voice: a smart, conversational sportswriter with a dry sense of humor. Concrete details over clichés,
confident verbs, a mix of short and long sentences, one good turn of phrase at most. No hype words, no
exclamation marks, no emojis, no rhetorical questions to the reader, no "buckle up" or "all eyes on". Write in an
original voice: never imitate or mention a named writer."""

GUARDRAILS = """Hard rules:
- Use ONLY the facts in FACTS. Every number you write must appear in FACTS exactly as written there (write "20",
  not "twenty" or "two decades"). Do no arithmetic: if a number isn't in FACTS, leave it out.
- Report, never advise: no picks, predictions or betting advice of your own. Never say who will or should win,
  never recommend a bet, never say "lock", "take the", "hammer", "best bet".
- Never copy a sentence or phrase from an article; say it your own way.
- Every sentence must be plainly true from FACTS. Don't merge two stats into a new one, don't compare things
  FACTS doesn't compare, and when unsure, leave it out.
- Call teams only by the names in FACTS (city or nickname as written there), never a different city."""

# ---------------------------------------------------------------- preview (C1): preview, edges, writers' picks
# GAME facts are built by code (facts.preview_facts); the model extracts only from the articles.

EXTRACT_PREVIEW = """You extract facts from news articles for a pre-game preview. Return JSON only.

GAME lists our own data about the game, already prepared: use it only to tell which team is which. ARTICLES are
news articles about this game, each with an id, outlet, url and date. Extract only what the ARTICLES say.

Return this JSON:
{{
  "storylines": [{{"fact": "one thing an article reports about this game", "article": <id>}}],
  "edges": {{
    "home": [{{"fact": "a concrete advantage {home} has in this matchup, as an article states it", "article": <id>}}],
    "away": [{{"fact": "the same for {away}", "article": <id>}}]
  }},
  "picks": [{{"writer": "full name as printed", "outlet": "...", "pick": "the pick as stated (team, or team and score)",
             "article": <id>}}]
}}

Rules:
- Only what the ARTICLES actually say. Never add anything from your own knowledge.
- storylines: up to 6, the most important things the articles report (injuries, form, matchups, the gist of quotes).
- edges: up to 3 per team, each tied to the article that says it. Fewer is fine; none is fine.
- picks: only when an article names a writer or analyst who explicitly picks a winner. Otherwise [].
- Skip point spreads, odds and over/unders: GAME already has the current line, and an article's is often older.
- Keep every number exactly as written in the article.

GAME:
{game}

ARTICLES:
{articles}
"""

WRITE_PREVIEW = """You write the pre-game preview for a personal scores app. Return JSON only:
{{
  "preview": "about 110 words: what this game is about and what to watch",
  "edges": {{"home": [{{"text": "one sentence", "article": <id>}}], "away": [{{"text": "...", "article": <id>}}]}}
}}

FACTS has "game" (our own data, each line plain and exact), "storylines" and "edges" (from articles).
- preview: from FACTS only. Lead with the storylines, not the stat sheet: at most 4 numbers and no list of
  season stats (the app shows those). Don't mention writers' picks (the app shows those separately).
  Don't forecast the game or its score ("expect", "should", "likely"): say what's at stake, not what will happen.
  The point spread and total only as "game" states them.
- edges: rewrite each edge in FACTS as one sentence, same article id. Same number of edges per team as FACTS.
  Don't add edges.

{voice}

{guardrails}

FACTS:
{facts}
"""

# ---------------------------------------------------------------- recap voice: Adam's own samples (2026-10-01)
# Few-shot examples for WRITE_RECAP, the shape and tone to hit. Adam's three, trimmed only where our own checks would
# refuse the line (an invented "$79" and "three-hour", "late push"). A recap for one of these games is shown the other
# two, so the model never sees its own game's example. (teams as the writer names them, text)
RECAP_STYLE = """Voice for the recap (Adam's house style; the two team paragraphs use it too, shorter):
- Open with one line that frames the game, then a paragraph on the winner and one on the loser, and end on a short
  kicker. A recap is about 120 words.
- Find the tension in FACTS (won the yards, lost the game; three giveaways against none) and build around it.
- One joke or comparison per paragraph at most, and every one is tied to a real stat from FACTS: the joke makes the
  number memorable, it never replaces it. Never invent a figure for a joke (no made-up hours, dollars or counts).
- Aim the jokes at how a team played, never at a player's body, family or character.
- Mix short sentences with a longer one that carries the stat; a rhetorical fragment now and then ("The Eagles?").
- You may round with a word: "nearly 37 minutes" for 36:53.
- A box score shows what happened, never why or how it felt, so the code refuses these words and you must not use
  them: dominated, momentum, relinquished, erased, capitalized, controlled the game, took advantage, checked out,
  woke up, bolstered, buoyed, fueled, sparked, led to, resulted in, due to, thanks to, because, as a result, all game,
  start to finish, to the final whistle, the rest of the way, winless, streak, "two more scores", a late or early
  field goal or score, "scored first". Make the point with the stat itself and put the voice in how you frame it.
- The examples show the shape and tone only. Never reuse their lines, jokes or comparisons."""

RECAP_EXAMPLES = [
    (("eagles", "bears"), """This was a “check the score, check it again, wonder whether Philadelphia knew kickoff was today” game. Chicago jumped ahead early and never trailed, turning Soldier Field into the site of an Eagles troubleshooting session. Case Keenum delivered the football equivalent of a surprisingly competent substitute teacher: 24-of-34, 247 yards, two touchdowns. D’Andre Swift added 84 yards on 20 carries, and the Bears controlled the ball for nearly 37 minutes.

The Eagles? Three turnovers, 10 penalties, and 248 total yards. Jalen Hurts finished with 153 passing yards and an interception. Chicago won the turnover battle 3–0 and the yardage battle 375–248. You don’t need an advanced metric for this one. Philadelphia brought a shovel and spent the afternoon digging."""),
    (("patriots", "jaguars"), """New England outgained Jacksonville 317–315 and lost by 29. That’s not a silver lining. That’s the box score trying to establish an alibi.

The Jaguars scored 14 in the second quarter, 14 more in the third, and another touchdown in the fourth. The Patriots answered with two field goals—the offensive equivalent of replying “sounds good” to a breakup text. Trevor Lawrence threw for 182 yards and three touchdowns, with one interception. Drake Maye threw for 199 yards and two picks, and Jacksonville won the takeaway battle 3–1.

Some blowouts require a complicated explanation. This one doesn’t: Jacksonville turned its opportunities into touchdowns. New England turned its yardage advantage into a deeply unconvincing talking point."""),
    (("seahawks", "commanders"), """Seattle outgained Washington 437–258, scored 14 in the fourth quarter, and still lost. Somewhere, a Seahawks fan is staring at those numbers like they’re a restaurant bill with an unexplained charge.

Washington led 17–10 at halftime and 24–17 after three quarters, then scored nine in the fourth to survive Seattle’s fourth-quarter push. The difference was the turnover column: three Seattle giveaways, zero for Washington. The Commanders also held the ball for nearly three minutes longer, which helped offset an offense that produced 179 fewer yards.

Both teams contributed plenty of laundry—eight Seattle penalties, nine for Washington—but only one kept handing over the football. Seattle won the yardage argument. Washington won the game. Unfortunately for the Seahawks, the standings recognize only one of those things."""),
]
# Jokes from the examples: a recap that reuses one is rewritten (the copy check only sees 6-word runs).
EXAMPLE_JOKES = r"substitute teacher|alibi|breakup text|restaurant bill|brought a shovel|troubleshooting"


def recap_examples(home: str, away: str) -> str:
    """The examples for a game, leaving out the one about this game's own teams."""
    names = f"{home} {away}".lower()
    shown = [t for teams, t in RECAP_EXAMPLES if not any(n in names for n in teams)]
    return "\n\n".join(f"Example {i + 1}:\n{t}" for i, t in enumerate(shown))


# ---------------------------------------------------------------- recap (D): recap and team summaries
# The fact sheet is built by code (facts.recap_facts): no model extract step, so nothing can be misread.
# The text that is the same for every game comes first ({style} is RECAP_STYLE or empty), then the game's own parts:
# the examples (all three unless the game is one of theirs), the reply format with the team names, FACTS. Groq can
# then reuse the cached prefix from one recap to the next (M2, 2026-10-02; the wording is unchanged, only the order).

WRITE_RECAP = """You write the post-game recap for a personal scores app.

{style}FACTS is a list of plain, exact statements about the final. Each one is true exactly as written; restate them,
don't reinterpret them. Scoring and who led are given per quarter: say which quarter something happened in, or
who led when, only as FACTS states it. The score is known only at quarter breaks, so never say a team took or
held the lead inside a quarter, or what happened first or "before" something else within a quarter.

Also (each of these was a real error):
- A player's numbers come only from that player's own leader line. Team totals (total yards, passing, rushing)
  belong to the team: never "128 rushing yards from Swift" when 128 is the team's rushing total.
- Home team, who had the ball longer, who gained more yards, and when the lead changed are each stated in FACTS:
  repeat those lines, don't work them out.
- No causes: say what happened, never why ("kept them off balance", "limited any momentum", "buoyed by",
  "bolstered by", "capitalized on", "checked out", "woke up", "allowing X to", "to control the game", "took
  advantage", "thanks to", "due to", "led to"). No streaks or records going in ("winless", "unbeaten", "streak").
- Don't name the kind or count of scoring plays ("a late field goal", "two more scores", "the game's only
  touchdown"); a player's touchdowns as FACTS lists them are fine.
- Never "dominated" or "never relinquished". "Only", "just" and "over" are claims too: use FACTS' exact numbers.
- Never about the whole game: not "all game", "from start to finish", "through the end", "the rest of the way",
  "a lead through the third and fourth". Say who led at each quarter break, as FACTS gives it.
- Inside a quarter, say only what that quarter added. Never "before" (one score before another), "responded",
  "scored first", "late", "early", or "tied" except a tie FACTS shows at a quarter break ("tied 7-7 after the first").
- Comparisons point the way FACTS says: the team with more is the one FACTS' edge line names. Write "N to M" with
  the first team's number first. "Each side", "split" or "even" only when FACTS shows the two numbers equal.
  "Kept" or "extended" a lead only for a team that led at the break before; "close the gap" only for a team that
  trailed then.

{voice}

{guardrails}

{examples}Return JSON only:
{{
  "recap": "about {recap_words} words on how the game went. Don't mention bets, lines or the spread: the app adds them",
  "home": "{team_len}, on {home}'s day",
  "away": "{team_len}, on {away}'s day"
}}
{length_note}
{outline}FACTS:
{facts}
"""

# Added to WRITE_RECAP only while writer.RECAP_OUTLINE is on (M3, the T3 eval): code's pick of the lead and the FACTS
# lines that tell it (facts.recap_outline). It sits with the game's own parts, after the text every game shares (M2).
OUTLINE_NOTE = """OUTLINE (picked by code from FACTS; every key moment is a FACTS line, copied exactly):
Angle: {frame}
Key moments, in order:
{lines}
Open with the angle and take the key moments in this order; other FACTS lines can add detail. Every rule above
still applies.

"""

# Added to WRITE_RECAP only while writer.ONE_MINUTE_READ is on.
ONE_MINUTE_NOTE = """
The whole recap is a one-minute read. Those lengths are the most this game deserves: when FACTS has less worth
saying, write less. Never pad, and don't repeat in a team's paragraph a stat the recap already gave.
"""

# ---------------------------------------------------------------- fact check (every text; a different model)

FACT_CHECK = """You are a strict fact-checker. Compare TEXT with FACTS. Return JSON only:
{{"problems": [{{"quote": "the exact words from TEXT", "verdict": "wrong" or "unsupported",
               "why": "under 25 words: the FACTS line it contradicts, or that FACTS doesn't say it"}}]}}

Decide each claim before you write anything. List only claims that are wrong or unsupported; never list a claim
and then explain that it is fine. No reasoning in the reply.

A problem is any factual claim in TEXT that FACTS does not directly support:
- a wrong number, team, player, quarter, or who led / who won / who scored when;
- a claim FACTS never makes (a streak, a comparison, a cause, a record, "never trailed", "dominated");
- a mix-up of sides (turnovers committed vs forced, home vs away, offense vs defense);
- a team total given to a player (FACTS lists each player's own line), or the kind or count of scoring plays
  ("a late field goal", "two more scores") when FACTS doesn't show it;
- any claim about who led, took the lead or pulled ahead DURING a quarter: FACTS only knows the score at quarter
  breaks, so "surged ahead in the fourth" is unsupported unless a quarter-break score shows that team ahead.
Not a problem: wording, tone, or color that makes no factual claim ("a long afternoon", "never in doubt"
when FACTS shows a big lead throughout). Return {{"problems": []}} when every claim is supported.

FACTS:
{facts}

TEXT:
{text}
"""

# ---------------------------------------------------------------- live one-liner (C2)

# Adam's examples (Oct 1), the ones our live box score can back up. Others he gave need drive or play-by-play data
# we don't have ("three straight punts", "abandoning the run", "backup quarterback", "two explosive plays").
# facts.live_facts keeps only the lines these examples and ONE_LINER's "Look first for" list use (M4, 2026-10-02):
# a new hook here needs its stat added to facts.LIVE_STATS or LIVE_LEADERS.
ONE_LINER_EXAMPLES = [
    "One-score game. Somehow, only one team feels like it's in trouble.",
    "Close on the scoreboard. Not particularly close at the line of scrimmage.",
    "Two picks already. We're approaching 'just don't lose us the game' territory.",
    "The favorite is still trailing. This has graduated from cute to concerning.",
    "Ranked team, road game, down at halftime. Upset-watch conditions are excellent.",
]

ONE_LINER = """Write the line that sits under a live score in a scores app: two short sentences, under 20 words in
all, that tell a fan at a glance what is happening and what it feels like. Return JSON only: {{"line": "..."}}

The score and the margin are shown right above your line: never restate them, and don't copy a FACTS line.
First sentence: the one fact that tells this game's story right now, in a few words. Look first for: the team
favored before kickoff now trailing; a big gap in total, passing or rushing yards; turnovers; third downs; one
team's points in a quarter; a road team ahead. Every number must appear in FACTS exactly as written; words for
small counts are fine ("two picks", "one turnover").
Second sentence: a dry, knowing read of that same fact. Wry, never mean, never hype. It adds no new fact: no
number, name, play, streak or cause that FACTS doesn't give, nothing about the crowd or anyone's feelings, and no
prediction of how the game ends. No betting words (spread, cover, over/under, bet).

FACTS knows the score only at quarter breaks: say nothing about who scored first, last or when inside a quarter.
The quarter marked "(in progress)" isn't over.

The voice, from other games (their facts are not yours; write your own words):
{examples}

FACTS:
{facts}
"""

# ---------------------------------------------------------------- headlines (Screen A)

EXTRACT_HEADLINES = """You pick the news for a sports app's home screen. Return JSON only.

NEWS is recent ESPN news (id, headline, description, date). FINALS are recent final scores.

Return {{"items": [{{"fact": "what happened, in plain words, numbers exactly as in the source", "news": <id or null>}}]}}
with the 8 most important items across NEWS and FINALS: big results, injuries, trades, firings, records.
Skip fantasy advice, betting odds and listicles. Only what NEWS and FINALS say.

NEWS:
{news}

FINALS:
{finals}
"""

WRITE_HEADLINES = """You write the headline list for a sports app's home screen. Return JSON only:
{{"items": [{{"text": "one line, under 15 words", "news": <same id or null>}}]}}

Write 5 to 8 items from FACTS, most important first, one line each, same ids. Each line tells exactly one
item's story; never join two items into one line.

{voice}

{guardrails}

FACTS:
{facts}
"""
