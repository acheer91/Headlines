"""The outside AI calls: write() for the text, check() for the fact check, groq_search() for preview articles.

Who writes and checks each kind of text is ROUTES (CTO, 2026-10-01): one named writer and at most one named backup,
every failover logged (the voice changes with the writer, so it never rotates freely); one checker, never from the
writer's family (gpt-oss-20b doesn't check gpt-oss-120b), plus an overflow pool used only while the checker is
cooling down or out of budget. With no checker outside the writer's family the text fails closed. A model's name
picks its provider: "or:<id>" is an OpenRouter free model, "gemini-*" is Gemini, anything else is Groq.

Every model also has a daily budget (Groq's rolling 24 h of tokens, OpenRouter's and Gemini's requests a day),
counted in the quota both the api and the worker share: a model that has spent it is skipped like one cooling down
after a 429, so routing stops before the provider says no. Keys come from the environment only (GROQ_API_KEY,
GEMINI_API_KEY, OPENROUTER_API_KEY) and are never logged, nor are full prompts. When every model is limited,
RateLimited reaches the caller, which shows fallback text; any other failure raises AIError. 30 s timeout per call.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import NamedTuple

import httpx

log = logging.getLogger(__name__)
TIMEOUT = 30
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_SEARCH_MODEL = "openai/gpt-oss-20b"     # the free models with browser search
# Free tier per model (headers and 429 text, 2026-09-29): 1,000 requests and 200K tokens a day (a rolling 24 h),
# 8,000 tokens a minute.
GROQ_TPM = int(os.environ.get("GROQ_TPM", "8000"))
GROQ_TPD = int(os.environ.get("GROQ_TPD", "200000"))
GEMINI_RPM = int(os.environ.get("GEMINI_RPM", "5"))    # gemini-3.6-flash free tier
GEMINI_RPD = int(os.environ.get("GEMINI_RPD", "20"))   # per model; Google resets at midnight PT, we count 24 h back
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_PREFIX = "or:"
OPENROUTER_RPM = int(os.environ.get("OPENROUTER_RPM", "20"))    # free models: 20 requests a minute, no token limit
# Free models, the whole account (more keys add nothing), under $10 of credits ever bought (OpenRouter's docs,
# 2026-10-01). CTO: overflow only, no credits.
OPENROUTER_RPD = int(os.environ.get("OPENROUTER_RPD", "50"))


class Route(NamedTuple):
    writer: str
    backup: str | None       # the one named backup writer; None = the text fails closed to its fallback
    checker: str             # never the writer's family
    overflow: str | None     # the checker's extra pool, only while the checker is cooling down or out of budget


# Overflow checker (Oct 6): OpenRouter retired the free Qwen ("unavailable for free": a 404 on every overflow call). Nemotron 3
# Super is free, from a third family, and on check_eval's 23 cases it caught 15/15 errors with 0/8 false alarms (Groq's
# Qwen: 11/15, 1/8). Free pool: 50 requests a day for the whole account, so it stays overflow, not the first checker.
_120B, _QWEN, _NEMOTRON_OR = "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "or:nvidia/nemotron-3-super-120b-a12b:free"
# CTO, 2026-10-01. No backup writers: Qwen invents claims as a writer and 20b is untested as one, so with 120b out a
# recap is the stats-only template, a preview "unavailable", and headlines keep the last set. Qwen on Groq checks
# (its own quota, another family); OpenRouter's 50 a day is overflow. check_eval (2026-09-30): Qwen caught 11 of 15
# errors and rejected 1 of 8 correct sentences: a false rejection costs a template, so watch the rejection rate.
# Headlines move to Gemini (writer) and 20b (checker) once the Gemini key is replaced and the budgets are measured.
ROUTES = {
    "recap": Route(_120B, None, _QWEN, _NEMOTRON_OR),
    "preview": Route(_120B, None, _QWEN, _NEMOTRON_OR),
    "headlines": Route(_120B, None, _QWEN, _NEMOTRON_OR),
    "weekend": Route(_120B, None, _QWEN, _NEMOTRON_OR),       # the weekend columns (Adam, Oct 6): same rules as recaps
    # Live one-liner: cut by the CTO (Oct 1), back on Adam's call the same day. Its fallback is the template.
    "one_liner": Route(_120B, None, _QWEN, _NEMOTRON_OR),
}
DEFAULT_KIND = "recap"      # for tools that call write()/check() outside a text (check_eval)


def _override(var: str, most: int | None = None) -> list[str]:
    """AI_WRITERS / AI_CHECKERS pin the models for every kind (the samples bake-off, check_eval); unset = ROUTES."""
    ms = [m.strip() for m in os.environ.get(var, "").split(",") if m.strip()]
    if most and len(ms) > most:
        raise ValueError(f"{var} names {len(ms)} models: one writer and at most one backup")
    return ms


WRITERS = _override("AI_WRITERS", 2)
CHECKERS = _override("AI_CHECKERS")


def route(kind: str | None = None) -> tuple[list[str], list[str]]:
    """(writers, checkers) for a kind of text, best first."""
    r = ROUTES[kind or DEFAULT_KIND]
    return (WRITERS or [m for m in (r.writer, r.backup) if m],
            CHECKERS or [m for m in (r.checker, r.overflow) if m])


class AIError(Exception):
    pass


class RateLimited(AIError):
    def __init__(self, msg: str, retry_after: float = 60.0):
        super().__init__(msg)
        self.retry_after = retry_after


class BadReply(AIError):
    """The model's reply broke the requested format (Groq: json_validate_failed). Worth one rewrite."""


