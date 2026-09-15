#!/usr/bin/env python3
"""
captions.py — the second copy atom. Given a hook (the on-screen text, already
written), produce the two atoms that ship alongside it:

    caption       — the description under the post. Adds a concrete detail the
                    hook does NOT have, and ends with an explicit invitation to
                    reply. Never restates the hook.
    hashtags      — 4-5 tags, drawn from rotating sets (this module's own
                    rotation, not the LLM's invention) so they vary across a
                    batch. Instagram hard-caps at 5.
    self_comment  — 3-12 words, a question or aside Dani leaves on her own
                    post to seed engagement. Not a duplicate of the caption's
                    question.

Generation path
----------------
Live LLM first, via the same TrueFoundry gateway the rest of the project uses
($ANTHROPIC_BASE_URL / $ANTHROPIC_AUTH_TOKEN, OpenAI-compatible
`/v1/chat/completions`, confirmed live 2026-07-29 against
`aws-bedrock/global.anthropic.claude-sonnet-4-6`). On any failure — network,
auth, malformed JSON, or a gate/rule rejection repeated across
CAPTION_MAX_ATTEMPTS tries — this falls back to a deterministic template that
is constructed to satisfy every rule below by construction. `build_atoms()`
never raises for a hook the caller has already gate-checked; check
`result["source"]` ("llm" | "template") to see which path actually ran.

Compliance
----------
Every generated caption AND self_comment is run through `copy/gates.py`
(the same deterministic compliance floor the hooks use — no reimplementation),
plus caption-specific rules enforced here:

  - no em dashes
  - at most 5 hashtags
  - no mention of any app or product (this is the indirect arm)
  - caption is not character-identical to the hook
  - caption must not simply restate the hook (content-word overlap gate)
  - caption must contain an explicit invitation to reply
  - at most 1 emoji in the caption, 0 in the self_comment
  - self_comment is not a duplicate of the caption's question/invitation

Public API
----------
    build_atoms(hook_text, territory, density, *, index=0,
                claim_posture="no-product", use_llm=True,
                max_attempts=3) -> dict

CLI:
    python captions.py --text "..." --territory momguilt --density short
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gates  # noqa: E402  (copy/gates.py — the deterministic compliance gate, reused not reimplemented)

TERRITORIES = ("relationship", "momguilt", "screentime", "benchmark")
DENSITIES = ("short", "wall")

CAPTION_MIN_CHARS = 120
CAPTION_MAX_CHARS = 220
MAX_HASHTAGS = 5
SELF_COMMENT_MIN_WORDS = 3
SELF_COMMENT_MAX_WORDS = 12

# The model this project has confirmed live against the TrueFoundry gateway
# (see docs/superpowers/specs/2026-07-27-danielle-pipeline-design.md and
# golf-slm/caddie/spec.py's JUDGE_MODEL for prior art). Overridable so a
# different gateway model can be swapped in without touching call sites.
DEFAULT_MODEL = os.environ.get("CAPTIONS_MODEL", "aws-bedrock/global.anthropic.claude-sonnet-4-6")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ===========================================================================
# voice / persona — ground truth is the live posts, not the drafts
#
# PERSONA_BRIEF is Dani's brief and the DEFAULT used when a caller does not
# pass an explicit `persona_brief` (i.e. every existing call site, and every
# existing test). A persona-aware caller (batch.py) instead passes
# `personas.brief(persona)`, which reads the brief out of that persona's own
# persona.json and raises rather than returning anything if that persona has
# none configured — so a personaless persona can never fall through to
# Dani's voice here.
# ===========================================================================
PERSONA_BRIEF = """\
You write as Dani Foster: 38, Frisco TX, marketing ops manager, husband
travels for work. Kids: Owen (12, 7th grade), Maya (15, 10th grade).

Her real live posts read:
  "Intro post! Hi I'm Dani and this is my first time on TikTok. So excited to meet y'all!"
  "Starting a tiktok at 38 because I have thoughts and nobody at home is listening."
  "What are your Tiktok secrets?"
  "Why does it say I'm AI??"

Voice: sentence case, full punctuation, exclamation points are fine, casual
millennial mom, mildly self-deprecating. Emoji are allowed in captions
(never in on-screen text) but keep it to at most one.
"""

CAPTION_RULES = """\
Write the CAPTION (the description under the post) for the given on-screen
hook text. The caption is a SEPARATE piece of writing from the hook, not a
restatement of it.

