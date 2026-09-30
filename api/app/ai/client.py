"""The outside AI calls: write() for the text, groq_search() for preview articles.

Writer: Groq (Adam, 2026-09-29, after Gemini's free tier turned out to be 20 requests a day). Gemini stays
selectable with AI_WRITER=gemini. Keys come from the environment only (GROQ_API_KEY, GEMINI_API_KEY) and are
never logged, nor are full prompts. Free tiers: a 429 raises RateLimited and the caller shows fallback text;
anything else raises AIError. One attempt per call, 30 s timeout.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

import httpx

TIMEOUT = 30
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_SEARCH_MODEL = "openai/gpt-oss-20b"     # the free models with browser search
# Free tier per model (headers, 2026-09-29): 1,000 requests a day, 8,000 tokens a minute. Writing on a different
# model from search keeps their quotas apart.
GROQ_TPM = int(os.environ.get("GROQ_TPM", "8000"))
GEMINI_RPM = int(os.environ.get("GEMINI_RPM", "5"))    # gemini-3.6-flash free tier; 20 requests a day


class AIError(Exception):
    pass


class RateLimited(AIError):
    pass


class BadReply(AIError):
    """The model's reply broke the requested format (Groq: json_validate_failed). Worth one rewrite."""


def writer() -> str:
    return os.environ.get("AI_WRITER", "groq")


def model() -> str:
    if writer() == "gemini":
        return os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    return os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


def write(prompt: str, *, json_out: bool = False) -> str:
    """One call to the writing model. Returns the reply text (a JSON string when json_out)."""
    return _gemini_call(prompt, json_out) if writer() == "gemini" else _groq_write(prompt, json_out)


def check_model() -> str:
    # A different model family from the writer, so it doesn't share the writer's blind spots (Adam, 2026-09-29).
    return os.environ.get("GROQ_CHECK_MODEL", "qwen/qwen3.8-27b")


def check(prompt: str) -> str:
    """One call to the fact-checking model. Returns a JSON string."""
    return _groq_write(prompt, True, model_name=check_model())


# ---------------------------------------------------------------- pacing

# Per model: each Groq model has its own per-minute limit. [time, cost] of recent calls.
_windows: dict[str, deque] = {}
_pace = threading.Lock()
paced_seconds = 0.0     # total time spent waiting (the samples script subtracts it from timings)


def _wait(cost: int, limit: int, key: str) -> None:
    """Stay under a model's per-minute limit in this process (cost = tokens or 1 request) instead of hitting 429s."""
    global paced_seconds
    with _pace:
        _window = _windows.setdefault(key, deque())
        while True:
            now = time.monotonic()
            while _window and now - _window[0][0] >= 60:
                _window.popleft()
            if sum(c for _, c in _window) + cost <= limit or not _window:
                _window.append((now, cost))
                return
            wait = 60 - (now - _window[0][0]) + 0.1
            paced_seconds += wait
            time.sleep(wait)


# ---------------------------------------------------------------- Groq

def _groq_post(body: dict) -> dict:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise AIError("GROQ_API_KEY not set")
    try:
        r = httpx.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise AIError(f"groq: {type(exc).__name__}") from None
    if r.status_code == 429:
        raise RateLimited("groq 429")
    if r.status_code == 400 and "json_validate_failed" in r.text:
        raise BadReply("groq: reply was not valid JSON")
    if r.status_code != 200:
        raise AIError(f"groq {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise AIError("groq: bad reply") from None


MAX_OUT = 3000           # includes the model's reasoning tokens
CHECK_MAX_OUT = 1500     # the fact-check reply is a short JSON list
REASONING = os.environ.get("GROQ_REASONING", "medium")   # "low" made factual slips (wrong team, wrong bet result)
tokens_used = 0         # Groq writing tokens this process (the samples script reports it per text)


def _groq_write(prompt: str, json_out: bool, model_name: str | None = None) -> str:
    name = model_name or model()
    max_out = MAX_OUT if model_name is None else CHECK_MAX_OUT
    # Groq counts the prompt plus the whole reply allowance against the minute (429s when we counted real usage,
    # 2026-09-29), so that is what we reserve: ~4 characters a token plus max_out.
    _wait(len(prompt) // 4 + max_out, GROQ_TPM, name)
    body = {"model": name, "max_completion_tokens": max_out, "messages": [{"role": "user", "content": prompt}]}
    if name.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = REASONING
    else:
        body["reasoning_format"] = "hidden"     # Qwen: keep its thinking out of the JSON reply
    if model_name is not None:
        body["temperature"] = 0      # the checker should give the same verdict every time (it missed 1 of 8 once)
    if json_out:
        body["response_format"] = {"type": "json_object"}
    d = _groq_post(body)
    global tokens_used
    used = (d.get("usage") or {}).get("total_tokens") or 0
    tokens_used += used
    try:
        text = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise AIError("groq: bad reply") from None
    if not text:
        raise AIError("groq: empty reply")
    return text


SEARCH_SYSTEM = "Run exactly one web search with the user's query, unchanged. Do not open any page. Reply DONE."


def groq_search(query: str) -> list[dict]:
    """One Groq browser search. Returns [{title, url}] from the tool's own results, never the model's reply:
    the reply invents plausible links (seen 2026-09-29). Not opening pages keeps a search near 1,600 tokens
    against the free tier's 200K a day."""
    body = {"model": GROQ_SEARCH_MODEL, "reasoning_effort": "low", "max_completion_tokens": 300,
            "tools": [{"type": "browser_search"}],
            "messages": [{"role": "system", "content": SEARCH_SYSTEM}, {"role": "user", "content": query}]}
    try:
        tools = _groq_post(body)["choices"][0]["message"].get("executed_tools") or []
    except (KeyError, IndexError):
        raise AIError("groq: bad reply") from None
    out, seen = [], set()
    for t in tools:
        for x in ((t.get("search_results") or {}).get("results") or []):
            url = x.get("url")
            if url and url not in seen:
                seen.add(url)
                out.append({"title": x.get("title") or "", "url": url})
    return out


# ---------------------------------------------------------------- Gemini (AI_WRITER=gemini)

_gemini_client = None


def _gemini_call(prompt: str, json_out: bool) -> str:
    global _gemini_client
    from google.genai import errors, types
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise AIError("GEMINI_API_KEY not set")
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=TIMEOUT * 1000))
    _wait(1, GEMINI_RPM, model())
    # Low thinking: the default level took 20-30 s and hit the timeout on a 3 KB prompt; low took ~3 s (2026-09-29).
    cfg = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="low"),
                                      response_mime_type="application/json" if json_out else None)
    try:
        resp = _gemini_client.models.generate_content(model=model(), contents=prompt, config=cfg)
    except errors.APIError as exc:
        if exc.code == 429:
            raise RateLimited(f"gemini 429: {exc.status}") from None
        raise AIError(f"gemini {exc.code}: {exc.status}") from None
    except Exception as exc:   # timeouts and network errors
        raise AIError(f"gemini: {type(exc).__name__}") from None
    if not resp.text:
        raise AIError("gemini: empty reply")
    return resp.text
