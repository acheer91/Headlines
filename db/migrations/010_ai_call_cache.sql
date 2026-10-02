-- Phase 4 prep (M1, 2026-10-02): what each model call was for, and the prompt tokens Groq served from its cache.
-- LOG ONLY: the daily budget and the minute still count `reserved` / `used` (Groq's total_tokens, cached included)
-- until a log-only week (T1) shows Groq doesn't count cached tokens against its limits.
-- kind: '<text kind>:<step>' (client.call_kind), e.g. 'recap:write', 'recap:rewrite', 'recap:check',
-- 'preview:extract', 'preview:search'; NULL for calls made before this migration.
-- cached_tokens: Groq's usage.prompt_tokens_details.cached_tokens; NULL when the reply had none (OpenRouter, Gemini,
-- a call that failed, or Groq didn't say), so "not reported" and "nothing cached" (0) stay apart.
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS cached_tokens INT;
