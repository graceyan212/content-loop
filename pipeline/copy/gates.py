#!/usr/bin/env python3
"""
gates.py — the deterministic compliance gate. No model calls, no network.

Runs BEFORE render. A hook that fails never becomes an MP4.

The binding rule is 16 CFR 465.2 (see
docs/superpowers/specs/2026-07-27-danielle-pipeline-design.md, "Compliance
floor"): Dani does not exist, so a first-person claim that she used the
product is the centre of the rule, not its edge. Plus the SFFS brand guardrail:
never claim the app makes kids smarter / boosts IQ.

Public API:
    passed, reason = check(hook_text, claim_posture="no-product")

`reason` is "" when passed is True, otherwise a short human-readable string
naming the rule and the offending span.
"""

from __future__ import annotations

import re
import sys

# Postures under which a first-person product-use claim is permitted, because
# the video then carries an on-screen "Dramatization" tag (overlay.py draws it).
DRAMATIZED = "dramatized-labeled"

# Posture for a THIRD-PARTY POPULATION STATISTIC — added for Ray Kessler, whose
# defensible ground is public education data (NAEP declines, reading proficiency).
#
# WHY IT WAS NEEDED. Rules 1a/1b/2 below ban the surface FORM of a number: "%",
# "test scores", digit-plus-"points". They exist to stop an efficacy claim — a
# synthetic persona saying our product moved a number. They cannot tell that claim
# apart from a cited population statistic, because the discriminating features
# (is a product implicated, is the subject a child or a population, is there a
# source) appear in none of the patterns. Measured before this posture existed:
# NO posture, including dramatized-labeled, let a statistic through.
#
# WHY IT IS SAFE. Three conditions, all required:
#   1. the caller must ask for this posture explicitly;
#   2. the SAME text must carry an attribution (_ATTRIBUTION) — not a flag set
#      somewhere else, the visible line itself, because the line is what renders;
#   3. the text must contain no product referent (_PRODUCT_REFERENT).
# And the relaxation is partial: iq / smarter / grade level / percentile /
# guaranteed stay banned under every posture. See _BANNED_TOKENS_ALWAYS.
CITED_STAT = "cited-population-stat"

CLAIM_POSTURES = ("no-product", "named-in-text", DRAMATIZED, "second-person",
                  CITED_STAT)

# Apostrophe-ish characters stripped during normalization so that "we've" and
# "weve" are the same token to the gate. The hooks are written without
# apostrophes by house style, but a generated hook may not be.
_APOSTROPHES = "'’ʼ‘`"


def normalize(text: str) -> str:
    """Lowercase, strip apostrophes, collapse whitespace. Matching happens here."""
    t = (text or "").lower()
    for ch in _APOSTROPHES:
        t = t.replace(ch, "")
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# Rule 1 — banned quantified-efficacy tokens
# ---------------------------------------------------------------------------
# Word-boundary anchored on purpose:
#   \biq\b        must not fire on "unique"
#   \bsmarter\b   must not fire on "smart kids" (a legal, in-voice phrase)
#   \bgrade level\b must not fire on "fifth grade"
#
# Split into ALWAYS and STAT_RELAXABLE. Only "test scores" relaxes under
# CITED_STAT, and the choice of what stays banned is deliberate:
#   iq         the Lumosity token. The brand guardrail names it explicitly.
#   smarter    the SFFS brand guardrail's only enforcement point.
#   guaranteed never appropriate in any posture.
#   grade level / percentile
#              these read as measurements OF A CHILD ("reading two grade levels
#              behind", "in the 30th percentile"). A persona that speaks in the
#              second person is one word away from turning a population figure
#              into a claim about the viewer's kid, and the gate cannot see the
#              difference. NAEP's public framing uses proficiency levels and
#              scale scores, so nothing defensible is lost by keeping these shut.
_BANNED_TOKENS_ALWAYS = [
    (r"\biq\b", "iq"),
    # levels? — the plural used to walk straight through. "two grade levels
    # behind" is the form this phrase actually takes in the wild, and it is the
    # exact shape of a measured-child claim, so the singular-only pattern was
    # banning the rarer half of the risk. Found while testing the stat posture.
    (r"\bgrade levels?\b", "grade level"),
    # "reading level" is the same measured-child claim wearing a different noun, and
    # "went up two grades" / "ahead two grades" is the same claim with the noun
    # removed entirely. Measured: "his reading level went up two grades" passed every
    # posture, including no-product, because the banned list only knew the exact
    # phrase "grade level".
    (r"\b(?:reading|math|maths|writing|comprehension)\s+level\b", "reading/math level"),
    (r"\b(?:up|ahead|behind|gained|jumped|advanced)\s+(?:\w+\s+){0,2}?grades?\b",
     "grade advancement"),
    (r"\b(?:above|below|at)\s+grade\b", "above/below grade"),
    # the modifier can also FOLLOW the noun: "reading two grades ahead".
    (r"\bgrades?\s+(?:ahead|behind|above|below)\b", "grades ahead/behind"),
    (r"\bsmarter\b", "smarter"),
    (r"\bpercentiles?\b", "percentile"),
    (r"\bguarantee(?:d|s)?\b", "guaranteed"),
]
_BANNED_TOKENS_STAT_RELAXABLE = [
    (r"\btest scores?\b", "test score(s)"),
]
# Union, preserved under the original name so existing callers/tests still work.
_BANNED_TOKENS = _BANNED_TOKENS_ALWAYS + _BANNED_TOKENS_STAT_RELAXABLE

