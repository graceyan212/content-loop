#!/usr/bin/env python3
"""
settings.py — which physical setting a post belongs to, and therefore which
photo it can be shot in.

ONE implementation, imported by both gallery.py (to tag and filter) and batch.py
(to pair a post with a still). It used to live only in gallery.py, so the batch
driver paired purely on mood and happily put kitchen copy on a car photo.

Two tiers, because a single loose keyword mis-tags badly: STRONG terms are
decisive on their own; WEAK terms need two hits. A first pass keyed on bare
"eat|snack|meal|lunch" filed 48 of 80 posts as kitchen, and "car wash plan"
filed a subscriptions post under car.

A post can override inference with an explicit `setting:` line in its markdown,
and a still with `setting_tag` in the manifest. Taste belongs in the data.
"""

from __future__ import annotations

import re

TAGS = ("car", "kitchen", "outdoor", "home", "any")

SETTINGS = {
    "car": {
        "strong": r"glovebox|backseat|back seat|carpool|pickup line|car ?line|"
                  r"minivan|dashboard|windshield|seat ?belt|drop ?off|school run",
        "weak":   r"\bcar\b|\bdriv\w+|\btrunk\b|parking lot|commute|traffic",
        "not":    r"car wash|carpet",
    },
    "kitchen": {
        "strong": r"\bdinner\b|\bkitchen\b|\bfridge\b|\bfreezer\b|\bpantry\b|"
                  r"crockpot|\boven\b|leftovers|grocer\w+|meal ?prep|sheet pan",
        "weak":   r"\bcook\w*|\bsnack\w*|\blunch\w*|\bmeal\w*|\bcounter\b|"
                  r"\brecipe\w*|\beat\w*|\bplate\b",
        "not":    r"",
    },
    "outdoor": {
        "strong": r"backyard|\bporch\b|front steps|sidewalk|neighborhood|driveway|"
                  r"\bgarage\b|\bfence\b|\bhose\b|\bwalk the dog\b",
        "weak":   r"\boutside\b|\byard\b|\bwalk\w*|\bsun\b|weather",
        "not":    r"",
    },
    "home": {
        "strong": r"\blaundry\b|fitted sheet|\bcloset\b|\bcouch\b|\bbathroom\b|"
                  r"\bhallway\b|\bshower\b|make (?:the )?bed|\bsocks\b|\bclutter\b",
        "weak":   r"\bbedroom\b|\bhouse\b|\btidy\w*|\bmess\b|\bfold\w*",
        "not":    r"",
    },
}


def setting_of(post: dict) -> str:
    """
    Best-guess setting for a post. An explicit `setting:` field wins.
    Returns "any" when nothing is decisive — better than being confidently wrong,
    and "any" posts are genuinely usable on every image.
    """
    if post.get("setting"):
        s = str(post["setting"]).strip().lower()
        return s if s in TAGS else "any"

    blob = " ".join([
        str(post.get("title") or ""),
        str(post.get("on-screen") or ""),
        str(post.get("caption") or ""),
        " ".join(post.get("items") or []),
    ]).lower()

    scores = {}
    for name, r in SETTINGS.items():
        b = re.sub(r["not"], " ", blob) if r["not"] else blob
        strong = len(re.findall(r["strong"], b))
        weak = len(re.findall(r["weak"], b))
        scores[name] = strong * 3 + (weak if weak >= 2 else 0)   # weak needs corroboration

    best = max(scores, key=scores.get)
    runner = sorted(scores.values())[-2] if len(scores) > 1 else 0
    return best if scores[best] >= 3 and scores[best] > runner else "any"


def still_setting(still: dict) -> str:
    """
    A still's setting tag. Prefers an explicit `setting_tag`; otherwise infers from
    the free-text `setting` description, which is prose written for humans
    ("kitchen, holding a coffee mug") rather than a controlled vocabulary.
    """
    t = (still.get("setting_tag") or "").strip().lower()
    if t in TAGS:
        return t
    return setting_of({"caption": still.get("setting") or "",
                       "title": still.get("framing") or ""})


def compatible(post_setting: str, still_setting_tag: str) -> bool:
    """
    Can this post be shot on this still?

    An "any" post fits any still. A still tagged "any" takes any post. Otherwise
    the settings must match: kitchen copy does not belong on a car photo.
    """
    return (post_setting == "any" or still_setting_tag == "any"
            or post_setting == still_setting_tag)
