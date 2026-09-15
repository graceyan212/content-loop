#!/usr/bin/env python3
"""
dimensions.py — the A/B dimension catalog + one-axis-deviation arm generation.
Ported from the PATTERN in the reference implementation's hermes/src/dimensions.ts
(/tmp/sffs-ai-video-pipeline) — not its quiz-specific content. That file's control
= current defaults (full narration + cliffhanger); every OTHER arm changes AT MOST
ONE axis, and arms are generated as "universe minus the current default" so:
  - the reigning default is never re-listed as its own challenger, and
  - a human-approved promotion (promote.py) that flips a default automatically
    makes the OLD default a testable challenger again — no code change needed.

This pipeline's dimensions (derived from 6 real posts, see danielle/learn/):

    type            ask · value · joke · intro · observe
    slot            late-night (22:15-22:55) · lunch (12:10-12:50) · pickup (14:45-15:25)
    audio           none · autoAddMusic
    setting         car · kitchen · outdoor · home · any
    density         short · wall
    first_comment   yes · no
    text_color      light · dark

`setting` deliberately has NO entry in DEFAULTS — the real posts so far split
car/home/any without a settled reigning choice, so there is nothing to deviate
FROM yet. It is still tracked for observational rollups (rollup.py's by_setting)
but build_arms() below never generates a one-axis-deviation test for it. Treating
"no default" as if it were e.g. "car" would fabricate a decision nobody made.
"""

from __future__ import annotations

from typing import Any

DIMENSIONS: dict[str, tuple[str, ...]] = {
    "type": ("ask", "value", "joke", "intro", "observe", "event", "pov"),
    "slot": ("dawn", "late-night", "lunch", "pickup"),
    "audio": ("none", "autoAddMusic"),
    "setting": ("car", "kitchen", "outdoor", "home", "any"),
    "density": ("short", "wall"),
    # NEW 2026-08-03. Where the list LIVES, which is a different question from how
    # long it is. Every post so far has been short-text-on-image + list-in-caption.
    # The accounts this pipeline was modelled on do the opposite: the entire body
    # goes ON the image, covering the face, and the caption is almost nothing. That
    # is the format @rareZuhair described as "text on screen on ai video is killing
    # it" — and it is the one thing from the source research never actually tested
    # here. value posts currently earn 0 median comments, so the caption-list may
    # simply be the wrong container rather than the wrong content.
    # numbered-count: the title PROMISES a number ("5 dinners I make when...") and
    # the items are 1. 2. 3. rather than bullets. Different from on-screen-wall,
    # which is about WHERE the list lives; this is about whether the reader is told
    # up front how many things are coming. caption.build() already numbers when the
    # title's promised count matches the item count — this arm makes titles that
    # promise one, so that path actually fires.
    "list_style": ("caption-list", "on-screen-wall", "numbered-count"),
    # Added 2026-08-03 from a reference grid the operator found: one account posting
    # selfie photos with text overlays at 3.5M-13.5M views each. Its on-screen text
    # is SMALLER and LONGER than ours (4-6 lines vs our 1-2), lowercase, barely
    # punctuated, and states a THOUGHT rather than asking a question. Ours is a
    # 62px headline. Different enough to be a real arm, not a tweak.
    "screen_style": ("headline", "confessional"),
    # Mechanics aimed at COHESION rather than reach — giving a returning follower
    # something a first-time viewer does not get.
    #   callback  — names a specific earlier post of hers, so following the account
    #               over time is rewarded with continuity a new viewer misses.
    #   segment   — a fixture that recurs on the same day, which is the only way a
    #               feed account earns a return visit rather than a single scroll.
    #   ingroup   — reuses a phrase she coined in an earlier post ("the bar", "the
    #               4:50 fridge stare"). Shared vocabulary is what a community is.
    # NOT an arm here: answering her own question first with a specific number. That
    # is already the account's best-performing move (4 of the top 5 posts by
    # comments/1k do it, 36.2 / 30.0 / 19.9 / 17.0) and much of the library already
    # does it implicitly, so making it an "arm" would compare it against itself.
    "community_move": ("none", "callback", "segment", "ingroup"),
    # Loss framing vs gain framing, same question underneath. Operator-requested.
    # NOTE the incumbent is "negative" on purpose, not by preference: every one of
    # the top five posts by comments/1k self-deprecates, so loss framing is what the
    # account already does. A negative-arm win is therefore not news; a positive-arm
    # win is. Only matched pairs (ask-06-sharp.md) can settle it — differing topic
    # AND tone would make the result unreadable.
    "framing": ("negative", "positive"),
    # Emotional register. Operator-requested: "fearmonger, ragebait, make people
    # feel good". Built as four arms with matched content (ask-09-register.md).
    #
    # `neutral` is the incumbent because it is what the top five posts by
    # comments/1k already do — disclose an unflattering number, then ask. fear and
    # provoke are the challengers and both can win on comment VOLUME while losing
    # on everything else, so adjust.py scores this dimension on participation
    # (comments per like), not comments per view, and a human reads the comment
    # sentiment before any promotion.
    #
    # PARTIALLY CONFOUNDED with `framing`: fear is loss-framed and warm is
    # gain-framed almost by construction. Read the two experiments together.
    "register": ("neutral", "fear", "provoke", "warm"),
    # cta RETIRED 2026-08-13, same day it was built. Operator: "i dont want u to
    # explicitly ask for these on posts". The asks are gone from the copy. A dimension
    # left at one arm would have plan.py proposing it forever with nothing to schedule,
    # so it is removed rather than parked.
    # RETIRED 2026-08-01. The operator removed first comments entirely: the image
    # carries text and the description carries text, so a third block from the same
    # account adds nothing. Keeping it in the universe meant the planner proposed
    # testing a dimension that no longer physically varies — every post is "no".
    # Left here, commented, so nobody re-adds it without reading this.
    # "first_comment": ("yes", "no"),
    # UN-RETIRED 2026-08-03. I retired this on 2026-08-01 because every post was
    # white and "dark" was unrenderable — correct at the time, wrong as a
    # conclusion. The reference grid uses YELLOW on roughly half its posts, which
    # is a third arm I had ruled out of existence rather than tested. Retiring a
    # dimension for not varying is not the same as establishing it should not vary.
    # HELD AT ONE ARM, not retired. The reference grid the operator found uses
    # yellow, so this is a real candidate — but "text should always be white" is a
    # standing instruction from the operator, and a dimension the operator has ruled
    # on is not an open question the loop gets to reopen on its own. It is also the
    # wrong axis to move right now: shipping confessional+yellow together would
    # confound the density test with a colour test and neither result would mean
    # anything. Flip to ("white", "yellow") the moment the operator says yes.
    "text_color": ("white",),
}