class TooLarge(AIError):
    """The prompt can't fit in one model-minute with room for a reply: retrying won't help."""


class NoKey(AIError):
    """No API key configured (the off switch): retrying won't help."""


class NoChecker(AIError):
    """No checker outside the writer's family is configured: retrying won't help, and the text is never published
    unchecked."""


_last = threading.local()


def begin(kind: str | None) -> None:
    """Start a new text of this kind (a ROUTES key): its models, and no memory of who wrote or checked the last."""
    if kind is not None and kind not in ROUTES:
        raise ValueError(f"no route for {kind!r}")
    _last.kind = kind
    _last.writer = None
    _last.checker = None
    _last.prompt = None


def forget_last() -> None:
    begin(None)


def _kind() -> str | None:
    return getattr(_last, "kind", None)


def model() -> str:
    """This text's first-choice writer (what it is written with unless it failed over)."""
    return route(_kind())[0][0]


def last_writer() -> str:
    """The model that wrote this thread's most recent text."""
    return getattr(_last, "writer", None) or model()


def write(prompt: str, *, json_out: bool = False, light: bool = False, reasoning: str | None = None) -> str:
    """One text from the kind's writer (or its named backup). Returns the reply (a JSON string when json_out).
    light: a short structured reply, not prose (an extraction step pulling claims out of articles or news, or the
    live one-liner): low reasoning and a smaller reply allowance (LIGHT_MAX_OUT), so it holds less of the minute.
    reasoning: a gpt-oss writer's effort for this call instead of REASONING (only outline_eval's T3 arms, M3); the
    reply allowance stays MAX_OUT."""
    text, used = _failover(route(_kind())[0], prompt, json_out, _write_cap(light),
                           avoid=None, low_effort=light, kind=call_kind(_write_step(prompt, light)),
                           reasoning=reasoning)
    _last.writer = used
    return text


def check(prompt: str) -> str:
    """One fact check, by a model from another family than the one that wrote the text. Returns a JSON string."""
    text, used = _failover(route(_kind())[1], prompt, True, _check_cap(), avoid=last_writer(), checker=True,
                           kind=call_kind("check"))
    _last.checker = used
    return text


SEARCH_KIND = "preview:search"      # groq_search finds a preview's articles, before the preview's text begins


def call_kind(step: str) -> str:
    """What a model call was for, as logged on its ai_calls row (migration 010): '<text kind>:<step>' (recap:write,
    preview:extract, headlines:check, ...), or the step alone outside a text (check_eval). Log only: routing and
    budgets never read it."""
    k = _kind()
    return f"{k}:{step}" if k else step


def _write_step(prompt: str, light: bool) -> str:
    """The step a writer call is: 'extract' (a light call, except the one-liner, which is light itself) or 'write';
    're-extract' / 'rewrite' when it is this text's previous prompt again plus why the draft was rejected
    (writer._step appends that to the first prompt). A rewrite repeats a whole prompt, so it is where Groq's cache
    should hit (T1 counts cached tokens on rewrites)."""
    step = "extract" if light and _kind() != "one_liner" else "write"
    first = getattr(_last, "prompt", None)
    if first and prompt.startswith(first):
        return "re-extract" if step == "extract" else "rewrite"
    _last.prompt = prompt
    return step


def last_checker() -> str | None:
    """The model that fact-checked this thread's most recent text."""
    return getattr(_last, "checker", None)