# Rule 2 — any digit immediately quantifying a result.
_QUANTIFIED = [
    (r"\d\s*%", "a digit followed by %"),
    (r"\d\s*(?:percent|percentage)\b", 'a digit followed by "percent"'),
    (r"\d\s*points?\b", 'a digit followed by "points"'),
]

# An ALLOWLIST, on purpose. A generic pattern like r"\bper \w" would match "per
# week" and "per kid" and hand the relaxation to any line with a stray "per" in
# it. Requiring a named source means adding one here is a deliberate act by
# someone who checked that the source is real.
_ATTRIBUTION = [
    r"\bnaep\b",
    r"\bnations report card\b",          # normalize() strips the apostrophe
    r"\baccording to \w",
    r"\bsource:",
]

# If any of these appear, CITED_STAT does not apply however good the attribution
# is. This is what stops "the app raised test scores, per NAEP" — a sentence that
# trips none of the first-person patterns in Rule 3 and would otherwise sail
# through on a real citation.
_PRODUCT_REFERENT = r"\b(?:app|apps|the game|the program|the subscription|download(?:ed|s)?)\b"

# ---------------------------------------------------------------------------
# Rule 3 — first-person product-use language
# ---------------------------------------------------------------------------
# These only fail when claim_posture != dramatized-labeled.
#
# The rule exists to catch 16 CFR 465.2 exposure: a synthetic persona claiming
# she acquired or used THE PRODUCT. It must not fire on ordinary life.
#
# The bare verbs used to match anything: "store bought", "I bought bread",
# "we use paper plates" all failed, which rejected large amounts of innocent
# grocery and household copy once the account moved to community content.
# So each pattern now requires a first-person subject AND a referent that could
# plausibly be the product. Up to two filler words are allowed between them so
# "we finally just downloaded it" still trips.
_PRODUCT_OBJ = r"(?:it|this|that|the app|the game|the program|the subscription)"
_PRODUCT_USE = [
    (rf"\b(?:i|we)\s+(?:\w+\s+){{0,2}}(?:bought|purchased|paid for)\s+{_PRODUCT_OBJ}\b",
     "bought it"),
    (rf"\b(?:i|we)\s+(?:\w+\s+){{0,2}}downloaded\b", "downloaded"),
    (rf"\b(?:i|we)\s+(?:\w+\s+){{0,2}}subscribed\b", "subscribed"),
    (rf"\bwe\s+(?:\w+\s+){{0,2}}use\s+{_PRODUCT_OBJ}\b", "we use it"),
    (r"\bweve been using\b", "weve been using"),
    (r"\btried it\b", "tried it"),
    # "took it" / "taking it" — an ASSESSMENT is used, not downloaded, so none of the
    # acquisition verbs above fired. Measured: "I found this and I took it and I was
    # appalled by my score. Letting my kids take it tonight" passed EVERY posture
    # including no-product. That is the single most exposed sentence this account
    # could publish (16 CFR 465.2: a testimonial by someone who does not exist,
    # about the operator's own product, reporting a result that never happened)
    # and the gate waved it through.
    # The article must NOT sit directly against the noun: "I took the adult version"
    # is how the first live product post actually phrased it, and requiring `the
    # <noun>` missed it. Up to two filler words, and `version` counts as the noun
    # because this product ships an adult version and a version per grade.
    (rf"\b(?:i|we)\s+(?:\w+\s+){{0,2}}(?:took|take|taking)\s+"
     rf"(?:(?:the|this|that|a|an|my|our)\s+)?(?:\w+\s+){{0,2}}?"
     rf"(?:quiz|test|assessment|screener|benchmark|version)\b", "took the assessment"),
    # The BARE-PRONOUN forms of the same claim. The noun list above needs an
    # explicit test-ish word, so "I took it" and "I did mine" both walked through
    # -- and those are how the copy actually phrases it once the product has
    # already been named earlier in the same post. `tried it` above already sets
    # the precedent that a bare "it" is enough when the verb is this specific.
    # NOT followed by a particle. "I took it down" is Ray's entire premise -- the
    # app he built and pulled -- and it appears across his whole library; "took it
    # out / back / home / to the office" is ordinary life. Without this exclusion
    # the pattern blocks the persona's central sentence.
    (r"\b(?:i|we)\s+(?:\w+\s+){0,2}?took\s+it\b"
     r"(?!\s+(?:down|out|back|home|off|away|apart|over|up|to|with|for|from|in|on))",
     "took it"),
    (r"\b(?:i|we)\s+(?:\w+\s+){0,2}?(?:did|took|finished)\s+mine\b", "did mine"),
    # a fabricated RESULT is its own claim, even without a use verb.
    (r"\bmy (?:score|result|results)\b", "my score/result"),
    (r"\b(?:scored|i got)\s+(?:a\s+)?\d", "reported a score"),
]


