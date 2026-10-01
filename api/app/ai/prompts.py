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
  "preview": "about 120 words: what this game is about and what to watch",
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

# ---------------------------------------------------------------- recap (D): recap and team summaries
# The fact sheet is built by code (facts.recap_facts): no model extract step, so nothing can be misread.

WRITE_RECAP = """You write the post-game recap for a personal scores app. Return JSON only:
{{
  "recap": "about {recap_words} words on how the game went. Don't mention bets, lines or the spread: the app adds them",
  "home": "{team_len}, on {home}'s day",
  "away": "{team_len}, on {away}'s day"
}}
{length_note}
FACTS is a list of plain, exact statements about the final. Each one is true exactly as written; restate them,
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

FACTS:
{facts}
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
