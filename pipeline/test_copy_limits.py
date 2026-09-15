#!/usr/bin/env python3
"""
test_copy_limits.py — enforce the copy rules that were only ever prose.

Run:  python -m pytest test_copy_limits.py -q

WHY THIS FILE EXISTS. Building Ray produced a pile of copy rules — a 35-word cap on
the on-screen line, a 56px legibility floor, a banned brain-claim vocabulary, one
posture per territory — and every one of them lived in `hard_rules`, which is prose
the LLM reads. That makes them advisory. Nothing stopped a hand-edited draft from
violating all four, and in fact:

  * a wall hook at 85 words rendered at 34px, unreadable on a phone;
  * an 18-word "short" hook overflowed a narrow zone and the renderer relocated the
    text ON TOP OF HIS FACE rather than truncate it;
  * "nobody taught me to make it addictive" sat in the confession library for a day
    after `addict` was banned, because the ban was a sentence in a JSON string.

Each of those was caught by a human eye or an ad-hoc script typed at the moment. The
rules are now STRUCTURED DATA in persona.json's `copy_limits`, and this file is what
makes them real.

Personas that declare no `copy_limits` are skipped rather than assumed — Dani and
Chloe predate this and their libraries were written against different constraints.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "copy"), os.path.join(HERE, "render"),
          os.path.join(HERE, "post"), os.path.join(HERE, "learn")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas   # noqa: E402
import batch      # noqa: E402
import gates      # noqa: E402

PERSONA_NAMES = sorted(json.load(open(os.path.join(HERE, "personas.json"))))


def _limits(persona: dict) -> dict:
    return (persona.get("config") or {}).get("copy_limits") or {}


def _hooks(persona: dict) -> list[dict]:
    copy_dir = batch._persona_data_paths(persona)["copy_dir"]
    files = batch.draft_files_in(copy_dir)
    if not files:
        return []
    return batch.parse_hooks(copy_dir=copy_dir, files=files,
                             territories=batch.persona_territories(persona))


@pytest.fixture(params=PERSONA_NAMES)
def persona(request):
    p = personas.resolve(request.param)
    if not _limits(p):
        pytest.skip(f"{request.param} declares no copy_limits")
    if not _hooks(p):
        pytest.skip(f"{request.param} has no drafts-*.md library")
    return p


def test_no_hook_exceeds_the_word_cap(persona):
    """The cap is a LEGIBILITY floor, not a style preference. The renderer picks the
    largest font that fits; past the cap it steps down and the post becomes a wall of
    small text, which is the one thing that does not survive a phone screen."""
    cap = _limits(persona).get("max_words_on_screen")
    if not cap:
        pytest.skip("no max_words_on_screen declared")
    over = [(len(h["hook_text"].split()), h["source_file"], h["source_line"])
            for h in _hooks(persona) if len(h["hook_text"].split()) > cap]
    assert not over, (
        f"{persona['name']}: {len(over)} hook(s) over the {cap}-word cap. Each will "
        f"render smaller than the rest of the feed.\n  " +
        "\n  ".join(f"{f}:{ln} ({n}w)" for n, f, ln in sorted(over, reverse=True)))


def test_no_hook_renders_below_the_font_floor(persona):
    """The measurement, not the proxy. Word count predicts font size but the renderer
    decides, so ask the renderer. This is what caught wall hooks landing at 34px."""
    floor = _limits(persona).get("min_font_px")
    if not floor:
        pytest.skip("no min_font_px declared")
    import overlay as ov
    from PIL import Image

    # The most generous zone this persona has: if a hook is too small even here, it is
    # too small everywhere.
    paths = batch._persona_data_paths(persona)
    stills, _ = batch.load_stills(paths["manifest_path"], paths["stills_dir"])
    zones = [s.get("safe_text_zone_wall") or s.get("safe_text_zone") for s in stills]
    zones = [z for z in zones if z]
    if not zones:
        pytest.skip("no manifest zones to measure against")
    zone = max(zones, key=lambda z: z["w"] * z["h"])

    small = []
    for h in _hooks(persona):
        img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        size, _lines, _bbox, _tr = ov._style_tiktok_native(
            img, h["hook_text"], zone, valign="center", text_color="light",
            screen_style=None)
        if size < floor:
            small.append((size, h["source_file"], h["source_line"]))
    assert not small, (
        f"{persona['name']}: {len(small)} hook(s) render below the {floor}px floor in "
        f"its own largest zone.\n  " +
        "\n  ".join(f"{f}:{ln} ({s}px)" for s, f, ln in sorted(small)))


def test_no_hook_uses_banned_vocabulary(persona):
    """Brain claims. These are the most fact-checked words in this subject and the
    persona's credibility rests on not making them — one correction in the comments
    from someone who actually studies it is terminal."""
    banned = [b.lower() for b in _limits(persona).get("banned_terms") or []]
    if not banned:
        pytest.skip("no banned_terms declared")
    hits = []
    for h in _hooks(persona):
        low = h["hook_text"].lower()
        for b in banned:
            if b in low:
                hits.append((b, h["source_file"], h["source_line"]))
    assert not hits, (
        f"{persona['name']}: banned term(s) in the library.\n  " +
        "\n  ".join(f"{f}:{ln} -> {b!r}" for b, f, ln in hits))


def test_every_hook_passes_the_posture_its_territory_runs_under(persona):
    """A territory listed in claim_posture_allowed runs under that posture; everything
    else runs under the default. A hook that fails its own posture can never ship, and
    it fails at render time rather than at authoring time."""
    cfg = persona["config"]
    default = cfg.get("claim_posture_default", "no-product")
    bad = []
    for h in _hooks(persona):
        # Trust the parser. This used to re-derive the posture from a hardcoded
        # {"sourced"} set, which is a second source of truth for the same fact, and
        # it diverged the moment a file declared `claim_posture:` in its own header
        # (drafts-onramp.md, dramatized-labeled). The posture a hook is actually
        # gated and RENDERED under is the one batch.parse_hooks read off the file,
        # so that is the one to assert against.
        posture = h.get("claim_posture") or default
        ok, why = gates.check(h["hook_text"], posture)
        if not ok:
            bad.append((h["source_file"], h["source_line"], posture, why))
    assert not bad, (
        f"{persona['name']}: hook(s) fail the posture their territory runs under.\n  " +
        "\n  ".join(f"{f}:{ln} [{p}] {w[:80]}" for f, ln, p, w in bad))


def test_every_declared_territory_has_hooks(persona):
    """A territory in persona.json with no drafts file is a silent hole: batch.py will
    accept `--territory X` and render nothing, and dimensions.py will generate an A/B
    arm for content that does not exist."""
    declared = set(batch.persona_territories(persona))
    present = {h["territory"] for h in _hooks(persona)}
    missing = declared - present
    assert not missing, f"{persona['name']}: declared but empty territories: {sorted(missing)}"


def test_an_account_has_a_way_to_introduce_itself(persona):
    """Ray shipped 92 hooks with no intro territory. Every post assumed the reader
    already knew the premise, which on a new account is nobody — and the confession
    format has nothing to confess against without it."""
    present = {h["territory"] for h in _hooks(persona)}
    assert "intro" in present, (
        f"{persona['name']} has no `intro` territory. A first post that explains who "
        f"is talking is not optional on an account with no followers.")
