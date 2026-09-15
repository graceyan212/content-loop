#!/usr/bin/env python3
"""
generate.py — write new posts with Opus 5, steered by what the operator kept.

WHY THE LOOP NEEDS THIS: without it the cycle just drains a fixed library. 98 posts
at 3/day is about a month, then it posts nothing. Generation is what makes the loop
autonomous rather than a scheduler with a queue.

WHAT STEERS IT, in order of weight:

  1. OPERATOR DECISIONS (learn/decisions.json). The strongest signal available and
     it needs no sample size, because it is direct judgment rather than inference.
     18 decisions already produced an unambiguous split: asks-for-advice 4 kept /
     0 cut, one-number "be honest" questions 0 kept / 4 cut. Every kept and cut
     post goes into the prompt as an example.
  2. ENGAGEMENT (learn/learnings.json). Real but weak — front-runners are mostly
     null below min_n, and ~20-30 posts per arm are needed to detect a 50%
     difference at this account's volume. Included, labelled provisional.
  3. THE EXISTING LIBRARY, as a do-not-repeat list.

The LLM writes copy. It does NOT decide what to test, which slot to use, or what to
schedule — that stays deterministic in cycle.py, the same boundary Hermes draws.

Everything generated passes copy/gates.py before it is written to disk. A model that
produces a compliance failure gets its output dropped, not patched.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "copy"), os.path.join(ROOT, "post"),
          os.path.join(ROOT, "render")):
    if p not in sys.path:
        sys.path.insert(0, p)

import llm                     # noqa: E402
import gates                   # noqa: E402
import timing as tmg           # noqa: E402
import personas                # noqa: E402
import guard                   # noqa: E402

GENERATED_FILE = "generated.md"

# The brief is the single most important input to this prompt. It used to be
# sliced at 2200 chars, which silently truncated Chloe's 3140-char brief and threw
# away her word budget, her series thesis, and her whole "Hard limits" paragraph —
# which, in every brief in this repo, sits at the END. A cap is still worth having
# so a runaway file cannot blow the context, but it has to be large enough not to
# bite in normal use and loud when it does.
BRIEF_MAX = 8000


# Deliberately says "that persona" and never "she". This prompt is shared by every
# persona; the previous wording ("who she is") described Dani and Chloe and silently
# mis-gendered anyone else. The persona's own pronouns belong in its brief, which is
# the only place that knows them. The em dash that used to sit in this prompt is gone
# too: it is contradictory to ban em dashes in the output while modelling one here.
SYSTEM = """You write short social posts for a specific fictional persona on TikTok.

You are given: who that persona is, the exact posts the operator KEPT, the exact
posts the operator CUT, and everything already in the library. You return new posts.

The operator's kept/cut record is the most important input. It is direct human
judgment, not a metric. Match the shape of what was kept. Do not produce anything
resembling what was cut, even on a different topic. The cuts are about FORM, not
subject matter.

