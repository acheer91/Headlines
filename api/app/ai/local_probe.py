"""Is the local Ollama model usable for the AI text? One command, no keys, no database.

python -m app.ai.local_probe [qwen3:8b]     (laptop only; OLLAMA_URL points elsewhere, e.g. from a container)
Reports: Ollama reachable, the tag pulled, memory it holds, a plain reply and a low-reasoning JSON reply with
their speed. Then trials run with the model's `local:<tag>` name, e.g. the fact checker against the hand-found errors
(a checker may not share the writer's family, so the writer is pinned to another one):
    AI_WRITERS=openai/gpt-oss-120b AI_CHECKERS=local:qwen3:8b python -m app.ai.check_eval
"""
from __future__ import annotations

import json
import sys
import time

import httpx

from . import client


def _chat(tag: str, **body) -> tuple[dict, float]:
    t = time.monotonic()
    r = httpx.post(f"{client.OLLAMA_URL}/api/chat", timeout=client.LOCAL_TIMEOUT,
                   json={"model": tag, "stream": False, **body})
    r.raise_for_status()
    return r.json(), time.monotonic() - t


def _speed(d: dict) -> str:
    ns = d.get("eval_duration") or 0
    return f"{d.get('eval_count', 0) / (ns / 1e9):.1f} tok/s" if ns else "n/a"


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "llama3.2"
    try:
        client._local_tag(client.LOCAL_PREFIX + tag)      # a cloud model is refused before anything is sent
    except client.AIError as exc:
        print(f"FAIL  {exc}")
        return 1
    try:
        have =httpx.get(f"{client.OLLAMA_URL}/api/tags", timeout=5).json().get("models", [])
    except httpx.HTTPError as exc:
        print(f"FAIL  nothing answering at {client.OLLAMA_URL}: {type(exc).__name__} (start Ollama)")
        return 1
    print(f"ok    Ollama at {client.OLLAMA_URL}; pulled: {', '.join(m['name'] for m in have) or 'nothing'}")
    if not any(m["name"] in (tag, f"{tag}:latest") for m in have):
        print(f"FAIL  {tag} is not pulled: run `ollama pull {tag}`")
        return 1
    d, secs = _chat(tag, messages=[{"role": "user", "content": "Reply with the single word: ready"}],
                    options={"num_predict": 200})
    print(f"ok    plain reply {d['message']['content'].strip()[:40]!r} in {secs:.1f}s (first call includes loading), "
          f"{_speed(d)}")
    try:
        mem = [m for m in httpx.get(f"{client.OLLAMA_URL}/api/ps", timeout=5).json().get("models", [])
               if m["name"].startswith(tag)]
        if mem:
            m = mem[0]
            print(f"ok    loaded: {m['size'] / 2**30:.1f} GB, {m.get('size_vram', 0) / 2**30:.1f} GB of it on the GPU")
    except httpx.HTTPError:
        pass
    # The same call the app makes: size the context, low reasoning, JSON mode.
    try:
        text = client._local_write('Reply as JSON: {"problems": []}', True, client.LOCAL_PREFIX + tag, 600, True)[0]
        json.loads(text)
        print(f"ok    client path (num_ctx sized, JSON mode): {text.strip()[:60]}")
    except (client.AIError, ValueError) as exc:
        print(f"FAIL  client path: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
