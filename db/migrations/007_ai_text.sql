-- Phase 4: AI text. Idempotent (see app/migrate.py).

-- One row per game and kind (headlines have no game: one row per run). Nothing here is ever sent back to a model
-- except a preview's own `extract`, reused by the game-morning refresh.
CREATE TABLE IF NOT EXISTS ai_texts (
    id           SERIAL PRIMARY KEY,
    game_id      INT REFERENCES games(id) ON DELETE CASCADE,   -- NULL for headlines
    kind         TEXT NOT NULL CHECK (kind IN ('preview', 'recap', 'one_liner', 'headlines')),
    status       TEXT NOT NULL CHECK (status IN ('writing', 'ready', 'failed', 'no_sources')),
    body         JSONB,      -- preview: {preview, edges: {home, away}, picks}; recap: {recap, bets, home, away};
                             -- one_liner: {line}; headlines: {items: [{text, url}]}
    sources      JSONB,      -- previews: [{title, url, outlet, published}]
    basis        TEXT,       -- what `body` was written from; a different basis means stale (handoff 2.2):
                             -- preview: game date, recap: final score, one_liner: live score
    fingerprint  TEXT,       -- preview: hash of the game facts and kept article URLs (game-morning refresh)
    claim_basis  TEXT,       -- what the writer holding the claim is writing from; becomes `basis` on save.
    claim_fingerprint TEXT,  -- A claim never touches body/basis, so the last good text stays until a new one lands.
    extract      JSONB,      -- preview: the article extract, so a refresh can rewrite without re-reading articles
    writer       TEXT,       -- the model that wrote it, and the one that fact-checked it
    checker      TEXT,
    reason       TEXT,       -- midweek | refresh | final | nightly | open | manual
    attempts     INT NOT NULL DEFAULT 0,  -- also the claim token: only the latest claim's writer may save
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ai_texts_game_kind ON ai_texts (game_id, kind) WHERE game_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ai_texts_headlines ON ai_texts (created_at DESC) WHERE kind = 'headlines';

-- Free-tier quota shared by the api and the worker: every model call reserves its tokens here first, so the two
-- processes can't both take a model's last slot in a minute. Also the record of tokens used per model per day.
CREATE TABLE IF NOT EXISTS ai_calls (
    id        BIGSERIAL PRIMARY KEY,
    model     TEXT NOT NULL,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    reserved  INT NOT NULL,          -- tokens held against the minute (prompt + reply allowance)
    used      INT                    -- tokens the call really used, filled in after it returns
);
CREATE INDEX IF NOT EXISTS ai_calls_model_at ON ai_calls (model, at DESC);

-- A model that returned 429 is skipped until `until` (Groq says how long).
CREATE TABLE IF NOT EXISTS ai_cooling (
    model  TEXT PRIMARY KEY,
    until  TIMESTAMPTZ NOT NULL
);
