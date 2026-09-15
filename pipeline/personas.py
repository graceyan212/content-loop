#!/usr/bin/env python3
"""
personas.py — shared persona resolution for every script in ugc-pipeline/.

The pipeline's CODE lives here (ugc-pipeline/); each persona's DATA lives in
its own sibling directory (../danielle, ../chloe, ...). personas.json is the
registry that maps a short name to that data root. Every persona-aware script
(gallery.py, batch.py, learn/track.py, post/publish.py, post/metricool.py)
calls `resolve(name)` and hangs its paths off the returned `root`.

Default persona, everywhere, is "dani" — so every script run with zero
`--persona` flag behaves exactly as it did before this module existed.

registry entry shape (personas.json):
    {"<name>": {"root": "../<dir>", "blog_id": "<id-or-null>", "timezone": "<tz>"}}

`root` is resolved relative to THIS file's directory (the ugc-pipeline root),
not the caller's cwd. Once a root is resolved, the fuller per-persona config
(voice, brief, accounts, compliance, ...) is read from `<root>/persona.json`
when present — that file is the authoritative source for blog_id/timezone;
the registry's copies are only a fallback for a persona that has no
persona.json yet.
"""

from __future__ import annotations

import json
import os

CODE_ROOT = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(CODE_ROOT, "personas.json")
DEFAULT_PERSONA = "dani"


class PersonaError(SystemExit):
    """Raised (as a SystemExit) for any persona-resolution failure. Callers
    should let this propagate — it always carries an actionable message."""


def _load_registry() -> dict:
    if not os.path.isfile(REGISTRY_PATH):
        raise PersonaError(f"FATAL: persona registry not found: {REGISTRY_PATH}")
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise PersonaError(f"FATAL: {REGISTRY_PATH} will not parse ({e})")


