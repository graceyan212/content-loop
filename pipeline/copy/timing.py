#!/usr/bin/env python3
"""
timing.py — what time of day a post is ABOUT, so it can be posted then.

A post that says "we are consistently leaving at 7:38 for a house that has to
leave at 7:20" reads wrong at 10pm. A post about the pickup line should land
while people are sitting in the pickup line. That is the whole idea: the content
names a moment, so publish it in that moment.

This is separate from `settings.py` (which physical setting a post can be SHOT in).
A kitchen post might be about breakfast or about 5pm dinner — same setting, very
different hour.

STRONG terms are decisive alone; WEAK terms need two hits, the same two-tier trick
settings.py uses, because a single loose keyword mis-tags badly. Explicit `when:`
in the markdown always wins.

Slots deliberately avoid :00 and :30 — everyone schedules on the clock marks and
it clusters delivery.
"""

from __future__ import annotations

import re

# name -> (start_h, start_m, end_h, end_m). Local to the persona's timezone.
SLOTS: dict[str, tuple[int, int, int, int]] = {
    # Added 2026-08-03 at operator request ("I once heard it was good to post at
    # 4am"). NO EVIDENCE supports this: timing appears nowhere in TikTok's
    # documented ranking signals, and four large-N timing studies contradict each
    # other. The folklore is a low-competition-window theory nobody has measured.
    # It is a legitimate arm precisely because it is cheap to test and currently
    # unfalsified — not because it is likely.
    "dawn":       (4, 10, 4, 50),
    "morning":    (6, 15, 7, 10),     # the school-morning scramble
    "midmorning": (9, 35, 10, 20),    # kids gone, first quiet
    "lunch":      (12, 10, 12, 50),
    "pickup":     (14, 45, 15, 25),   # sitting in the car line
    "dinner":     (17, 15, 18, 5),    # the 5pm meltdown / what's-for-dinner hour
    "evening":    (20, 5, 20, 50),    # homework done, kids winding down
    "late-night": (22, 15, 22, 55),   # the proven slot on this account
}

# Chronological, for filling a day.
CHRONO = ["dawn", "morning", "midmorning", "lunch", "pickup", "dinner", "evening", "late-night"]

AFFINITY = {
    "dawn": {
        "strong": r"4 ?am|awake before|can.?t sleep|up in the night|middle of the night|"
                  r"3 ?am|before the sun",
        "weak":   r"\bawake\b|\bdark\b|\bquiet\b|\binsomnia\b",
    },
    "morning": {
        "strong": r"\balarm\b|\bsnooze\b|\bwake up\b|woke up|\bbreakfast\b|out the door|"
                  r"school morning|7[:.]\d\d ?am|6[:.]\d\d ?am|before school|"
                  r"getting out the door|morning routine",
        "weak":   r"\bmorning\b|\bcereal\b|\bbus\b|\bshoes\b|\blate\b",
    },
    "pickup": {
        "strong": r"pickup line|pick ?up line|car ?line|carpool|after school|"
                  r"school pickup|3[:.]\d\d ?pm|waiting in the car",
        "weak":   r"\bpickup\b|\bcar\b|\bwait\w*\b",
    },
    "dinner": {
        "strong": r"what.?s for dinner|\bdinner\b|5[:.]\d\d ?pm|6[:.]\d\d ?pm|"
                  r"after school meltdown|\bhangry\b|feed (?:them|him|her)|"
                  r"cooking|\bmeal\b",
        "weak":   r"\beat\w*|\bsnack\w*|\bhungry\b|\bkitchen\b|\bfridge\b",
    },
    "evening": {
        "strong": r"\bhomework\b|\bbath\b|after dinner|\bdishes\b|8[:.]\d\d ?pm|"
                  r"before bed|winding down",
        "weak":   r"\bevening\b|\btonight\b|\bcouch\b|\btv\b",
    },
    "late-night": {
        "strong": r"\bbedtime\b|go to bed|\basleep\b|10[:.]\d\d ?pm|11[:.]\d\d ?pm|"
                  r"lights out|up until|after they.?re? (?:down|asleep)|"
                  r"lying (?:in bed|there)|can.?t sleep",
        "weak":   r"\bbed\b|\bnight\b|\blate\b|\bquiet\b",
    },
    "midmorning": {
        "strong": r"once they.?re gone|house is quiet|after drop ?off|\bwork from home\b",
        "weak":   r"\balone\b|\bquiet\b|\bcoffee\b",
    },
}


def affinity_of(post: dict) -> str:
    """
    Which slot this post is ABOUT. Returns "any" when nothing is decisive —
    those are the posts that can fill whatever slot is left.
    """
    if post.get("when"):
        w = str(post["when"]).strip().lower()
        return w if w in SLOTS else "any"

    blob = " ".join([
        str(post.get("title") or ""),
        str(post.get("on-screen") or ""),
        str(post.get("caption") or ""),
        " ".join(post.get("items") or []),
    ]).lower()

    scores = {}
    for name, r in AFFINITY.items():
        strong = len(re.findall(r["strong"], blob))
        weak = len(re.findall(r["weak"], blob))
        scores[name] = strong * 3 + (weak if weak >= 2 else 0)

    best = max(scores, key=scores.get)
    runner = sorted(scores.values())[-2] if len(scores) > 1 else 0
    return best if scores[best] >= 3 and scores[best] > runner else "any"


def slot_window(slot: str) -> tuple[int, int, int, int]:
    return SLOTS.get(slot, SLOTS["late-night"])


def order_for_per_day(per_day: int, prefer: list[str] | None = None) -> list[str]:
    """
    Which slots to use when filling a day, in chronological order.

    `prefer` lets the caller keep a proven slot in every day's rotation — on this
    account late-night is the only slot with real history, so dropping it from a
    3/day plan would throw away the one comparison baseline that exists.
    """
    prefer = prefer or ["late-night"]
    chosen = [s for s in prefer if s in SLOTS][:per_day]
    for s in CHRONO:
        if len(chosen) >= per_day:
            break
        if s not in chosen:
            chosen.append(s)
    return [s for s in CHRONO if s in chosen]
