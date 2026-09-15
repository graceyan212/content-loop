#!/usr/bin/env python3
"""
batch.py — the batch driver. Hooks in, TikTok Photo-post PNGs (or MP4s) out.

    python batch.py --run-id 2026-07-27 --count 20 [--format png|mp4|both]
                    [--territory ...] [--out render/out/] [--max-reuse N]

What it does, in order:

  1. Parses hooks out of copy/drafts-*.md (## territory / ### short|wall / numbered list).
  2. Runs the deterministic compliance gate (copy/gates.py) BEFORE anything renders.
  3. Runs the dedup gate against copy/bank.json (exact sig + fuzzy sig).
  4. Assigns each surviving hook a still, paired by MOOD. Stills MAY be reused
     freely (see below), just never twice in a row, and never if the manifest
     says to hold one back.
  5. Mints variantId = <runId>-v<NN> and renders through the EXISTING renderer
     (render/compose.py -> render/overlay.py), passing the still's safe_text_zone
     so the type never lands on her eyes.
  5b. Generates the other two copy atoms per post via copy/captions.py: caption
      (description text) + hashtags (array, <=5) + self_comment. Live LLM first
      via the TrueFoundry gateway, deterministic template fallback on failure.
      Every caption/self_comment is run through copy/gates.py, same as hooks.
      Pass --no-captions to skip this stage entirely.
  6. Writes copy/batch-<runId>.json and appends the shipped hooks to copy/bank.json.

The renderer is called, never reimplemented. See render/compose.py.
The caption stage is called, never reimplemented. See copy/captions.py.

STILL REUSE: earlier versions consumed each still exactly once, which capped a
run at the number of usable stills. That was wrong for this account — the
reference account it imitates runs ONE image across its whole feed and varies
only the framing. So reuse is unlimited by default; `--max-reuse N` restores a
cap (N=1 is the old behaviour). The only hard rule left is that two CONSECUTIVE
posts in the scheduled sequence never share a still, so the feed does not read
as a copy-paste.

NOTE ON text_style: the default is `tiktok-native` (centred white text, soft
shadow, no bands) because that is what the live posts look like. The old
white-bar/notes-app rotation is still available via `--text-style rotate`. The
neo-brutalist arm in overlay.py hard-codes `uppercase=True`, which would destroy
the all-lowercase house voice the hooks are written in, so it is excluded from
that rotation unless you pass --allow-neo-brutalist. That is a renderer
property, not a bug introduced here.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import string
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))     # ugc-pipeline/ (CODE root)
CODE_RENDER_DIR = os.path.join(HERE, "render")         # ugc-pipeline/render (compose.py, overlay.py)
CODE_COPY_DIR = os.path.join(HERE, "copy")             # ugc-pipeline/copy   (gates.py, captions.py)

# compose.py does a bare `import overlay`, so render/ must be importable.
# These two are CODE-root paths — they hold the shared modules, never persona
# data — so they are correct regardless of which persona a run targets.
for _p in (CODE_RENDER_DIR, CODE_COPY_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import compose as cp           # noqa: E402  (render/compose.py — the existing renderer)
import overlay as ov           # noqa: E402  (render/overlay.py)
import gates                   # noqa: E402  (copy/gates.py)
import captions as capmod      # noqa: E402  (copy/captions.py — caption/hashtags/self_comment stage)
import personas               # noqa: E402  (personas.py — persona registry + resolution)
import settings as cset       # noqa: E402  (copy/settings.py — ONE setting impl, shared with gallery.py)

# ---------------------------------------------------------------------------
# DATA paths below are per-persona (they live under a persona's own root, e.g.
# ../danielle or ../chloe), NOT under this CODE root. The module-level
# constants here are only the "dani" defaults, kept as plain os.path.join
# calls (no FS access, so `import batch` can never fail or touch disk) —
# they exist purely so every function keeps working with zero args, exactly
# as before this file moved. run()/main() always resolve the actual persona
# via personas.resolve() and pass its data paths through explicitly instead
# of relying on these fallbacks.
#
# The character in use. character/manifest.json + character/stills/ described a
# DIFFERENT woman and have been archived to _archive-wrong-person/; do not point
# back at them. character/dani/manifest.json does not exist yet (another pass
# writes it) — load_stills() degrades gracefully when it is absent.
# ---------------------------------------------------------------------------
_DANI_ROOT = os.path.normpath(os.path.join(HERE, "..", "danielle"))
CHARACTER_DIR = os.path.join(_DANI_ROOT, "character")
DANI_DIR = os.path.join(CHARACTER_DIR, "dani")
STILLS_DIR = os.path.join(DANI_DIR, "stills")
MANIFEST_PATH = os.path.join(DANI_DIR, "manifest.json")
SCENES_PATH = os.path.join(CHARACTER_DIR, "scenes.py")
COPY_DIR = os.path.join(_DANI_ROOT, "copy")            # persona DATA copy dir (dani default)
BANK_PATH = os.path.join(COPY_DIR, "bank.json")
RENDER_DIR = os.path.join(_DANI_ROOT, "render")        # persona's render dir (holds out/; dani default)

TERRITORIES = ("relationship", "momguilt", "screentime", "benchmark")
DENSITIES = ("short", "wall")

# Dani's two files and Dani's four territories. Both are now FALLBACKS, not the
# rule: DRAFT_FILES named her files literally, so a second persona's drafts were
# never opened, and TERRITORIES whitelisted her headings, so anything else was
# parsed and then dropped with a WARN. Either one alone silently yields a persona
# with zero hooks. Kept as defaults so Dani, who declares neither in persona.json,
# behaves exactly as before.
DRAFT_FILES = ("drafts-relationship-guilt.md", "drafts-screentime-benchmark.md")


def draft_files_in(copy_dir: str) -> list[str]:
    """Every drafts-*.md in a persona's copy dir, sorted. Globbing beats a literal
    tuple here: a new draft file is picked up by existing, and for Dani the glob
    returns exactly the two files DRAFT_FILES named."""
    if not os.path.isdir(copy_dir):
        return []
    return sorted(f for f in os.listdir(copy_dir)
                  if f.startswith("drafts-") and f.endswith(".md"))


def persona_territories(p: dict) -> tuple[str, ...]:
    """Territory headings this persona's drafts may use. persona.json's
    `territories` wins; TERRITORIES is the fallback."""
    t = (p.get("config") or {}).get("territories")
    return tuple(t) if t else TERRITORIES

# duration arm, keyed by text_density — a wall of 70 words needs the 8s read.
# Only consulted by the mp4 arms; a PNG has no duration.
DURATION_BY_DENSITY = {"short": 5.0, "wall": 8.0}

# The live posts are TikTok Photo posts, so stills are the default deliverable.
DEFAULT_FORMAT = cp.DEFAULT_FORMAT              # "png"
FORMATS = cp.FORMATS                           # ("png", "mp4", "both")

# The look the live posts actually use. "rotate" restores the old
# white-bar/notes-app(/neo-brutalist) test rotation.
DEFAULT_TEXT_STYLE = "tiktok-native"
TEXT_STYLE_CHOICES = tuple(ov.STYLES) + ("rotate",)

FRAME_W, FRAME_H = 1080, 1920
# Vertical band the text may occupy at all: below TikTok's top chrome, above its
# caption + right rail. overlay.DEFAULT_SAFE_ZONE stops at 1480; derived zones go
# to 1560 because a 40-80 word wall needs the extra height and 360px of bottom
# margin still clears TikTok's caption block.
ZONE_X, ZONE_W = 72, 936
ZONE_TOP, ZONE_BOTTOM = 300, 1560
EYE_CLEARANCE = 90        # px of air we insist on between type and her eye line
MIN_ZONE_H = 260

# ---------------------------------------------------------------------------
# Eye-line fallback, used ONLY when the manifest is absent or its entry has no
# usable eye_line_y / safe_text_zone.
#
# PROVENANCE: measured by eye off a 50px-gridded, cover-fit-to-1080x1920 contact
# sheet of the stills, to roughly +/-40px. These are a stopgap so a missing
# manifest does not silently put type across her face; the manifest is
# authoritative the moment it exists and always wins over this table.
#
# character/dani/* measured 2026-07-28 (character/dani/manifest.json does not
# exist yet). For 02-car-with-owen the value is the LOWEST eye line in frame —
# Owen's, in the back seat — not hers, because the guarantee is "type crosses
# nobody's eyes". Note both dani stills are TIGHT face crops: clearing the eye
# line by EYE_CLEARANCE still leaves the derived band starting over her mouth /
# chin. Real safe_text_zone / safe_text_zone_wall values in the manifest will fix
# that properly; this table only prevents the worst case.
# The 12 entries below the dani ones are for the ARCHIVED character
# (_archive-wrong-person/) and are dead unless STILLS_DIR is pointed back.
# ---------------------------------------------------------------------------
FALLBACK_EYE_LINE_Y = {
    "01-car-selfie": 630,
    "02-car-with-owen": 760,
    "01-minivan-selfie": 700,
    "02-kitchen-mug": 580,
    "03-bed-edge-night": 810,
    "04-pickup-line": 690,
    "05-couch-tv-glow": 590,
    "06-bathroom-mirror": 600,
    "07-kitchen-table-laptop": 500,
    "08-doorway-hallway": 580,
    "09-grocery-aisle": 700,
    "10-driveway-dusk": 760,
    "11-folding-laundry": 420,
    "12-porch-selfie": 620,
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ===========================================================================
# 1. hook parsing
# ===========================================================================
_H2 = re.compile(r"^##\s+(.+?)\s*$")
_H3 = re.compile(r"^###\s+(.+?)\s*$")
_ITEM = re.compile(r"^\s*\d+[.)]\s+(.*\S)\s*$")
_POSTURE = re.compile(r"^claim_posture:\s*([a-z-]+)\s*$", re.I)


def parse_hooks(copy_dir: str = COPY_DIR, files=None, territories=None) -> list[dict]:
    """
    Walk the draft markdown and return one dict per numbered hook.

    Structure is `## <territory>` then `### short` / `### wall` then a numbered
    list. Item numbers are NOT assumed to restart per section (the screentime
    file numbers 1..40 straight through), so ordinals come from position.

    A territory may also declare a posture with a `claim_posture: <name>` line
    directly under its `## heading`. It applies to every hook in that territory
    until the next `##`. Hooks default to "no-product", so every existing file is
    unaffected. This exists so a file that MUST carry a posture cannot be
    scheduled without it: the posture is what the gate checks and what makes
    overlay.py draw the "Dramatization" tag, and having those two read from
    different places is how you ship the exemption without the mitigation.
    """
    if files is None:
        files = draft_files_in(copy_dir)
    allowed = tuple(territories) if territories else TERRITORIES

    hooks: list[dict] = []
    for fname in files:
        path = os.path.join(copy_dir, fname)
        if not os.path.isfile(path):
            log(f"WARN: draft file missing, skipping: {path}")
            continue
        territory = None
        density = None
        posture = "no-product"
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                m = _H2.match(line)
                if m:
                    name = m.group(1).strip().lower()
                    territory = name if name in allowed else None
                    if territory is None:
                        log(f"WARN: {fname}:{lineno} unknown territory heading "
                            f"{m.group(1)!r}; its hooks are skipped")
                    density = None
                    posture = "no-product"
                    continue
                m = _POSTURE.match(line)
                if m:
                    want = m.group(1).strip().lower()
                    if want in gates.CLAIM_POSTURES:
                        posture = want
                    else:
                        raise SystemExit(
                            f"{fname}:{lineno}: unknown claim_posture {want!r}. "
                            f"Known: {', '.join(gates.CLAIM_POSTURES)}")
                    continue
                m = _H3.match(line)
                if m:
                    name = m.group(1).strip().lower()
                    density = name if name in DENSITIES else None
                    if density is None:
                        log(f"WARN: {fname}:{lineno} unknown density heading {m.group(1)!r}")
                    continue
                m = _ITEM.match(line)
                if m and territory and density:
                    text = m.group(1).strip()
                    if text:
                        hooks.append({
                            "hook_text": text,
                            "territory": territory,
                            "text_density": density,
                            "source_file": fname,
                            "source_line": lineno,
                            "claim_posture": posture,
                        })
    return hooks


# ===========================================================================
# 2. dedup signatures + the bank
# ===========================================================================
_PUNCT = str.maketrans("", "", string.punctuation + "“”‘’–—…")

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


def sig_exact(text: str) -> str:
    """Lowercased, punctuation and whitespace stripped."""
    t = (text or "").lower().replace("’", "").replace("'", "")
    t = t.translate(_PUNCT)
    return re.sub(r"\s+", "", t)


def sig_fuzzy(text: str) -> str:
    """Sorted set of content words (stopwords dropped), joined."""
    t = (text or "").lower().replace("’", "").replace("'", "")
    t = t.translate(_PUNCT)
    words = {w for w in t.split() if w and w not in STOPWORDS}
    return " ".join(sorted(words))


def load_bank(path: str = BANK_PATH) -> dict:
    if not os.path.isfile(path):
        return {"version": 1, "entries": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"FATAL: {path} exists but will not parse ({e}). "
                         f"Refusing to run — fix or move it, or the dedup gate is blind.")
    if isinstance(data, list):                 # tolerate a bare-array bank
        data = {"version": 1, "entries": data}
    data.setdefault("entries", [])
    return data


def save_bank(bank: dict, path: str = BANK_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bank, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ===========================================================================
# 3. stills: manifest with graceful degradation
# ===========================================================================
# Words that make a mood/expression read HEAVY vs WARM. Used both for stills
# (from the manifest `mood`, or the scene `expression` when it is absent) and,
# with a second list, for hooks.
STILL_HEAVY = (
    "exhausted", "drained", "numb", "vacant", "blank", "deflated", "defeated",
    "flat", "tired", "weary", "hollow", "slack", "checked out", "bleak", "heavy",
    "resigned", "grim", "sad", "low", "spent", "worn", "empty", "dark", "bereft",
    "distant", "withdrawn", "confessional", "raw", "unimpressed", "indifferent",
)
STILL_WARM = (
    "warm", "soft", "fond", "tender", "gentle", "hopeful", "amused", "wry",
    "light", "calm", "content", "relieved", "bright", "smile", "smiling",
    "playful", "affectionate", "open", "wistful", "steady", "composed",
)

# Notes phrasings that mean "do not ship this still".
# `hold\w*\s+(?:\w+\s+){0,3}?back` is what catches the real-world phrasing
# "Recommend holding this one back or re-rolling" as well as a plain
# "hold back" / "held it back".
#
# These must be TIGHT. A loose `exclude` matched notes describing a text
# REGION being excluded ("the safe zone already excludes it", "it is excluded
# here by the clearance rule") and wrongly benched two good stills, so every
# pattern below has to name the image itself.
_HOLD_BACK = re.compile(
    r"hold\w*\s+(?:this|it|that)\b(?:\s+\w+){0,2}\s+back\b"   # "holding this one back"
    r"|\bhold\s+back\b|\bheld\s+back\b|\bholdback\b|\bwithhold\b"
    r"|\b(?:do\s*not|dont|don t)\s+(?:use|ship|publish|post)\b"
    # "unusable" and "not usable" MUST name the image. Bare `\bunusable\b` broke the
    # file's own stated rule that every pattern here names the still, and it did the
    # exact damage that rule was written to prevent: a Ray manifest note describing
    # an oversized-but-unusable text ZONE benched all three of his stills, and the
    # batch died with "0 usable stills". Same false-positive class as the loose
    # `exclude` that already benched two good stills once.
    r"|\b(?:this|the)\s+(?:one|still|image|frame|shot|photo)\s+is\s+"
    r"(?:unusable|not\s+usable)\b"
    r"|\bunusable\s+(?:still|image|frame|shot|photo)\b"
    r"|\bquarantine\b"
    r"|\bexclude\s+(?:this|the)\s+(?:still|image|frame|shot|photo)\b"
    r"|\bsave\s+(?:this\s+)?for\s+later\b"
    r"|\bnot\s+for\s+(?:use|publication|the\s+batch|batch)\b",
    re.I)


def _lex_score(text: str, heavy=STILL_HEAVY, warm=STILL_WARM) -> float:
    """Map a free-text mood onto [0,1], 1 = heaviest."""
    t = " " + (text or "").lower() + " "
    h = sum(1 for w in heavy if w in t)
    w = sum(1 for w in warm if w in t)
    if h == 0 and w == 0:
        return 0.5
    raw = (h - w) / 3.0
    return 0.5 + 0.5 * max(-1.0, min(1.0, raw))


def _scene_expressions(scenes_path: str = SCENES_PATH) -> dict:
    """
    slug -> expression text, read STATICALLY out of character/scenes.py.

    Parsed with `ast` rather than imported, because scenes.py imports gen.py
    which reads ANTHROPIC_* env vars at module scope and would blow up here.
    This is the fallback used when character/manifest.json does not exist yet.
    """
    out: dict[str, str] = {}
    if not os.path.isfile(scenes_path):
        return out
    try:
        tree = ast.parse(open(scenes_path, encoding="utf-8").read())
    except (OSError, SyntaxError) as e:
        log(f"WARN: could not parse {scenes_path} for the mood fallback: {e}")
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        slug = kw.get("slug")
        if not (isinstance(slug, ast.Constant) and isinstance(slug.value, str)):
            continue
        parts = []
        for field in ("expression", "location", "action"):
            v = kw.get(field)
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
        out[slug.value] = " ".join(parts)
    return out


def _valid_zone(z) -> bool:
    if not isinstance(z, dict):
        return False
    try:
        x, y, w, h = (int(z[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return False
    return w >= 200 and h >= 120 and x >= 0 and y >= 0 and x + w <= FRAME_W and y + h <= FRAME_H


def zone_from_eye_line(eye_line_y) -> dict | None:
    """
    Derive a safe text zone that clears her eyes, given only eye_line_y.

    Takes whichever of the two bands (above the eyes / below the eyes) is
    taller, so the type has room without ever crossing the eye line.
    """
    try:
        eye = int(eye_line_y)
    except (TypeError, ValueError):
        return None
    if not (0 < eye < FRAME_H):
        return None
    below_y = eye + EYE_CLEARANCE
    below_h = ZONE_BOTTOM - below_y
    above_y = ZONE_TOP
    above_h = (eye - EYE_CLEARANCE) - above_y
    if below_h >= above_h and below_h >= MIN_ZONE_H:
        return {"x": ZONE_X, "y": below_y, "w": ZONE_W, "h": below_h}
    if above_h >= MIN_ZONE_H:
        return {"x": ZONE_X, "y": above_y, "w": ZONE_W, "h": above_h}
    return None


def load_stills(manifest_path: str = MANIFEST_PATH,
                stills_dir: str = STILLS_DIR,
                scenes_path: str = SCENES_PATH) -> tuple[list[dict], dict]:
    """
    Build the still pool. Returns (stills, report).

    Degrades in this order, per field:
      manifest.json entry  ->  scenes.py expression  ->  neutral default.
    A missing manifest is a warning, not an error.
    """
    report = {"manifest_present": False, "manifest_error": None,
              "held_back": [], "missing_files": [], "not_in_manifest": [],
              "derived_zone_from_eye_line": [], "zone_fallback_default": [],
              "zone_crosses_eye_line": [], "wall_zone_present": [],
              "wall_zone_absent": [], "wall_zone_invalid": []}

    entries: list[dict] = []
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                # tolerate {"stills":[...]} as well as a bare array
                raw = raw.get("stills") or raw.get("entries") or []
            if not isinstance(raw, list):
                raise ValueError("manifest is not an array of still records")
            entries = [e for e in raw if isinstance(e, dict)]
            report["manifest_present"] = True
        except (OSError, json.JSONDecodeError, ValueError) as e:
            report["manifest_error"] = str(e)
            log(f"WARN: {manifest_path} present but unreadable ({e}); "
                f"falling back to scenes.py expressions")
    else:
        log(f"WARN: {manifest_path} does not exist. Mood pairing falls back to the "
            f"`expression` strings in character/scenes.py, and the eye line to "
            f"batch.FALLBACK_EYE_LINE_Y (measured by eye, +/-40px). Re-run once the "
            f"manifest lands — it is authoritative and always wins.")

    if not os.path.isdir(stills_dir):
        raise SystemExit(f"FATAL: stills directory not found: {stills_dir}")
    # (each still gets _setting_tag attached below, after the manifest merge)
    on_disk = sorted(f for f in os.listdir(stills_dir)
                     if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    expressions = _scene_expressions(scenes_path)

    by_file = {}
    for e in entries:
        f = e.get("file")
        if isinstance(f, str) and f:
            by_file[os.path.basename(f)] = e

    for f in by_file:
        if f not in on_disk:
            report["missing_files"].append(f)
            log(f"WARN: manifest lists {f} but it is not in {stills_dir}; skipping")

    stills: list[dict] = []
    for fname in on_disk:
        e = by_file.get(fname, {})
        if not e and report["manifest_present"]:
            report["not_in_manifest"].append(fname)
        stem = os.path.splitext(fname)[0]
        # slug in scenes.py is the stem minus the numeric prefix ("01-minivan-selfie")
        slug = re.sub(r"^\d+[-_]", "", stem)

        notes = e.get("notes") or ""
        if isinstance(notes, str) and _HOLD_BACK.search(notes):
            report["held_back"].append({"file": fname, "notes": notes})
            log(f"HOLD-BACK: {fname} excluded — notes say: {notes!r}")
            continue

        # The manifest's `mood` is a single word (tired / flat / heavy /
        # distracted / warm), which alone gives only three distinct scores. The
        # longer `expression` sentence breaks ties, so blend them 70/30 with
        # mood dominant.
        mood = e.get("mood") or ""
        expression = e.get("expression") or expressions.get(slug) or ""
        if mood and expression:
            heaviness = 0.7 * _lex_score(mood) + 0.3 * _lex_score(expression)
            mood_source = "manifest.mood + manifest.expression"
        elif mood:
            heaviness = _lex_score(mood)
            mood_source = "manifest.mood"
        elif expression:
            heaviness = _lex_score(expression)
            mood_source = ("manifest.expression" if e.get("expression")
                           else "scenes.py:expression")
        else:
            heaviness = 0.5
            mood_source = "none"
        mood = (mood + " " + expression).strip()

        eye_line_y = e.get("eye_line_y")
        eye_source = "manifest.eye_line_y"
        try:
            int(eye_line_y)
        except (TypeError, ValueError):
            eye_line_y = FALLBACK_EYE_LINE_Y.get(stem, FALLBACK_EYE_LINE_Y.get(slug))
            eye_source = ("batch.FALLBACK_EYE_LINE_Y (measured by eye)"
                          if eye_line_y is not None else "none")

        zone = e.get("safe_text_zone")
        if _valid_zone(zone):
            zone = {k: int(zone[k]) for k in ("x", "y", "w", "h")}
            zone_source = "manifest.safe_text_zone"
            try:
                eye = int(eye_line_y)
                if zone["y"] <= eye <= zone["y"] + zone["h"]:
                    report["zone_crosses_eye_line"].append(
                        {"file": fname, "eye_line_y": eye, "safe_text_zone": zone})
                    log(f"WARN: {fname} manifest safe_text_zone {zone} spans its own "
                        f"eye_line_y={eye}. Using it as given (the manifest is "
                        f"authoritative) but the type may land on her eyes.")
            except (TypeError, ValueError):
                pass
        else:
            if zone is not None:
                log(f"WARN: {fname} has an unusable safe_text_zone {zone!r}; ignoring it")
            derived = zone_from_eye_line(eye_line_y)
            if derived:
                zone, zone_source = derived, "derived from eye_line_y"
                report["derived_zone_from_eye_line"].append({"file": fname, "zone": zone})
            else:
                zone = dict(ov.DEFAULT_SAFE_ZONE)
                zone_source = "overlay.DEFAULT_SAFE_ZONE"
                report["zone_fallback_default"].append(fname)

        # OPTIONAL per-still wall zone. The manifest's safe_text_zone is sized
        # for 8-15 word short hooks; a 40-80 word `wall` needs its own, taller
        # rect. When this key is present it is authoritative for wall hooks (see
        # choose_zone); when absent, walls fall back to the computed band.
        wall_zone = e.get("safe_text_zone_wall")
        if _valid_zone(wall_zone):
            wall_zone = {k: int(wall_zone[k]) for k in ("x", "y", "w", "h")}
            report["wall_zone_present"].append({"file": fname, "zone": wall_zone})
            try:
                eye = int(eye_line_y)
                if wall_zone["y"] <= eye <= wall_zone["y"] + wall_zone["h"]:
                    log(f"WARN: {fname} manifest safe_text_zone_wall {wall_zone} spans "
                        f"its own eye_line_y={eye}. Using it as given (the manifest is "
                        f"authoritative) but the type may land on her eyes.")
            except (TypeError, ValueError):
                pass
        else:
            if wall_zone is not None:
                log(f"WARN: {fname} has an unusable safe_text_zone_wall {wall_zone!r}; "
                    f"ignoring it — wall hooks fall back to the computed band")
                report["wall_zone_invalid"].append({"file": fname, "value": wall_zone})
            wall_zone = None
            report["wall_zone_absent"].append(fname)

        stills.append({
            "file": fname,
            "path": os.path.join(stills_dir, fname),
            "slug": slug,
            "mood": mood,
            "mood_source": mood_source,
            "heaviness": heaviness,
            "safe_text_zone": zone,
            "safe_text_zone_source": zone_source,
            # Optional, wall-hook-only zone straight from the manifest (None when
            # the key is absent). See choose_zone.
            "safe_text_zone_wall": wall_zone,
            # Largest band that still clears her eyes, used only when the
            # manifest zone cannot hold the hook (see choose_zone).
            "expanded_zone": zone_from_eye_line(eye_line_y),
            "eye_line_y": eye_line_y,
            "eye_line_source": eye_source,
            "setting": e.get("setting"),
            "framing": e.get("framing"),
            "intro_candidate": e.get("intro_candidate"),
            "notes": notes,
        })
    # Normalized setting tag per still, so pair_by_mood can match a post's setting
    # to a photo it could plausibly have been taken in. Explicit `setting_tag` in
    # the manifest wins; otherwise inferred from the free-text `setting` prose.
    # AUTO-NORMALIZE zones into 1080x1920 render space. The overlay is always
    # 1080x1920, but a still may be any size (941x1672, 1024x1536, 1086x1448 have
    # all appeared), and whoever measures a zone naturally does it in the image's
    # OWN pixels. Applying native coords to the render frame put text ~350px off
    # on one still and this is the third time the mistake has recurred, so it is
    # corrected here once rather than trusted to each manifest author.
    # An entry already carrying *_native keys was normalized by hand; leave it.
    for e in entries:
        if "eye_line_y_native" in e or "safe_text_zone_native" in e:
            continue
        f = e.get("file", "")
        path = f if os.path.isabs(f) else os.path.join(os.path.dirname(stills_dir),
                                                       os.path.basename(stills_dir), 
                                                       os.path.basename(f))
        if not os.path.exists(path):
            path = os.path.join(stills_dir, os.path.basename(f))
        try:
            from PIL import Image
            fw, fh = Image.open(path).size
        except Exception:
            continue
        if (fw, fh) == (FRAME_W, FRAME_H):
            continue
        sc = max(FRAME_W / fw, FRAME_H / fh)
        ox, oy = (fw * sc - FRAME_W) / 2.0, (fh * sc - FRAME_H) / 2.0
        for k in ("safe_text_zone", "safe_text_zone_wall"):
            z = e.get(k)
            if not isinstance(z, dict):
                continue
            e[k + "_native"] = dict(z)
            nz = {"x": max(60, round(z["x"] * sc - ox)), "y": max(60, round(z["y"] * sc - oy)),
                  "w": round(z["w"] * sc), "h": round(z["h"] * sc)}
            nz["w"] = min(nz["w"], FRAME_W - 60 - nz["x"])
            nz["h"] = min(nz["h"], FRAME_H - 60 - nz["y"])
            e[k] = nz
        if isinstance(e.get("eye_line_y"), (int, float)):
            e["eye_line_y_native"] = e["eye_line_y"]
            e["eye_line_y"] = round(e["eye_line_y"] * sc - oy)
        log(f"NORMALIZED {os.path.basename(f)} zones from {fw}x{fh} -> "
            f"{FRAME_W}x{FRAME_H} (scale {sc:.3f})")

    by_base = {os.path.basename(e.get("file", "")): e for e in entries}
    for s_ in stills:
        # match on BASENAME: a still's `file` is an absolute path while the
        # manifest stores it relative, so a direct == comparison silently missed
        # and every still fell back to prose inference (two came out "any").
        e_ = by_base.get(os.path.basename(s_.get("file", "")), {})
        s_["_setting_tag"] = cset.still_setting(
            {"setting_tag": e_.get("setting_tag"),
             "setting": s_.get("setting"), "framing": s_.get("framing")})
    return stills, report


# ===========================================================================
# 4. hook heaviness (the other half of the mood pairing)
# ===========================================================================
HOOK_HEAVY = (
    "cried", "crying", "cry", "guilt", "ashamed", "shame", "alone", "quiet",
    "silence", "silent", "locked", "dark", "confess", "villain", "empty",
    "deleted", "face down", "hid", "hiding", "dread", "afraid", "scared",
    "worried", "worry", "sorry", "never", "cannot", "couldnt", "wouldnt",
    "stopped", "forgot", "missed", "exhausted", "tired", "took me apart",
    "nobody", "no one", "in the dark", "at night", "bathroom", "bed", "wept",
    "lost", "wrong", "behind", "failed", "cant tell you", "would pay money",
)
HOOK_LIGHT = (
    "laughing", "laughed", "laugh", "funny", "joke", "sweet", "good mood",
    "happy", "popcorn", "movie", "frogs", "guess what", "trampoline",
    "birthday", "gatorade", "burp", "minecraft", "sticker", "swimming",
    "cupcake", "pizza", "party", "playing", "smiling", "grinning",
)

TERRITORY_WEIGHT = {"momguilt": 0.16, "relationship": 0.10,
                    "benchmark": 0.05, "screentime": 0.0}
DENSITY_WEIGHT = {"wall": 0.18, "short": 0.0}


def hook_heaviness(hook: dict) -> float:
    """
    Deterministic [0,1] heaviness for a hook.

    Walls are confessional by construction, momguilt/relationship are the heavy
    territories, and an explicit lexicon does the rest.
    """
    t = " " + hook["hook_text"].lower() + " "
    h = sum(1 for w in HOOK_HEAVY if w in t)
    l = sum(1 for w in HOOK_LIGHT if w in t)
    lex = 0.5 + 0.5 * max(-1.0, min(1.0, (h - l) / 4.0))
    score = (0.66 * lex
             + TERRITORY_WEIGHT.get(hook["territory"], 0.0)
             + DENSITY_WEIGHT.get(hook["text_density"], 0.0))
    return max(0.0, min(1.0, score))


def pair_by_mood(hooks: list[dict], stills: list[dict],
                 max_reuse: int | None = None) -> list[tuple[dict, dict]]:
    """
    Pair each hook with the still whose mood is closest to it. Stills MAY repeat.

    Mood pairing is unchanged in substance — heavy/confessional hooks still land
    on tired/flat stills and lighter ones on warm stills — but a still is no
    longer consumed. With reuse allowed each hook's choice is independent, so
    greedy nearest-heaviness IS the optimum for the 1-D cost
    |hook_heaviness - still_heaviness| (the old sorted matching was only needed
    because the assignment was constrained to a permutation).

    Two constraints remain:
      * SOFT, but always honoured when it can be: never the same still twice in
        a row in the scheduled sequence, so consecutive posts look different
        in-feed. Only violated if there is literally one usable still.
      * `max_reuse`: cap on how many times one still may appear in the run.
        None = unlimited (the default). 1 = the old no-reuse behaviour, in which
        case the run is truncated once every still is spent.

    `hooks` must arrive in SCHEDULED order (it is the interleaved list), because
    the no-two-in-a-row rule is defined over that order. Ties break on
    least-used-so-far then filename, which spreads a small still pool evenly
    instead of hammering the single nearest match.
    """
    used: dict[str, int] = {s["file"]: 0 for s in stills}
    pairs: list[tuple[dict, dict]] = []
    prev: str | None = None

    for h in hooks:
        avail = [s for s in stills
                 if max_reuse is None or used[s["file"]] < max_reuse]
        if not avail:
            log(f"WARN: every still hit --max-reuse {max_reuse}; stopping the batch at "
                f"{len(pairs)} variant(s). Raise --max-reuse or add stills.")
            break

        # SETTING FIRST, mood second. Kitchen copy belongs on the kitchen photo;
        # pairing on mood alone put dinner hooks on a car selfie. An "any" post
        # fits anything, and a setting with no matching still falls back to the
        # whole pool with a warning rather than dropping the hook.
        want = h.get("setting") or cset.setting_of(h)
        h["setting"] = want
        matched = [s for s in avail if cset.compatible(want, s.get("_setting_tag", "any"))]
        if not matched:
            log(f"WARN: no still tagged {want!r} for [{h.get('src','?')}:{h.get('id','?')}]; "
                f"falling back to the whole pool. Generate a {want} photo.")
            matched = avail
        avail = matched

        pool = [s for s in avail if s["file"] != prev] or avail
        if len(pool) == 1 and pool[0]["file"] == prev:
            log(f"WARN: only one usable still ({prev}); consecutive posts will "
                f"share it.")
        best = min(pool, key=lambda s: (abs(h["_heaviness"] - s["heaviness"]),
                                        used[s["file"]], s["file"]))
        used[best["file"]] += 1
        prev = best["file"]
        pairs.append((h, best))
    return pairs


# ===========================================================================
# 4b. zone pre-flight
# ===========================================================================
# Smallest font we will ship per style. overlay.py's fitters bottom out at 30
# (white-bar) and 26 (notes-app); scraping that floor on a 1080-wide frame is
# not readable on a phone, so we would rather widen the zone.
LEGIBLE_FLOOR = {"white-bar": 34, "notes-app": 30, "neo-brutalist": 42,
                 "tiktok-native": 46}

_PREFLIGHT_PNG = os.path.join("/tmp", "danielle_zone_preflight.png")


def _fit_in(text: str, style: str, zone: dict) -> tuple[int, bool, bool]:
    """(font_size, truncated, fits) for `text` in `zone`. Renders to a scratch PNG."""
    r = ov.render_overlay(text, _PREFLIGHT_PNG, style=style, safe_zone=zone)
    return r.font_size, bool(getattr(r, "truncated", False)), bool(r.fits_safe_zone)


def choose_zone(text: str, style: str, still: dict,
                density: str = "short") -> tuple[dict, str, dict]:
    """
    Pick the safe zone to actually render in. Returns (zone, source, diagnostics).

    Order of precedence:

      1. `safe_text_zone_wall` from the manifest, for `wall` hooks ONLY. The
         manifest's plain safe_text_zone is sized for SHORT hooks (8-15 words)
         and a 40-80 word wall truncates in every one of them, so a wall gets its
         own rect when the manifest supplies one. Authoritative — used as given.
      2. `safe_text_zone` from the manifest, whenever it holds the hook legibly.
      3. Otherwise the computed band: the largest band that still clears the
         still's eye_line_y. Truncating the copy is never acceptable, so this is
         where a short zone + a long hook ends up. Keeps the hard guarantee
         ("type never crosses her eyes") while preserving the whole hook.

    A wall zone that truncates the hook anyway is NOT shipped: we warn and drop
    through to (3), because mangled copy is worse than an off-spec rect.
    """
    diag = {}
    floor = LEGIBLE_FLOOR.get(style, 30)

    wall = still.get("safe_text_zone_wall")
    if density == "wall" and wall:
        wsize, wtrunc, _ = _fit_in(text, style, wall)
        diag["wall_zone_fit"] = {"font_size": wsize, "truncated": wtrunc, "zone": wall}
        if not wtrunc:
            return wall, "manifest.safe_text_zone_wall", diag
        log(f"WARN: {still['file']} manifest safe_text_zone_wall {wall} still truncated a "
            f"wall hook at {wsize}px; falling through to the computed band rather than "
            f"shipping truncated copy.")
    elif density == "wall":
        diag["wall_zone"] = "absent from manifest; using the short-hook precedence"

    base = still["safe_text_zone"]
    size, trunc, _ = _fit_in(text, style, base)
    diag["manifest_zone_fit"] = {"font_size": size, "truncated": trunc}
    if not trunc and size >= floor:
        return base, still["safe_text_zone_source"], diag

    exp = still.get("expanded_zone")
    if not exp:
        diag["expanded_zone"] = "unavailable (no usable eye_line_y)"
        return base, still["safe_text_zone_source"] + " (forced; no expanded zone)", diag

    esize, etrunc, _ = _fit_in(text, style, exp)
    diag["expanded_zone_fit"] = {"font_size": esize, "truncated": etrunc, "zone": exp}
    # Only move if the expanded zone is genuinely better.
    if (trunc and not etrunc) or (esize > size and not etrunc):
        why = "manifest zone truncated the hook" if trunc else \
              f"manifest zone only fit {size}px (floor {floor}px)"
        return exp, f"expanded to clear eye_line_y ({why})", diag
    return base, still["safe_text_zone_source"], diag


# ===========================================================================
# 5. arms
# ===========================================================================
def pick_text_style(index: int, density: str, allow_nb: bool,
                    text_style: str = DEFAULT_TEXT_STYLE) -> str:
    """
    Resolve the text_style for variant `index`.

    `text_style` is normally a fixed style name — tiktok-native by default,
    because that is what the live posts look like. Pass "rotate" for the old
    A/B rotation: white-bar is the control (the @rareZuhair look) so it carries
    the majority and every third variant gets notes-app. neo-brutalist stays
    opt-in only, because overlay._style_neo_brutalist forces uppercase and the
    house voice is strictly lowercase.
    """
    if text_style != "rotate":
        return text_style
    if allow_nb and index % 4 == 3:
        return "neo-brutalist"
    return "notes-app" if index % 3 == 2 else "white-bar"


# ===========================================================================
# 6. the run
# ===========================================================================
def interleave(hooks: list[dict]) -> list[dict]:
    """Round-robin across (territory, density) buckets so a batch is not all one thing."""
    buckets: dict[tuple, list] = {}
    for h in hooks:
        buckets.setdefault((h["territory"], h["text_density"]), []).append(h)
    keys = sorted(buckets)
    out = []
    i = 0
    while any(buckets[k] for k in keys):
        k = keys[i % len(keys)]
        if buckets[k]:
            out.append(buckets[k].pop(0))
        i += 1
    return out


def _persona_data_paths(persona: dict) -> dict:
    """DATA paths for one resolved persona (see personas.resolve()), all hung
    off persona['root'] — never off HERE (the shared CODE root)."""
    root = persona["root"]
    name = persona["name"]
    character_dir = os.path.join(root, "character")
    persona_char_dir = os.path.join(character_dir, name)
    copy_dir = os.path.join(root, "copy")
    return {
        "root": root,
        "character_dir": character_dir,
        "stills_dir": os.path.join(persona_char_dir, "stills"),
        "manifest_path": os.path.join(persona_char_dir, "manifest.json"),
        "scenes_path": os.path.join(character_dir, "scenes.py"),
        "copy_dir": copy_dir,
        "bank_path": os.path.join(copy_dir, "bank.json"),
        "render_out_default": os.path.join(root, "render", "out"),
    }


def run(run_id: str, count: int, territories=None, out_root=None,
        claim_posture="no-product", allow_nb=False, dry_run=False,
        fmt=DEFAULT_FORMAT, text_style=DEFAULT_TEXT_STYLE,
        max_reuse: int | None = None, jpeg=True, do_captions: bool = True,
        persona: str = personas.DEFAULT_PERSONA) -> dict:
    if fmt not in FORMATS:
        raise SystemExit(f"FATAL: unknown --format {fmt!r}; expected one of {list(FORMATS)}")
    want_png = fmt in ("png", "both")
    want_mp4 = fmt in ("mp4", "both")

    p = personas.resolve(persona)
    paths = _persona_data_paths(p)
    persona_root = paths["root"]

    out_root = out_root or paths["render_out_default"]
    out_dir = os.path.join(os.path.abspath(out_root), run_id)

    terr_allowed = persona_territories(p)
    draft_files = draft_files_in(paths["copy_dir"])
    all_hooks = parse_hooks(copy_dir=paths["copy_dir"], files=draft_files,
                            territories=terr_allowed)
    log(f"parsed {len(all_hooks)} hooks from {len(draft_files)} draft files "
        f"({', '.join(draft_files) or 'none found'})")
    if not all_hooks:
        raise SystemExit(
            f"FATAL: no hooks parsed for persona {p['name']!r} from "
            f"{paths['copy_dir']}. Looked for drafts-*.md with `## <territory>` "
            f"headings drawn from {list(terr_allowed)}.")

    if territories:
        wanted = set(territories)
        bad = wanted - set(terr_allowed)
        if bad:
            raise SystemExit(f"FATAL: unknown territory {sorted(bad)}; "
                             f"expected any of {list(terr_allowed)}")
        all_hooks = [h for h in all_hooks if h["territory"] in wanted]
        log(f"{len(all_hooks)} hooks after --territory {sorted(wanted)}")

    bank = load_bank(paths["bank_path"])
    bank_exact = {e.get("sig_exact") for e in bank["entries"]}
    bank_fuzzy = {e.get("sig_fuzzy") for e in bank["entries"]}
    log(f"bank holds {len(bank['entries'])} previously shipped hooks")

    rejected: list[dict] = []
    eligible: list[dict] = []
    seen_exact: set[str] = set()
    seen_fuzzy: set[str] = set()

    for h in interleave(all_hooks):
        h = dict(h)
        h["sig_exact"] = sig_exact(h["hook_text"])
        h["sig_fuzzy"] = sig_fuzzy(h["hook_text"])
        h["claim_posture"] = claim_posture

        passed, reason = gates.check(h["hook_text"], claim_posture)
        if not passed:
            log(f"GATE FAIL [{h['territory']}/{h['text_density']} "
                f"{h['source_file']}:{h['source_line']}] {reason}")
            rejected.append({**h, "rejected_by": "compliance-gate", "reason": reason,
                             "gate": {"passed": False, "reason": reason}})
            continue

        if h["sig_exact"] in bank_exact:
            r = "already in bank.json (exact sig)"
        elif h["sig_fuzzy"] in bank_fuzzy:
            r = "already in bank.json (fuzzy sig — near-duplicate of a shipped hook)"
        elif h["sig_exact"] in seen_exact:
            r = "duplicate of another hook in this same run (exact sig)"
        elif h["sig_fuzzy"] in seen_fuzzy:
            r = "duplicate of another hook in this same run (fuzzy sig)"
        else:
            r = None
        if r:
            log(f"DEDUP SKIP [{h['source_file']}:{h['source_line']}] {r}")
            rejected.append({**h, "rejected_by": "dedup-gate", "reason": r,
                             "gate": {"passed": True, "reason": ""}})
            continue

        seen_exact.add(h["sig_exact"])
        seen_fuzzy.add(h["sig_fuzzy"])
        h["_heaviness"] = hook_heaviness(h)
        eligible.append(h)

    log(f"{len(eligible)} hooks passed both gates "
        f"({len(rejected)} rejected)")

    selected = eligible[:count]
    if len(selected) < count:
        log(f"WARN: only {len(selected)} eligible hooks for a requested count of {count}")

    stills, still_report = load_stills(paths["manifest_path"], paths["stills_dir"],
                                       paths["scenes_path"])
    log(f"{len(stills)} usable stills "
        f"({len(still_report['held_back'])} held back by manifest notes)")
    if not stills:
        raise SystemExit("FATAL: no usable stills.")

    if max_reuse is not None and len(selected) > len(stills) * max_reuse:
        cap = len(stills) * max_reuse
        log(f"WARN: {len(selected)} hooks but only {len(stills)} usable stills at "
            f"--max-reuse {max_reuse} (cap {cap}). Truncating this batch to {cap}. "
            f"Raise --max-reuse or add stills to ship more.")
        selected = selected[:cap]

    pairs = pair_by_mood(selected, stills, max_reuse=max_reuse)
    reuse_counts: dict[str, int] = {}
    for _h, _s in pairs:
        reuse_counts[_s["file"]] = reuse_counts.get(_s["file"], 0) + 1
    log(f"still usage across {len(pairs)} variant(s): "
        + ", ".join(f"{k}x{v}" for k, v in sorted(reuse_counts.items()))
        + (f"  (--max-reuse {max_reuse})" if max_reuse is not None else "  (reuse unlimited)"))

    variants = []
    failures = []
    for i, (hook, still) in enumerate(pairs):
        vid = f"{run_id}-v{i + 1:02d}"
        style = pick_text_style(i, hook["text_density"], allow_nb, text_style)
        duration = DURATION_BY_DENSITY[hook["text_density"]]
        # compose() reuses the stem and appends the right extensions per --format.
        out_base = os.path.join(out_dir, f"{vid}.{'mp4' if fmt == 'mp4' else 'png'}")
        png = os.path.join(out_dir, f"{vid}.png")
        jpg = os.path.join(out_dir, f"{vid}.jpg")
        mp4 = os.path.join(out_dir, f"{vid}.mp4")
        zone, zone_source, zone_diag = choose_zone(hook["hook_text"], style, still,
                                                  density=hook["text_density"])
        log(f"ZONE {vid}  density={hook['text_density']:<5} still={still['file']:<22} "
            f"zone={zone} source={zone_source}")
        rec = {
            "variantId": vid,
            "hook_text": hook["hook_text"],
            "territory": hook["territory"],
            "text_density": hook["text_density"],
            "claim_posture": hook["claim_posture"],
            "text_style": style,
            "still": still["file"],
            "format": fmt,
            "duration": duration if want_mp4 else None,
            "gate": {"passed": True, "reason": ""},
            "safe_text_zone": zone,
            "safe_text_zone_source": zone_source,
            "manifest_safe_text_zone": still["safe_text_zone"],
            "manifest_safe_text_zone_wall": still.get("safe_text_zone_wall"),
            "zone_preflight": zone_diag,
            "eye_line_y": still["eye_line_y"],
            "eye_line_source": still["eye_line_source"],
            "still_mood": still["mood"],
            "mood_source": still["mood_source"],
            "hook_heaviness": round(hook["_heaviness"], 4),
            "still_heaviness": round(still["heaviness"], 4),
            "sig_exact": hook["sig_exact"],
            "sig_fuzzy": hook["sig_fuzzy"],
            "source": f"{hook['source_file']}:{hook['source_line']}",
        }
        # Media paths. `media_path` is the primary deliverable for this format;
        # mp4_path / png_path / jpg_path are only present when that file exists,
        # so a consumer cannot mistake a plan for a file.
        if want_png:
            rec["png_path"] = os.path.relpath(png, persona_root)
            if jpeg:
                rec["jpg_path"] = os.path.relpath(jpg, persona_root)
        if want_mp4:
            rec["mp4_path"] = os.path.relpath(mp4, persona_root)
        rec["media_path"] = rec["png_path"] if want_png else rec["mp4_path"]

        if do_captions:
            atoms = capmod.build_atoms(hook["hook_text"], hook["territory"],
                                    hook["text_density"], index=i,
                                    claim_posture=hook["claim_posture"],
                                    persona_brief=personas.brief(p),
                                    persona=p)
            rec["caption"] = atoms["caption"]
            rec["hashtags"] = atoms["hashtags"]
            rec["self_comment"] = atoms["self_comment"]
            rec["caption_source"] = atoms["source"]
            log(f"CAPTION {vid}  source={atoms['source']:<8} "
                f"attempts={atoms['attempts']}  hashtags={atoms['hashtags']}")

        if dry_run:
            rec["rendered"] = False
            variants.append(rec)
            continue

        try:
            res = cp.compose(
                text_color=(still.get("text_color") if isinstance(still, dict) else None) or "light",
                still=still["path"],
                out=out_base,
                text=hook["hook_text"],
                style=style,
                duration=duration,
                fmt=fmt,
                jpeg=jpeg,
                safe_zone=zone,
                dramatization=(hook["claim_posture"] == gates.DRAMATIZED),
                keep_overlay=True,
                verify=True,
            )
        except Exception as e:                                   # noqa: BLE001
            log(f"RENDER FAIL {vid}: {e}")
            rec["rendered"] = False
            rec["render_error"] = str(e)
            failures.append(rec)
            continue

        # Never ship mangled copy: overlay.py drops an ellipsis when the text
        # cannot fit even at its minimum size. That is a failed variant, not a
        # deliverable.
        if res.overlay.get("truncated"):
            log(f"RENDER FAIL {vid}: overlay TRUNCATED the hook in zone {zone}; "
                f"refusing to ship truncated copy")
            for p in (mp4, png, jpg):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            rec["rendered"] = False
            rec["render_error"] = "overlay truncated the hook text with an ellipsis"
            failures.append(rec)
            continue

        rec["rendered"] = True
        rec["font_size"] = res.overlay.get("font_size")
        rec["lines"] = res.overlay.get("lines")
        rec["fits_safe_zone"] = res.overlay.get("fits_safe_zone")
        rec["truncated"] = False
        rec["probe"] = res.probe
        rec["png_bytes"] = res.png_bytes
        rec["jpg_bytes"] = res.jpg_bytes
        variants.append(rec)
        sizes = ""
        if res.png_bytes:
            sizes = f"  png {res.png_bytes // 1024}KB"
            if res.jpg_bytes:
                sizes += f" / jpg {res.jpg_bytes // 1024}KB"
        log(f"OK {vid}  {style:<13} {fmt:<4} {still['file']:<22} "
            f"{res.overlay.get('font_size')}px x{len(res.overlay.get('lines') or [])} lines"
            f"{sizes}")

    shipped = [v for v in variants if v.get("rendered")]

    # bank only what actually shipped
    if shipped and not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        for v in shipped:
            bank["entries"].append({
                "sig_exact": v["sig_exact"],
                "sig_fuzzy": v["sig_fuzzy"],
                "hook_text": v["hook_text"],
                "territory": v["territory"],
                "text_density": v["text_density"],
                "variantId": v["variantId"],
                "runId": run_id,
                "added_at": now,
            })
        save_bank(bank, paths["bank_path"])
        log(f"banked {len(shipped)} hooks -> {paths['bank_path']} "
            f"({len(bank['entries'])} total)")

    batch = {
        "runId": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "out_dir": os.path.relpath(out_dir, persona_root),
        "requested_count": count,
        "format": fmt,
        "text_style": text_style,
        "max_reuse": max_reuse,
        "still_usage": reuse_counts,
        "territories": sorted(territories) if territories else list(terr_allowed),
        "claim_posture": claim_posture,
        "counts": {
            "hooks_parsed": len(all_hooks),
            "gate_rejected": sum(1 for r in rejected if r["rejected_by"] == "compliance-gate"),
            "dedup_skipped": sum(1 for r in rejected if r["rejected_by"] == "dedup-gate"),
            "eligible": len(eligible),
            "rendered": len(shipped),
            "render_failed": len(failures),
        },
        "stills": still_report,
        "variants": variants + failures,
        "rejected": rejected,
    }
    batch_path = os.path.join(paths["copy_dir"], f"batch-{run_id}.json")
    with open(batch_path, "w", encoding="utf-8") as fh:
        json.dump(batch, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    log(f"wrote {batch_path}")
    log(f"DONE: {len(shipped)} rendered, {len(failures)} render failures, "
        f"{len(rejected)} rejected upstream")
    return batch


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Render a batch of Dani hook stills (default) or videos.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--format", dest="fmt", default=DEFAULT_FORMAT, choices=FORMATS,
                    help="png (default; TikTok Photo posts, no ffmpeg) | mp4 | both")
    ap.add_argument("--no-jpeg", action="store_true",
                    help="skip the q92 JPEG sibling next to each PNG")
    ap.add_argument("--text-style", default=DEFAULT_TEXT_STYLE, choices=TEXT_STYLE_CHOICES,
                    help="a fixed overlay style (default tiktok-native, the live-post "
                         "look), or 'rotate' for the old white-bar/notes-app A/B")
    ap.add_argument("--max-reuse", type=int, default=None, metavar="N",
                    help="cap how many times one still may appear in a run "
                         "(default: unlimited; N=1 is the old no-reuse behaviour). "
                         "Two consecutive posts never share a still either way.")
    ap.add_argument("--territory", action="append", default=None,
                    help="repeatable, or comma-separated: "
                         "relationship|momguilt|screentime|benchmark")
    ap.add_argument("--out", default=None, help="output root (default render/out/)")
    ap.add_argument("--claim-posture", default="no-product", choices=gates.CLAIM_POSTURES,
                    help="dramatized-labeled also burns the on-screen Dramatization tag")
    ap.add_argument("--allow-neo-brutalist", action="store_true",
                    help="include the neo-brutalist arm. It UPPERCASES the hook, "
                         "which breaks the all-lowercase house voice.")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan and write batch json, render nothing, bank nothing")
    ap.add_argument("--no-captions", action="store_true",
                    help="skip the caption/hashtags/self_comment stage (copy/captions.py)")
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.")
    a = ap.parse_args(argv)

    terrs = None
    if a.territory:
        terrs = [t.strip().lower() for chunk in a.territory for t in chunk.split(",") if t.strip()]

    if a.count < 1:
        raise SystemExit("FATAL: --count must be >= 1")
    if a.max_reuse is not None and a.max_reuse < 1:
        raise SystemExit("FATAL: --max-reuse must be >= 1 (omit it for unlimited)")

    # Warn BEFORE spending LLM tokens on captions and CPU on renders. Rendering
    # without a Metricool brand is legitimate (you may intend to hand-post), so
    # this is not fatal - but discovering it after a 30-post batch is pure waste.
    try:
        _p = personas.resolve(getattr(a, "persona", None))
        # personas.blog_id(), NOT _p["metricool"]. resolve() returns
        # {name, root, registry_entry, config, persona_json_path} — the metricool
        # block lives under ["config"], so the old `_p.get("metricool")` was always
        # None and this NOTE fired for EVERY persona on EVERY batch, including Dani
        # with a perfectly good 1000001. A warning that is always wrong trains you
        # to ignore the one time it is right.
        if not personas.blog_id(_p):
            print(f"NOTE: persona {_p['name']!r} has no Metricool blog_id. This batch "
                  f"will render and caption fine, but nothing can be scheduled through "
                  f"the API until a brand is connected. Hand-posting still works.",
                  file=sys.stderr)
    except Exception:
        pass    # never let a warning break a render

    run(a.run_id, a.count, territories=terrs, out_root=a.out,
        claim_posture=a.claim_posture, allow_nb=a.allow_neo_brutalist,
        dry_run=a.dry_run, fmt=a.fmt, text_style=a.text_style,
        max_reuse=a.max_reuse, jpeg=not a.no_jpeg, do_captions=not a.no_captions,
        persona=a.persona)
    return 0


if __name__ == "__main__":
    sys.exit(main())
