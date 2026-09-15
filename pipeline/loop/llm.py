#!/usr/bin/env python3
"""
llm.py — the one place the loop talks to a model.

TrueFoundry gateway, OpenAI-compatible surface:
    POST $ANTHROPIC_BASE_URL/api/llm/chat/completions
    Authorization: Bearer $ANTHROPIC_AUTH_TOKEN

MODEL: claude-opus-5. Verified live on the gateway 2026-08-01.

TWO THINGS THAT WILL BITE YOU, both measured, not assumed:

1. REASONING TOKENS ARE BILLED AND INVISIBLE. Asking Opus 5 to reply "OPUS5 OK"
   with max_tokens=16 returned EMPTY content and still charged 16 completion
   tokens — the reasoning consumed the whole budget before any text existed.
   The same call at max_tokens=512 returned the string using 33 tokens. So a
   too-small cap does not truncate the answer, it deletes it. MIN_TOKENS below
   is a floor, not a suggestion.

2. COST IS REPORTED. The gateway returns `usage.costInUSD` per call. That is
   rare and worth using: the budget ceiling can be enforced in real dollars
   instead of a token estimate. Hermes could not do this — its READINESS.md
   admits "the TrueFoundry provider doesn't surface usage to the framework" and
   its dollar figure is a guess. Ours is measured.

Retries mirror Hermes's llm.ts: 3 attempts, linear backoff 1.5s * attempt.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

MODEL = "claude-opus-5"
MIN_TOKENS = 512          # below this, reasoning eats the answer — see above
DEFAULT_TOKENS = 2000
ATTEMPTS = 3


def _ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c


class LLMError(RuntimeError):
    pass


def chat(prompt: str, system: str | None = None, max_tokens: int = DEFAULT_TOKENS,
         model: str = MODEL, temperature: float = 1.0) -> tuple[str, dict]:
    """
    One completion. Returns (text, usage). Raises LLMError after ATTEMPTS failures.

    Callers must handle LLMError rather than shipping a half-made post — the cycle
    treats a generation failure as "make nothing this run", not "post something
    unverified".
    """
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not base or not token:
        raise LLMError("ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN not set")

    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs,
            "max_tokens": max(MIN_TOKENS, max_tokens),
            "temperature": temperature}

    last = None
    for attempt in range(ATTEMPTS):
        try:
            req = urllib.request.Request(
                f"{base}/api/llm/chat/completions",
                data=json.dumps(body).encode(), method="POST")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, context=_ctx(), timeout=180) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                raise LLMError(str(d["error"])[:200])
            text = (d["choices"][0]["message"].get("content") or "").strip()
            usage = d.get("usage") or {}
            if not text:
                # empty content with tokens spent is the reasoning-budget failure
                raise LLMError(
                    f"empty content, {usage.get('completion_tokens')} completion "
                    f"tokens spent — raise max_tokens")
            return text, usage
        except Exception as e:
            last = e
            if attempt < ATTEMPTS - 1:
                time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"after {ATTEMPTS} attempts: {last}")


def extract_json(text: str):
    """
    Pull the first balanced JSON object/array out of a reply.

    Models wrap JSON in prose or fences no matter how firmly you ask them not to,
    so parse defensively rather than trusting the format instruction.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        i = t.find(opener)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(t)):
            if t[j] == opener:
                depth += 1
            elif t[j] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[i:j + 1])
                    except Exception:
                        break
    raise LLMError(f"no parseable JSON in reply: {text[:200]}")


if __name__ == "__main__":
    txt, use = chat("Reply with exactly: GATEWAY OK")
    print("reply:", txt)
    print("usage:", use)
    print("cost:  $%.6f" % (use.get("costInUSD") or 0))