def check_model() -> str | None:
    """The first checker allowed to check the last writer's text; None if there is none."""
    return next((m for m in route(_kind())[1] if family(m) != family(last_writer())), None)


# ---------------------------------------------------------------- failover and pacing

paced_seconds = 0.0     # total time spent waiting for room (the samples script subtracts it from timings)
DAY = 24 * 3600


class MemoryQuota:
    """Per-model minute windows, daily spend and 429 cool-downs inside this process. Fine for one process (tests,
    the samples script); the api and the worker share quota.DbQuota instead (AI_QUOTA=db), so they can't collide."""

    def __init__(self):
        self._windows: dict[str, deque] = {}
        # model -> [monotonic time, reserved, used or None, kind, cached tokens, remaining tokens, reset seconds] for
        # 24 h (the last three None until Groq reports them)
        self._day: dict[str, deque] = {}
        self._cooling: dict[str, float] = {}     # model -> monotonic time it may be tried again
        self._lock = threading.Lock()

    def _room(self, key: str, cost: int, limit: int, now: float) -> bool:
        w = self._windows.setdefault(key, deque())
        while w and now - w[0][0] >= 60:
            w.popleft()
        return sum(c for _, c in w) + cost <= limit or not w

    def reserve(self, models: list[str], cost: int, limit: int, wait: bool,
                kind: str | None = None) -> tuple[str, object] | None:
        """The first model that isn't cooling down and has room this minute, reserved. With wait, sleep until the
        first usable model has room; without, None at once. None when every model is cooling down. kind is only
        logged (call_kind)."""
        global paced_seconds
        with self._lock:
            while True:
                now = time.monotonic()
                usable = [m for m in models if self._cooling.get(m, 0) <= now]
                if not usable:
                    return None
                for m in usable:
                    if self._room(m, cost, limit, now):
                        self._windows[m].append((now, cost))
                        entry = [now, cost, None, kind, None, None, None]
                        self._day.setdefault(m, deque()).append(entry)
                        return m, entry
                if not wait:
                    return None
                pause = 60 - (now - self._windows[usable[0]][0][0]) + 0.1
                paced_seconds += pause
                time.sleep(pause)

    def cool(self, model_name: str, seconds: float) -> None:
        with self._lock:
            self._cooling[model_name] = max(self._cooling.get(model_name, 0), time.monotonic() + seconds)

    def used(self, handle: object, tokens: int, cached: int | None = None, remaining: int | None = None,
             reset: float | None = None) -> None:
        """tokens: what the call counts against the day (Groq's total_tokens). cached: the prompt tokens Groq served
        from its cache; remaining / reset: Groq's x-ratelimit-remaining-tokens and -reset-tokens after the call. All
        three logged only, never taken off `tokens` (M1 waits for the log-only week, T1)."""
        if handle is not None:
            with self._lock:
                handle[2] = tokens
                handle[4:7] = [cached, remaining, reset]

    def spent_today(self, pool: str, requests: bool) -> tuple[float, float]:
        """(spent in the last 24 h, seconds until the oldest of it leaves the window) for a model, or for every
        model whose name starts with `pool` when pool is a prefix ("or:"). Spent is requests, or tokens used
        (reserved until the call reports)."""
        now = time.monotonic()
        spent, oldest = 0, None
        with self._lock:
            for m, d in self._day.items():
                if not (m == pool or (pool.endswith(":") and m.startswith(pool))):
                    continue
                while d and now - d[0][0] >= DAY:
                    d.popleft()
                spent += len(d) if requests else sum(e[1] if e[2] is None else e[2] for e in d)
                if d and (oldest is None or d[0][0] < oldest):
                    oldest = d[0][0]
        return spent, (DAY - (now - oldest)) if oldest is not None else 0.0

    def is_cooling(self, model_name: str) -> bool:
        with self._lock:
            return self._cooling.get(model_name, 0) > time.monotonic()

    def cooling_left(self, models: list[str]) -> float | None:
        """Seconds until the first of `models` stops cooling down; None if one of them isn't cooling."""
        now = time.monotonic()
        with self._lock:
            left = [self._cooling.get(m, 0) - now for m in models]
        return None if not left or min(left) <= 0 else min(left)


quota = MemoryQuota()
_mode = threading.local()


