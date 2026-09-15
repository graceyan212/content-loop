#!/usr/bin/env python3
"""
typetest.py — render one cover in several type stacks so the choice is made by
eye rather than by argument.

    python3 render/typetest.py --out ../chloe/render/out/typetest

Same copy, same layout, same palette in every panel. Only the typefaces change.
Variable fonts are set by axes where useful — notably Fraunces' WONK axis, which
is the one thing in the candidate set that matches flat hand-cut illustration.
"""

from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
sys.path.insert(0, HERE)
import design as D  # noqa: E402

W, H = 1080, 1350
MARGIN = 84
KICKER = "what i wish i knew at 15"
HEAD = "5 summer programs that actually count"
SUB = "one is free, one is just a job, and the last one nobody expects"
CTA = "save this for february"

# name, display font, display axes, accent font (None = letterspaced sans caps),
# body font, body axes, display leading, note
STACKS = [
    ("A · current", "InstrumentSerif-Regular.ttf", None, "Caveat.ttf",
     "DMSans.ttf", {"wght": 400}, 0.86, "Instrument Serif + Caveat + DM Sans"),
    ("B · Fraunces wonky", "Fraunces.ttf", {"wght": 600, "SOFT": 60, "WONK": 1},
     None, "Figtree.ttf", {"wght": 400}, 0.88,
     "Fraunces (wonk) + small caps + Figtree"),
    ("C · Fraunces black", "Fraunces.ttf", {"wght": 900, "SOFT": 30, "WONK": 0},
     None, "Figtree.ttf", {"wght": 400}, 0.86,
     "Fraunces Black + small caps + Figtree"),
    ("D · Playfair", "PlayfairDisplay.ttf", {"wght": 700}, None,
     "DMSans.ttf", {"wght": 400}, 0.88, "Playfair Display + small caps + DM Sans"),
    ("E · Young Serif", "YoungSerif.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "Young Serif + small caps + Figtree"),
    ("F · Fraunces + Gaegu", "Fraunces.ttf", {"wght": 700, "SOFT": 80, "WONK": 1},
     "Gaegu.ttf", "Figtree.ttf", {"wght": 400}, 0.88,
     "Fraunces + Gaegu hand + Figtree"),
]

# Tall / condensed high-contrast serifs — the Instrument Serif family of
# proportions. width@100px for "summer programs" is in each note, measured, so
# the comparison is by proportion and not by vibe. Playfair is absent on
# purpose: even at its narrowest width axis (87) it measures 806 vs 631.
ALTS = [
    ("A · Instrument Serif", "InstrumentSerif-Regular.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.86, "current baseline · 631 · no weights"),
    ("B · Bellefair", "Bellefair.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.88, "closest proportion · 660 · single weight"),
    ("C · Suranna", "Suranna.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "narrow · 697 · single weight"),
    ("D · Cormorant Garamond 600", "CormorantGaramond.ttf", {"wght": 600}, None,
     "Figtree.ttf", {"wght": 400}, 0.86, "703 · weight axis 300-700"),
    ("E · Cormorant Garamond 700", "CormorantGaramond.ttf", {"wght": 700}, None,
     "Figtree.ttf", {"wght": 400}, 0.86, "703 · heaviest available"),
    ("F · Italiana", "Italiana.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.94, "775 · very light, all-caps feel"),
    ("G · Bodoni Moda", "BodoniModa.ttf", {"wght": 500, "opsz": 96}, None,
     "Figtree.ttf", {"wght": 400}, 0.88, "851 · wght 400-900 + optical size"),
    ("H · Antic Didone", "AnticDidone.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "856 · single weight"),
    ("I · Marcellus", "Marcellus.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "820 · single weight"),
]

TNR = "/System/Library/Fonts/Supplemental/Times New Roman.ttf"
TNRB = "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"

# Times New Roman Condensed is a Monotype commercial cut and is not installed on
# macOS, so condensing is synthesised: each line is rendered to its own layer and
# squeezed horizontally. Measured, "summer programs" at 100px is 724 in Times New
# Roman against 631 in Instrument Serif — so 87% lands on the proportions already
# approved. Tinos is metric-compatible with Times New Roman and OFL-licensed, so
# it is the version that can actually ship in a repo.
TIMES = [
    ("A · Instrument Serif", "InstrumentSerif-Regular.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.86, "baseline · width 631", 1.00),
    ("B · Times New Roman", TNR, None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "uncondensed · width 724", 1.00),
    ("C · Times New Roman 87%", TNR, None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "condensed to match 631", 0.87),
    ("D · Times New Roman 80%", TNR, None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "condensed · 579", 0.80),
    ("E · Times New Roman 74%", TNR, None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "condensed · 536", 0.74),
    ("F · Times New Roman Bold 82%", TNRB, None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "bold, condensed", 0.82),
    ("G · Tinos 87%", "Tinos.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "OFL Times clone · shippable", 0.87),
    ("H · Tinos Bold 82%", "Tinos-Bold.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.90, "OFL · bold condensed", 0.82),
    ("I · Bellefair", "Bellefair.ttf", None, None,
     "Figtree.ttf", {"wght": 400}, 0.88, "for comparison · 660", 1.00),
]


def load(name, size, axes=None):
    f = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    if axes:
        try:
            cur = [a["default"] for a in f.get_variation_axes()]
            names = [a["name"].decode() if isinstance(a["name"], bytes) else a["name"]
                     for a in f.get_variation_axes()]
            short = {"Weight": "wght", "Optical Size": "opsz", "Optical size": "opsz",
                     "Softness": "SOFT", "Wonky": "WONK", "Width": "wdth"}
            for i, n in enumerate(names):
                key = short.get(n, n)
                if key in axes:
                    cur[i] = axes[key]
            f.set_variation_by_axes(cur)
        except Exception:
            pass
    return f


def wrap(d, s, f, mw):
    lines, cur = [], ""
    for w_ in s.split():
        t = f"{cur} {w_}".strip()
        if d.textlength(t, font=f) <= mw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def fit(d, s, name, axes, mw, avail, leading):
    for size in range(210, 88, -3):
        f = load(name, size, axes)
        lines = wrap(d, s, f, mw)
        a, de = f.getmetrics()
        step = int((a + de) * leading)
        if step * len(lines) <= avail:
            return f, lines, step
    return f, lines, step


def tracked(d, xy, s, f, fill, tr):
    x, y = xy
    for ch in s:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + tr
    return x - xy[0]


def draw_condensed(img, x, y, text, f, fill, factor):
    """Render one line then squeeze it horizontally. Synthetic condensing —
    it does thin the vertical stems slightly, which is exactly what a real
    condensed cut avoids, but it is what 'Times condensed' looks like in use."""
    from PIL import Image as _I
    bb = f.getbbox(text)
    w, h = max(1, bb[2] + 12), max(1, bb[3] + 12)
    lay = _I.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(lay).text((0, 0), text, font=f, fill=fill)
    lay = lay.resize((max(1, int(w * factor)), h), _I.LANCZOS)
    D._paste(img, lay, (int(x), int(y)))
    return int(w * factor)


def panel(label, disp, disp_axes, accent, body, body_axes, leading, note,
          condense=1.0):
    img = Image.new("RGB", (W, H), D.T["cream"])
    d = ImageDraw.Draw(img)
    col = W - 2 * MARGIN
    x, top = MARGIN, 150

    D.tree(img, MARGIN + 96, top - 16, h=142, canopy=D.T["green"])
    d = ImageDraw.Draw(img)

    y = top + 168
    if accent:
        fk = load(accent, 52)
        d.text((x, y), KICKER, font=fk, fill=D.T["cardinal"])
        y += int(fk.getmetrics()[0] * 1.04)
    else:
        fk = load(body, 27, {"wght": 600})
        tracked(d, (x, y + 6), KICKER.upper(), fk, D.T["cardinal"], 3.6)
        y += 62

    fb = load(body, 35, body_axes)
    sub_lines = wrap(d, SUB, fb, col - 130)
    a, de = fb.getmetrics()
    sub_h = int((a + de) * 1.36) * len(sub_lines) + 46

    f, lines, step = fit(d, HEAD, disp, disp_axes, int(col / condense),
                         1180 - y - sub_h - 110, leading)
    y2 = y
    lw = 0
    for ln in lines:
        if condense < 1.0:
            lw = draw_condensed(img, x, y2, ln, f, D.T["ink"], condense)
        else:
            d.text((x, y2), ln, font=f, fill=D.T["ink"])
            lw = d.textlength(ln, font=f)
        y2 += step
    d = ImageDraw.Draw(img)
    lw = min(lw, col)
    D.wobble_underline(img, x, y2 - step * 0.16, lw, D.T["cardinal"], 7, 6)
    d = ImageDraw.Draw(img)
    y2 += 52
    for ln in sub_lines:
        d.text((x, y2), ln, font=fb, fill=D.T["meta"])
        y2 += int((a + de) * 1.36)

    if accent:
        fc = load(accent, 50)
        d.text((x, H - 208), CTA, font=fc, fill=D.T["cardinal"])
        D.arrow_tip(img, x + d.textlength(CTA, font=fc) + 18, H - 178, 46, D.T["cardinal"])
    else:
        fc = load(body, 29, {"wght": 700})
        w_ = tracked(d, (x, H - 196), CTA.upper(), fc, D.T["cardinal"], 4.2)
        D.arrow_tip(img, x + w_ + 20, H - 182, 46, D.T["cardinal"])

    d = ImageDraw.Draw(img)
    fm = load("DMSans.ttf", 24, {"wght": 500})
    d.text((MARGIN, 52), label, font=fm, fill=D.T["meta"])
    d.text((MARGIN, H - 78), note, font=load("DMSans.ttf", 21, {"wght": 400}),
           fill=D.T["meta"])
    return img, f.size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", default="stacks", choices=["stacks", "alts", "times"])
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    made = []
    sets = {"stacks": STACKS, "alts": ALTS, "times": TIMES}
    for i, st in enumerate(sets[a.set]):
        img, size = panel(*st)
        p = os.path.join(a.out, f"stack-{chr(65 + i)}.png")
        img.save(p)
        made.append(p)
        print(f"  {st[0]:24s} display {size}px  -> {os.path.basename(p)}")

    ims = [Image.open(p).resize((300, 375), Image.LANCZOS) for p in made]
    cols = 3
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 300 + (cols - 1) * 12,
                              rows * 375 + (rows - 1) * 12), "#3A3A3A")
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % cols) * 312, (i // cols) * 387))
    sheet.save(os.path.join(a.out, "_contact.png"))
    print(f"  contact sheet {sheet.size}")


if __name__ == "__main__":
    main()