# A child taking/using an assessment is Rule 3b territory but the noun differs.
_CHILD_ASSESSMENT = [
    (rf"\b(?:letting|let|had|having|make|making)\s+(?:my |the )?"
     rf"(?:kids?|son|daughter|owen|maya)\s+(?:\w+\s+){{0,2}}?"
     rf"(?:take|takes|taking|try|tries|trying|do|doing)\s+"
     rf"(?:it|this|that|the quiz|the test|the assessment)\b",
     "child made to use the product"),
]

# ---------------------------------------------------------------------------
# Rule 3b — a CHILD is claimed to have used or liked the product.
# ---------------------------------------------------------------------------
# Rule 3 only guards first-person subjects (i / we). It let this straight through:
#     "Owen has been obsessed with this app all week"
#     "things my kids have been loving: the brain games app"
# That is the same 16 CFR 465.2 exposure and arguably worse. Dani does not exist, so
# neither does Owen, so a line attesting that he used and enjoyed the product is a
# fabricated consumer testimonial — and "it helped him" edges into an efficacy claim
# about a child, which the brand guardrail bans outright.
#
# It requires an EXPLICIT product noun, never a bare "it". "He plays it every night"
# is far more likely to be soccer than software, and the earlier version of Rule 3
# had to be narrowed once already for exactly this reason: bare verbs rejected large
# amounts of innocent grocery and household copy.
_CHILD_SUBJECT = r"(?:owen|maya|my (?:kid|kids|son|daughter)|the kids|my youngest|my oldest)"
# SPLIT BY AMBIGUITY. "app" in mom copy is almost always software, so it trips on its
# own. "game" is not: a soccer game, a board game, a game with his friends. Requiring a
# demonstrative or definite article for `game` keeps "loves the game" / "that game"
# while letting "Owen plays a game with his friends after school" through — which the
# undifferentiated version rejected. Same lesson the original Rule 3 comment records:
# bare verbs and bare nouns reject large amounts of innocent household copy.
_PRODUCT_NOUN = (r"(?:app|apps|program|subscription|test|quiz|assessment|version|versions"
                 r"|(?:the|this|that)\s+game)")
