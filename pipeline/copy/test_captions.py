#!/usr/bin/env python3
"""
test_captions.py — unit tests for copy/captions.py.

Run:  python -m pytest copy/test_captions.py -q

These tests deliberately never hit the network (use_llm=False / direct calls
to generate_atoms_template / validate_atoms), so they are fast and
deterministic regardless of gateway availability.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import captions as cap  # noqa: E402


HOOK_SHORT = "I said five more minutes four times tonight and only the fourth one was real."
HOOK_WALL = (
    "We had a show we watched together on Thursdays and I fell asleep through it "
    "twice and then a week got away from us and then we just didn't, and last "
    "Thursday I noticed the remote sitting on his side of the couch and figured "
    "out that he has been finishing it on his own for a while and never once "
    "mentioned it to me."
)


# ---------------------------------------------------------------------------
# hashtag cap
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("territory", cap.TERRITORIES)
def test_hashtags_respect_5_cap(territory):
    for index in range(10):
        tags = cap.generate_hashtags(territory, index=index)
        assert 1 <= len(tags) <= cap.MAX_HASHTAGS
        assert all(t.startswith("#") for t in tags)


def test_hashtags_vary_across_a_batch():
    """Rotating sets: consecutive indices should not all produce the same set."""
    sets = [tuple(cap.generate_hashtags("momguilt", index=i)) for i in range(6)]
    assert len(set(sets)) > 1


def test_validate_rejects_too_many_hashtags():
    reasons = cap.validate_atoms(
        HOOK_SHORT,
        "My son's Chromebook charger has its own zip code in the junk drawer now. What's yours like?",
        ["#a", "#b", "#c", "#d", "#e", "#f"],
        "Does anyone else deal with this",
    )
    assert any("hashtags" in r and "cap" in r for r in reasons)


# ---------------------------------------------------------------------------
# em-dash rejection
# ---------------------------------------------------------------------------
def test_has_em_dash_detects_unicode_em_dash():
    assert cap.has_em_dash("He stopped asking — and I never noticed.")


def test_has_em_dash_detects_double_hyphen():
    assert cap.has_em_dash("He stopped asking -- and I never noticed.")


def test_has_em_dash_false_on_clean_text():
    assert not cap.has_em_dash("He stopped asking, and I never noticed.")


def test_validate_rejects_em_dash_in_caption():
    caption = ("My son's Chromebook charger has its own zip code in the junk "
              "drawer now — anyone else's kitchen look like this?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Does this happen at your house")
    assert any("em dash" in r for r in reasons)


def test_validate_rejects_em_dash_in_self_comment():
    caption = ("My son's Chromebook charger has legit created its own zip code "
              "in the junk drawer. Anyone else dealing with this mess?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Wait — is this just us?")
    assert any("em dash" in r for r in reasons)


# ---------------------------------------------------------------------------
# caption-equals-hook rejection
# ---------------------------------------------------------------------------
def test_validate_rejects_caption_identical_to_hook():
    reasons = cap.validate_atoms(HOOK_SHORT, HOOK_SHORT, ["#momlife"], "Anyone else relate to this")
    assert any("character-identical" in r for r in reasons)


def test_validate_rejects_caption_identical_ignoring_case_and_punctuation():
    variant = HOOK_SHORT.upper().rstrip(".") + "!!!"
    reasons = cap.validate_atoms(HOOK_SHORT, variant, ["#momlife"], "Anyone else relate to this")
    assert any("character-identical" in r for r in reasons)


def test_validate_rejects_caption_that_merely_restates_hook():
    """Different casing/wrapper but same content words as the hook -> still a restate."""
    caption = ("Tonight I said five more minutes four different times and only "
              "the fourth time was actually real, if that makes sense. Anyone else?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Does this happen to you too")
    assert any("restates the hook" in r for r in reasons)


def test_validate_allows_caption_with_new_detail():
    caption = ("His dad is traveling again so I'm solo on bedtime duty this week. "
              "Not sure if that's normal. What's it like at your house?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Please tell me it gets easier")
    assert reasons == [] or "restates the hook" not in reasons


# ---------------------------------------------------------------------------
# emoji limits
# ---------------------------------------------------------------------------
def test_emoji_count_zero_on_plain_text():
    assert cap.emoji_count("Anyone else dealing with this tonight?") == 0


def test_emoji_count_detects_one_emoji():
    assert cap.emoji_count("Anyone else dealing with this tonight? \U0001F605") == 1


def test_validate_allows_one_emoji_in_caption():
    caption = ("His dad is traveling again so I'm solo on bedtime duty this week \U0001F605. "
              "Not sure if that's normal. What's it like at your house?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Please tell me it gets easier")
    assert not any("emoji" in r for r in reasons)


def test_validate_rejects_two_emoji_in_caption():
    caption = ("His dad is traveling again \U0001F605 so I'm solo on bedtime duty \U0001F62D this week. "
              "What's it like at your house?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Please tell me it gets easier")
    assert any("emoji" in r for r in reasons)


def test_validate_rejects_any_emoji_in_self_comment():
    caption = ("His dad is traveling again so I'm solo on bedtime duty this week. "
              "Not sure if that's normal. What's it like at your house?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Please tell me it gets easier \U0001F605")
    assert any("emoji" in r for r in reasons)


# ---------------------------------------------------------------------------
# gate integration (reuses copy/gates.py, does not reimplement it)
# ---------------------------------------------------------------------------
def test_validate_rejects_caption_that_fails_the_compliance_gate():
    caption = ("His test scores went up so much this month I could not believe "
              "it honestly. Anyone else notice this happening?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Does this happen to you too")
    assert any("gates.check(caption)" in r for r in reasons)


def test_validate_rejects_self_comment_that_fails_the_compliance_gate():
    caption = ("His dad is traveling again so I'm solo on bedtime duty this week. "
              "Not sure if that's normal. What's it like at your house?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "As a teacher I would say yes")
    assert any("gates.check(self_comment)" in r for r in reasons)


def test_validate_rejects_first_person_product_claim_in_caption():
    caption = ("I downloaded it last week and honestly it changed everything "
              "for us at bedtime. Anyone else tried something like this?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Does this happen to you too")
    assert any("gates.check(caption)" in r for r in reasons)


def test_validate_rejects_product_term_even_if_gate_would_pass():
    """gates.py doesn't know about product NAMES; captions.py's own product
    filter is the backstop for that (indirect-arm requirement)."""
    caption = ("Our new app has been such a lifesaver this week at bedtime "
              "honestly. Anyone else found something that works?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Does this happen to you too")
    assert any("app/product" in r for r in reasons)


# ---------------------------------------------------------------------------
# caption length window
# ---------------------------------------------------------------------------
def test_validate_rejects_too_short_caption():
    reasons = cap.validate_atoms(HOOK_SHORT, "Anyone else?", ["#momlife"], "Does this happen to you too")
    assert any("caption length" in r for r in reasons)


def test_validate_rejects_too_long_caption():
    caption = "This is a very long caption. " * 15 + "Anyone else dealing with this?"
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Does this happen to you too")
    assert any("caption length" in r for r in reasons)


# ---------------------------------------------------------------------------
# invitation requirement
# ---------------------------------------------------------------------------
def test_validate_rejects_caption_without_invitation():
    caption = ("His dad is traveling again so I'm solo on bedtime duty this week. "
              "Not sure if that's normal for us honestly at this point in time.")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Please tell me it gets easier")
    assert any("invitation" in r for r in reasons)


# ---------------------------------------------------------------------------
# self_comment word count + duplicate-of-caption
# ---------------------------------------------------------------------------
def test_validate_rejects_self_comment_too_short():
    caption = ("His dad is traveling again so I'm solo on bedtime duty this week. "
              "Not sure if that's normal. What's it like at your house?")
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "Yep")
    assert any("self_comment word count" in r for r in reasons)


def test_validate_rejects_self_comment_too_long():
    caption = ("His dad is traveling again so I'm solo on bedtime duty this week. "
              "Not sure if that's normal. What's it like at your house?")
    long_comment = "Is this a totally normal thing that happens at every single house in America or not"
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], long_comment)
    assert any("self_comment word count" in r for r in reasons)


def test_validate_rejects_self_comment_duplicating_caption_question():
    caption = "His dad is traveling again this week. What's it like at your house?"
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], "What's it like at your house?")
    assert any("duplicate" in r for r in reasons)


def test_validate_rejects_self_comment_identical_to_full_caption():
    caption = "His dad is traveling again this week. What's it like at your house?"
    reasons = cap.validate_atoms(HOOK_SHORT, caption, ["#momlife"], caption)
    assert any("duplicates the caption" in r for r in reasons)


# ---------------------------------------------------------------------------
# template fallback
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("territory", cap.TERRITORIES)
@pytest.mark.parametrize("density", cap.DENSITIES)
def test_template_fallback_passes_its_own_validation(territory, density):
    for index in range(6):
        atoms = cap.generate_atoms_template(HOOK_SHORT, territory, density, index=index)
        hashtags = cap.generate_hashtags(territory, index=index)
        reasons = cap.validate_atoms(HOOK_SHORT, atoms["caption"], hashtags,
                                     atoms["self_comment"])
        assert reasons == [], f"{territory}/{density}#{index}: {reasons}"


def test_template_fallback_passes_against_wall_hooks_too():
    for territory in cap.TERRITORIES:
        atoms = cap.generate_atoms_template(HOOK_WALL, territory, "wall", index=0)
        hashtags = cap.generate_hashtags(territory, index=0)
        reasons = cap.validate_atoms(HOOK_WALL, atoms["caption"], hashtags,
                                     atoms["self_comment"])
        assert reasons == [], reasons


def test_template_fallback_varies_by_index():
    a = cap.generate_atoms_template(HOOK_SHORT, "momguilt", "short", index=0)
    b = cap.generate_atoms_template(HOOK_SHORT, "momguilt", "short", index=1)
    assert a["caption"] != b["caption"]


def test_build_atoms_uses_template_when_llm_disabled():
    result = cap.build_atoms(HOOK_SHORT, "screentime", "short", index=0, use_llm=False)
    assert result["source"] == "template"
    assert result["attempts"] == 0
    assert 1 <= len(result["hashtags"]) <= cap.MAX_HASHTAGS


def test_build_atoms_falls_back_when_llm_always_errors(monkeypatch):
    def _boom(*a, **k):
        raise cap.CaptionGenError("simulated gateway outage")
    monkeypatch.setattr(cap, "generate_atoms_llm", _boom)
    result = cap.build_atoms(HOOK_SHORT, "benchmark", "short", index=2,
                             use_llm=True, max_attempts=2)
    assert result["source"] == "template"


def test_build_atoms_falls_back_when_llm_output_never_validates(monkeypatch):
    def _bad(*a, **k):
        return {"caption": "too short", "self_comment": "still too short"}
    monkeypatch.setattr(cap, "generate_atoms_llm", _bad)
    result = cap.build_atoms(HOOK_SHORT, "relationship", "short", index=1,
                             use_llm=True, max_attempts=2)
    assert result["source"] == "template"


def test_build_atoms_uses_llm_output_once_it_validates(monkeypatch):
    good_caption = ("His dad is traveling again so I'm solo on bedtime duty this week. "
                    "Not sure if that's normal. What's it like at your house?")
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise cap.CaptionGenError("simulated transient failure")
        return {"caption": good_caption, "self_comment": "Please tell me it gets easier"}

    monkeypatch.setattr(cap, "generate_atoms_llm", _flaky)
    result = cap.build_atoms(HOOK_SHORT, "screentime", "short", index=0,
                             use_llm=True, max_attempts=3)
    assert result["source"] == "llm"
    assert result["caption"] == good_caption
    assert result["attempts"] == 2


# ---------------------------------------------------------------------------
# no-network sanity: template pool itself never contains banned tokens
# ---------------------------------------------------------------------------
def test_template_pools_contain_no_product_terms():
    for pool in cap._TEMPLATE_DETAILS.values():
        for text in pool:
            assert not cap._PRODUCT_RE.search(text), text
    for text in cap._TEMPLATE_CONTEXT + cap._TEMPLATE_INVITES + cap._TEMPLATE_SELF_COMMENTS:
        assert not cap._PRODUCT_RE.search(text), text


# ---------------------------------------------------------------------------
# product_mentions: the gate and the prompt are two halves of ONE decision
# ---------------------------------------------------------------------------
# These exist because they were briefly out of sync. captions.product_re_for()
# was lifted for Ray while personas.voice_rules() still rendered "No product,
# app, brand or service mentioned or implied" into his prompt. Nothing failed —
# the gate simply stopped guarding a rule the prompt still asserted, which is the
# worst of both: no net, and no stated intent either.

def _rules(name):
    import personas
    return personas.voice_rules(personas.resolve(name))


def test_product_gate_and_prompt_agree_for_every_persona():
    """If the regex lets a class of term through, the rendered rules must say so
    (and vice versa). This is the assertion that would have caught the drift."""
    import personas
    for name in ("dani", "ray"):
        p = personas.resolve(name)
        rx = cap.product_re_for(p)
        rules = personas.voice_rules(p)
        blocks_ours = bool(rx and rx.search("smart fella"))
        says_blocked = "No product, app, brand or service mentioned or implied." in rules
        assert blocks_ours == says_blocked, (
            f"{name}: gate blocks our product = {blocks_ours}, but prompt says "
            f"total block = {says_blocked}. These must not disagree.")
        if not blocks_ours:
            assert "You may refer to" in rules and "Never name any OTHER" in rules, (
                f"{name}: product terms are unblocked but the prompt never says "
                f"what is allowed or that competitors still are not.")


def test_third_party_products_blocked_for_every_persona_always():
    """The one axis that is never lifted. Lifting 'our product' and 'any product'
    were treated as one switch once; they are not the same decision."""
    import personas
    for name in ("dani", "ray"):
        rx = cap.product_re_for(personas.resolve(name))
        for term in ("Khan Academy", "Duolingo", "Photomath", "ChatGPT"):
            assert rx and rx.search(term), f"{name} may name {term}"


def test_naming_our_product_is_opt_in_per_persona():
    """Dani was granted the same scope as Ray when the operator asked her to share
    the test, so the old "ray may / dani may not" assertion no longer describes the
    system. The invariant that survives is that the grant is PER PERSONA and absent
    by default: chloe has no product_mentions block and is still shut."""
    import personas
    caption = ("Smart Fella or Fart Smella is the app I would hand a kid instead "
               "of the one I took down. Try it and see what happens.")
    for name in ("ray", "dani"):
        bad = cap.validate_atoms(HOOK_SHORT, caption, ["#parenting"],
                                 "Ask them tonight", persona=personas.resolve(name))
        assert not [r for r in bad if "product" in r], (name, bad)
    ungranted = cap.validate_atoms(HOOK_SHORT, caption, ["#studytok"],
                                   "Ask them tonight", persona=personas.resolve("chloe"))
    assert any("app/product" in r for r in ungranted), ungranted


def test_naming_a_product_does_not_grant_testifying_about_it():
    """The two controls are independent and must stay that way. product_mentions
    lets a persona SAY the name; claim_posture governs whether she may claim to
    have USED it. Collapsing them would mean granting the name silently granted the
    testimonial, which is the 16 CFR 465.2 claim itself."""
    import personas, gates
    line = "I took the adult version of Smart Fella or Fart Smella last night."
    for name in ("ray", "dani"):
        assert cap.product_re_for(personas.resolve(name)).search("Smart Fella") is None
        ok, why = gates.check(line, "no-product")
        assert not ok and "product use" in why, (name, ok, why)
        assert gates.check(line, gates.DRAMATIZED)[0]


def test_ray_prompt_forbids_conflating_his_dead_app_with_ours():
    """Naming our product next to 'the app I built and pulled' invents an origin
    story for a real product. The exposure is created BY the lift, so the rule
    against it has to travel with the lift."""
    rules = _rules("ray").lower()
    assert "never conflated" in rules
    assert "never says he built ours" in rules


def test_stat_posture_still_refuses_to_share_a_line_with_a_product():
    """Unaffected by the lift, on purpose: a cited decline plus a product on one
    line reads as efficacy no matter who is allowed to say the product's name."""
    import gates
    ok, why = gates.check(
        "NAEP reading scores fell at the bottom. Smart Fella is the app for that.",
        claim_posture=gates.CITED_STAT)
    assert not ok and "product referent" in why, (ok, why)