Hard rules:
- 120-220 characters, not counting hashtags (hashtags are handled separately —
  do not include any "#" in your output).
- Add ONE concrete, specific detail that is NOT already in the hook text.
- End with an explicit, direct invitation for people to reply (a real
  question, or a clear ask like "tell me I'm not the only one").
- Do NOT restate or rephrase the hook. If your caption could be read as a
  paraphrase of the hook, rewrite it.
- Never mention any app, product, download, subscription, or similar.

Also write a SELF_COMMENT: a short comment (3-12 words) the persona would leave
on their own post to invite replies — a question or aside, distinct from the
caption's own question, no emoji, subject to every rule above and every rule in
THIS PERSONA'S RULES below.

Respond with a single JSON object and nothing else, in exactly this shape:
{"caption": "...", "self_comment": "..."}
"""


def _build_messages(hook_text: str, territory: str, density: str,
                    persona_brief: str = PERSONA_BRIEF,
                    claim_posture: str = "no-product",
                    persona: dict | None = None) -> list[dict]:
    # THIS PERSONA'S RULES, from personas.voice_rules — the same block the hook
    # generator uses. Without it the caption model only ever saw CAPTION_RULES,
    # which was Dani's ("no measured claim about Owen or Maya"), so Ray's
    # hard_rules steered his hooks and never his captions.
    persona_rules = ""
    if persona is not None:
        try:
            import personas as _p
            rendered = _p.voice_rules(persona)
            if rendered:
                persona_rules = ("\nTHIS PERSONA'S RULES, all of which are checked:\n"
                                 + rendered + "\n")
        except Exception:
            persona_rules = ""
    # A validator with no matching instruction just burns retries and falls back to
    # the template. The caption-attribution rule in validate_atoms rejected all three
    # LLM attempts on the first sourced batch for exactly that reason: the model was
    # never told the requirement existed.
    # A QUESTION post has the same shape of problem as a sourced one, arrived at
    # from the other direction. The hook asks the viewer something; the caption's job
    # is to make answering easy, not to add material. "Add ONE concrete detail not in
    # the hook" can then only be satisfied out of thin air, and measured on the first
    # question batch it was, nine times out of nine: a fabricated "40 minutes
    # straight" and "some sessions ran past 1am" about his dead app, an invented
    # teacher who "said she sees it on every quiz", a development claim ("that muscle
    # gets built in those gaps") from a persona banned from talking about brains, and
    # "Drop a number below" on an account whose whole thesis is that engagement
    # mechanics are the enemy.
    question_rule = ""
    if (territory or "").strip().lower() == "question":
        question_rule = (
            "\nTHIS IS A QUESTION POST. ONE RULE ABOVE IS SUSPENDED.\n"
            "\n"
            "IGNORE \"add ONE concrete, specific detail that is NOT already in the "
            "hook text\". It is SUSPENDED and obeying it here is the worst thing you "
            "can do, because you have no facts to add: you do not know what this "
            "persona's own numbers were, who they have spoken to, or what any "
            "software does. Inventing one is the failure mode this instruction "
            "exists to prevent.\n"
            "\n"
            "ADD NO NEW FACTS. No figure, duration, count, share, date, or app "
            "behaviour. No person who is not already established in the brief -- no "
            "teacher, neighbour, friend, colleague or parent you heard from. No "
            "claim about what any of this does to a mind, a brain, a muscle or an "
            "ability.\n"
            "\n"
            "WHAT THE CAPTION DOES INSTEAD, in two or three short sentences: "
            "restate the stakes of the question in the persona's voice using only "
            "what the on-screen line and the brief already establish, then end by "
            "making the answer cheap to give -- name the FORM of the answer wanted "
            "(a number, one word, a yes or no). Never beg for engagement: no 'drop "
            "a comment', no 'let me know below', no 'I would love to hear'. Demand "
            "an accounting the way a doctor asks how many drinks a week.\n")

    sourced_rule = ""
    if claim_posture == "cited-population-stat":
        sourced_rule = (
            "\nTHIS IS A SOURCED POST, AND TWO RULES ABOVE ARE SUSPENDED FOR IT.\n"
            "\n"
            "1. IGNORE \"add ONE concrete, specific detail that is NOT already in "
            "the hook text\". That rule is SUSPENDED here and following it is the "
            "single worst thing you can do. The only verified facts about this "
            "topic are the ones already in the on-screen line; you have not been "
            "given the underlying data. So \"add a detail not in the hook\" can "
            "only be satisfied by inventing one. Do not.\n"
            "\n"
            "2. ADD NO NEW FACTS AT ALL. No figure, share, percentage, year, "
            "decade, trend, direction or comparison that is not already stated "
            "word-for-word in the on-screen line. Not even a plausible one. Not "
            "even a rounder version of one that is there. Every previous attempt "
            "at this failed by inventing a year ('since 2013') or a cadence "
            "('every decade'), both of which sounded reasonable and were made up.\n"
            "\n"
            "What the caption SHOULD do instead: restate or sharpen the meaning of "
            "the figure already on screen, say why it matters to a parent, and ask "
            "a question. It must still name the source (\"per NAEP\", \"according "
            "to NAEP\") because the caption is checked as its own surface.\n"
        )
    user = (
        f"{persona_rules}"
        f"{sourced_rule}"
        f"{question_rule}"
        f"Territory: {territory}\n"
        f"Density: {density}\n"
        f"On-screen hook text (already finished, do not change or repeat it "
        f"verbatim in the caption):\n{hook_text!r}\n\n"
        f"{CAPTION_RULES}"
    )
    return [
        {"role": "system", "content": persona_brief},
        {"role": "user", "content": user},
    ]


# ===========================================================================
# LLM path
# ===========================================================================
_client = None


def _get_client():
    """Lazily build the OpenAI-compatible client against the TrueFoundry
    gateway. Returns None (never raises) if the SDK or env vars are missing,
    so callers can fall through to the template path cleanly."""
    global _client
    if _client is not None:
        return _client
    try:
        from openai import OpenAI
    except ImportError:
        log("WARN: `openai` package not installed; captions will use the template fallback")
        return None
    base = os.environ.get("ANTHROPIC_BASE_URL")
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not base or not key:
        log("WARN: ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN not set; "
            "captions will use the template fallback")
        return None
    _client = OpenAI(base_url=base.rstrip("/") + "/v1", api_key=key,
                      timeout=60.0, max_retries=0)
    return _client


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    """Defensive JSON extraction: strip code fences, grab the outermost {...}."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    m = _JSON_OBJ.search(t)
    if not m:
        raise ValueError(f"no JSON object found in model output: {text!r}")
    return json.loads(m.group(0))