_CHILD_PRODUCT_USE = [
    (rf"\b{_CHILD_SUBJECT}\b(?:\W+\w+){{0,6}}?\W+(?:loves?|loved|loving|obsessed|"
     rf"plays?|playing|played|uses?|using|used|into|hooked|"
     rf"took|take|takes|taking|doing|did|does)\b(?:\W+\w+){{0,5}}?\W+"
     rf"(?:the |this |that |a |an )?{_PRODUCT_NOUN}\b",
     "child used/liked the product"),
    (rf"\b(?:loves?|loved|loving|obsessed with|playing|hooked on)\b(?:\W+\w+){{0,3}}?"
     rf"\W+(?:the |this |that )?{_PRODUCT_NOUN}\b(?:\W+\w+){{0,6}}?\W+{_CHILD_SUBJECT}\b",
     "child used/liked the product"),
    # product ... child ... verb, which is the order the "cool things I found this
    # week: an app Owen loves" framing produces and which the two patterns above miss.
    (rf"\b{_PRODUCT_NOUN}\b(?:\W+\w+){{0,4}}?\W+{_CHILD_SUBJECT}\b"
     rf"(?:\W+\w+){{0,3}}?\W+(?:loves?|loved|uses?|plays?|is into|is hooked)\b",
     "child used/liked the product"),
]

# A REPORTED RESULT for a child — a rank, a tier, a label the product assigned.
#
# Measured hole: "Owen thought it was funny he got ranked a fart fella but now I am a
# bit concerned" passed every posture. Rule 3b wants a use-verb plus a product noun and
# this sentence has neither, yet it is the most exposed line the account could publish:
# a fabricated assessment OUTCOME for a child who does not exist, written so that real
# parents worry about their own. That is 16 CFR 465.2 and an efficacy claim at once.
#
# "scored" is deliberately absent — "he scored a goal" is ordinary Saturday copy. The
# ranking verbs below have no common innocent reading next to a child.
# "came out AS" only. Bare "came out" caught "Maya came out of her room for
# approximately four minutes", which is just a Tuesday.
_RANK_VERB = r"(?:ranked|rated|came out as|tested as|classified as|labell?ed as|placed as)"
_CHILD_RESULT = [
    (rf"\b{_CHILD_SUBJECT}\b(?:\W+\w+){{0,8}}?\W+{_RANK_VERB}\b",
     "reported a rank/result for a child"),
    (rf"\b{_RANK_VERB}\b(?:\W+\w+){{0,6}}?\W+{_CHILD_SUBJECT}\b",
     "reported a rank/result for a child"),
    # FIRST PERSON + a rank verb. "I got ranked a fart smella" is Dani reporting her
    # own result and was passing: Rule 3 covers acquisition verbs and "my score", not
    # being ranked. Same exposure as the child version.
    # "rated" is excluded for the first-person form: "I rated that dinner a success"
    # is ordinary speech. The remaining verbs have no such reading about oneself.
    (r"\b(?:i|we)\b(?:\W+\w+){0,4}?\W+(?:ranked|came out as|tested as|classified as"
     r"|labell?ed as|placed as)\b", "reported a rank for herself"),
    # NOTE: the bare tier names ("a smart fella", "a fart smella") are deliberately NOT
    # banned. They are a public fact about what the quiz outputs, and blocking them
    # stopped a legitimate line — "there is a thing that tells you if you are a smart
    # fella or a fart smella" — which claims no result for anybody. What is banned is a
    # tier attached to a PERSON via the patterns above.
    # subject-free, because these have no innocent reading in this account's voice
    # and the subject is often a pronoun the child-subject list cannot see.
    (r"\b(?:tested as|classified as|placed as)\b", "an assessment outcome"),
]

# ---------------------------------------------------------------------------
# Rule 4 — fabricated credential, i.e. the word used as a SELF-description.
# ---------------------------------------------------------------------------
# "his teacher ended the email" must PASS; "as a teacher" must FAIL. So the
# credential noun is only banned when it sits behind a first-person copula or
# an "as a/an" appositive. Up to two filler words are allowed between so that
# "as a former teacher" and "im a licensed child psychologist" are caught.
_CREDENTIALS = r"(doctors?|therapists?|teachers?|psychologists?)"
_CREDENTIAL_PATTERNS = [
    (rf"\bas an?\s+(?:\w+\s+){{0,2}}?{_CREDENTIALS}\b", "as a <credential>"),
    (rf"\b(?:i am|im)\s+an?\s+(?:\w+\s+){{0,2}}?{_CREDENTIALS}\b", "i am a <credential>"),
    (rf"\bspeaking as an?\s+(?:\w+\s+){{0,2}}?{_CREDENTIALS}\b", "speaking as a <credential>"),
    (rf"\bmy (?:job|day job) is (?:being )?an?\s+(?:\w+\s+){{0,2}}?{_CREDENTIALS}\b",
     "my job is a <credential>"),
]



