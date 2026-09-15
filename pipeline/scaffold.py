#!/usr/bin/env python3
"""
scaffold.py — create a new persona's directory tree.

    python scaffold.py --name chloe

Creates GTM/<name>/ next to GTM/danielle/ (a sibling persona DATA root, never
touching the shared CODE in ugc-pipeline/), with:

    <name>/persona.json          — filled in from the template below; the
                                    operator must replace every TODO before
                                    this persona can generate or post anything
    <name>/character/<name>/stills/
    <name>/copy/
    <name>/learn/
    <name>/render/out/
    <name>/post/
    <name>/README.md             — exactly what is still missing, and why

Also adds a stub entry ("root": "../<name>", "blog_id": null, "timezone": null)
to personas.json — the registry — if that name is not already registered,
so `--persona <name>` resolves immediately (personas.py raises a clear error
for anything not configured yet; nothing here fakes readiness).

IDEMPOTENT / SAFE: refuses to touch anything if GTM/<name>/ already exists.
This never overwrites an existing persona — re-run scaffold.py on a persona
that already has a directory and it does nothing but say so.

Deliberately does NOT invent biographical details, a voice, or accounts for
the new persona — that is the operator's call. The template's "brief" field
is a literal "TODO" string; captions.py/personas.brief() refuses to generate
copy for a persona whose brief still contains that word, on purpose.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # ugc-pipeline/ (CODE root)
GTM_ROOT = os.path.dirname(HERE)                            # GTM/
REGISTRY_PATH = os.path.join(HERE, "personas.json")


def persona_template(name: str) -> dict:
    """The persona.json written for a brand-new persona. Every field an
    operator has to fill in is spelled "TODO" (or left null/empty) rather than
    guessed — see personas.brief()/require_blog_id(), which both refuse to
    proceed while these are unset."""
    return {
        "name": name,
        "display_name": name.capitalize(),
        "root": f"GTM/{name}",
        "metricool": {"blog_id": None, "timezone": None},
        "accounts": {},
        "tiktok_account_type": None,
        "render": {"style": "tiktok-native", "text_color_default": "light", "format": "png"},
        "voice": {
            "case": "sentence",
            "max_emoji_caption": 1,
            "emoji_on_screen": 0,
            "em_dashes": False,
            "max_hashtags": 5,
        },
        "posting_slots": [],
        "compliance": {
            "no_product_claims": True,
            "no_measured_child_claims": True,
            "no_fabricated_credentials": True,
            "ai_label_required": True,
            "notes": "TODO - operator to fill in this persona's compliance framing.",
        },
        "brief": "TODO - operator to fill in",
        "notes": "",
    }


README_TEMPLATE = """\
# {display_name} — setup checklist

This directory was created by `scaffold.py`. Nothing here is ready to generate
or post yet. Before this persona can be used with gallery.py / batch.py /
learn/track.py / post/publish.py, the operator needs to supply:

- [ ] **persona.json**: replace every "TODO" —
      - `brief`: this persona's voice/backstory system prompt. Until this is a
        real string with no "TODO" in it, `copy/captions.py` (via
        `personas.brief()`) REFUSES to generate captions for this persona
        rather than falling back to another persona's voice.
      - `metricool.blog_id`: this persona's brand id in Metricool. Until this
        is set, every posting command (and `learn/track.py pull`) REFUSES to
        run rather than risk posting under the wrong (or no) brand.
      - `accounts`, `tiktok_account_type`, `posting_slots`, `compliance.notes`.
- [ ] **character sheet**: a character brief/backstory doc (see
      `../danielle/docs/dani-character-brief.md` for the shape), plus
      generated/selected reference stills in `character/{name}/stills/`
      (PNG, 1080x1920 or croppable to it). `character/{name}/manifest.json`
      is optional but strongly recommended — it is what tells the renderer
      where this persona's eyes are in each still, so on-screen text never
      lands across her face. Without it, batch.py falls back to a hand-measured
      table that only exists for Dani's stills.
- [ ] **copy library**: `copy/drafts-*.md` (hooks, by territory/density — see
      `../danielle/copy/drafts-*.md` for the format `batch.py` parses) and/or
      `copy/value-*.md` + `copy/ask-and-intro.md` (the gallery.py library
      format — see `../danielle/copy/value-01-meals.md`).
- [ ] **Metricool brand**: create or identify this persona's brand in the
      Metricool account, then put its blogId into persona.json above. The
      token/userId stay in `~/.dani-metricool.env` — shared, never per-persona.

Nothing above was guessed on this persona's behalf. In particular, this
persona's identity/backstory is the operator's call, not scaffold.py's.
"""


def scaffold(name: str) -> str:
    name = name.strip().lower()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise SystemExit(f"FATAL: {name!r} is not a usable persona name "
                         f"(letters, digits, - and _ only)")

    root = os.path.join(GTM_ROOT, name)
    if os.path.exists(root):
        raise SystemExit(f"FATAL: {root} already exists. scaffold.py refuses to "
                         f"touch an existing persona directory — remove it first "
                         f"if you really mean to recreate it from scratch.")

    dirs = [
        root,
        os.path.join(root, "character", name, "stills"),
        os.path.join(root, "copy"),
        os.path.join(root, "learn"),
        os.path.join(root, "render", "out"),
        os.path.join(root, "post"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    persona_json_path = os.path.join(root, "persona.json")
    with open(persona_json_path, "w", encoding="utf-8") as fh:
        json.dump(persona_template(name), fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    readme_path = os.path.join(root, "README.md")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(README_TEMPLATE.format(display_name=name.capitalize(), name=name))

    _register(name)

    print(f"scaffolded {root}/")
    for d in dirs[1:]:
        print(f"  {os.path.relpath(d, GTM_ROOT)}/")
    print(f"  persona.json  (template — see {os.path.relpath(readme_path, GTM_ROOT)})")
    print(f"  README.md")
    return root


def _register(name: str) -> None:
    """Add a stub entry to personas.json if this name is not already there.
    Never overwrites an existing entry."""
    registry = {}
    if os.path.isfile(REGISTRY_PATH):
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            registry = json.load(fh)
    if name in registry:
        return
    registry[name] = {"root": f"../{name}", "blog_id": None, "timezone": None}
    with open(REGISTRY_PATH, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"registered {name!r} in {os.path.relpath(REGISTRY_PATH, GTM_ROOT)} "
         f"(blog_id/timezone null until persona.json sets them)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="new persona's short name, e.g. chloe")
    a = ap.parse_args(argv)
    scaffold(a.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