@contextmanager
def no_wait():
    """Inside this block no call waits for a model's minute to free up: with no room anywhere it raises
    RateLimited at once. The api uses it so a page open never sits behind the free tier (spec: only the worker
    waits)."""
    before = getattr(_mode, "wait", True)
    _mode.wait = False
    try:
        yield
    finally:
        _mode.wait = before


def _waiting() -> bool:
    return getattr(_mode, "wait", True)


def _wait(cost: int, limit: int, key: str, kind: str | None = None) -> object:
    """Hold one call to a single model (search) until it has room this minute. Returns the quota handle."""
    day = _over_budget(key, cost)
    if day is not None:
        raise RateLimited(f"{key}: today's budget is spent", day)
    got = quota.reserve([key], cost, limit, _waiting(), kind=kind)
    if got is None:
        raise RateLimited(f"{key}: no room this minute")
    return got[1]


def _is_gemini(name: str) -> bool:
    return name.startswith("gemini")


def _minute(name: str, cost: int) -> tuple[int, int]:
    """(what one call costs, the minute's limit): Groq limits tokens a minute, OpenRouter and Gemini requests."""
    if _is_openrouter(name):
        return 1, OPENROUTER_RPM
    if _is_gemini(name):
        return 1, GEMINI_RPM
    return cost, GROQ_TPM


def _over_budget(name: str, cost: int) -> float | None:
    """None when `name` has room in today's budget for a call costing `cost` (tokens, or 1 request); else seconds
    until its oldest call leaves the 24 h window. OpenRouter's budget is the whole account's (pool "or:")."""
    if _is_openrouter(name):
        pool, requests, limit = OPENROUTER_PREFIX, True, OPENROUTER_RPD
    elif _is_gemini(name):
        pool, requests, limit = name, True, GEMINI_RPD
    else:
        pool, requests, limit = name, False, GROQ_TPD
    spent, frees_in = quota.spent_today(pool, requests)
    if spent + (1 if requests else cost) <= limit:
        return None
    return max(frees_in, 60.0)


PREFLIGHT_TOKENS = 2000     # a Groq call costs thousands: with less left in the day than this, a model is spent


def _blocked_for(name: str) -> float | None:
    """Seconds until `name` can be asked (cooling down after a 429, or today's budget spent); None when it can."""
    cool = quota.cooling_left([name])
    spent = _over_budget(name, 1 if _is_openrouter(name) or _is_gemini(name) else PREFLIGHT_TOKENS)
    return None if cool is None and spent is None else max(cool or 0.0, spent or 0.0)


def _keyless(name: str) -> bool:
    """A pool whose key isn't set is left out of every route (_failover), so it can't check anything."""
    if _is_openrouter(name):
        return not os.environ.get("OPENROUTER_API_KEY")
    if _is_gemini(name):
        return not os.environ.get("GEMINI_API_KEY")
    return False


def unavailable_for(kind: str) -> float | None:
    """Seconds until a text of this kind can be written at all, or None when it can be tried now. It can't when its
    writer is cooling down or out of budget, or when every checker allowed to check that writer's text is (the
    writer's tokens would go on a draft nobody can check; an overflow checker with no key doesn't count). Per-minute
    room isn't looked at: a call waits for that. The worker asks before it claims a text, so a limited model costs a
    few queries instead of a claim, a draft and a failed row, and a retry comes back when the model does."""
    writers, checkers = route(kind)
    waits = {m: _blocked_for(m) for m in writers}
    usable = next((m for m in writers if waits[m] is None), None)
    if usable is None:
        return min(waits.values(), default=None)
    allowed = [m for m in checkers if family(m) != family(usable) and not _keyless(m)]
    blocked = [_blocked_for(m) for m in allowed]
    return None if not allowed or None in blocked else min(blocked)


_RETRY = re.compile(r"try again in ((?:[\d.]+(?:ms|h|m|s))+)")
_UNIT = re.compile(r"([\d.]+)(ms|h|m|s)")
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def _retry_after(text: str) -> float:
    """Seconds from Groq's 429 text ("Please try again in 6m49.1s", "...in 340ms"); a minute when it doesn't say."""
    m = _RETRY.search(text or "")
    secs = sum(float(n) * _UNIT_SECONDS[u] for n, u in _UNIT.findall(m.group(1))) if m else 0.0
    return secs if secs > 0 else 60.0


# ---------------------------------------------------------------- counting prompt tokens

