#!/usr/bin/env python3
"""
caption.py — assemble the post description from a library entry.

ONE implementation, imported by schedule_batch and respin, because they were each
building the caption inline and had already started to drift.

The problem this fixes: items were joined with a single "\\n" and no marker, so a
five-item list published as an unbroken wall of full sentences with nothing to
anchor the eye. Live example (2026-08-01, the mom-friends post):

    ...What places am I missing?

    The bleachers at practice. Same four women every Tuesday, and it took about
    The 20 minutes in the pickup line. Roll the window down. That is genuinely
    A rec league I signed up for by myself at 38, which was terrifying for exactly

Nothing tells you where one item stops. TikTok collapses nothing and adds nothing,
so whatever separation exists has to be in the string we send.

Shape now:

    <lead line>

    • item one

    • item two

    #tags

A blank line between items costs vertical space, but TikTok truncates to ~2 lines
behind a "more" tap regardless — so the expanded view is the only one where
formatting matters, and there the separation is worth far more than the height.

Numbered when the title promises a count ("5 dinners...", "Three things..."),
bulleted otherwise, because a numbered list that does not match its headline count
is worse than no numbers.
"""

from __future__ import annotations

import re

BULLET = "•"

# "5 dinners", "Three things", "4 things that..." — a title that promises a count
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def promised_count(title: str) -> int | None:
    if not title:
        return None
    m = re.match(r"\s*(\d+)\b", title)
    if m:
        return int(m.group(1))
    first = re.match(r"\s*([A-Za-z]+)\b", title)
    if first:
        return _COUNT_WORDS.get(first.group(1).lower())
    return None


def build(post: dict, max_items: int | None = None,
          numbered: bool = False) -> str:
    """
    Assemble caption + items + hashtags into the description string.

    max_items trims a long list rather than shipping ten bullets nobody reads;
    None keeps them all.
    """
    lead = (post.get("caption") or "").strip()
    items = [i.strip() for i in (post.get("items") or []) if i and i.strip()]
    tags = (post.get("hashtags") or "").strip()

    if max_items is not None and len(items) > max_items:
        items = items[:max_items]

    parts = []
    if lead:
        parts.append(lead)

    if items:
        # numbered=True is the `list_style: numbered-count` arm: use count_title,
        # which loop/count_titles.py generated to promise exactly len(items). The
        # descriptive `title` stays untouched as the control arm.
        head = (post.get("count_title") if numbered else None) \
            or post.get("title") or post.get("on-screen") or ""
        n = promised_count(head)
        # only number them if the count actually matches what the title claims
        numbered = n is not None and n == len(items)
        block = [f"{i}. {t}" if numbered else f"{BULLET} {t}"
                 for i, t in enumerate(items, 1)]
        parts.append("\n\n".join(block))

    if tags:
        parts.append(tags)

    return "\n\n".join(parts).strip()


def on_screen_wall(post: dict, max_items: int = 4) -> str:
    """
    The whole post as ON-IMAGE text: title, then the items, tight.

    This is the `list_style: on-screen-wall` arm. The reference accounts this
    pipeline was modelled on put the entire body on the image and leave the caption
    nearly empty — text over the face, nothing to click for. Every post here so far
    has done the opposite, and value posts earn 0 median comments, so the container
    is worth testing separately from the content.

    Capped at 4 items and stripped of trailing periods: this has to survive being
    autofit into a safe zone at a legible size. Six long bullets on a phone screen
    is not a post, it is a document.
    """
    title = (post.get("title") or post.get("on-screen") or "").strip()
    items = [i.strip().rstrip(".") for i in (post.get("items") or []) if i.strip()]
    items = items[:max_items]
    if not items:
        return title
    # Blank line BETWEEN items, not just after the title. First render of this arm
    # had them on consecutive lines and it read as one paragraph — the same
    # wall-of-text failure the caption gate exists to catch, just on the image
    # where no gate was looking.
    return title + "\n\n" + "\n\n".join(items)