class CaptionGenError(Exception):
    """LLM call or parse failed. Caller retries or falls back."""


def generate_atoms_llm(hook_text: str, territory: str, density: str,
                       model: str = DEFAULT_MODEL,
                       persona_brief: str = PERSONA_BRIEF,
                       claim_posture: str = "no-product",
                       persona: dict | None = None) -> dict:
    """One LLM call -> {"caption": str, "self_comment": str}. Raises
    CaptionGenError on any failure (network, auth, malformed JSON, missing
    keys). Does NOT validate against the rules — the caller does that."""
    client = _get_client()
    if client is None:
        raise CaptionGenError("no gateway client available")
    messages = _build_messages(hook_text, territory, density, persona_brief,
                               claim_posture, persona)
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0.9, max_tokens=300,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise CaptionGenError("empty completion")
        data = _extract_json(content)
    except CaptionGenError:
        raise
    except Exception as e:  # noqa: BLE001 — surface as one error type to callers
        raise CaptionGenError(f"gateway call failed: {e}") from e
    caption = str(data.get("caption") or "").strip()
    self_comment = str(data.get("self_comment") or "").strip()
    if not caption or not self_comment:
        raise CaptionGenError(f"missing caption/self_comment keys in {data!r}")
    return {"caption": caption, "self_comment": self_comment}


# ===========================================================================
# deterministic validation — applies identically to LLM and template output
# ===========================================================================
_EM_DASH_RE = re.compile(r"[—]|--")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF\U0001F000-\U0001F02F]"
)
_HASH_RE = re.compile(r"#\w")

