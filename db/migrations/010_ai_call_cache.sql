-- Phase 4 prep (M1, 2026-10-02): what each model call was for, the prompt tokens Groq served from its cache, and
-- Groq's per-minute rate-limit headers after the call.
-- LOG ONLY: the daily budget and the minute still count `reserved` / `used` (Groq's total_tokens, cached included)
-- until a log-only week (T1) shows Groq doesn't count cached tokens against its limits.
-- kind: '<text kind>:<step>' (client.call_kind), e.g. 'recap:write', 'recap:rewrite', 'recap:check',
-- 'preview:extract', 'preview:search'; NULL for calls made before this migration.
-- cached_tokens: Groq's usage.prompt_tokens_details.cached_tokens; NULL when the reply had none (OpenRouter, Gemini,
-- a call that failed, or Groq didn't say), so "not reported" and "nothing cached" (0) stay apart.
-- remaining_tokens / reset_tokens_secs: x-ratelimit-remaining-tokens and x-ratelimit-reset-tokens (in seconds) from
-- the reply, both per minute; NULL when there were none. T1 reads how far remaining falls from one call to the next.
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS cached_tokens INT;
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS remaining_tokens INT;
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS reset_tokens_secs REAL;