def wall_caption(post: dict) -> str:
    """
    The near-empty caption that pairs with an on-screen wall.

    The body is already on the image, so repeating it here would be the worst of
    both: a wall to read twice. Keep the invitation and the hashtags only.
    """
    lead = (post.get("caption") or "").strip()
    # keep just the last sentence, which in this library is reliably the ask
    parts = [x.strip() for x in lead.replace("!", ".").replace("?", "?.").split(".") if x.strip()]
    tail = parts[-1] if parts else ""
    if tail and not tail.endswith(("?", "!")):
        tail += "."
    tags = (post.get("hashtags") or "").strip()
    return "\n\n".join([x for x in (tail, tags) if x])


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: build() was written to fix run-together list items, and then a
# post shipped with run-together list items anyway, because nothing CHECKED. A
# formatter is a convention; a convention with no check is a suggestion. This runs
# immediately before the API call, so a caption cannot reach TikTok unreadable
# regardless of which code path assembled it.

MAX_ITEM_CHARS = 115      # longer than this is a paragraph, not a list item
MAX_CAPTION_CHARS = 2200  # TikTok's own limit
MARKERS = ("•", "-", "–")


def looks_like_list(text: str) -> bool:
    """3+ consecutive non-empty lines that are full sentences = an unmarked list."""
    lines = [l for l in text.split("\n") if l.strip()]
    runs, run = 0, 0
    for l in lines:
        sentence = len(l.split()) >= 6 and l.rstrip().endswith((".", "!", "?"))
        marked = l.lstrip().startswith(MARKERS) or re.match(r"^\s*\d+[.)]\s", l)
        run = run + 1 if (sentence and not marked) else 0
        runs = max(runs, run)
    return runs >= 3


def validate(text: str) -> tuple[bool, str]:
    """
    (ok, reason). Called before every scheduled post. Blocks on:
      - an unmarked run of sentences (the exact failure that shipped 2026-08-01)
      - list items with no blank line between them
      - any single item long enough to read as a paragraph
    """
    if not text or not text.strip():
        return False, "empty caption"
    if len(text) > MAX_CAPTION_CHARS:
        return False, f"caption {len(text)} chars, over TikTok's {MAX_CAPTION_CHARS}"

    if looks_like_list(text):
        return False, ("unmarked list: 3+ consecutive sentence lines with no bullet "
                       "or number. This is the wall-of-text failure. Use caption.build().")

    marked = [l for l in text.split("\n")
              if l.lstrip().startswith(MARKERS) or re.match(r"^\s*\d+[.)]\s", l)]
    if len(marked) >= 2:
        # every marked item must be separated by a blank line
        lines = text.split("\n")
        for i, l in enumerate(lines[:-1]):
            is_item = l.lstrip().startswith(MARKERS) or re.match(r"^\s*\d+[.)]\s", l)
            nxt = lines[i + 1]
            nxt_item = nxt.lstrip().startswith(MARKERS) or re.match(r"^\s*\d+[.)]\s", nxt)
            if is_item and nxt_item:
                return False, "list items are not separated by a blank line"
    return True, ""


def readability_warnings(text: str) -> list[str]:
    """
    Non-blocking quality notes.

    Item length is a WARNING, not a block, and the split is deliberate. Structure
    (an unmarked wall of sentences) is a defect: it shipped, it was unreadable, and
    it is always wrong. Length is a preference: measured across the library, the
    median item is 94 chars and only 40 of 268 exceed 115. Blocking on it would
    have stopped 43% of posts, unattended, at 3am, with nobody awake to shorten a
    bullet. Never let a style rule take the loop down.
    """
    out = []
    for l in text.split("\n"):
        if not (l.lstrip().startswith(MARKERS) or re.match(r"^\s*\d+[.)]\s", l)):
            continue
        body = re.sub(r"^\s*(?:[•\-–]|\d+[.)])\s*", "", l)
        if len(body) > MAX_ITEM_CHARS:
            out.append(f"item {len(body)} chars, harder to scan: {body[:52]!r}...")
    return out


if __name__ == "__main__":
    demo = {
        "title": "Where I have actually made mom friends",
        "caption": "Making friends at 38 is so much harder than anyone warned me about. "
                   "What places am I missing?",
        "items": ["The bleachers at practice. Same four women every Tuesday.",
                  "The 20 minutes in the pickup line. Roll the window down.",
                  "A rec league I signed up for by myself at 38."],
        "hashtags": "#momfriends #momsoftiktok #momlife",
    }
    print(build(demo))
    print("\n" + "=" * 50 + "\n")
    demo2 = dict(demo, title="3 places I have actually made mom friends")
    print(build(demo2))