# ---------------------------------------------------------------------------
# Rule 5 — causal admit claim.
# ---------------------------------------------------------------------------
# persona.json sets no_causal_admit_claims for Chloe, and nothing enforced it:
# "what got me into stanford" and "this is why i got in" both passed.
#
# The banned thing is attributing the OUTCOME to an action or a product.
# Being a student somewhere is canon and must still pass, so this fires only on
# an explicit causal link, never on a bare identity statement. The source corpus
# makes the same argument on the merits: causation here is untraceable, and a
# former admissions officer says so outright about "the essays that got me in".
#
# MUST PASS: "i go to stanford", "i got rejected", "what they write when they
#            say no", "leave activities blank"
# MUST FAIL: "what got me into stanford", "this is why i got in",
#            "the essay that got me accepted"
_ADMIT = r"(?:in|into|accepted|admitted)"
_CAUSAL_ADMIT = [
    (rf"\bgot me {_ADMIT}\b", "got me in"),
    (rf"\bgot her {_ADMIT}\b", "got her in"),
    (rf"\b(?:why|how|what)\s+(?:\w+\s+){{0,3}}?i got {_ADMIT}\b", "why/how i got in"),
    (rf"\bthats (?:why|how) i got {_ADMIT}\b", "thats why i got in"),
    (rf"\bthe reason i got {_ADMIT}\b", "the reason i got in"),
    (rf"\b(?:secret|trick|formula) to getting {_ADMIT}\b", "secret to getting in"),
]

def _first_hit(norm: str, patterns):
    for pat, label in patterns:
        m = re.search(pat, norm)
        if m:
            return label, m.group(0).strip()
    return None, None


def _stat_relaxation(norm: str, claim_posture: str) -> tuple[bool, str]:
    """
    Does CITED_STAT apply to this text? Returns (applies, why_not).

    `why_not` is non-empty only for a NEAR MISS: the caller asked for the posture
    and did not earn it. It gets appended to whatever rule then rejects the line,
    so the writer is told "there is no source on this line" rather than the bare
    quantified-efficacy message, which would send them off fixing the wrong thing.
    """
    if claim_posture != CITED_STAT:
        return False, ""

    m = re.search(_PRODUCT_REFERENT, norm)
    if m:
        return False, (f'cited-population-stat refused: this line names a product '
                       f'referent ("{m.group(0)}"), and a statistic must not share '
                       f'a line with one')

    if not any(re.search(p, norm) for p in _ATTRIBUTION):
        return False, ('cited-population-stat refused: no source named in this line. '
                       'The attribution has to be IN the text, because the text is '
                       'what renders on the slide')

    return True, ""


