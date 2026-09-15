#!/usr/bin/env python3
"""
carousel.py — Chloe's carousel renderer. Implements the final visual guidelines.

    python3 render/carousel.py --deck ../chloe/copy/deck.json --out <dir>

Two cover modes, set per slide with "mode". PHOTO IS THE DEFAULT:
    photo  — full-bleed campus photograph, cream type only, gradient veil
    cream  — cream ground, cardinal headline, ink copy, forest accent.
             Used sparingly, as a break in a run of photographs.

FIXED HIERARCHY. Four levels, every cover, always in this order:
    1  series label    "chloe's notes 01"   Figtree 500, letterspaced, 30px
    2  headline         dominant             Playfair Display 600, 120-190px
    3  qualifier        optional, 1-2 lines  Figtree 400, 40px
    4  slide counter    "01 / 06"            Figtree 400, 28px
The two number systems stay separate: the post number lives in the series
label, the slide number lives in the counter.

TEXT LIMITS are linted and reported, never silently absorbed. If a headline
will not fit at the 120px floor, that is reported as copy too long rather than
shrunk away, per the guideline that dramatic shrinking means the copy is wrong.

CONTRAST is measured on the worst 12px tile, not a region mean. Display type is
large text so its floor is 3.0; label, qualifier and counter are body text at
4.5. Photographs get a gradient veil only where copy sits — never a flat scrim
and never an opaque panel.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
sys.path.insert(0, HERE)

import design as D  # noqa: E402

W, H = 1080, 1350
MARGIN = 84
FLOOR_LARGE = 3.0
FLOOR_BODY = 4.5
S = D.SCALE

MODES = {
    "photo": {
        "ground": None,
        "headline": D.T["cream"], "label": D.T["cream"],
        "qualifier": D.T["cream"], "counter": D.T["cream"],
        "icons": False,
    },
    "cream": {
        "ground": D.T["cream"],
        "headline": D.T["cardinal"], "label": D.T["cardinal"],
        "qualifier": D.T["ink"], "counter": D.T["meta"],
        "icons": True,
    },
}


# ------------------------------------------------------------------- colour

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(rgb):
    r, g, b = (_lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la = _lum(_rgb(a) if isinstance(a, str) else a)
    lb = _lum(_rgb(b) if isinstance(b, str) else b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def worst_tile(img, box, fg, tile=12):
    """Lowest contrast over any tile in the region. A region mean hides local
    failures: one plate measured 11.82:1 mean and 3.77:1 on its worst tile."""
    px = img.convert("RGB").load()
    lo = None
    for y in range(box[1], max(box[1] + 1, box[3] - tile), tile):
        for x in range(box[0], max(box[0] + 1, box[2] - tile), tile):
            t = [px[x + i, y + j] for i in range(0, tile, 4) for j in range(0, tile, 4)]
            g = tuple(round(sum(p[k] for p in t) / len(t)) for k in range(3))
            r = contrast(fg, g)
            if lo is None or r < lo:
                lo = r
    return lo if lo is not None else 21.0


# --------------------------------------------------------------- typography

_cache = {}


def font(kind, size, wght=None):
    key = (kind, size, wght)
    if key in _cache:
        return _cache[key]
    f = ImageFont.truetype(os.path.join(FONT_DIR, D.FONTS[kind]), size)
    if wght is not None:
        try:
            axes = f.get_variation_axes()
            cur = [a["default"] for a in axes]
            for i, a in enumerate(axes):
                n = a["name"].decode() if isinstance(a["name"], bytes) else a["name"]
                if n in ("Weight", "weight"):
                    cur[i] = wght
            f.set_variation_by_axes(cur)
        except Exception:
            pass
    _cache[key] = f
    return f


def _greedy(draw, s, f, max_w):
    lines, cur = [], ""
    for word in s.split():
        t = f"{cur} {word}".strip()
        if draw.textlength(t, font=f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def wrap(draw, s, f, max_w):
    """Greedy wrap plus one rebalance pass: a final line holding a single short
    word is the ugliest break available, so retry narrower to pull one down."""
    lines = _greedy(draw, s, f, max_w)
    if len(lines) > 1 and len(lines[-1].split()) == 1 and len(lines[-1]) <= 4:
        for k in (0.94, 0.89, 0.84):
            alt = _greedy(draw, s, f, int(max_w * k))
            if len(alt) == len(lines) and len(alt[-1].split()) > 1:
                return alt
    return lines


def fit_headline(draw, text, max_w, avail_h):
    """Largest size in the 120-190 band whose block fits the height, keeps every
    line inside max_w, and stays within 4 lines."""
    for size in range(S["headline_max"], S["headline_min"] - 1, -2):
        f = font("display", size, D.DISPLAY_WEIGHT["headline"])
        lines = wrap(draw, text, f, max_w)
        a, d = f.getmetrics()
        step = int((a + d) * S["headline_leading"])
        widest = max(draw.textlength(l, font=f) for l in lines)
        if (step * len(lines) <= avail_h and widest <= max_w
                and len(lines) <= S["headline_lines_max"]):
            return f, lines, step, ""
    f = font("display", S["headline_min"], D.DISPLAY_WEIGHT["headline"])
    lines = wrap(draw, text, f, max_w)
    a, d = f.getmetrics()
    note = (f"COPY TOO LONG: does not fit at the {S['headline_min']}px floor "
            f"({len(lines)} lines). Cut the headline.")
    return f, lines, int((a + d) * S["headline_leading"]), note


def tracked(draw, xy, s, f, fill, tr):
    x, y = xy
    for ch in s:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + tr
    return x - xy[0]


def para(draw, x, y, s, f, fill, max_w, leading=1.32):
    a, d = f.getmetrics()
    step = int((a + d) * leading)
    for line in wrap(draw, s, f, max_w):
        draw.text((x, y), line, font=f, fill=fill)
        y += step
    return y


def para_h(draw, s, f, max_w, leading=1.32):
    a, d = f.getmetrics()
    return int((a + d) * leading) * len(wrap(draw, s, f, max_w))


# -------------------------------------------------------------------- photo

def fill_crop(path, size):
    # exif_transpose is not optional: iPhone originals carry an orientation tag
    # (6 = rotate 90) that Image.open does NOT apply, and sips reports as <nil>.
    # Without this every portrait plate renders on its side.
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    tw, th = size
    sc = max(tw / im.width, th / im.height)
    im = im.resize((round(im.width * sc), round(im.height * sc)), Image.LANCZOS)
    l, t = (im.width - tw) // 2, (im.height - th) // 2
    return im.crop((l, t, l + tw, t + th))


def _veil(size, a_top, a_bot):
    W_, H_ = size
    mask = Image.new("L", (1, H_))
    mp = mask.load()
    for y in range(H_):
        t = y / H_
        top = max(0.0, (0.18 - t) / 0.18) * a_top
        bot = (0.0 if t < 0.20 else min(1.0, (t - 0.20) / 0.45)) * a_bot
        mp[0, y] = int(255 * min(0.96, max(top, bot)))
    veil = Image.new("RGBA", (W_, H_), (0, 0, 0, 255))
    veil.putalpha(mask.resize((W_, H_)))
    return veil


def gradient_veil(img, fg, reqs, cap=0.96):
    """Subtle ramp only where copy sits, never a flat scrim and never a panel.

    `reqs` is [(band, floor), ...] for every element that must stay legible. The
    top alpha covers bands reaching into the top 18% of the canvas; the bottom
    alpha covers everything else. Solving the bottom alpha against only the
    headline left the series label at 3.05:1, because the label sits mid-canvas
    where the bottom ramp has barely started.
    """
    steps = [round(x * 0.04, 2) for x in range(0, int(cap / 0.04) + 2)]
    top_reqs = [(b, f) for b, f in reqs if b[1] < img.size[1] * 0.18]
    bot_reqs = [(b, f) for b, f in reqs if b[1] >= img.size[1] * 0.18]

    a_top = 0.0
    for a in steps:
        t = Image.alpha_composite(img.convert("RGBA"), _veil(img.size, a, 0)).convert("RGB")
        if all(worst_tile(t, b, fg) >= f for b, f in top_reqs):
            a_top = a
            break

    a_bot = cap
    for a in steps:
        t = Image.alpha_composite(img.convert("RGBA"), _veil(img.size, a_top, a)).convert("RGB")
        if all(worst_tile(t, b, fg) >= f for b, f in bot_reqs):
            a_bot = a
            break

    out = Image.alpha_composite(img.convert("RGBA"),
                               _veil(img.size, a_top, a_bot)).convert("RGB")
    return out, (a_top, a_bot)


# ------------------------------------------------------------------- render

def lint(spec, idx, report):
    hw = len(spec.get("headline", "").split())
    if not (S["headline_words_min"] <= hw <= S["headline_words_max"]):
        report.append(f"    {idx:02d} LINT headline is {hw} words, limit is "
                      f"{S['headline_words_min']}-{S['headline_words_max']}")
    q = spec.get("qualifier", "")
    if q and len(q.split()) > 16:
        report.append(f"    {idx:02d} LINT qualifier is {len(q.split())} words, "
                      f"that reads as a paragraph")
    b = spec.get("body", "")
    if b and len(b.split()) > S["interior_words_max"]:
        report.append(f"    {idx:02d} LINT interior copy is {len(b.split())} words, "
                      f"limit is {S['interior_words_max']}")


def render_slide(spec, deck, idx, total, report):
    mode = spec.get("mode", "photo")   # photo is the default; cream is the break
    m = MODES[mode]
    series = deck.get("series", "chloe's notes")
    label = f"{series} {int(deck.get('post', 1)):02d}"
    counter = f"{idx:02d} / {total:02d}"
    col = W - 2 * MARGIN
    lint(spec, idx, report)

    f_lab = font("body", S["label"], 500)
    f_qual = font("body", S["qualifier"], 400)
    f_cnt = font("body", S["counter"], 400)

    d0 = ImageDraw.Draw(Image.new("RGB", (W, H)))
    qual = spec.get("qualifier")
    qual_h = (para_h(d0, qual, f_qual, col - 120) + 26) if qual else 0
    cnt_y = H - MARGIN - S["counter"] - 6
    floor_y = cnt_y - 46
    lab_h = S["label"] + 40

    if mode == "photo":
        if not spec.get("photo"):
            raise SystemExit(f"slide {idx}: mode 'photo' needs a photo")
        img = fill_crop(spec["photo"], (W, H))
        # Fit the headline first, so the veil is solved against the rects the type
        # will ACTUALLY occupy. Solving a fixed band while drawing at a computed
        # position left the series label unprotected on a bright roofline.
        f_probe, l_probe, step_probe, note_probe = fit_headline(
            ImageDraw.Draw(img), spec["headline"], col, int(H * 0.32))
        head_top = floor_y - qual_h - step_probe * len(l_probe)
        head_band = (0, max(0, head_top - 8), W, min(H, floor_y - qual_h + 8))
        body_bands = [(0, max(0, head_top - lab_h - 12), W, max(1, head_top - 4)),
                      (0, max(0, floor_y - qual_h - 4), W, H)]
        img, veil = gradient_veil(img, m["headline"], [
            (head_band, FLOOR_LARGE),
            (body_bands[0], FLOOR_BODY),
            (body_bands[1], FLOOR_BODY),
        ])
        m = dict(m, _head_band=head_band, _label_band=body_bands[0],
                 _lower_band=body_bands[1])
        report.append(f"    {idx:02d} veil top={veil[0]:.2f} bot={veil[1]:.2f}")
        # Hold the veiled ground BEFORE any type is drawn. Verifying contrast on
        # the finished slide measures cream text against cream text and returns
        # 1.00:1 — a false failure this has produced three separate times.
        ground = img.copy()
    else:
        img = Image.new("RGB", (W, H), m["ground"])
        ground = None

    draw = ImageDraw.Draw(img)

    if mode == "photo":
        f_h, lines, step, note = f_probe, l_probe, step_probe, note_probe
        y = head_top
        tracked(draw, (MARGIN, y - lab_h), label.upper(), f_lab, m["label"], 3.4)
    else:
        tracked(draw, (MARGIN, MARGIN + 26), label.upper(), f_lab, m["label"], 3.4)
        y = MARGIN + 26 + lab_h + 44
        f_h, lines, step, note = fit_headline(draw, spec["headline"], col,
                                             floor_y - y - qual_h - 40)

    for line in lines:
        draw.text((MARGIN, y), line, font=f_h, fill=m["headline"])
        y += step
    if note:
        report.append(f"    {idx:02d} {note}")
    report.append(f"    {idx:02d} {mode:5s} headline {f_h.size}px, {len(lines)} line(s)")

    if qual:
        para(draw, MARGIN, y + 24, qual, f_qual, m["qualifier"], col - 120)

    cw = draw.textlength(counter, font=f_cnt)
    draw.text((W - MARGIN - cw, cnt_y), counter, font=f_cnt, fill=m["counter"])

    ic = spec.get("icon")
    if ic and not m["icons"]:
        report.append(f"    {idx:02d} LINT icon dropped: illustrations never sit "
                      f"over photographs")
    elif ic and os.path.isfile(ic):
        sp = Image.open(ic).convert("RGBA")
        sc = S["icon"] / max(sp.width, sp.height)
        sp = sp.resize((int(sp.width * sc), int(sp.height * sc)), Image.LANCZOS)
        D._paste(img, sp, (W - MARGIN - sp.width, cnt_y - sp.height - 40))

    return img, m, mode, ground


def verify(img, m, mode, idx, report, ground=None):
    """Assert the finished slide, not the intent."""
    ok = True
    if m["ground"]:
        ref = _rgb(m["ground"])
        px = img.convert("RGB").load()
        for y in range(H - 30, H - 8):
            for x in range(MARGIN, W - MARGIN):
                if sum(abs(px[x, y][i] - ref[i]) for i in range(3)) > 42:
                    report.append(f"    {idx:02d} FAIL content in the bottom margin at y={y}")
                    return False
        for name, c in (("headline", m["headline"]), ("qualifier", m["qualifier"]),
                        ("label", m["label"]), ("counter", m["counter"])):
            r = contrast(c, m["ground"])
            floor = FLOOR_LARGE if name == "headline" else FLOOR_BODY
            if r < floor:
                report.append(f"    {idx:02d} FAIL {name} {r:.2f}:1 below {floor}")
                ok = False
    else:
        probe = ground if ground is not None else img
        for name, box, floor in (("headline", m["_head_band"], FLOOR_LARGE),
                                 ("series label", m["_label_band"], FLOOR_BODY),
                                 ("lower copy", m["_lower_band"], FLOOR_BODY)):
            r = worst_tile(probe, box, m["headline"])
            if r < floor:
                report.append(f"    {idx:02d} FAIL {name} {r:.2f}:1 below {floor}")
                ok = False
    return ok


def render_deck(deck_path, outdir):
    with open(deck_path, encoding="utf-8") as fh:
        deck = json.load(fh)
    slides = deck["slides"]
    os.makedirs(outdir, exist_ok=True)
    report, written, fails = [], [], 0
    for i, spec in enumerate(slides, 1):
        img, m, mode, ground = render_slide(spec, deck, i, len(slides), report)
        assert img.size == (W, H), f"slide {i} is {img.size}"
        if not verify(img, m, mode, i, report, ground):
            fails += 1
        p = os.path.join(outdir, f"{i:02d}.png")
        img.save(p)
        written.append(p)
    print(f"  {len(written)} slides at {W}x{H} -> {outdir}")
    for line in report:
        print(line)
    print(f"  {'ALL PASS' if fails == 0 else str(fails) + ' slide(s) FAILED'}")
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    render_deck(a.deck, a.out)


if __name__ == "__main__":
    main()