# Tokens Groq wraps around a gpt-oss prompt (its chat template), measured against Groq's own prompt_tokens on
# 2026-10-01: 71 for a plain reply, 95 in JSON mode, the same at low and medium reasoning.
GPT_OSS_OVERHEAD = 100
TOKENIZER = "o200k_harmony"     # gpt-oss's tokenizer, published in tiktoken
_encoding: object = None
_encoding_lock = threading.Lock()


def _gpt_oss_encoding():
    """tiktoken's o200k_harmony, loaded once; None when it can't be (no package, or its file can't be fetched).
    The Docker image bakes the file in (TIKTOKEN_CACHE_DIR), so a server never downloads it at run time."""
    global _encoding
    with _encoding_lock:
        if _encoding is None:
            try:
                import tiktoken
                _encoding = tiktoken.get_encoding(TOKENIZER)
            except Exception as exc:  # noqa: BLE001 — counting falls back to an estimate, never fails a text
                log.warning("tokenizer %s unavailable, estimating gpt-oss prompts: %s", TOKENIZER, exc)
                _encoding = False
        return _encoding or None


def prompt_tokens(name: str, prompt: str) -> int:
    """A prompt's size in tokens as Groq will count it, for reserving the minute. gpt-oss: counted with its own
    tokenizer plus the chat template. The old ~4 characters a token ran low: an article extract reserved 7,800 and
    used 9,029 (Oct 1), and a fact-check prompt came to 3.5 characters a token. Other models: estimated at ~4
    characters a token (their prompts are short checks); gpt-oss without the tokenizer: ~3."""
    if _same_model(name).startswith("openai/gpt-oss"):
        enc = _gpt_oss_encoding()
        if enc is not None:
            return len(enc.encode(prompt, disallowed_special=())) + GPT_OSS_OVERHEAD
        return len(prompt) // 3 + GPT_OSS_OVERHEAD
    return len(prompt) // 4


def _sizing(name: str, prompt: str, max_out: int) -> tuple[int, int]:
    """(prompt tokens, reply allowance) for one call to `name`. Groq counts the prompt plus the whole reply allowance
    against the minute (429s when we counted real usage, 2026-09-29), so that is what is reserved. One request can
    never be more than the minute, so the allowance shrinks to fit; a prompt that leaves too little room is refused.
    OpenRouter and Gemini count requests, not tokens: nothing to fit."""
    pt = prompt_tokens(name, prompt)
    if _is_openrouter(name) or _is_gemini(name):
        return pt, max_out
    out = min(max_out, GROQ_TPM - pt - MARGIN)
    if out < min(MIN_OUT, max_out):         # a caller that asked for less than MIN_OUT (the one-liner) is owed what it asked
        raise TooLarge(f"prompt of ~{pt} tokens leaves no room for a reply in {GROQ_TPM} a minute")
    return pt, out


