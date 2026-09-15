#!/usr/bin/env python3
"""
gen_assets.py — generate the brand's illustrated assets with GPT image gen.

    python3 render/gen_assets.py --persona chloe --only icons
    python3 render/gen_assets.py --persona chloe            # everything

Writes into <persona>/brand/. Sheets are generated once and sliced locally, so
regenerating is cheap and the design system stays reproducible rather than a
folder of mystery PNGs.

Two rules baked into every prompt:
  * NO TEXT, EVER. The model cannot letter reliably; all type is composited in
    Pillow by carousel.py. Prompts say so explicitly.
  * A named hex and a named stroke weight, so a re-run matches the existing set
    instead of drifting to a new illustration style.

Endpoint: $ANTHROPIC_BASE_URL/api/llm/images/generations (the /openai/v1/ path
404s on this gateway). Uses curl, not urllib — urllib fails cert verification
here with CERTIFICATE_VERIFY_FAILED.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_ROOT = os.path.dirname(HERE)
MODEL = "openai-group/gpt-image-2"

NO_TEXT = ("Absolutely no text, no letters, no numbers, no words, no captions, "
           "no watermarks and no signatures anywhere in the image.")

CARDINAL = "#C8102E"
CREAM = "#FBF6F0"
GREEN = "#4A6B41"
GREEN_LIGHT = "#7A9471"

SPECS = {
    # 3x3 sheet of line-art doodles, transparent, for compositing as accents
    # 3x3 sheet of FLAT FILLED hand-made shapes. Explicitly not line art —
    # the first pass came back as clean single-weight outlines, which read as
    # stock iconography rather than something a person made.
    "icons": {
        "size": "1024x1024",
        "background": "cream",
        "grid": (3, 3),
        "prompt": (
            f"A tidy 3 by 3 grid of nine separate small illustrations, evenly spaced "
            f"with generous margins, each fully inside its own cell and not touching "
            f"any other. "
            f"CRITICAL STYLE: every shape is a SOLID FLAT FILLED silhouette of colour. "
            f"Absolutely no outlines, no contour lines, no strokes, no line drawing, "
            f"no hollow shapes and no sketch style. Think cut-paper collage or a shape "
            f"painted flat with a brush, not an icon drawn with a pen. "
            f"The edges are deliberately imperfect: wobbly, slightly lumpy, hand-cut "
            f"rather than smooth, and each object is a little lopsided and asymmetric, "
            f"as though a person made it quickly by hand. Nothing is geometrically "
            f"perfect, nothing is centred, nothing is symmetrical. "
            f"Interior detail is only a few tiny hand-drawn tick marks, short dashes "
            f"or small dots sitting on top of the filled shape, in the same naive "
            f"sticker style as a hand-drawn tree. No shading, no gradients, no "
            f"texture, no drop shadows, completely flat colour. "
            f"Mostly warm red {CARDINAL}, with a few small accent parts in muted "
            f"green {GREEN}. "
            f"The nine objects: a sealed envelope, a lightbulb, a pencil, an alarm "
            f"clock, a stack of three books, a clipboard, a paper aeroplane, a "
            f"four-point star, a speech bubble. "
            f"Plain flat solid {CREAM} background with nothing else on it. "
            f"{NO_TEXT}"
        ),
    },
    # 3x2 sheet of flat conifer trees in the inspiration's sticker style
    "trees": {
        "size": "1024x1024",
        "background": "cream",
        "grid": (3, 2),
        "prompt": (
            f"A tidy 3 by 2 grid of six separate flat hand-drawn conifer trees, "
            f"evenly spaced with generous margins, each tree fully inside its own "
            f"cell and not touching any other. Simple cute sticker-illustration "
            f"style: a solid flat triangular or stacked-tier canopy with softly "
            f"rounded wobbly edges, a short solid rectangular trunk with rounded "
            f"corners, and a few tiny hand-drawn tick and dash marks scattered on "
            f"the canopy to suggest needles. No outlines, no gradients, no "
            f"shading, completely flat colour. Canopies in muted forest green "
            f"{GREEN} and a lighter sage {GREEN_LIGHT}, trunks in a muted warm brown. Six subtly "
            f"different silhouettes: some wide and squat, some tall and narrow, "
            f"some in three stacked tiers. Plain flat solid {CREAM} background "
            f"with nothing else on it, no ground line, no shadows. "
            f"{NO_TEXT}"
        ),
    },
    # portrait photo grounds — Stanford's visual vocabulary, not Stanford itself
    "plates": {
        "size": "1024x1536",
        "background": "opaque",
        "grid": None,
        "n_variants": 4,
        "prompts": [
            "A quiet sunlit sandstone colonnade on a university campus, repeating "
            "rounded romanesque arches casting long shadows across a stone walkway, "
            "terracotta clay tile roofline above, warm honey-coloured stone. Late "
            "golden afternoon light raking in from the left, one side of the "
            "exposure blowing out to pure white in the archway openings. Shot on "
            "35mm film, deep depth of field, everything in focus, visible film "
            "grain, slightly desaturated and muted, no colour grade. No people. "
            + NO_TEXT,
            "A wide grassy university quad in early evening, tall palm trees "
            "silhouetted against a pale washed-out sky, low sandstone buildings "
            "with terracotta tile roofs along the far edge, a few bicycles leaning "
            "on a rack in the foreground. Overcast flat light, the sky clipping to "
            "featureless white. Shot on 35mm film, deep focus, heavy grain, muted "
            "desaturated palette. No people. " + NO_TEXT,
            "A dim university library reading room at night, long wooden tables, "
            "green-shaded brass lamps pooling warm light, tall arched windows dark "
            "behind, stacks of books. Only the lamps light the scene so the corners "
            "fall away into crushed muddy shadow with visible noise. Shot on 35mm "
            "film, deep focus, grainy, warm and desaturated. No people. " + NO_TEXT,
            "A narrow shaded courtyard on a university campus, a stone fountain, "
            "climbing ivy on a sandstone wall, terracotta roof tiles, dappled light "
            "through a large oak. Bright midday sun through the leaves blowing out "
            "in patches to pure white, deep shadow elsewhere with no fill. Shot on "
            "35mm film, deep depth of field, film grain, muted. No people. " + NO_TEXT,
        ],
    },
}


def generate(prompt, size, background, out_path):
    # gpt-image-2 rejects background="transparent" outright, so flat art is
    # generated on a solid cream ground and keyed out locally by key_out().
    payload = {"model": MODEL, "prompt": prompt, "size": size, "n": 1}
    r = subprocess.run(
        ["curl", "-s", "-m", "300",
         f"{os.environ['ANTHROPIC_BASE_URL'].rstrip('/')}/api/llm/images/generations",
         "-H", f"Authorization: Bearer {os.environ['ANTHROPIC_AUTH_TOKEN']}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"curl failed: {r.stderr[:400]}")
    try:
        body = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"non-JSON reply: {r.stdout[:400]}")
    if "data" not in body:
        raise SystemExit(f"no data in reply: {json.dumps(body)[:400]}")
    with open(out_path, "wb") as fh:
        fh.write(base64.b64decode(body["data"][0]["b64_json"]))
    return out_path


def key_out(cell, bg=CREAM, tol=58, soft=26):
    """Turn a flat background into alpha by distance-from-background, keeping
    anti-aliased edges as partial alpha instead of a hard 1-bit matte."""
    from PIL import Image
    ref = tuple(int(bg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    px = cell.convert("RGB").load()
    out = cell.convert("RGBA")
    op = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, b = px[x, y]
            dist = max(abs(r - ref[0]), abs(g - ref[1]), abs(b - ref[2]))
            if dist <= tol:
                op[x, y] = (r, g, b, 0)
            elif dist <= tol + soft:
                op[x, y] = (r, g, b, int(255 * (dist - tol) / soft))
    return out


def slice_sheet(path, cols, rows, outdir, prefix, trim=True):
    """Cut a generated grid sheet into individual transparent PNGs, cropped to
    each item's actual alpha bounds so compositing is predictable."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    cw, ch = im.width // cols, im.height // rows
    os.makedirs(outdir, exist_ok=True)
    made = []
    for r in range(rows):
        for c in range(cols):
            cell = im.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
            cell = key_out(cell)
            if trim:
                bbox = cell.getchannel("A").point(lambda v: 255 if v > 12 else 0).getbbox()
                if bbox:
                    cell = cell.crop(bbox)
            if cell.width < 24 or cell.height < 24:
                continue
            p = os.path.join(outdir, f"{prefix}-{r * cols + c + 1:02d}.png")
            cell.save(p)
            made.append((p, cell.size))
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="chloe")
    ap.add_argument("--only", choices=sorted(SPECS), action="append")
    a = ap.parse_args()

    brand = os.path.join(CODE_ROOT, "..", a.persona, "brand")
    brand = os.path.abspath(brand)
    os.makedirs(brand, exist_ok=True)
    todo = a.only or list(SPECS)

    for name in todo:
        spec = SPECS[name]
        if name == "plates":
            for i, p in enumerate(spec["prompts"][:spec["n_variants"]], 1):
                out = os.path.join(brand, f"plate-{i:02d}.png")
                generate(p, spec["size"], spec["background"], out)
                print(f"  plate-{i:02d}.png")
            continue
        sheet = os.path.join(brand, f"_{name}-sheet.png")
        generate(spec["prompt"], spec["size"], spec["background"], sheet)
        cols, rows = spec["grid"]
        made = slice_sheet(sheet, cols, rows, os.path.join(brand, name), name[:-1])
        print(f"  {name}: sheet + {len(made)} sliced")
        for p, sz in made:
            print(f"    {os.path.basename(p)} {sz[0]}x{sz[1]}")


if __name__ == "__main__":
    main()
