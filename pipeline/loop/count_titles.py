#!/usr/bin/env python3
"""
count_titles.py — give list posts a title that promises a number.

    python loop/count_titles.py --persona dani            # preview
    python loop/count_titles.py --persona dani --go

WHY: caption.build() already numbers items 1. 2. 3. instead of bulleting them — but
only when the title's promised count MATCHES the item count, because a "5 things"
headline over four numbered items is worse than no numbers at all. Measured across
the library: 0 of 63 list posts promise a count, so that branch has never once run.

This writes a `count_title:` field alongside the existing `title:`. Two reasons not
to overwrite `title:`:
  - the descriptive title is the control arm and has to stay intact to compare against
  - a stored variant is reviewable in the dashboard before it ships, where one
    generated at render time is not

The count in the title is taken from len(items), never invented, so the promise and
the list cannot drift apart.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "copy"), os.path.join(ROOT, "post")):
    if p not in sys.path:
        sys.path.insert(0, p)

import llm         # noqa: E402
import gates       # noqa: E402
import caption     # noqa: E402
import personas    # noqa: E402
import respin      # noqa: E402
import guard       # noqa: E402

SYSTEM = """You rewrite social post titles for a 38-year-old mom's TikTok so they
promise a specific number of things.

Input: a title and the exact number of list items under it.
Output: the same title reworked to start with that number.

  "What I make for dinner when I am running on empty" (5 items)
    -> "5 dinners I make when I am running on empty"
  "What actually lives in my freezer and why" (4 items)
    -> "4 things that always live in my freezer"
  "Things I do that sound unhinged but genuinely work" (3 items)
    -> "3 things I do that sound unhinged and genuinely work"

Rules:
- START with the digit. Never spell it out.
- The number MUST be the one given. Never change it.
- Keep her voice: sentence case after the number, casual, no em dashes, never a
  slogan, never clickbait ("You won't believe...").
- Keep the specific noun from the original. "5 things" is weaker than "5 dinners".
- Under 12 words.

Return ONLY a JSON array of strings, same length and order as the input."""


def needs_title(post: dict) -> bool:
    if post.get("count_title"):
        return False
    items = post.get("items") or []
    if len(items) < 3:
        return False            # "2 things" is not a listicle
    return caption.promised_count(post.get("title") or "") != len(items)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True)
    ap.add_argument("--batch", type=int, default=14)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args(argv)

    p = personas.resolve(a.persona)
    root = p["root"]
    lib = respin.library_by_uid(root)
    todo = [(uid, x) for uid, x in lib.items() if needs_title(x)]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo)} list post(s) need a count title\n")
    if not todo:
        return 0

    accepted, rejected, cost = [], [], 0.0
    for i in range(0, len(todo), a.batch):
        chunk = todo[i:i + a.batch]
        payload = "\n".join(
            f"{n+1}. \"{x.get('title')}\" ({len(x['items'])} items)"
            for n, (_uid, x) in enumerate(chunk))
        try:
            text, usage = llm.chat(
                f"Rewrite each title to promise its item count.\n\n{payload}\n\n"
                f"Return ONLY a JSON array of {len(chunk)} strings.",
                system=SYSTEM, max_tokens=2000)
            out = llm.extract_json(text)
            if not isinstance(out, list) or len(out) != len(chunk):
                raise llm.LLMError(f"expected {len(chunk)} strings")
        except llm.LLMError as e:
            print(f"  batch failed, skipping: {e}")
            continue
        cost += usage.get("costInUSD") or 0
        for (uid, post), new in zip(chunk, out):
            new = str(new).strip().strip('"')
            want = len(post["items"])
            why = None
            if caption.promised_count(new) != want:
                why = f"promises {caption.promised_count(new)}, has {want} items"
            elif "—" in new:
                why = "em dash"
            elif len(new.split()) > 13:
                why = f"{len(new.split())} words"
            else:
                ok, g = gates.check(new)
                if not ok:
                    why = f"gate: {g}"
            (rejected if why else accepted).append((uid, post, new, why))

    for uid, post, new, _ in accepted:
        print(f"  {uid}")
        print(f"      was: {post.get('title')}")
        print(f"      now: {new}")
    for uid, post, new, why in rejected:
        print(f"  SKIPPED {uid}: {why}  ({new[:52]!r})")

    print(f"\n{len(accepted)} accepted, {len(rejected)} skipped, ${cost:.4f}")
    guard.ledger_append(root, "llm", 1, cost_usd=cost, task="count_titles")
    if not a.go:
        print("PREVIEW — nothing written. Re-run with --go")
        return 0

    import collections
    by_file = collections.defaultdict(list)
    for uid, post, new, _ in accepted:
        by_file[uid.split("::")[0]].append((post.get("title"), new))
    for fname, pairs in by_file.items():
        path = os.path.join(root, "copy", fname)
        text = open(path).read()
        n = 0
        for old, new in pairs:
            anchor = f"title: {old}\n"
            if anchor in text:
                text = text.replace(anchor, f"{anchor}count_title: {new}\n", 1)
                n += 1
        open(path, "w").write(text)
        print(f"  {fname}: {n} count_title field(s) added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