def _failover(models: list[str], prompt: str, json_out: bool, max_out: int, avoid: str | None,
              checker: bool = False, low_effort: bool = False, kind: str | None = None,
              reasoning: str | None = None) -> tuple[str, str]:
    order = [m for m in models if avoid is None or family(m) != family(avoid)]
    if not order:
        # Never fall back to the writer's own family (it once fell back to the whole list here).
        raise NoChecker(f"no checker outside the {family(avoid)} family in {models}")
    asked = max_out
    last: AIError = RateLimited("every model is rate-limited or has no room this minute")
    tried: set[str] = set()
    while True:
        # One queue per model, best first (Adam, 2026-09-29): a model whose minute is full is waited for (worker)
        # or given up on (a page open, no_wait); the next model is used only when this one is cooling down after
        # a 429 (daily limit) or erroring. Handing work to the backups whenever the writer's minute was full put
        # most batch writing on Qwen, which invented claims in 8 of 9 bake-off drafts (audit).
        name = next((m for m in order if m not in tried and not quota.is_cooling(m)), None)
        if name is None:
            # Every model cooling down: come back when the first one is free (Groq's "try again in 3h"), not in
            # a minute. A retry at 60 s burned all 8 tries in minutes (audit, 2026-09-29).
            wait = quota.cooling_left(order)
            if wait is not None:
                raise RateLimited(f"every model cooling down for {wait:.0f}s", max(wait, 1.0))
            raise last
        pt, max_out = _sizing(name, prompt, asked)       # per model: gpt-oss prompts are counted exactly
        each, limit = _minute(name, pt + max_out)
        day = _over_budget(name, each)
        if day is not None:
            # Today's budget is spent: skip it like a 429 until its oldest call leaves the 24 h window (both
            # processes see the cool-down), instead of asking and being refused.
            log.warning("ai budget: %s has spent today's budget; skipped for %.0fs", name, day)
            quota.cool(name, day)
            tried.add(name)
            last = RateLimited(f"{name}: today's budget is spent", day)
            continue
        got = quota.reserve([name], each, limit, _waiting(), kind=kind)
        if got is None:
            if quota.is_cooling(name):         # started cooling meanwhile (another process got a 429)
                tried.add(name)
                continue
            raise RateLimited(f"{name}: no room this minute", 60.0)      # a page open: fallback text now
        name, handle = got
        tried.add(name)
        try:
            logged = {}           # what only a Groq reply adds to its ai_calls row (_groq_log)
            if _is_openrouter(name):
                text, used = _openrouter_write(prompt, json_out, name, max_out, checker)
            elif _is_gemini(name):
                text, used = _gemini_call(prompt, json_out, name), 1
            else:
                text, used, logged = _groq_write(prompt, json_out, name, max_out, checker, low_effort=low_effort,
                                                 reasoning=reasoning)
            quota.used(handle, used, **logged)
            if name != order[0]:
                log.warning("ai failover: %s %s -> %s (%s)", "checker" if checker else "writer", order[0], name,
                            "cooling down" if order[0] not in tried else last)
            return text, name
        except RateLimited as exc:
            quota.cool(name, exc.retry_after)
            last = exc
        except BadReply as exc:
            if not checker:
                raise                 # a writer's bad JSON is the text's fault: the writer rewrites it
            last = exc                # a checker's bad JSON: ask the next checker (gpt-oss-20b ran out of room, Sep 29)
        except NoKey as exc:
            quota.used(handle, 0)         # nothing was asked, so nothing is spent against the day
            if not (_is_openrouter(name) or _is_gemini(name)):
                raise                 # every Groq model uses the same key: no point trying the others
            last = exc                # no OpenRouter or Gemini key: that pool is just left out
        except AIError as exc:        # a 5xx, a timeout, a retired model: try the next one
            last = exc


# ---------------------------------------------------------------- OpenRouter (free models, "or:<id>")

def _is_openrouter(name: str) -> bool:
    return name.startswith(OPENROUTER_PREFIX)


def _same_model(name: str) -> str:
    """The model behind a pool's name: Qwen on Groq and Qwen on OpenRouter are one model."""
    return name.removeprefix(OPENROUTER_PREFIX).removesuffix(":free")


def family(name: str) -> str:
    """The model's family: its vendor before the '/' (openai/gpt-oss-120b and -20b are both openai), else the name's
    first word (gemini-3.6-flash -> gemini). A checker never shares the writer's family (CTO, 2026-10-01)."""
    m = _same_model(name)
    return (m.split("/", 1)[0] if "/" in m else m.split("-", 1)[0]).lower()


