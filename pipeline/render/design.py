#!/usr/bin/env python3
"""
design.py — design tokens + procedural doodles for the carousel renderer.

Everything decorative is DRAWN, not an asset file, so it scales to any canvas
and recolours from tokens. Nothing here reproduces a third party's mark: the
mascot is an original character and the pennant is a blank shape that takes an
optional caller-supplied label.

MEASURED palette (printed with a contrast probe, not estimated — re-run
tools/measure if you change a value):

    cardinal #C8102E on cream #FBF6F0 ....  5.47:1  AA body
    cream    #FBF6F0 on cardinal ........  5.47:1  AA body   (reverse slides)
    deep     #A8172C on cream ............ 6.92:1  AA body   (small red text)
    ink      #231A18 on cream ............ 15.86:1 AA body
    meta     #6B5A54 on cream ............  6.08:1 AA body
    ink      #231A18 on blush #F7DCD9 .... 13.14:1 AA body
    cardinal #C8102E on blush #F7DCD9 ....  4.54:1 AA body   (only just)

    green    #4A6B41 on cream ............  5.63:1  AA body  (accent)
    green_light #7A9471 on cream .........  3.10:1  shape/fill only

    NEVER put red and green in contact as foreground/background: cardinal vs
    green measures 1.03:1. They are both accents on cream, never on each other.

    REJECTED: #8A7A72 on cream = 3.83:1  — fails AA for small text.
    FILL ONLY, never text on it: blush-deep #F2C9C4 (cardinal on it = 3.90),
    sage #7A9471 (3.10 vs cream — mascot shape, given an ink outline).
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

# The six colours of the guidelines, plus the optional blush surface and the
# mascot's trunk brown. Nothing else. Every one passes peach_check().
T = {
    "cream":    "#FBF6F0",   # base
    "cardinal": "#C8102E",   # headline on cream
    "deep":     "#A8172C",   # alternate headline on cream
    "forest":   "#4A6B41",   # accent
    "ink":      "#231A18",   # body copy on cream
    "meta":     "#6B5A54",   # counter, captions
    "blush":    "#F7DCD9",   # optional pale surface, cream slides only
    # #8A6046 was the original trunk brown; peach_check bans it (green 26
    # above blue = orange-brown). Meta brown is in-palette and passes.
    "trunk":    "#6B5A54",   # mascot only
}

# aliases kept so existing asset scripts keep resolving
T["green"] = T["forest"]
T["blush_deep"] = "#F2C9C4"

DISPLAY_WEIGHT = {"headline": 600}

FONTS = {
    "display": "PlayfairDisplay.ttf",   # headline, weight 600, the only display face
    "body":    "Figtree.ttf",           # label 500, qualifier and counter 400
}

# Type scale for 1080x1350, per the final visual guidelines. The headline is
# autoscaled inside its band; everything else is fixed.
SCALE = {
    "headline_max":        190,
    "headline_min":        120,
    "headline_leading":    0.90,
    "headline_lines_max":  4,
    "headline_words_min":  3,
    "headline_words_max":  6,
    "qualifier":           40,   # spec band 36-44
    "label":               30,   # spec band 28-32
    "counter":             28,   # spec band 26-30
    "interior_words_max":  18,
    "icon":                150,
}


def peach_check(hex_colour):
    """The guidelines ban poppy, orange-red and peach. Peach and coral have green
    well above blue; pink and true red do not. Only meaningful where red is the
    dominant channel, and only where there is real chroma — otherwise cream and
    ink read as peach and forest green reads as peach, which earlier drafts of
    this check both did.

    Returns (allowed, reason).
    """
    r, g, b = (int(hex_colour.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    chroma = max(r, g, b) - min(r, g, b)
    if chroma <= 14:
        return True, "neutral"
    if r < max(g, b):
        return True, "not a warm hue"
    if g - b >= 18:
        return False, f"peach or orange-red (green exceeds blue by {g - b})"
    return True, "pink or true red"


def _layer(size):
    return Image.new("RGBA", size, (0, 0, 0, 0))


def _paste(img, layer, xy=(0, 0)):
    """Composite an RGBA layer onto img. Clips to bounds — an out-of-range xy
    would otherwise make crop() pad with black and leave a bar on the slide."""
    x, y = int(xy[0]), int(xy[1])
    lx = max(0, -x)
    ly = max(0, -y)
    x, y = max(0, x), max(0, y)
    w = min(layer.width - lx, img.width - x)
    h = min(layer.height - ly, img.height - y)
    if w <= 0 or h <= 0:
        return
    layer = layer.crop((lx, ly, lx + w, ly + h))
    if img.mode == "RGBA":
        img.alpha_composite(layer, (x, y))
        return
    base = img.crop((x, y, x + w, y + h)).convert("RGBA")
    img.paste(Image.alpha_composite(base, layer).convert("RGB"), (x, y))


# --------------------------------------------------------------- the mascot

def _contrast(a, b):
    def lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def lum(h):
        h = h.lstrip("#")
        r, g, bl = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(bl)

    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def tree(img, cx, cy, h=180, canopy=None, trunk=None, face=True, blossoms=True):
    """The little tree. An original character: four overlapping canopy lobes, a
    stubby trunk showing beneath them, dot eyes, a small smile and blush cheeks.
    Deliberately not any school's mark.

    Face colour is CHOSEN by contrast against the canopy, not hardcoded. The
    first version always drew ink features, which measured 2.90:1 on the
    cardinal canopy and rendered an eyeless red blob.
    """
    canopy = canopy or T["cardinal"]
    trunk = trunk or T["trunk"]
    feat = T["ink"] if _contrast(T["ink"], canopy) >= _contrast(T["cream"], canopy) else T["cream"]
    cheek = T["blush_deep"] if feat == T["ink"] else T["blush"]
    # blossoms must NOT match the face colour, or the eyes read as more
    # blossoms and the character loses its face entirely.
    bloom = T["blush"] if feat == T["cream"] else T["blush_deep"]

    lay = _layer((int(h * 1.30), int(h * 1.04)))
    d = ImageDraw.Draw(lay)
    ox = lay.width / 2

    # trunk first, drawn tall enough that the canopy tucks over its top
    tw = h * 0.15
    d.rounded_rectangle([ox - tw / 2, h * 0.58, ox + tw / 2, h * 1.0],
                        radius=h * 0.055, fill=trunk)

    for dx, ly, r in [(-h * 0.22, h * 0.44, h * 0.24), (h * 0.22, h * 0.44, h * 0.24),
                      (0, h * 0.25, h * 0.25), (0, h * 0.42, h * 0.23)]:
        d.ellipse([ox + dx - r, ly - r, ox + dx + r, ly + r], fill=canopy)

    # On a dark canopy the pale blossoms compete with the pale eyes and the
    # character reads as a spotted mushroom. Face wins; blossoms are dropped.
    if blossoms and feat == T["ink"]:
        for dx, dy, r in [(-.30, .46, .036), (.31, .34, .032), (-.09, .18, .026),
                          (.16, .56, .030), (.04, .34, .022)]:
            d.ellipse([ox + dx * h - r * h, dy * h - r * h,
                       ox + dx * h + r * h, dy * h + r * h], fill=bloom)

    if face:
        ey, er = h * 0.40, h * 0.048
        for dx in (-h * 0.13, h * 0.13):
            d.ellipse([ox + dx - er, ey - er, ox + dx + er, ey + er], fill=feat)
        for dx in (-h * 0.235, h * 0.235):
            d.ellipse([ox + dx - h * 0.055, ey + h * 0.03,
                       ox + dx + h * 0.055, ey + h * 0.11], fill=cheek)
        d.arc([ox - h * 0.10, ey + h * 0.02, ox + h * 0.10, ey + h * 0.15],
              start=15, end=165, fill=feat, width=max(2, int(h * 0.018)))

    _paste(img, lay, (int(cx - lay.width / 2), int(cy)))


def arrow_tip(img, x, y, w=44, fill=None, width=5):
    """A small drawn arrow. Caveat has no U+2192 — typing '→' renders a tofu
    box, which is what shipped in the first cute render."""
    fill = fill or T["cardinal"]
    lay = _layer((int(w + 12), int(w * 0.8)))
    d = ImageDraw.Draw(lay)
    my = lay.height / 2
    d.line([(2, my), (w, my)], fill=fill, width=width)
    d.line([(w, my), (w - w * 0.32, my - w * 0.24)], fill=fill, width=width)
    d.line([(w, my), (w - w * 0.32, my + w * 0.24)], fill=fill, width=width)
    _paste(img, lay, (int(x), int(y - my)))


def pennant(img, cx, cy, w=220, label=None, font=None, fill=None, flip=False):
    """A blank pennant: tall straight hoist edge tapering to a point.

    `label` is optional and caller-supplied — no mark is baked in. The first
    version built a near-flat triangle from three bad points and stroked the
    hypotenuse, which rendered a jagged sliver with the label hanging outside
    the shape.
    """
    fill = fill or T["cardinal"]
    h = int(w * 0.62)
    pad = 14
    lay = _layer((w + pad * 2, h + pad * 2))
    d = ImageDraw.Draw(lay)

    if flip:
        pts = [(w + pad, pad), (w + pad, h + pad), (pad, h / 2 + pad)]
        hoist = [(w + pad, pad), (w + pad, h + pad)]
        lx = w * 0.58 + pad
    else:
        pts = [(pad, pad), (pad, h + pad), (w + pad, h / 2 + pad)]
        hoist = [(pad, pad), (pad, h + pad)]
        lx = w * 0.42 + pad

    d.polygon(pts, fill=fill)
    d.line(hoist, fill=T["cream"], width=max(4, h // 14))          # hoist band
    d.line([hoist[1], pts[2]], fill=fill, width=2)                 # clean taper

    if label and font:
        tw = d.textlength(label, font=font)
        a, de = font.getmetrics()
        d.text((lx - tw / 2, h / 2 + pad - (a + de) * 0.46), label,
               font=font, fill=T["cream"])

    lay = lay.rotate(-7 if not flip else 7, resample=Image.BICUBIC, expand=True)
    _paste(img, lay, (int(cx - lay.width / 2), int(cy - lay.height / 2)))


# ---------------------------------------------------------------- doodles

def star(img, cx, cy, r=22, fill=None, points=4):
    """Four-point sparkle with concave sides — the cute one, not a pentagram."""
    fill = fill or T["cardinal"]
    lay = _layer((int(r * 2.6), int(r * 2.6)))
    d = ImageDraw.Draw(lay)
    o = lay.width / 2
    pts = []
    for i in range(points * 2):
        a = math.pi * i / points - math.pi / 2
        rad = r if i % 2 == 0 else r * 0.32
        pts.append((o + rad * math.cos(a), o + rad * math.sin(a)))
    d.polygon(pts, fill=fill)
    _paste(img, lay, (int(cx - o), int(cy - o)))


def sparkles(img, cx, cy, fill=None, scale=1.0):
    fill = fill or T["cardinal"]
    for dx, dy, r in [(0, 0, 24), (34, 26, 13), (-28, 30, 9)]:
        star(img, cx + dx * scale, cy + dy * scale, int(r * scale), fill)


def bow(img, cx, cy, w=110, fill=None):
    """Coquette bow. Two loops, a knot, two tails."""
    fill = fill or T["cardinal"]
    s = w / 110.0
    lay = _layer((int(w * 1.5), int(w * 1.3)))
    d = ImageDraw.Draw(lay)
    o = lay.width / 2, lay.height * 0.38
    for sign in (-1, 1):
        d.ellipse([o[0] + sign * 8 * s - (0 if sign > 0 else 46 * s),
                   o[1] - 28 * s,
                   o[0] + sign * 8 * s + (46 * s if sign > 0 else 0),
                   o[1] + 26 * s], fill=fill)
        d.polygon([(o[0] + sign * 6 * s, o[1] + 8 * s),
                   (o[0] + sign * 34 * s, o[1] + 62 * s),
                   (o[0] + sign * 12 * s, o[1] + 58 * s)], fill=fill)
    d.ellipse([o[0] - 13 * s, o[1] - 13 * s, o[0] + 13 * s, o[1] + 13 * s], fill=fill)
    _paste(img, lay, (int(cx - lay.width / 2), int(cy - lay.height / 2)))


def heart(img, cx, cy, w=40, fill=None):
    fill = fill or T["cardinal"]
    lay = _layer((int(w * 1.4), int(w * 1.4)))
    d = ImageDraw.Draw(lay)
    o = lay.width / 2
    r = w * 0.28
    d.ellipse([o - r * 1.9, o - r * 1.2, o - r * 0.1, o + r * 0.6], fill=fill)
    d.ellipse([o + r * 0.1, o - r * 1.2, o + r * 1.9, o + r * 0.6], fill=fill)
    d.polygon([(o - r * 1.85, o + r * 0.05), (o + r * 1.85, o + r * 0.05), (o, o + r * 1.9)],
              fill=fill)
    _paste(img, lay, (int(cx - o), int(cy - o)))


def tape(img, x, y, w=280, h=64, angle=-6, fill=None, alpha=205):
    """Washi tape strip — a soft panel to sit small text on."""
    rgb = fill or T["blush"]
    rgb = tuple(int(rgb.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    lay = _layer((w, h))
    ImageDraw.Draw(lay).rectangle([0, 0, w, h], fill=rgb + (alpha,))
    lay = lay.rotate(angle, resample=Image.BICUBIC, expand=True)
    _paste(img, lay, (int(x), int(y)))


def wobble_underline(img, x, y, w, fill=None, width=6, amp=5):
    """Hand-drawn underline. Two passes, slightly offset, so it reads drawn."""
    fill = fill or T["cardinal"]
    lay = _layer((int(w + 30), int(amp * 6 + width * 4)))
    d = ImageDraw.Draw(lay)
    for pass_i, off in enumerate((0, 3)):
        pts = []
        for i in range(41):
            t = i / 40
            px = 12 + t * w
            py = amp * 3 + off + math.sin(t * math.pi * 2.4 + pass_i) * amp
            pts.append((px, py))
        d.line(pts, fill=fill, width=max(2, width - pass_i * 2), joint="curve")
    _paste(img, lay, (int(x - 12), int(y)))


def check(img, cx, cy, s=34, fill=None):
    fill = fill or T["cardinal"]
    lay = _layer((int(s * 2), int(s * 2)))
    ImageDraw.Draw(lay).line(
        [(s * 0.35, s * 1.0), (s * 0.8, s * 1.45), (s * 1.65, s * 0.45)],
        fill=fill, width=max(3, int(s * 0.18)), joint="curve")
    _paste(img, lay, (int(cx - s), int(cy - s)))


def dotted_rule(img, x, y, w, fill=None, r=3, gap=16):
    fill = fill or T["meta"]
    lay = _layer((int(w), int(r * 3)))
    d = ImageDraw.Draw(lay)
    n = int(w // gap)
    for i in range(n):
        cx = i * gap + r
        d.ellipse([cx - r, r, cx + r, r * 3], fill=fill)
    _paste(img, lay, (int(x), int(y)))


def curly_arrow(img, x, y, length=170, fill=None, width=5, flip=False):
    fill = fill or T["cardinal"]
    lay = _layer((int(length + 60), 120))
    d = ImageDraw.Draw(lay)
    p = [(10, 60), (10 + length * 0.3, 18), (10 + length * 0.7, 96), (10 + length, 52)]
    pts = []
    for i in range(61):
        t = i / 60
        u = 1 - t
        pts.append((u ** 3 * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t ** 3 * p[3][0],
                    u ** 3 * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t ** 3 * p[3][1]))
    d.line(pts, fill=fill, width=width, joint="curve")
    hx, hy = pts[-1]
    d.line([(hx, hy), (hx - 26, hy - 16)], fill=fill, width=width)
    d.line([(hx, hy), (hx - 28, hy + 12)], fill=fill, width=width)
    if flip:
        lay = lay.transpose(Image.FLIP_LEFT_RIGHT)
    _paste(img, lay, (int(x), int(y - 60)))