def resolve(name: str | None = None) -> dict:
    """
    Resolve a persona name (default "dani") to its config.

    Returns:
        {"name": str, "root": <absolute path>, "registry_entry": dict,
         "config": dict (persona.json contents, or {} if absent),
         "persona_json_path": str}

    Raises PersonaError if the name is unknown or its root does not exist.
    """
    name = (name or DEFAULT_PERSONA).strip().lower()
    registry = _load_registry()
    entry = registry.get(name)
    if entry is None:
        known = ", ".join(sorted(registry)) or "(none registered)"
        raise PersonaError(f"FATAL: unknown persona {name!r}. Known personas: {known}")

    root = os.path.normpath(os.path.join(CODE_ROOT, entry["root"]))
    if not os.path.isdir(root):
        raise PersonaError(f"FATAL: persona {name!r} root does not exist: {root}")

    persona_json_path = os.path.join(root, "persona.json")
    config: dict = {}
    if os.path.isfile(persona_json_path):
        try:
            with open(persona_json_path, encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            raise PersonaError(f"FATAL: {persona_json_path} will not parse ({e})")

    return {
        "name": name,
        "root": root,
        "registry_entry": entry,
        "config": config,
        "persona_json_path": persona_json_path,
    }


def character_dir(persona: dict) -> str:
    """
    <root>/character/<name> — where this persona's stills and manifest.json live.

    Keyed on the persona NAME, not the data root's basename: Dani's root is
    GTM/danielle but her character dir is character/dani.

    Six call sites in post/ (schedule_batch.py, experiment.py, respin.py) used to
    build this path as os.path.join(root, "character", "dani", ...) — a
    persona-correct root with a hardcoded Dani leaf. Every other persona therefore
    resolved to <their own root>/character/dani/manifest.json, which cannot exist.
    schedule_batch.py is the loop's `make` phase, so that was a hard crash for any
    persona but Dani, not a degradation.
    """
    return os.path.join(persona["root"], "character", persona["name"])


def blog_id(persona: dict) -> str | None:
    """Resolved Metricool blog_id: persona.json's metricool.blog_id wins,
    the registry's blog_id is the fallback. None means "not configured" —
    callers MUST refuse to post rather than fall through to another
    persona's brand."""
    cfg_blog = ((persona.get("config") or {}).get("metricool") or {}).get("blog_id")
    if cfg_blog:
        return cfg_blog
    return (persona.get("registry_entry") or {}).get("blog_id") or None


def require_blog_id(persona: dict) -> str:
    """Like blog_id(), but raises PersonaError instead of returning None.
    Call this immediately before any Metricool posting operation — posting
    one persona's content under another persona's (or no) blog_id is the
    single worst failure mode in this pipeline."""
    bid = blog_id(persona)
    if not bid:
        raise PersonaError(
            f"FATAL: persona {persona['name']!r} has no Metricool blog_id "
            f"configured (checked {persona['persona_json_path']} and "
            f"personas.json). Refusing to post — set "
            f"metricool.blog_id in that persona's persona.json first."
        )
    return bid


def timezone(persona: dict) -> str:
    cfg_tz = ((persona.get("config") or {}).get("metricool") or {}).get("timezone")
    if cfg_tz:
        return cfg_tz
    return (persona.get("registry_entry") or {}).get("timezone") or "America/Chicago"


DEFAULT_POSTS_PER_DAY = 3


def posts_per_day(persona: dict) -> int:
    """How many posts a day this persona should carry.

    ONE definition. This was hardcoded as `POSTS_PER_DAY = 3` in BOTH
    post/experiment.py and loop/cycle.py, which meant changing the cadence in one
    place left the other silently disagreeing — the experiment placer would refuse a
    day the fill logic considered short, and the calendar would sit permanently one
    post under target with nothing reporting why.

    It lives in personas.json so cadence is an operating decision, not a code edit,
    and so two personas can run at different rates.
    """
    v = (persona.get("registry_entry") or {}).get("posts_per_day")
    try:
        v = int(v)
    except (TypeError, ValueError):
        return DEFAULT_POSTS_PER_DAY
    return v if 1 <= v <= 8 else DEFAULT_POSTS_PER_DAY


# Compliance flags in persona.json -> the rule each one implies in a prompt.
# Only flags that constrain COPY appear here; ai_label_required is a posting-time
# setting and means nothing to a model writing text.
# no_product_claims is handled by _product_rule() below, NOT here, because its
# rendered text depends on config.product_mentions. Leaving it in this dict was
# the bug: the gate in copy/captions.py was lifted for Ray while this block still
# rendered "No product, app, brand or service mentioned or implied" into his
# prompt. A half-open door is worse than either extreme — the safety net is gone
# AND the stated intent contradicts it, so behaviour becomes whatever the model
# guesses.
_COMPLIANCE_RULES = {
    "no_measured_child_claims":
        'No measured claim about a child: no percentages, IQ, grade level, test '
        'scores, "smarter", "behind", "gifted".',
    "no_fabricated_credentials":
        "No credentials beyond the ones the persona brief establishes.",
    "no_causal_admit_claims":
        "Never attribute an admission or acceptance to an action, essay or product.",
    "no_admissions_reader_claims":
        "Never state how admissions readers behave in the first person.",
    "no_iq_or_clinical_framing":
        "Never frame anything as clinical, diagnostic, or a measure of intelligence.",
    "no_family_financial_need_claims":
        "No claims about the persona's family being in financial need.",
    "requires_source_attribution":
        "Any population statistic must name its source in the same line.",
}


_DEFAULT_PRODUCT_NAME = "Smart Fella or Fart Smella"


def _product_rule(cfg: dict) -> str | None:
    """The product-mention rule, reconciled with config.product_mentions.

    compliance.no_product_claims is the DEFAULT posture (total block). The
    product_mentions{} block narrows it, one axis at a time, and MUST agree with
    captions.product_re_for() — that function is the gate, this string is the
    instruction, and they are two halves of one decision.

    Third-party and competitor names are never unblocked on either side.
    """
    if not (cfg.get("compliance") or {}).get("no_product_claims"):
        return None

    pm = cfg.get("product_mentions") or {}
    own_app = bool(pm.get("own_app_noun"))
    ours = bool(pm.get("our_product"))
    if not (own_app or ours):
        return "No product, app, brand or service mentioned or implied."

    name = pm.get("product_name") or _DEFAULT_PRODUCT_NAME
    allowed = []
    if ours:
        allowed.append(f'our own product, by name ("{name}")')
    if own_app:
        allowed.append('the generic noun "app"')
    return ("You may refer to " + " and ".join(allowed) +
            ". Never name any OTHER product, app, brand, service or competitor.")


def voice_rules(persona: dict) -> str:
    """
    THE one rendered rules block for a persona, shared by every prompt.

    It lived in loop/generate.py, which writes HOOKS. copy/captions.py, which
    writes CAPTIONS, had its own hardcoded block — and that block was Dani's,
    down to "no measured claim about Owen or Maya". So a persona's hard_rules
    reached the hook model and never reached the caption model, and captions
    drifted while hooks held. Observed on Ray: an invented nephew, an exclamation
    point his voice config forbids, and a fallback template in Dani's voice.

    Sources, in order: voice{} formatting fields, compliance{} booleans, then the
    free-text hard_rules[]. A persona with no hard_rules gets the universal rules
    only — weaker steering, not a compliance hole, since gates.py still runs. What
    it is never again is another persona's rules.
    """
    cfg = persona.get("config") or {}
    voice = cfg.get("voice") or {}
    compliance = cfg.get("compliance") or {}
    rules: list[str] = []

    if not voice.get("em_dashes", False):
        rules.append("No em dashes anywhere.")

    max_emoji = voice.get("max_emoji_caption", 0)
    if max_emoji == 0:
        rules.append("No emoji at all, in the caption or the on-screen line.")
    else:
        rules.append(f"At most {max_emoji} emoji in the caption"
                     + (", none in the on-screen line."
                        if not voice.get("emoji_on_screen") else "."))

    if voice.get("exclamation_points") is False:
        rules.append("No exclamation points.")
    if voice.get("case") == "lower":
        rules.append("Lowercase throughout, including the first word of a sentence.")
    if voice.get("second_person"):
        rules.append('Second person, always. "your kid", "your house". Never "as a '
                     'parent myself" or any other bid for warmth.')

    rules.append("Americanized spelling.")

    prod = _product_rule(cfg)
    if prod:
        rules.append(prod)

    for flag, rule in _COMPLIANCE_RULES.items():
        if compliance.get(flag):
            rules.append(rule)

    extra = cfg.get("hard_rules") or []
    if isinstance(extra, str):
        extra = [extra]
    rules.extend(str(r).strip() for r in extra if str(r).strip())

    return "\n".join(f"- {r}" for r in rules)


def brief(persona: dict) -> str:
    """The persona's caption-voice system prompt. Raises PersonaError if none
    is configured (including the scaffold's literal TODO placeholder) —
    callers must never fall back to another persona's voice."""
    cfg = persona.get("config") or {}
    b = cfg.get("brief")
    if isinstance(b, str) and b.strip() and "TODO" not in b.upper():
        return b
    raise PersonaError(
        f"FATAL: persona {persona['name']!r} has no brief configured in "
        f"{persona['persona_json_path']}. Refusing to generate captions in "
        f"another persona's voice — add a \"brief\" string to that "
        f"persona.json first."
    )