def _openrouter_post(body: dict) -> dict:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise NoKey("OPENROUTER_API_KEY not set")
    try:
        r = httpx.post(OPENROUTER_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise AIError(f"openrouter: {type(exc).__name__}") from None
    if r.status_code == 429:
        # Usually the shared upstream pool ("temporarily rate-limited"): a minute is enough. OpenRouter's own
        # account limit says when it resets, in epoch milliseconds.
        wait = 60.0
        try:
            reset = (r.json()["error"]["metadata"]["headers"] or {}).get("X-RateLimit-Reset")
            wait = max(wait, float(reset) / 1000 - time.time())
        except (ValueError, KeyError, TypeError):
            pass
        raise RateLimited(f"openrouter 429 ({body.get('model')})", wait)
    if r.status_code != 200:
        raise AIError(f"openrouter {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        raise AIError("openrouter: bad reply") from None
    if d.get("error"):      # an upstream failure can arrive inside a 200
        if (d["error"].get("code") if isinstance(d["error"], dict) else None) == 429:
            raise RateLimited(f"openrouter 429 ({body.get('model')})", 60.0)
        raise AIError("openrouter: upstream error")
    return d


def _openrouter_write(prompt: str, json_out: bool, name: str, max_out: int, checker: bool) -> tuple[str, int]:
    """One reply from an OpenRouter free model. The thinking comes back in its own field, not in the text."""
    body = {"model": name[len(OPENROUTER_PREFIX):], "max_tokens": max_out,
            "messages": [{"role": "user", "content": prompt}]}
    if checker:
        body["temperature"] = 0
    if json_out:
        body["response_format"] = {"type": "json_object"}
    d = _openrouter_post(body)
    try:
        text = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError("openrouter: bad reply") from None
    if not text:
        raise AIError("openrouter: empty reply")
    if json_out and not _is_json(text):
        raise BadReply("openrouter: reply was not valid JSON")
    return text, 1


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- Groq

# The reply's rate-limit headers, kept with its JSON under this key (T1 reads them from ai_calls; M1, log only).
RATE_HEADERS = "_x_ratelimit"


def _seconds(text: str | None) -> float | None:
    """Groq's durations ("7.66s", "2m59.56s", "340ms") in seconds; None when there is none."""
    parts = _UNIT.findall(text or "")
    return round(sum(float(n) * _UNIT_SECONDS[u] for n, u in parts), 3) if parts else None


def _rate_headers(headers) -> tuple[int | None, float | None]:
    """(x-ratelimit-remaining-tokens, x-ratelimit-reset-tokens in seconds): the model's tokens-a-minute room after
    this call and when it is whole again (Groq's rate-limit docs: both per minute only)."""
    left = headers.get("x-ratelimit-remaining-tokens")
    try:
        left = int(left) if left is not None else None
    except ValueError:
        left = None
    return left, _seconds(headers.get("x-ratelimit-reset-tokens"))


def _groq_post(body: dict) -> dict:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise NoKey("GROQ_API_KEY not set")
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
        d = r.json()
    except ValueError:
        raise AIError("groq: bad reply") from None
    if isinstance(d, dict):
        d[RATE_HEADERS] = _rate_headers(r.headers)
    return d


MAX_OUT = 3000           # includes the model's reasoning tokens
CHECK_MAX_OUT = 2000     # the fact-check reply is a short JSON list (plus the model's reasoning)
# A light reply (an extract's JSON list of claims, a one-liner) at low reasoning. The one measured extract (medium
# reasoning, PIT @ CLE, Oct 1) spent its whole 3,000 and a medium one-liner 4,391 in all; 1,500 is a first setting,
# to tighten once low-reasoning replies are measured.
LIGHT_MAX_OUT = int(os.environ.get("AI_LIGHT_MAX_OUT", "1500"))
# The live one-liner's replies are measured (server ai_calls, Oct 3-4): a write used 864-977 tokens in all with a
# ~740-token prompt, so ~150-240 of reply plus reasoning, and its fact check ~90 over a ~580-token prompt. Groq counts
# the whole allowance against the minute, so 1,500 and 2,000 held two thirds of it for nothing; these keep 2.5 times
# the largest reply seen (the checker's, Qwen with hidden reasoning, more: a reply cut off by the cap comes back as an
# empty reply, an AIError the worker retries with a whole prompt). Those figures are from the old prompts: after the
# first live game read ai_calls' used tokens for one_liner:write / one_liner:check, and AI_ONE_LINER_MAX_OUT /
# AI_ONE_LINER_CHECK_MAX_OUT move them without a deploy.
ONE_LINER_MAX_OUT = int(os.environ.get("AI_ONE_LINER_MAX_OUT", "600"))
ONE_LINER_CHECK_MAX_OUT = int(os.environ.get("AI_ONE_LINER_CHECK_MAX_OUT", "800"))


def _check_cap() -> int:
    """The reply allowance for a fact check: the one-liner's own, else CHECK_MAX_OUT."""
    return ONE_LINER_CHECK_MAX_OUT if _kind() == "one_liner" else CHECK_MAX_OUT


def _write_cap(light: bool) -> int:
    """The reply allowance for a writer call: the one-liner's own, else LIGHT_MAX_OUT (a light step) or MAX_OUT."""
    if light and _kind() == "one_liner":
        return ONE_LINER_MAX_OUT
    return LIGHT_MAX_OUT if light else MAX_OUT
MARGIN = 200             # slack on top of the prompt count (an estimate, for models we can't count exactly)
MIN_OUT = 600            # less room than this for a reply isn't worth a call
REASONING = os.environ.get("GROQ_REASONING", "medium")   # "low" made factual slips (wrong team, wrong bet result)
tokens_used = 0         # Groq writing tokens this process (the samples script reports it per text)


def _groq_usage(d: dict) -> tuple[int, int | None]:
    """(total tokens, cached prompt tokens) from a Groq reply. total_tokens includes the cached ones and is what the
    budget counts. Cached is usage.prompt_tokens_details.cached_tokens (Groq's prompt-caching docs; their example:
    6,458 total = 4,641 prompt, 4,608 of them cached, + 1,817 reply), None when Groq doesn't send it."""
    u = d.get("usage") or {}
    details = u.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return u.get("total_tokens") or 0, cached if type(cached) is int and cached >= 0 else None


def _groq_log(d: dict) -> dict:
    """What a Groq reply adds to its ai_calls row beyond the tokens it counts, as quota.used's keywords: the cached
    prompt tokens and the rate-limit headers (T1's two conditions: cached tokens on the rewrites, and whether
    x-ratelimit-remaining-tokens falls by the uncached tokens only). Logged only."""
    remaining, reset = d.get(RATE_HEADERS) or (None, None)
    return {"cached": _groq_usage(d)[1], "remaining": remaining, "reset": reset}


def _groq_write(prompt: str, json_out: bool, name: str, max_out: int, checker: bool,
                low_effort: bool = False, reasoning: str | None = None) -> tuple[str, int, dict]:
    """(reply, total tokens, _groq_log's fields). reasoning: this write's effort instead of REASONING."""
    body = {"model": name, "max_completion_tokens": max_out, "messages": [{"role": "user", "content": prompt}]}
    if name.startswith("openai/gpt-oss"):
        # Checking is comparison and extraction is copying claims out, not writing: low reasoning leaves the reply
        # room for the JSON and holds less of the minute (Adam, Sep 29: low for extract, medium for write). The
        # live one-liner is light too: two short sentences, each written again in 15 minutes.
        body["reasoning_effort"] = "low" if checker or low_effort else (reasoning or REASONING)
    else:
        body["reasoning_format"] = "hidden"     # Qwen: keep its thinking out of the JSON reply
    if checker:
        body["temperature"] = 0      # the checker should give the same verdict every time (it missed 1 of 8 once)
    if json_out:
        body["response_format"] = {"type": "json_object"}
    d = _groq_post(body)
    global tokens_used
    used = _groq_usage(d)[0]
    tokens_used += used
    try:
        text = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise AIError("groq: bad reply") from None
    if not text:
        raise AIError("groq: empty reply")
    return text, used, _groq_log(d)


SEARCH_SYSTEM = "Run exactly one web search with the user's query, unchanged. Do not open any page. Reply DONE."


def groq_search(query: str) -> list[dict]:
    """One Groq browser search. Returns [{title, url}] from the tool's own results, never the model's reply:
    the reply invents plausible links (seen 2026-09-29). Not opening pages keeps a search near 1,600 tokens
    against the free tier's 200K a day."""
    body = {"model": GROQ_SEARCH_MODEL, "reasoning_effort": "low", "max_completion_tokens": 300,
            "tools": [{"type": "browser_search"}],
            "messages": [{"role": "system", "content": SEARCH_SYSTEM}, {"role": "user", "content": query}]}
    handle = _wait(2000, GROQ_TPM, GROQ_SEARCH_MODEL, SEARCH_KIND)    # search shares gpt-oss-20b's minute and day
    try:
        d = _groq_post(body)
        quota.used(handle, _groq_usage(d)[0] or 2000, **_groq_log(d))
        tools = d["choices"][0]["message"].get("executed_tools") or []
    except RateLimited as exc:
        quota.cool(GROQ_SEARCH_MODEL, exc.retry_after)   # writing and checking skip it too until then
        raise
    except NoKey:
        quota.used(handle, 0)                            # nothing was asked, so nothing is spent against the day
        raise
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


# ---------------------------------------------------------------- Gemini ("gemini-*" in a route)

_gemini_client = None


def _gemini_call(prompt: str, json_out: bool, name: str) -> str:
    global _gemini_client
    from google.genai import errors, types
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise NoKey("GEMINI_API_KEY not set")
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=TIMEOUT * 1000))
    # Low thinking: the default level took 20-30 s and hit the timeout on a 3 KB prompt; low took ~3 s (2026-09-29).
    cfg = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="low"),
                                      response_mime_type="application/json" if json_out else None)
    try:
        resp = _gemini_client.models.generate_content(model=name, contents=prompt, config=cfg)
    except errors.APIError as exc:
        if exc.code == 429:
            raise RateLimited(f"gemini 429: {exc.status}") from None
        raise AIError(f"gemini {exc.code}: {exc.status}") from None
    except Exception as exc:   # timeouts and network errors
        raise AIError(f"gemini: {type(exc).__name__}") from None
    if not resp.text:
        raise AIError("gemini: empty reply")
    return resp.text