Return ONLY a JSON array. No prose, no code fence, no commentary."""


# Compliance flags in persona.json -> the rule each one implies in the prompt.
# Only the flags that constrain COPY appear here. ai_label_required, for instance,
# is a posting-time setting and means nothing to a model writing a caption.
_COMPLIANCE_RULES = {
    "no_product_claims":
        "No product, app, brand or service mentioned or implied.",
    "no_measured_child_claims":
        'No measured claim about a child: no percentages, IQ, grade level, test '
        'scores, "smarter", "behind", "gifted".',
    "no_fabricated_credentials":
        "No credentials beyond the ones the persona brief above establishes.",
    "no_causal_admit_claims":
        "Never attribute an admission or acceptance to an action, essay or product.",
    "no_admissions_reader_claims":
        "Never state how admissions readers behave in the first person. Attribute "
        "any such claim on-slide to a named former admissions officer.",
    "no_iq_or_clinical_framing":
        "Never frame anything as clinical, diagnostic, or a measure of intelligence. "
        "It is an assessment, a benchmark or a readiness check.",
    "no_family_financial_need_claims":
        "No claims about the persona's family being in financial need.",
    "requires_source_attribution":
        "Any population statistic must name its source in the same line.",
}


def _rules_block(persona: dict) -> str:
    """Delegates to personas.voice_rules — THE single renderer. This function used
    to own the logic while copy/captions.py kept a separate hardcoded block of
    Dani's rules, so a persona's hard_rules steered its hooks and never its
    captions. One source now feeds both."""
    return personas.voice_rules(persona)



def load_json(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def build_prompt(persona: dict, n: int) -> str:
    root = persona["root"]
    import respin
    lib = respin.library_by_uid(root)
    dec = load_json(os.path.join(root, "learn", "decisions.json"), {}).get("decisions", [])
    learn = load_json(os.path.join(root, "learn", "learnings.json"), {})

    kept, cut = [], []
    for d in dec:
        p = lib.get(d.get("uid"))
        if not p:
            continue
        line = (p.get("on-screen") or p.get("title") or "").strip()
        (kept if d.get("action") == "approve" else cut).append(line)

    existing = [(p.get("on-screen") or p.get("title") or "").strip()
                for p in lib.values()]

    # MUST be the persona we were called for, not the default. This used to read
    # personas.resolve(None), which always resolves to DEFAULT_PERSONA ("dani") —
    # so `--persona chloe` prompted with Dani's voice and filed the result in
    # Chloe's library. personas.brief() exists precisely to make that impossible;
    # resolving the default instead of the argument walked around it. No hasattr
    # guard either: a missing brief must raise, never silently degrade to "".
    brief = personas.brief(persona)
    if len(brief) > BRIEF_MAX:
        print(f"  WARNING: {persona['name']!r} brief is {len(brief)} chars; "
              f"truncating to {BRIEF_MAX}. The tail of a brief is where the hard "
              f"limits live. Shorten it deliberately rather than letting this cut it.")
        brief = brief[:BRIEF_MAX]

    return f"""## The persona
{brief}

## Posts the operator KEPT ({len(kept)}) — match these shapes
{chr(10).join('- ' + k for k in kept) or '- (none yet)'}

## Posts the operator CUT ({len(cut)}) — never produce anything like these
{chr(10).join('- ' + c for c in cut) or '- (none yet)'}

## Engagement so far — PROVISIONAL, small sample, do not over-fit
{json.dumps(learn.get('front_runners', {}), indent=2)[:600]}
status: {str(learn.get('status',''))[:180]}

## Already in the library — do not repeat these ideas
{chr(10).join('- ' + e for e in existing[:120])}

## Task
Write {n} NEW posts. Each is a JSON object:

  "on-screen"  the line burned onto the photo. Max ~14 words. Readable in 2 seconds.
  "caption"    120-220 chars. Adds a SPECIFIC detail not in the on-screen line.
               Ends by inviting a reply. Never restates the on-screen line.
  "hashtags"   exactly 5, space separated, lowercase, starting with #
  "when"       one of: morning, midmorning, lunch, pickup, dinner, evening,
               late-night, any — whichever time of day the post is ABOUT
  "items"      OPTIONAL array of 3-6 strings, only for list posts. Every item
               must contain a number, a technique, or a self-deprecating
               admission. No filler.

Hard rules, all of which will be checked mechanically:
{_rules_block(persona)}

