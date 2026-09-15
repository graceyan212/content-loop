#!/usr/bin/env python3
"""
rewrite.py — revise existing copy in place, in the persona's voice.

generate.py writes NEW posts. This one fixes posts that already exist but read
badly, which is a different job: the idea is already approved, only the execution
is wrong. Rewriting beats regenerating here because the concrete detail in an item
("browning the meatballs first", "six weeks before anyone said a word") is the part
that took judgment. Throwing it away to write a fresh bullet loses the good bit.

Current job: list items long enough to stop being scannable. Measured across the
library — median item 94 chars, p90 120, max 157. The 40 items over 115 are two
sentences doing a bullet's work.

SAFETY: every rewrite must survive the same gates as new copy, AND must keep the
specific detail. A shortening that drops the number or the admission has destroyed
the thing that made the item worth reading, so that is checked explicitly rather
than trusted.

    python loop/rewrite.py --dry-run          # show before/after, change nothing
    python loop/rewrite.py --go
"""

from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "copy"), os.path.join(ROOT, "post"),
          os.path.join(ROOT, "render")):
    if p not in sys.path:
        sys.path.insert(0, p)

import llm          # noqa: E402
import gates        # noqa: E402
import caption      # noqa: E402
import personas     # noqa: E402
import guard        # noqa: E402

TARGET = 110        # aim under this; the warning threshold is 115


SYSTEM = """You shorten list items for a mom's social media posts without changing
her voice or losing what makes them worth reading.

Every item you are given is too long to scan. Your job is to cut it down while
keeping the SPECIFIC thing in it: the number, the technique, or the self-deprecating
admission. That specific detail is the entire value of the item. An item shortened
into a generic tip is worse than the long version.

Keep her voice: sentence case, full punctuation, no em dashes, casual, never
moralizing. Do not add anything. Do not make it a slogan.

Return ONLY a JSON array of strings, same length and order as the input."""


def has_specific(text: str) -> bool:
    """A number, a technique verb, or a first-person admission — the value carrier."""
    if re.search(r"\d", text):
        return True
    if re.search(r"\b(i|my|we|our)\b", text, re.I):
        return True
    if re.search(r"\b(brown|press|freeze|swap|stack|label|prep|fold|toss|microwave|"
                 r"roll|dump|batch|portion|hide|stash)\w*\b", text, re.I):
        return True
    return False


def collect(root: str) -> list[tuple[str, int, str]]:
    """(uid, item index, text) for every item over the readability threshold."""
    import respin
    out = []
    for uid, p in respin.library_by_uid(root).items():
        for i, it in enumerate(p.get("items") or []):
            if len(it) > caption.MAX_ITEM_CHARS:
                out.append((uid, i, it))
    return out


def rewrite_batch(items: list[str]) -> list[str]:
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
    prompt = (f"Shorten each of these to under {TARGET} characters. Keep the "
              f"specific number, technique or admission in each one.\n\n{numbered}\n\n"
              f"Return ONLY a JSON array of {len(items)} strings.")
    text, usage = llm.chat(prompt, system=SYSTEM, max_tokens=4000)
    out = llm.extract_json(text)
    if not isinstance(out, list) or len(out) != len(items):
        raise llm.LLMError(f"expected {len(items)} strings, got {type(out).__name__} "
                           f"of length {len(out) if isinstance(out, list) else '?'}")
    return [str(x).strip() for x in out], usage


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args(argv)

    p = personas.resolve(a.persona)
    root = p["root"]
    targets = collect(root)
    print(f"{len(targets)} item(s) over {caption.MAX_ITEM_CHARS} chars\n")
    if not targets:
        return 0

    accepted, rejected, total_cost = [], [], 0.0
    for start in range(0, len(targets), a.batch):
        chunk = targets[start:start + a.batch]
        try:
            new, usage = rewrite_batch([t[2] for t in chunk])
        except llm.LLMError as e:
            print(f"  batch failed, leaving these unchanged: {e}")
            continue
        total_cost += usage.get("costInUSD") or 0
        for (uid, idx, old), fresh in zip(chunk, new):
            why = None
            if len(fresh) > caption.MAX_ITEM_CHARS:
                why = f"still {len(fresh)} chars"
            elif not has_specific(fresh):
                why = "lost the number/technique/admission"
            elif "—" in fresh:
                why = "em dash"
            else:
                ok, g = gates.check(fresh)
                if not ok:
                    why = f"gate: {g}"
            (rejected if why else accepted).append((uid, idx, old, fresh, why))

    for uid, idx, old, fresh, why in accepted:
        print(f"  {len(old):>3} -> {len(fresh):>3}  {uid}")
        print(f"        was: {old}")
        print(f"        now: {fresh}")
    for uid, idx, old, fresh, why in rejected:
        print(f"  KEPT AS-IS  {uid}: {why}")

    print(f"\n{len(accepted)} rewritten, {len(rejected)} left alone, ${total_cost:.4f}")
    guard.ledger_append(root, "llm", 1, cost_usd=total_cost, task="rewrite")

    if not a.go:
        print("DRY RUN — nothing written. Re-run with --go")
        return 0

    # apply by exact string replacement in the source markdown, so nothing else moves
    import collections
    by_file = collections.defaultdict(list)
    for uid, idx, old, fresh, _ in accepted:
        by_file[uid.split("::")[0]].append((old, fresh))
    for fname, pairs in by_file.items():
        path = os.path.join(root, "copy", fname)
        text = open(path).read()
        n = 0
        for old, fresh in pairs:
            if f"- {old}" in text:
                text = text.replace(f"- {old}", f"- {fresh}", 1)
                n += 1
        open(path, "w").write(text)
        print(f"  {fname}: {n} item(s) updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