def check(hook_text: str, claim_posture: str = "no-product") -> tuple[bool, str]:
    """
    Return (passed, reason).

    reason is "" on pass. On fail it names the rule and quotes the match, so
    the batch log says *why* a hook was dropped without a second lookup.
    """
    norm = normalize(hook_text)
    if not norm:
        return False, "empty hook"

    # Rule 5 — causal admit claim. Checked first: it is the highest-stakes rule
    # in Chloe's brief and the cheapest to get wrong.
    label, hit = _first_hit(norm, _CAUSAL_ADMIT)
    if label:
        return False, (f'causal admit claim: "{label}" (matched "{hit}") — the '
                       f'outcome is never attributed to an action')

    # A product referent on a line the caller DECLARED to be a cited statistic is
    # an outright rejection, not merely a failed relaxation.
    #
    # This was a hole. _PRODUCT_REFERENT was only ever consulted as a CONDITION on
    # the relaxation, so it could only reject a line that already tripped a number
    # rule. "NAEP reading scores fell at the bottom. Smart Fella is the app for
    # that." trips nothing — "reading scores" is not "test scores", there is no
    # digit and no percent — so it passed, under this posture and every other one.
    # The comment on _PRODUCT_REFERENT claims it stops "the app raised test scores,
    # per NAEP", and it does, but only because "test scores" happens to be a banned
    # token. Drop the token and the same sentence walked through.
    if claim_posture == CITED_STAT:
        m = re.search(_PRODUCT_REFERENT, norm)
        if m:
            return False, (f'cited-population-stat: this line names a product referent '
                           f'("{m.group(0)}"). A population statistic must not share a '
                           f'line with a product, because the pairing reads as an '
                           f'efficacy claim however well the statistic is sourced')

    # Does the cited-population-stat relaxation apply? Computed once, consulted by
    # rules 1a / 1b-relaxable / 2 below. Every other rule ignores it.
    stat_ok, stat_why = _stat_relaxation(norm, claim_posture)

    def deny(msg: str) -> tuple[bool, str]:
        """Reject, appending the near-miss reason when the caller asked for
        CITED_STAT and did not qualify."""
        return False, (f"{msg}. {stat_why}" if stat_why else msg)

    # Rule 1a — a bare percent sign anywhere, digits or not.
    if "%" in norm and not stat_ok:
        return deny('quantified-efficacy: contains a "%" character')

    # Rule 1b — banned tokens. The always-banned set is checked first and is not
    # subject to any posture; only then the set CITED_STAT can relax.
    label, hit = _first_hit(norm, _BANNED_TOKENS_ALWAYS)
    if label:
        return False, f'quantified-efficacy: banned token "{label}" (matched "{hit}")'
    if not stat_ok:
        label, hit = _first_hit(norm, _BANNED_TOKENS_STAT_RELAXABLE)
        if label:
            return deny(f'quantified-efficacy: banned token "{label}" (matched "{hit}")')

    # Rule 2 — digit-quantified results.
    if not stat_ok:
        label, hit = _first_hit(norm, _QUANTIFIED)
        if label:
            return deny(f'quantified-efficacy: {label} (matched "{hit}")')

    # Rule 3 — first-person product use, exempt only under dramatized-labeled.
    label, hit = _first_hit(norm, _PRODUCT_USE)
    if label and claim_posture != DRAMATIZED:
        return False, (f'first-person product use: "{label}" (matched "{hit}") '
                       f'under claim_posture={claim_posture!r}; '
                       f'only {DRAMATIZED!r} exempts this')

    # Rule 3b — a child is claimed to have used or liked the product.
    label, hit = _first_hit(norm, _CHILD_PRODUCT_USE)
    if label and claim_posture != DRAMATIZED:
        return False, (f'{label}: matched "{hit}" under '
                       f'claim_posture={claim_posture!r}. A synthetic persona cannot '
                       f'attest that her child used or enjoyed the product '
                       f'(16 CFR 465.2); only {DRAMATIZED!r} exempts this')

    # Rule 3d — a rank / tier / result reported for a child.
    label, hit = _first_hit(norm, _CHILD_RESULT)
    if label and claim_posture != DRAMATIZED:
        return False, (f'{label}: matched "{hit}" under claim_posture={claim_posture!r}. '
                       f'A synthetic persona cannot report an assessment outcome for '
                       f'her child (16 CFR 465.2 + the no-measured-claims floor)')

    # Rule 3c — a child is put through the product.
    label, hit = _first_hit(norm, _CHILD_ASSESSMENT)
    if label and claim_posture != DRAMATIZED:
        return False, (f'{label}: matched "{hit}" under claim_posture={claim_posture!r}. '
                       f'A synthetic persona cannot report putting her child through '
                       f'the product (16 CFR 465.2)')

    # Rule 4 — fabricated credential used as self-description.
    label, hit = _first_hit(norm, _CREDENTIAL_PATTERNS)
    if label:
        return False, f'fabricated credential: {label} (matched "{hit}")'

    return True, ""


def main(argv=None):
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Run the compliance gate over a hook.")
    ap.add_argument("--text", required=True)
    ap.add_argument("--claim-posture", default="no-product", choices=CLAIM_POSTURES)
    a = ap.parse_args(argv)
    passed, reason = check(a.text, a.claim_posture)
    print(json.dumps({"passed": passed, "reason": reason}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