Return ONLY the JSON array."""


def validate(post: dict, persona: dict) -> tuple[bool, str]:
    """Mechanical check on one generated post. Reads the same `voice` fields that
    _rules_block() renders into the prompt, so what the model is told and what the
    checker enforces are the one source of truth. They used to be two hardcoded
    lists that happened to agree for Dani."""
    voice = (persona.get("config") or {}).get("voice") or {}
    screen = (post.get("on-screen") or "").strip()
    cap = (post.get("caption") or "").strip()
    if not screen or not cap:
        return False, "missing on-screen or caption"
    if len(screen.split()) > 16:
        return False, f"on-screen too long ({len(screen.split())} words)"
    if not (100 <= len(cap) <= 260):
        return False, f"caption length {len(cap)}"
    if not voice.get("em_dashes", False) and ("—" in screen or "—" in cap):
        return False, "em dash"
    max_tags = int(voice.get("max_hashtags") or 5)
    tags = (post.get("hashtags") or "").split()
    if not (max_tags - 1 <= len(tags) <= max_tags) or not all(t.startswith("#") for t in tags):
        return False, f"hashtags {len(tags)} (want {max_tags - 1}-{max_tags})"
    if screen.lower() == cap.lower():
        return False, "caption restates the on-screen line"
    for label, text in (("on-screen", screen), ("caption", cap)):
        ok, why = gates.check(text)
        if not ok:
            return False, f"gate {label}: {why}"
    for i, it in enumerate(post.get("items") or []):
        ok, why = gates.check(it)
        if not ok:
            return False, f"gate item{i+1}: {why}"
    return True, ""


def generate(persona: dict, n: int, dry: bool = False) -> list[dict]:
    """Takes the RESOLVED persona dict (personas.resolve(...)), not a bare root,
    so the voice the prompt uses and the library the output lands in cannot
    disagree — they are now derived from the same object."""
    root = persona["root"]
    prompt = build_prompt(persona, n)
    text, usage = llm.chat(prompt, system=SYSTEM, max_tokens=6000)
    cost = usage.get("costInUSD") or 0
    guard.ledger_append(root, "llm", 1, cost_usd=cost,
                        tokens=usage.get("total_tokens"))
    print(f"  opus-5: {usage.get('total_tokens')} tokens, ${cost:.4f}")

    raw = llm.extract_json(text)
    if isinstance(raw, dict):
        raw = [raw]

    good, bad = [], []
    for p in raw:
        if not isinstance(p, dict):
            continue
        ok, why = validate(p, persona)
        (good if ok else bad).append((p, why))
    kept = [p for p, _ in good]

    for p, why in bad:
        print(f"  REJECTED  {(p.get('on-screen') or '?')[:56]!r}: {why}")
    print(f"  {len(kept)}/{len(raw)} passed validation")

    if kept and not dry:
        path = os.path.join(root, "copy", GENERATED_FILE)
        new = not os.path.exists(path)
        with open(path, "a") as fh:
            if new:
                fh.write(
                    "<!--\nGENERATED by loop/generate.py (Opus 5 via TrueFoundry).\n"
                    "Steered by learn/decisions.json — the operator's kept/cut record.\n"
                    "Everything here passed copy/gates.py before being written.\n"
                    "Safe to edit or delete by hand; the loop appends, never rewrites.\n-->\n\n"
                    "# Generated\n")
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
            for i, p in enumerate(kept, 1):
                fh.write(f"\n### g-{stamp}-{i:02d}\n")
                fh.write(f"on-screen: {p['on-screen'].strip()}\n")
                for it in (p.get("items") or []):
                    pass
                if p.get("items"):
                    fh.write("items:\n")
                    for it in p["items"]:
                        fh.write(f"- {it.strip()}\n")
                fh.write(f"caption: {p['caption'].strip()}\n")
                fh.write(f"hashtags: {p['hashtags'].strip()}\n")
                w = (p.get("when") or "").strip().lower()
                if w and w in tmg.SLOTS:
                    fh.write(f"when: {w}\n")
        print(f"  appended {len(kept)} to copy/{GENERATED_FILE}")
    return kept


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    p = personas.resolve(a.persona)
    out = generate(p, a.count, a.dry_run)
    for x in out:
        print(f"\n  {x['on-screen']}")
        print(f"    {x['caption'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