# Word-boundary anchored, mirroring gates.py's style, so "happy" doesn't trip
# on "app" and "happen" doesn't trip on "app" either.
# --- product terms ---------------------------------------------------------
# These were a single flat list, which meant "let Ray name the product" and "let Ray
# say the word app" and "let Ray post a signup link" were the same switch. They are
# three different decisions with three different exposures.
#
#   OWN_APP_NOUN  the bare noun "app". Ray's whole premise is an app he built and
#                 pulled, so blocking the word cost three rejected LLM attempts on
#                 every post and forced the template fallback on his best copy.
#                 Allowing it is a PERSONA FACT, not a marketing decision.
#   OURS          our product by name, plus the conversion funnel. This is the
#                 on-ramp switch. Flipping it is the decision that changes his FTC
#                 exposure, because it connects the expertise claim to a thing sold.
#   THIRD_PARTY   any other product/app/brand. Never allowed for anyone. Naming a
#                 competitor or a real ed-tech product is a separate liability and
#                 no persona has a reason to do it.
_PT_OWN_APP_NOUN = [r"\bapps?\b"]
# NEVER LIFTED, for any persona. Naming a competitor is a different liability from
# naming our own product — trademark and disparagement rather than endorsement — and
# no persona has a reason to do it. This list is deliberately short and is NOT
# exhaustive; a regex cannot enumerate every ed-tech product. The real control is the
# prompt rule ("no other product, app, brand or service is mentioned or implied").
# This catches the names a model actually reaches for.
_PT_THIRD_PARTY = [
    r"\bkhan\s*academy\b", r"\bduolingo\b", r"\bixl\b", r"\bprodigy\b",
    r"\babcmouse\b", r"\bkumon\b", r"\bquizlet\b", r"\bchegg\b",
    r"\bphotomath\b", r"\bchatgpt\b", r"\bcopilot\b", r"\bgemini\b",
]
_PT_OURS = [
    r"\bsmart\s+fella\b", r"\bfart\s+smella\b", r"\bsfss\b",
    r"\bdownloads?(?:ed|ing)?\b", r"\bsign\s*up\b", r"\bsignups?\b",
    r"\bsubscri\w*\b", r"\bfree\s+trial\b", r"\btestflight\b", r"\bapp\s*store\b",
    r"\bpremium\b", r"\bin-app\b", r"\bwaitlist\b",
]
# Preserved under the original name: the full block, which is what a persona with no
# `product_mentions` config still gets. Dani and Chloe are unchanged.
_PRODUCT_TERMS = _PT_OWN_APP_NOUN + _PT_OURS + _PT_THIRD_PARTY
_PRODUCT_RE = re.compile("|".join(_PRODUCT_TERMS), re.I)


def product_re_for(persona: dict | None = None):
    """The product-term matcher for ONE persona.

    Reads persona.json's `product_mentions`:
        own_app_noun: true  -> the bare word "app" is allowed
        our_product:  true  -> our brand names and the conversion funnel are allowed

    Absent config blocks everything, exactly as before.
    """
    cfg = ((persona or {}).get("config") or {}).get("product_mentions") or {}
    terms = list(_PT_THIRD_PARTY)          # never lifted, for anyone
    if not cfg.get("own_app_noun"):
        terms += _PT_OWN_APP_NOUN
    if not cfg.get("our_product"):
        terms += _PT_OURS
    return re.compile("|".join(terms), re.I)

_INVITE_MARKERS = (
    "?", "lmk", "tell me", "drop your", "comment your", "curious what",
    "curious how", "anyone else", "does anyone", "what's your", "whats your",
    "what would you", "would love to hear", "let me know", "how do you",
)

STOPWORDS = {
    "a", "about", "above", "after", "again", "all", "am", "an", "and", "any", "are",
    "as", "at", "back", "be", "because", "been", "before", "being", "between", "both",
    "but", "by", "can", "cant", "could", "couldnt", "did", "didnt", "do", "does",
    "doing", "dont", "down", "during", "each", "even", "ever", "every", "few", "for",
    "from", "further", "get", "got", "had", "has", "have", "having", "he", "hed",
    "her", "here", "hers", "herself", "hes", "him", "himself", "his", "how", "i",
    "id", "if", "ill", "im", "in", "into", "is", "isnt", "it", "its", "itself",
    "ive", "just", "like", "me", "more", "most", "much", "my", "myself", "no", "nor",
    "not", "now", "of", "off", "on", "once", "one", "only", "or", "other", "ought",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she", "shes", "should",
    "so", "some", "still", "such", "than", "that", "thats", "the", "their", "theirs",
    "them", "themselves", "then", "there", "these", "they", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "wasnt", "we",
    "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will",
    "with", "would", "you", "your", "yours", "yourself",
}
_PUNCT_RE = re.compile(r"[^\w\s]")


