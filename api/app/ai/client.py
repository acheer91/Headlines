"""The outside AI calls: write() for the text, check() for the fact check, groq_search() for preview articles.

Writing and checking fail over across free Groq models (Adam, 2026-09-29, option C): each model has its own quota,
so when one is rate-limited the call moves to the next instead of failing. A model never fact-checks its own text.
Gemini stays selectable with AI_WRITER=gemini (its free tier is 20 requests a day). Keys come from the environment
only (GROQ_API_KEY, GEMINI_API_KEY) and are never logged, nor are full prompts. When every model is limited,
RateLimited reaches the caller, which shows fallback text; any other failure raises AIError. 30 s timeout per call.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque

import httpx

TIMEOUT = 30
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_SEARCH_MODEL = "openai/gpt-oss-20b"     # the free models with browser search
# Free tier per model (headers and 429 text, 2026-09-29): 1,000 requests and 200K tokens a day (a rolling 24 h),
# 8,000 tokens a minute.
GROQ_TPM = int(os.environ.get("GROQ_TPM", "8000"))
GEMINI_RPM = int(os.environ.get("GEMINI_RPM", "5"))    # gemini-3.6-flash free tier; 20 requests a day

# Best first. The writer leads with gpt-oss-120b; Qwen checks. Both lists fall back to the other models.
WRITERS = [m.strip() for m in os.environ.get(
    "AI_WRITERS", "openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b").split(",") if m.strip()]
CHECKERS = [m.strip() for m in os.environ.get(
    "AI_CHECKERS", "qwen/qwen3.8-27b,openai/gpt-oss-20b,openai/gpt-oss-120b").split(",") if m.strip()]


class AIError(Exception):
    pass


class RateLimited(AIError):
    def __init__(self, msg: str, retry_after: float = 60.0):
        super().__init__(msg)
        self.retry_after = retry_after


class BadReply(AIError):
    """The model's reply broke the requested format (Groq: json_validate_failed). Worth one rewrite."""


def writer() -> str:
    return os.environ.get("AI_WRITER", "groq")


def model() -> str:
    """The first-choice writing model (what a text is written with unless it failed over)."""
    if writer() == "gemini":
        return os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    return WRITERS[0]


_last = threading.local()


def last_writer() -> str:
    """The model that wrote this thread's most recent text."""
    return getattr(_last, "writer", None) or model()


def write(prompt: str, *, json_out: bool = False) -> str:
    """One text from the first writing model that has room. Returns the reply (a JSON string when json_out)."""
    if writer() == "gemini":
        _last.writer = model()
        return _gemini_call(prompt, json_out)
    text, used = _failover(WRITERS, prompt, json_out, MAX_OUT, avoid=None)
    _last.writer = used
    return text


def check(prompt: str) -> str:
    """One fact check, by a model other than the one that wrote the text. Returns a JSON string."""
    text, _ = _failover(CHECKERS, prompt, True, CHECK_MAX_OUT, avoid=last_writer(), checker=True)
    return text


def check_model() -> str:
    return next((m for m in CHECKERS if m != last_writer()), CHECKERS[0])


# ---------------------------------------------------------------- failover and pacing

# Per model: each Groq model has its own per-minute limit. (time, reserved tokens) of recent calls.
_windows: dict[str, deque] = {}
_cooling: dict[str, float] = {}     # model -> monotonic time it may be tried again after a 429
_pace = threading.Lock()
paced_seconds = 0.0     # total time spent waiting (the samples script subtracts it from timings)


def _room(key: str, cost: int, limit: int, now: float) -> bool:
    w = _windows.setdefault(key, deque())
    while w and now - w[0][0] >= 60:
        w.popleft()
    return sum(c for _, c in w) + cost <= limit or not w


def _reserve(models: list[str], cost: int, limit: int) -> str | None:
    """The first model that isn't cooling down and has room this minute, reserved; else wait for the first one
    that isn't cooling down. None when every model is cooling down."""
    global paced_seconds
    with _pace:
        while True:
            now = time.monotonic()
            usable = [m for m in models if _cooling.get(m, 0) <= now]
            if not usable:
                return None
            for m in usable:
                if _room(m, cost, limit, now):
                    _windows[m].append((now, cost))
                    return m
            first = _windows[usable[0]]
            wait = 60 - (now - first[0][0]) + 0.1
            paced_seconds += wait
            time.sleep(wait)


def _wait(cost: int, limit: int, key: str) -> None:
    """Block until one model (Gemini, search) has room this minute."""
    _reserve([key], cost, limit)


_RETRY = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?")


def _retry_after(text: str) -> float:
    """Seconds from Groq's 429 text ("Please try again in 6m49.1s"); a minute when it doesn't say."""
    m = _RETRY.search(text or "")
    if not m or not any(m.groups()):
        return 60.0
    h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _failover(models: list[str], prompt: str, json_out: bool, max_out: int, avoid: str | None,
              checker: bool = False) -> tuple[str, str]:
    order = [m for m in models if m != avoid] or list(models)
    # Groq counts the prompt plus the whole reply allowance against the minute (429s when we counted real usage,
    # 2026-09-29), so that is what we reserve: ~4 characters a token plus max_out.
    cost = len(prompt) // 4 + max_out
    last: AIError = RateLimited("every model is rate-limited")
    tried: set[str] = set()
    while True:
        name = _reserve([m for m in order if m not in tried], cost, GROQ_TPM)
        if name is None:
            raise last
        tried.add(name)
        try:
            return _groq_write(prompt, json_out, name, max_out, checker), name
        except RateLimited as exc:
            _cooling[name] = time.monotonic() + exc.retry_after
            last = exc
        except BadReply:
            raise                     # the text's fault, not the model's: the writer rewrites it
        except AIError as exc:        # a 5xx, a timeout: try the next model
            last = exc


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
        raise RateLimited(f"groq 429 ({body.get('model')})", _retry_after(r.text))
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


def _groq_write(prompt: str, json_out: bool, name: str, max_out: int, checker: bool) -> str:
    body = {"model": name, "max_completion_tokens": max_out, "messages": [{"role": "user", "content": prompt}]}
    if name.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = REASONING
    else:
        body["reasoning_format"] = "hidden"     # Qwen: keep its thinking out of the JSON reply
    if checker:
        body["temperature"] = 0      # the checker should give the same verdict every time (it missed 1 of 8 once)
    if json_out:
        body["response_format"] = {"type": "json_object"}
    d = _groq_post(body)
    global tokens_used
    tokens_used += (d.get("usage") or {}).get("total_tokens") or 0
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
    _wait(2000, GROQ_TPM, GROQ_SEARCH_MODEL)    # search shares gpt-oss-20b's minute with its writing fallback
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