# Slot definitions in America/Chicago wall-clock time (see danielle/persona.json
# posting_slots) — informational only, not used for math.
SLOT_WINDOWS = {
    "late-night": ("22:15", "22:55"),
    "lunch": ("12:10", "12:50"),
    "pickup": ("14:45", "15:25"),
}

# Dimensions that currently HAVE a human-set default (defaults.json's `defaults`
# object) and are therefore eligible for one-axis-deviation arm generation.
# `setting` is excluded on purpose — see module docstring.
# Dimensions that are NOT defaulted — the loop observes them but never assigns them,
# because nothing in the pipeline chooses them. `setting` is a property of which still
# got picked, not a decision the scheduler makes.
UNDEFAULTED_DIMENSIONS: tuple[str, ...] = ("setting",)

# DERIVED, not hand-listed. This was a second hardcoded tuple alongside DIMENSIONS,
# and adding `community_move` to DIMENSIONS + defaults.json was not enough to make it
# testable: build_arms() and plan.py iterate THIS list, so the new dimension was
# silently never proposed and validate_defaults() never checked it either. A
# dimension that exists but can never be tested is worse than one that is absent,
# because the dashboard shows the column and implies it is being measured.
DEFAULTED_DIMENSIONS: tuple[str, ...] = tuple(
    d for d in DIMENSIONS if d not in UNDEFAULTED_DIMENSIONS
)

FALLBACK_DEFAULTS: dict[str, str] = {
    "type": "ask",
    "slot": "late-night",
    "audio": "autoAddMusic",
    "density": "short",
    "list_style": "caption-list",
    "screen_style": "headline",
    "text_color": "white",
    # first_comment retired — see ARMS above
    # text_color retired — see ARMS above
}


def dimensions_for(persona: dict | None = None) -> dict[str, tuple[str, ...]]:
    """
    The arm universe for ONE persona.

    DIMENSIONS above is Dani's, derived from six of her real posts. Its `type`
    axis is ask/value/joke/intro/observe and its `slot` axis is her three slots.
    Ray's content is confession/unanswerable/comparison/five-minute-test/two-kids
    and his slots are different, so running him against the module constant would
    rotate an axis that does not describe anything he posts.

    Two axes are overridden from persona.json when it declares them:
      type  <- territories[]      (the same headings batch.py parses from drafts)
      slot  <- posting_slots[].name

    A persona declaring neither gets the module constant unchanged, which is why
    Dani is unaffected.
    """
    out = {k: tuple(v) for k, v in DIMENSIONS.items()}
    cfg = (persona or {}).get("config") or {}

    territories = cfg.get("territories")
    if territories:
        out["type"] = tuple(territories)

    slots = [s.get("name") for s in (cfg.get("posting_slots") or []) if s.get("name")]
    if slots:
        out["slot"] = tuple(slots)

    return out


def slot_windows_for(persona: dict | None = None) -> dict[str, tuple[str, str]]:
    """This persona's slot name -> (start, end), from persona.json. SLOT_WINDOWS
    above is Dani's and stays as the fallback."""
    slots = ((persona or {}).get("config") or {}).get("posting_slots") or []
    got = {s["name"]: (s.get("start"), s.get("end")) for s in slots if s.get("name")}
    return got or dict(SLOT_WINDOWS)


