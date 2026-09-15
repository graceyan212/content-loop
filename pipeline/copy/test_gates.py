#!/usr/bin/env python3
"""
test_gates.py — unit tests for the deterministic compliance gate.

Run:  python -m pytest copy/test_gates.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gates import DRAMATIZED, check, normalize  # noqa: E402


# ---------------------------------------------------------------------------
# Rule 1 — banned quantified-efficacy tokens
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "his iq went up after we started",
    "she said his IQ is above average",
])
def test_iq_token_fails(text):
    passed, reason = check(text)
    assert not passed
    assert "iq" in reason


def test_grade_level_fails():
    passed, reason = check("he is reading a grade level ahead now")
    assert not passed
    assert "grade level" in reason


@pytest.mark.parametrize("text", [
    "his test score jumped in a month",
    "her test scores are finally where they should be",
])
def test_test_scores_fail(text):
    passed, reason = check(text)
    assert not passed
    assert "test score" in reason


def test_smarter_fails():
    passed, reason = check("twenty minutes a day and he is smarter already")
    assert not passed
    assert "smarter" in reason


def test_percentile_fails():
    passed, reason = check("he moved up a percentile since june")
    assert not passed
    assert "percentile" in reason


def test_guaranteed_fails():
    passed, reason = check("guaranteed results before the school year starts")
    assert not passed
    assert "guaranteed" in reason


def test_percent_character_fails():
    passed, reason = check("he is doing so much better now %")
    assert not passed
    assert "%" in reason


# ---------------------------------------------------------------------------
# Rule 2 — a digit quantifying a result
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,needle", [
    ("his scores went up 40% in a month", "%"),
    ("he improved 40 percent in a month", "percent"),
    ("he gained 12 points on the assessment", "points"),
    ("up 7point on the thing", "points"),
])
def test_digit_quantified_fails(text, needle):
    passed, reason = check(text)
    assert not passed, f"expected {text!r} to fail"
    assert needle in reason


def test_bare_digits_still_pass():
    """Times and counts are everywhere in the corpus; only digit+unit is banned."""
    passed, reason = check(
        "6:40 on a saturday and the living room is already glowing")
    assert passed, reason
    passed, reason = check("his report card is all 3s and the back of it says 3 means meeting")
    assert passed, reason
    passed, reason = check("he got off at 6 and was mean until 7 and then he was mine again")
    assert passed, reason


# ---------------------------------------------------------------------------
# Rule 3 — first-person product use, and the dramatized-labeled exemption
# ---------------------------------------------------------------------------
PRODUCT_USE_HOOKS = [
    "i bought the app on a tuesday and hid in the bathroom to set it up",
    "i downloaded it after the parent teacher conference",
    "i subscribed the same night i sat in the garage",
    "we use it for twenty minutes before dinner now",
    "weve been using it since the report card came home",
    "i tried it myself first to see what it even was",
]


@pytest.mark.parametrize("text", PRODUCT_USE_HOOKS)
def test_product_use_fails_under_default_posture(text):
    passed, reason = check(text)
    assert not passed, f"expected {text!r} to fail"
    assert "first-person product use" in reason


@pytest.mark.parametrize("text", PRODUCT_USE_HOOKS)
def test_product_use_exempt_under_dramatized_labeled(text):
    """The whole point of the dramatized-labeled arm: it buys back this rule."""
    passed, reason = check(text, claim_posture=DRAMATIZED)
    assert passed, reason


@pytest.mark.parametrize("posture", ["no-product", "named-in-text", "second-person",
                                     "cited-population-stat"])
def test_product_use_not_exempt_under_other_postures(posture):
    passed, reason = check("i downloaded it after the conference", claim_posture=posture)
    assert not passed
    assert posture in reason


def test_dramatized_does_not_exempt_the_other_rules():
    """The label buys back product-use only. Efficacy claims stay banned."""
    passed, reason = check("i downloaded it and his test scores went up",
                           claim_posture=DRAMATIZED)
    assert not passed
    assert "test score" in reason


def test_we_used_to_is_not_we_use():
    """Corpus hook. 'we used to ride' must not trip the 'we use' rule."""
    passed, reason = check(
        "we used to ride down to the pond at the end of the neighborhood on sundays "
        "and he made me stop at the same storm drain every time to look for frogs")
    assert passed, reason


# ---------------------------------------------------------------------------
# Rule 4 — fabricated credential as a self-description
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "as a teacher i can tell you this is not normal",
    "as a doctor i see this every single week",
    "as a therapist this is the part that worries me",
    "as a child psychologist i want to say one thing",
    "as a former teacher i should have caught it sooner",
    "im a teacher and i still missed it",
    "i am a therapist and i could not see it in my own house",
    "speaking as a psychologist the quiet is the tell",
])
def test_self_described_credential_fails(text):
    passed, reason = check(text)
    assert not passed, f"expected {text!r} to fail"
    assert "fabricated credential" in reason


@pytest.mark.parametrize("text", [
    "his teacher ended the email with an exclamation point and told me absolutely nothing",
    "i already know which teacher i want him to get and i would never say that out loud",
    "a mom at pickup asked which teacher he has like the answer was supposed to mean something",
    "owen has had six teachers now and all six have told me some version of hes doing great",
    "my sister is a teacher and she says nothing either",
    "the doctor asked how he sleeps and i did not have an answer",
])
def test_third_party_credential_mentions_pass(text):
    """Talking ABOUT a teacher is legal. Claiming to BE one is not."""
    passed, reason = check(text)
    assert passed, reason


# ---------------------------------------------------------------------------
# no false positives on the real corpus vocabulary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "he asked me while i was merging onto the tollway whether i thought he was "
    "one of the smart kids and i said of course you are",
    "maya was the gifted one until she wasnt and no one ever sent a letter about that",
    "i googled what long division looks like in fifth grade at 11pm and closed the laptop",
    "there is a unique thing he does with his hands",
    "i dont need him first i just want a number and no one in that building has one",
])
def test_corpus_near_misses_pass(text):
    passed, reason = check(text)
    assert passed, reason


def test_empty_fails():
    passed, reason = check("")
    assert not passed
    assert "empty" in reason


def test_normalize_strips_apostrophes_and_case():
    assert normalize("We'Ve  Been   Using") == "weve been using"


def test_apostrophe_form_is_caught():
    """House style has no apostrophes, but a generated hook might."""
    passed, reason = check("we've been using it since may")
    assert not passed
    assert "first-person product use" in reason


def test_reason_is_empty_on_pass():
    passed, reason = check("the chime that game makes when he loses is the loudest thing in this house")
    assert passed
    assert reason == ""


# ---------------------------------------------------------------------------
# the OTHER gate: batch.py's still hold-back rule
#
# This regex got it wrong twice while being written — once too narrow (missed
# "Recommend holding this one back"), once too loose (a bare `exclude` matched
# notes about a text REGION being excluded and benched two good stills). Both
# real manifest strings are pinned here.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "render"))


def _hold_back(notes: str) -> bool:
    import batch
    return bool(batch._HOLD_BACK.search(notes))


@pytest.mark.parametrize("notes", [
    "Recommend holding this one back or re-rolling.",
    "hold back",
    "Held back pending a re-roll.",
    "Do not use — identity drift.",
    "dont ship this one",
    "This frame is unusable.",
    "Withhold from the batch.",
    "Exclude this still from any batch.",
])
def test_hold_back_phrases_are_caught(notes):
    assert _hold_back(notes), f"expected {notes!r} to be held back"


@pytest.mark.parametrize("notes", [
    # verbatim from character/manifest.json — these describe a text REGION being
    # excluded, not the still, and must NOT bench the image
    "it straddles the eye-line row, so it is excluded here by the clearance rule; "
    "use it if your layout allows.",
    "The patio rug in the bottom-right is mushy — the safe zone already excludes it.",
    "Safe zone is the grey t-shirt; keep text left-weighted.",
    "Do not hold a static shot on the lower third; the Ken Burns push helps hide it.",
    "avoid holding a static crop on the bottom third.",
    "",
])
def test_region_notes_do_not_hold_the_still_back(notes):
    assert not _hold_back(notes), f"{notes!r} should NOT bench the still"


# ---------------------------------------------------------------------------
# Regression: the product-use rule used to fire on the bare verb, which
# rejected ordinary grocery and household copy ("store bought", "we use paper
# plates"). It must catch a persona claiming she used THE PRODUCT, and nothing
# else. Narrowed 2026-07-29 after it blocked real posts in the meals library.
# ---------------------------------------------------------------------------
import pytest


@pytest.mark.parametrize("text", [
    "Store bought cookies and I am not apologizing for it.",
    "I bought bread two loaves at a time because we were running out.",
    "We use paper plates on Wednesdays and I sleep fine.",
    "I bought the big bag of frozen chicken and portioned it myself.",
    "We use the crockpot maybe twice a year, be honest about yours.",
])
def test_ordinary_life_is_not_product_use(text):
    passed, reason = check(text)
    assert passed, f"false positive on innocent copy: {reason}"


@pytest.mark.parametrize("text", [
    "I bought it after seeing it on here.",
    "We finally just downloaded it last week.",
    "I subscribed for a year.",
    "We use the app every night before bed.",
    "Tried it for two weeks and he loves it.",
])
def test_real_product_use_still_caught(text):
    passed, _ = check(text)
    assert not passed, "product-use claim slipped through the narrowed rule"


@pytest.mark.parametrize("text", [
    "I bought it after seeing it on here.",
    "We finally just downloaded it last week.",
])
def test_product_use_still_exempt_when_dramatized(text):
    passed, _ = check(text, claim_posture="dramatized-labeled")
    assert passed, "dramatized-labeled must still exempt product-use claims"


# ---------------------------------------------------------------------------
# cited-population-stat — the posture that lets Ray cite public education data.
#
# The rule it relaxes exists to stop an EFFICACY claim ("our thing moved this
# number"). It cannot distinguish that from a cited population statistic, because
# the discriminating features (product implicated? subject a child or a
# population? source named?) appear in none of the patterns. This posture supplies
# the missing conditions, so the tests below are mostly about what it must NOT let
# through.
# ---------------------------------------------------------------------------

CITED_STAT = "cited-population-stat"


@pytest.mark.parametrize("text", [
    "Reading scores fell 5 points since 2019, per NAEP.",
    "According to NAEP, 33% of 8th graders read below basic.",
    "Source: NAEP. Fourth grade reading is at a 30 year low.",
])
def test_cited_stat_allows_sourced_population_figures(text):
    passed, reason = check(text, claim_posture=CITED_STAT)
    assert passed, reason


@pytest.mark.parametrize("text", [
    "Reading scores fell 5 points since 2019.",
    "33% of 8th graders read below basic.",
    "Test scores are down and nobody is telling you.",
])
def test_cited_stat_requires_a_source_in_the_same_line(text):
    """The attribution must be IN the text. The text is what renders on the
    slide, so a source recorded anywhere else is a source the viewer never sees."""
    passed, reason = check(text, claim_posture=CITED_STAT)
    assert not passed, f"unsourced figure slipped through: {text!r}"
    assert "no source named" in reason


@pytest.mark.parametrize("text", [
    "The app raised test scores 12 points, per NAEP.",
    "According to NAEP, scores fell 5 points. The program fixes that.",
    "Per NAEP, reading is down. We downloaded it and it helped.",
])
def test_cited_stat_refused_when_a_product_shares_the_line(text):
    """A real citation must not launder a product claim. This is the case that
    trips none of the first-person patterns in Rule 3 and would otherwise pass."""
    passed, reason = check(text, claim_posture=CITED_STAT)
    assert not passed, f"product + statistic slipped through: {text!r}"


@pytest.mark.parametrize("text", [
    "Per NAEP, average iq fell 3 points.",
    "According to NAEP, kids are not getting smarter.",
    "Per NAEP, the 30th percentile dropped 4 points.",
    "According to NAEP, they are two grade levels behind.",
    "Source: NAEP. Guaranteed to be worse next year.",
])
def test_cited_stat_does_not_relax_the_always_banned_tokens(text):
    """iq / smarter / percentile / grade level / guaranteed stay shut under every
    posture. smarter in particular is the SFFS brand guardrail's only enforcement
    point, and it lives in this list."""
    passed, reason = check(text, claim_posture=CITED_STAT)
    assert not passed, f"always-banned token relaxed by the stat posture: {text!r}"
    assert "banned token" in reason


@pytest.mark.parametrize("text", [
    "Reading scores fell 5 points since 2019, per NAEP.",
    "According to NAEP, 33% of 8th graders read below basic.",
])
def test_default_posture_is_unchanged_by_the_new_posture(text):
    """Dani and Chloe never ask for CITED_STAT, so their behaviour must be exactly
    what it was before it existed."""
    passed, _ = check(text)
    assert not passed, "adding a posture must not loosen the default one"