def _content_words(text: str) -> set[str]:
    t = _PUNCT_RE.sub("", (text or "").lower())
    return {w for w in t.split() if w and w not in STOPWORDS}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT_RE.sub("", (text or "").lower())).strip()


def emoji_count(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


def has_em_dash(text: str) -> bool:
    return bool(_EM_DASH_RE.search(text or ""))


def has_invitation(caption: str) -> bool:
    low = (caption or "").lower()
    return any(marker in low for marker in _INVITE_MARKERS)


def restates_hook(caption: str, hook_text: str, threshold: float = 0.6) -> bool:
    """True if `caption` reuses >= threshold of the hook's own content words —
    a proxy for "just paraphrases the hook" rather than adding something new."""
    hw = _content_words(hook_text)
    if not hw:
        return False
    cw = _content_words(caption)
    overlap = len(hw & cw) / len(hw)
    return overlap >= threshold


def validate_atoms(hook_text: str, caption: str, hashtags: list[str],
                   self_comment: str, claim_posture: str = "no-product",
                   persona: dict | None = None) -> list[str]:
    """Return a list of failure reasons (empty = all rules pass).

    `persona` scopes the product-term block. Without it the full block applies,
    which is what Dani and Chloe get and what every existing caller gets."""
    reasons: list[str] = []
    _prod_re = product_re_for(persona) if persona is not None else _PRODUCT_RE

    # A sourced post's CAPTION must name its source, same as its on-screen line.
    #
    # gates.py catches FORMATTED numbers — "%", digit-plus-"points", banned tokens.
    # It does not catch CLAIMS. A real Ray caption shipped the sentence "The bottom
    # third lost ground every cycle since 2019": fabricated ("bottom third" is not a
    # NAEP category, and there has been one cycle since 2019), containing none of
    # those tokens, so nothing ever looked at it — on a post whose entire premise is
    # that it is sourced.
    #
    # This cannot catch a fabricated stat that DOES cite a source; no regex can, and
    # that is what human review is for. What it does close is the unsourced-number-
    # in-a-caption hole, by forcing the model to either attribute or stay qualitative.
    if claim_posture == "cited-population-stat":
        import gates as _g
        if not any(re.search(p, _g.normalize(caption)) for p in _g._ATTRIBUTION):
            reasons.append(
                "sourced post: the caption names no source. Every population claim "
                "must be attributed on the surface it appears on, not just on the "
                "on-screen line")

    if not (CAPTION_MIN_CHARS <= len(caption) <= CAPTION_MAX_CHARS):
        reasons.append(f"caption length {len(caption)} outside "
                       f"[{CAPTION_MIN_CHARS}, {CAPTION_MAX_CHARS}]")
    if _HASH_RE.search(caption):
        reasons.append("caption embeds a hashtag; hashtags must be a separate field")
    if has_em_dash(caption):
        reasons.append("caption contains an em dash")
    if has_em_dash(self_comment):
        reasons.append("self_comment contains an em dash")
    if _prod_re and _prod_re.search(caption):
        reasons.append(f'caption mentions an app/product term '
                       f'("{_prod_re.search(caption).group(0)}")')
    if _prod_re and _prod_re.search(self_comment):
        reasons.append("self_comment mentions an app/product term")
    if _norm(caption) == _norm(hook_text):
        reasons.append("caption is character-identical to the hook")
    if restates_hook(caption, hook_text):
        reasons.append("caption restates the hook instead of adding a new detail")
    if not has_invitation(caption):
        reasons.append("caption has no explicit invitation to reply")
    if emoji_count(caption) > 1:
        reasons.append(f"caption has {emoji_count(caption)} emoji (max 1)")
    if emoji_count(self_comment) > 0:
        reasons.append(f"self_comment has {emoji_count(self_comment)} emoji (max 0)")
    n_words = len(self_comment.split())
    if not (SELF_COMMENT_MIN_WORDS <= n_words <= SELF_COMMENT_MAX_WORDS):
        reasons.append(f"self_comment word count {n_words} outside "
                       f"[{SELF_COMMENT_MIN_WORDS}, {SELF_COMMENT_MAX_WORDS}]")
    if _norm(self_comment) == _norm(caption):
        reasons.append("self_comment duplicates the caption")
    # A self_comment consisting only of the caption's trailing question is also
    # a duplicate in spirit even when the caption carries more text around it.
    trailing_q = re.search(r"([^.!]*\?)\s*$", caption)
    if trailing_q and _norm(self_comment) == _norm(trailing_q.group(1)):
        reasons.append("self_comment duplicates the caption's closing question")
    if len(hashtags) > MAX_HASHTAGS:
        reasons.append(f"{len(hashtags)} hashtags exceeds the {MAX_HASHTAGS} cap")
    if not hashtags:
        reasons.append("no hashtags")
    if any(not h.startswith("#") or len(h) < 2 for h in hashtags):
        reasons.append("a hashtag is malformed (must start with # and have content)")
    if len(set(h.lower() for h in hashtags)) != len(hashtags):
        reasons.append("duplicate hashtags")

    passed, reason = gates.check(caption, claim_posture)
    if not passed:
        reasons.append(f"gates.check(caption): {reason}")
    passed, reason = gates.check(self_comment, claim_posture)
    if not passed:
        reasons.append(f"gates.check(self_comment): {reason}")

    return reasons


# ===========================================================================
# hashtags — rotating sets, never the LLM's invention, so the 5-cap and the
# "drawn from rotating sets" requirement are both structural, not prompted.
# ===========================================================================
GENERAL_TAGS = ["#momsoftiktok", "#momlife", "#momtok", "#parentingtok",
               "#momhumor", "#realtalk"]
TERRITORY_TAGS = {
    "relationship": ["#momandson", "#boymom", "#tweenlife", "#growingupfast",
                     "#lettinggo", "#momandkids"],
    "momguilt": ["#momguilt", "#workingmomlife", "#momstruggles", "#momanxiety",
                "#momsbelike", "#realmomtalk"],
    "screentime": ["#screentime", "#screentimestruggles", "#techbalance",
                  "#kidsandtech", "#ipadkid", "#parentinglife"],
    "benchmark": ["#fifthgrademom", "#middleschoolmom", "#momworries",
                 "#reportcardseason", "#parentingtween", "#schoolmom"],
}


def hashtag_pools(persona: dict | None = None) -> tuple[list[str], dict]:
    """
    (general, by_territory) for ONE persona, from persona.json's `hashtags`.

    GENERAL_TAGS and TERRITORY_TAGS above are DANI'S, and they were the only
    pools that existed: #momsoftiktok, #momlife, #momtok, #momhumor. Worse,
    TERRITORY_TAGS is keyed by HER four territories, so a persona with different
    territory names fell through to GENERAL_TAGS for both halves of the set and
    got mom tags twice over.

    That shipped on a real Ray batch: all six posts came out tagged #momhumor
    and #momsoftiktok, under a man who builds software. Nothing failed, nothing
    warned. It was caught by a human reading the batch, which is exactly why the
    review step exists.

    A persona with no `hashtags` block gets Dani's pools, so she is unchanged.
    """
    cfg = ((persona or {}).get("config") or {}).get("hashtags") or {}
    general = list(cfg.get("general") or []) or GENERAL_TAGS
    by_territory = dict(cfg.get("by_territory") or {}) or TERRITORY_TAGS
    return general, by_territory


def generate_hashtags(territory: str, index: int = 0,
                      persona: dict | None = None) -> list[str]:
    """4-5 tags: 2 territory + 2 general, rotated by `index` so a batch of
    posts on the same territory doesn't repeat the identical hashtag set,
    plus a 5th general tag when it doesn't collide. Deterministic in `index`."""
    gen_pool, terr_map = hashtag_pools(persona)
    terr_pool = terr_map.get(territory) or gen_pool
    tags: list[str] = []
    for pool, n in ((terr_pool, 2), (gen_pool, 2)):
        for k in range(n):
            tag = pool[(index + k) % len(pool)]
            if tag not in tags:
                tags.append(tag)
    extra = gen_pool[(index + 2) % len(gen_pool)]
    if extra not in tags and len(tags) < MAX_HASHTAGS:
        tags.append(extra)
    return tags[:MAX_HASHTAGS]


# ===========================================================================
# template fallback — constructed to satisfy every rule in validate_atoms()
# by construction, for any hook_text/territory/density.
# ===========================================================================
_TEMPLATE_DETAILS = {
    "relationship": [
        "Owen used to narrate his whole day through the bathroom door and now I get one word answers.",
        "My husband has been in Dallas all week so I am the one clocking every little change with Owen.",
        "I found an old video of him explaining Minecraft to me and could not believe how much has changed.",
    ],
    "momguilt": [
        "I still have not opened the extra practice packet I ordered back in March.",
        "My neighbor casually mentioned tutoring last week and I have not stopped thinking about it since.",
        "I keep meaning to ask his teacher one real follow up question and then I chicken out.",
    ],
    "screentime": [
        "We are on day four of the five more minutes negotiation and he is winning every round.",
        "His dad is traveling again so I am solo on bedtime screen duty this whole week.",
        "I caught myself scrolling right next to him and did not love what that said about me.",
    ],
    "benchmark": [
        "His teacher's emails are all exclamation points and zero actual detail about seventh grade math.",
        "There is a class group chat and I genuinely cannot follow half of what people mean in it.",
        "Maya's report cards used to make total sense to me and now I mostly just nod along.",
    ],
}
_TEMPLATE_CONTEXT = [
    "Not sure if that's totally normal or if I'm just in my feelings about it.",
    "I genuinely cannot tell anymore what's typical for this age.",
    "Feels like this happened overnight and nobody warned me.",
    "I know I'm probably overthinking this but here we are.",
]
_TEMPLATE_INVITES = [
    "What's it like at your house?",
    "Anyone else dealing with this?",
    "Tell me I'm not the only one!",
    "What would you have done?",
    "How do you handle this at your place?",
    "Does this get better, moms?",
]
_TEMPLATE_SELF_COMMENTS = [
    "Wait is this a phase or forever",
    "Not me overthinking this at 11pm again",
    "Owen has no idea I posted this lol",
    "Someone please tell me this is normal",
    "Low key needed to vent about this one",
    "Please tell me it gets easier",
]


class TemplateUnavailable(Exception):
    """No template material exists for this persona. Raised instead of emitting
    another persona's voice — see generate_atoms_template."""


def generate_atoms_template(hook_text: str, territory: str, density: str,
                            index: int = 0, persona: dict | None = None) -> dict:
    """Deterministic fallback. Rotates through fixed, pre-vetted pools keyed
    by `index` so a batch doesn't repeat the same caption verbatim, and is
    constructed so every check in validate_atoms() passes regardless of the
    hook, territory, or density it's paired with.

    THE POOLS ABOVE ARE DANI'S, STRUCTURALLY. They name her kids, her husband's
    work travel, and they sign off "Does this get better, moms?" — and
    _TEMPLATE_DETAILS is keyed by HER four territories, so any other persona
    missed the lookup and silently got her `momguilt` pool.

    That shipped. Ray's THESIS post, the one meant to be pinned, went onto the
    live calendar reading "My neighbor casually mentioned tutoring last week and
    I have not stopped thinking about it since. Not sure if that's totally normal
    or if I'm just in my feelings about it." Three LLM attempts had failed and
    this function filled the gap with a 38-year-old mother of two.

    So: a persona may supply its own pools via persona.json's `caption_template`.
    A persona with none, and which is not the one these constants were written
    for, gets a RAISE — the caller then reports a blocked post rather than
    publishing in the wrong voice. A blocked post is recoverable; a wrong-voice
    post is live.
    """
    cfg = ((persona or {}).get("config") or {})
    own = cfg.get("caption_template") or {}

    if own:
        details = (own.get("details") or {}).get(territory) or own.get("details_any") or []
        contexts = own.get("contexts") or [""]
        invites = own.get("invites") or []
        selfs = own.get("self_comments") or []
        if not (details and invites and selfs):
            raise TemplateUnavailable(
                f"persona {(persona or {}).get('name')!r} declares caption_template but it "
                f"is missing details/invites/self_comments")
        detail = details[index % len(details)]
        context = contexts[index % len(contexts)]
        invite = invites[index % len(invites)]
        caption = " ".join(x for x in (detail, context, invite) if x)
        return {"caption": caption,
                "self_comment": selfs[(index * 3 + 1) % len(selfs)]}

    # No own pools. Only the persona these constants describe may use them.
    name = (persona or {}).get("name")
    if persona is not None and name != "dani":
        raise TemplateUnavailable(
            f"no caption_template for persona {name!r}, and the built-in pools are "
            f"Dani's (her children, her husband, \"moms\"). Refusing to caption in "
            f"another persona's voice — add a `caption_template` block to that "
            f"persona.json, or let the post be blocked.")

    details = _TEMPLATE_DETAILS.get(territory, _TEMPLATE_DETAILS["momguilt"])
    detail = details[index % len(details)]
    context = _TEMPLATE_CONTEXT[index % len(_TEMPLATE_CONTEXT)]
    invite = _TEMPLATE_INVITES[index % len(_TEMPLATE_INVITES)]
    caption = f"{detail} {context} {invite}"
    # self_comment rotates on a different modulus than the invite so the two
    # pools desync instead of marching in lockstep.
    self_comment = _TEMPLATE_SELF_COMMENTS[(index * 3 + 1) % len(_TEMPLATE_SELF_COMMENTS)]
    return {"caption": caption, "self_comment": self_comment}


# ===========================================================================
# public entry point
# ===========================================================================
def build_atoms(hook_text: str, territory: str, density: str, *, index: int = 0,
                claim_posture: str = "no-product", use_llm: bool = True,
                max_attempts: int = 3, model: str = DEFAULT_MODEL,
                persona_brief: str = PERSONA_BRIEF,
                persona: dict | None = None) -> dict:
    """
    Produce {"caption", "hashtags", "self_comment", "source", "attempts"}.

    Tries the live LLM up to `max_attempts` times, validating each attempt
    against `validate_atoms`. Falls back to the deterministic template (which
    always passes) if the LLM is unavailable or every attempt fails
    validation. Never raises.

    `persona_brief` defaults to Dani's PERSONA_BRIEF so every existing call
    site (and every test) is unaffected. A persona-aware caller passes
    `personas.brief(persona)` instead, which raises before we ever get here
    if that persona has no brief configured.
    """
    hashtags = generate_hashtags(territory, index=index, persona=persona)
    last_reasons: list[str] = []

    if use_llm:
        for attempt in range(1, max_attempts + 1):
            try:
                atoms = generate_atoms_llm(hook_text, territory, density, model=model,
                                          persona_brief=persona_brief,
                                          claim_posture=claim_posture,
                                          persona=persona)
            except CaptionGenError as e:
                last_reasons = [str(e)]
                log(f"CAPTION LLM attempt {attempt}/{max_attempts} failed: {e}")
                continue
            reasons = validate_atoms(hook_text, atoms["caption"], hashtags,
                                     atoms["self_comment"], claim_posture,
                                     persona=persona)
            if not reasons:
                return {
                    "caption": atoms["caption"],
                    "hashtags": hashtags,
                    "self_comment": atoms["self_comment"],
                    "source": "llm",
                    "attempts": attempt,
                }
            last_reasons = reasons
            log(f"CAPTION LLM attempt {attempt}/{max_attempts} rejected: {'; '.join(reasons)}")

    if use_llm and last_reasons:
        log(f"CAPTION falling back to template after {max_attempts} attempt(s): "
            f"{'; '.join(last_reasons)}")

    tmpl = generate_atoms_template(hook_text, territory, density, index=index,
                                   persona=persona)
    reasons = validate_atoms(hook_text, tmpl["caption"], hashtags,
                             tmpl["self_comment"], claim_posture, persona=persona)
    if reasons:
        # The template is meant to be rule-proof by construction; a failure
        # here is a bug in this module, not in the hook. Surface it loudly
        # rather than silently shipping non-compliant copy.
        raise AssertionError(f"template fallback failed its own rules: {reasons}")
    return {
        "caption": tmpl["caption"],
        "hashtags": hashtags,
        "self_comment": tmpl["self_comment"],
        "source": "template",
        "attempts": max_attempts if use_llm else 0,
    }


# ===========================================================================
# CLI
# ===========================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate caption/hashtags/self_comment for one hook.")
    ap.add_argument("--text", required=True, help="the on-screen hook text")
    ap.add_argument("--territory", required=True, choices=TERRITORIES)
    ap.add_argument("--density", required=True, choices=DENSITIES)
    ap.add_argument("--index", type=int, default=0, help="rotation seed for hashtags/template")
    ap.add_argument("--claim-posture", default="no-product", choices=gates.CLAIM_POSTURES)
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM, use the template only")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    a = ap.parse_args(argv)

    result = build_atoms(a.text, a.territory, a.density, index=a.index,
                         claim_posture=a.claim_posture, use_llm=not a.no_llm,
                         model=a.model)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
