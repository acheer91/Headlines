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
- Keep every number exactly as written in the article.

GAME:
{game}

ARTICLES:
{articles}
"""

WRITE_PREVIEW = """You write the pre-game preview for a personal scores app. Return JSON only:
{{
  "preview": "about 120 words: what this game is about and what to watch",
  "edges": {{"home": [{{"text": "one sentence", "article": <id>}}], "away": [{{"text": "...", "article": <id>}}]}}
}}

FACTS has "game" (our own data, each line plain and exact), "storylines" and "edges" (from articles).
- preview: from FACTS only. Lead with the storylines, not the stat sheet: at most 4 numbers and no list of
  season stats (the app shows those). Don't mention writers' picks (the app shows those separately).
  Don't forecast the game or its score ("expect", "should", "likely"): say what's at stake, not what will happen.
- edges: rewrite each edge in FACTS as one sentence, same article id. Same number of edges per team as FACTS.
  Don't add edges.

{voice}

{guardrails}

FACTS:
{facts}
"""

# ---------------------------------------------------------------- recap (D): recap and team summaries
# The fact sheet is built by code (facts.recap_facts): no model extract step, so nothing can be misread.

WRITE_RECAP = """You write the post-game recap for a personal scores app. Return JSON only:
{{
  "recap": "about 120 words on how the game went. Don't mention bets, lines or the spread: the app adds them",
  "home": "2-3 sentences on {home}'s day",
  "away": "2-3 sentences on {away}'s day"
}}

FACTS is a list of plain, exact statements about the final. Each one is true exactly as written; restate them,
don't reinterpret them. Scoring and who led are given per quarter: say which quarter something happened in, or
who led when, only as FACTS states it. The score is known only at quarter breaks, so never say a team took or
held the lead inside a quarter.

{voice}

{guardrails}

FACTS:
{facts}
"""

# ---------------------------------------------------------------- fact check (every text; a different model)

FACT_CHECK = """You are a strict fact-checker. Compare TEXT with FACTS. Return JSON only:
{{"problems": [{{"quote": "the exact words from TEXT", "why": "what FACTS says instead, or that FACTS doesn't say it"}}]}}

A problem is any factual claim in TEXT that FACTS does not directly support:
- a wrong number, team, player, quarter, or who led / who won / who scored when;
- a claim FACTS never makes (a streak, a comparison, a cause, a record, "never trailed", "dominated");
- a mix-up of sides (turnovers committed vs forced, home vs away, offense vs defense).
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

ONE_LINER = """Write one sentence (under 30 words) summing up this live game right now, for a scores app.
Return JSON only: {{"line": "..."}}

FACTS is a list of plain, exact statements about the game so far (built from the live box score). Use only
FACTS. Every number must appear in FACTS exactly as written. The quarter marked "(in progress)" isn't over.
Say what has happened, never how it will end: no predictions, no advice, no hype.

{voice}

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