def fallback_defaults_for(persona: dict | None = None) -> dict[str, str]:
    """
    Seed defaults for a persona that has no defaults.json yet.

    FALLBACK_DEFAULTS is Dani's (type=ask, slot=late-night, ...). Handing those to
    another persona invents a reigning choice nobody made — the exact mistake this
    module already refuses to make for `setting`, whose docstring says treating
    "no default" as if it were "car" would fabricate a decision.

    So: return Dani's seed only if every value in it is actually valid in THIS
    persona's universe. Otherwise return {} — no defaults, and build_arms() then
    generates no arms, which is the honest state for a persona with no posts.
    """
    universe = dimensions_for(persona)
    seed = dict(FALLBACK_DEFAULTS)
    if all(seed.get(d) in universe.get(d, ()) for d in DEFAULTED_DIMENSIONS):
        return seed
    return {}


def validate_defaults(defaults: dict[str, str],
                      universe: dict[str, tuple[str, ...]] | None = None) -> None:
    """Raise if `defaults` names an arm outside its own dimension's universe —
    a typo'd default would otherwise silently make that arm un-testable
    (it would never appear as "universe minus the default")."""
    universe = universe or DIMENSIONS
    for dim in DEFAULTED_DIMENSIONS:
        val = defaults.get(dim)
        if val is None:
            raise ValueError(f"defaults.json is missing a value for dimension {dim!r}")
        if val not in universe[dim]:
            raise ValueError(
                f"defaults.json sets {dim}={val!r}, which is not one of {universe[dim]!r}"
            )


def deviations(defaults: dict[str, str], variant: dict[str, str]) -> list[str]:
    """Which axes `variant` differs from `defaults` on, restricted to the
    defaulted dimensions. Used by tests to assert the one-axis invariant."""
    return [d for d in DEFAULTED_DIMENSIONS if variant.get(d) != defaults.get(d)]


def resolve_variant(defaults: dict[str, str], dim: str | None = None, arm: str | None = None) -> dict[str, str]:
    """Apply `defaults`, letting the caller override AT MOST one axis (the arm
    under test). Unset axes inherit. This is what makes every non-tested post a
    true baseline and the arm-under-test the only deviation."""
    out = dict(defaults)
    if dim is not None:
        out[dim] = arm
    return out


def build_arms(defaults: dict[str, str],
               universe: dict[str, tuple[str, ...]] | None = None) -> list[dict[str, Any]]:
    """The rotating arm catalog for the CURRENT defaults: control first, then
    every single-axis challenger (arm universe minus the current default) for
    each defaulted dimension.

    `universe` defaults to DIMENSIONS (Dani's). Pass dimensions_for(persona) to
    generate arms against that persona's own axes.

    EMPTY DEFAULTS RETURN NO ARMS, deliberately. A persona with no posts has no
    reigning choice to deviate from, and inventing one would fabricate a decision
    nobody made — the same reasoning that keeps `setting` out of
    DEFAULTED_DIMENSIONS. Raising here instead would just push the fabrication
    upstream.

    Each returned dict:
        {"dimension": str, "arm": str|None, "variant": {..full 6-axis set..},
         "rationale": str, "baseline": bool}
    """
    if not defaults:
        return []
    universe = universe or DIMENSIONS
    validate_defaults(defaults, universe)
    out: list[dict[str, Any]] = [{
        "dimension": "control",
        "arm": "control",
        "variant": dict(defaults),
        "rationale": "baseline: the current defaults ("
                     + ", ".join(f"{d}={defaults[d]}" for d in DEFAULTED_DIMENSIONS) + ")",
        "baseline": True,
    }]
    for dim in DEFAULTED_DIMENSIONS:
        current = defaults[dim]
        for candidate in universe[dim]:
            if candidate == current:
                continue
            out.append({
                "dimension": dim,
                "arm": candidate,
                "variant": resolve_variant(defaults, dim, candidate),
                "rationale": f"TEST ARM vs the {dim}={current!r} default: change ONLY "
                             f"{dim} to {candidate!r}, inherit every other axis from defaults",
                "baseline": False,
            })
    return out


def dimension_catalog(defaults: dict[str, str]) -> list[dict[str, Any]]:
    """Read-only view of the full arm catalog resolved against the current
    defaults, plus the un-defaulted `setting` dimension listed separately so
    callers can see the whole 7-dimension picture without mistaking `setting`
    for a defaulted axis."""
    arms = build_arms(defaults)
    for a in arms:
        a = a  # no-op; kept for symmetry with the reference's dimensionCatalog
    return arms


def undefaulted_dimensions() -> dict[str, tuple[str, ...]]:
    """Dimensions with no current default (today: just `setting`) — tracked for
    rollups only, never used to generate one-axis-deviation test arms."""
    return {d: arms for d, arms in DIMENSIONS.items() if d not in DEFAULTED_DIMENSIONS}
