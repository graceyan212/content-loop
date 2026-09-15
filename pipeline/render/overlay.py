#!/usr/bin/env python3
"""
overlay.py — render a block of hook text to a transparent 1080x1920 RGBA PNG.

WHY THIS EXISTS: the ffmpeg on this machine is built WITHOUT libfreetype, so
`drawtext` does not exist (`ffmpeg -filters | grep -c drawtext` -> 0). All type
is therefore rasterized here with Pillow and composited by ffmpeg's `overlay`
filter (see compose.py).

Three styles:
  white-bar     (DEFAULT) TikTok-native: white DM Sans on per-line semi-opaque
                black rounded bands, left-aligned, generous leading.
  notes-app     pastiche of an iOS Notes screenshot: off-white rounded card,
                soft shadow, gray meta line, near-black Helvetica Neue body.
  neo-brutalist SFFS brand system, values lifted from
                GTM/brand-assets/tokens.css: Anton headline, flat bright fill,
                thick ink border, zero-blur hard shadow.

Everything word-wraps and auto-fits (shrinks until it fits; never overflows)
inside a `safe_zone` rect {x, y, w, h}. Optional small "Dramatization" tag.

CLI:
  python overlay.py --text "..." --style white-bar --out ov.png \
      [--safe-zone x,y,w,h] [--dramatization] [--nb-color yellow]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, asdict

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")

W, H = 1080, 1920

STYLES = ("white-bar", "notes-app", "neo-brutalist", "tiktok-native")
DEFAULT_STYLE = "white-bar"

# Default safe zone: below the TikTok top chrome, above the caption + right rail.
DEFAULT_SAFE_ZONE = {"x": 72, "y": 300, "w": 936, "h": 1180}

# Where the block sits inside the safe zone when it is shorter than the zone.
# white-bar rides the top of the zone (keeps a talking-head's face clear);
# the two card styles centre, which is how those objects read best.
DEFAULT_VALIGN = {
    "tiktok-native": "center","white-bar": "top", "notes-app": "center", "neo-brutalist": "center"}


def _block_y(sz, block_h, valign):
    if valign == "top":
        return float(sz["y"])
    if valign == "bottom":
        return float(sz["y"] + sz["h"] - block_h)
    return sz["y"] + (sz["h"] - block_h) / 2.0

# ---------------------------------------------------------------------------
# Brand tokens — verbatim from /Users/graceyan/Desktop/alpha/GTM/brand-assets/tokens.css
# ---------------------------------------------------------------------------
TOKENS = {
    "ink":    (0x00, 0x00, 0x00),
    "paper":  (0xFF, 0xFF, 0xFF),
    "cream":  (0xF6, 0xF4, 0xEE),
    "blue":   (0x83, 0x9A, 0xFF),
    "mint":   (0xC6, 0xFC, 0xD0),
    "coral":  (0xFD, 0x79, 0x62),
    "yellow": (0xFC, 0xE5, 0x52),
}
# tokens.css: --border-standard:6px  /* 4px for 1080p video */
NB_BORDER = 4
# tokens.css: --shadow-card:16px 16px 0 var(--ink); scaled by the same 4/6 factor
NB_SHADOW = 11
# tokens.css: --radius:44px / --radius-sm:18px
NB_RADIUS = 40
NB_RADIUS_SM = 16

# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------
ANTON = os.path.join(FONT_DIR, "Anton-Regular.ttf")
DMSANS = os.path.join(FONT_DIR, "DMSans.ttf")
# TikTok's own typeface, open-sourced July 2025 under SIL OFL 1.1 - permissive for
# commercial use. This is what the in-app caption tool renders (older builds used
# Proxima Nova, which TikTok Sans closely resembles). Using it closes the family gap
# that made DM Sans renders read as not-quite-native.
TIKTOKSANS = os.path.join(FONT_DIR, "TikTokSans-Variable.ttf")
TIKTOKSANS_URL = ("https://raw.githubusercontent.com/google/fonts/main/ofl/"
                  "tiktoksans/TikTokSans%5Bopsz%2Cslnt%2Cwdth%2Cwght%5D.ttf")
ANTON_URL = "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
DMSANS_URL = "https://github.com/google/fonts/raw/main/ofl/dmsans/DMSans%5Bopsz%2Cwght%5D.ttf"

# macOS system face used for the notes-app pastiche (SF-adjacent).
HELVETICA_TTC = "/System/Library/Fonts/HelveticaNeue.ttc"
HELV_INDEX = {"regular": 0, "bold": 1, "medium": 10, "light": 7}

_TTF_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")


def ensure_fonts(verbose: bool = False) -> None:
    """Download + validate Anton and DM Sans (adapted from build_round_01.ensure_fonts)."""
    os.makedirs(FONT_DIR, exist_ok=True)
    for path, url, name in ((ANTON, ANTON_URL, "Anton"), (DMSANS, DMSANS_URL, "DM Sans"),
                            (TIKTOKSANS, TIKTOKSANS_URL, "TikTok Sans")):
        ok = os.path.exists(path) and os.path.getsize(path) > 20000
        if not ok:
            if verbose:
                print(f"downloading {name} -> {path}", file=sys.stderr)
            subprocess.run(["curl", "-sL", "-o", path, url], check=False)
        if not os.path.exists(path):
            raise RuntimeError(f"font {name} missing and download failed: {path}")
        with open(path, "rb") as fh:
            head = fh.read(4)
        if head not in _TTF_MAGIC:
            raise RuntimeError(
                f"{name} at {path} is not a valid TTF (magic={head!r}). "
                f"Delete it and re-run to re-download."
            )


_font_cache: dict = {}


def _cached(key, factory):
    if key not in _font_cache:
        _font_cache[key] = factory()
    return _font_cache[key]


def anton(size: int) -> ImageFont.FreeTypeFont:
    return _cached(("anton", size), lambda: ImageFont.truetype(ANTON, size))


def dmsans(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    def make():
        f = ImageFont.truetype(DMSANS, size)
        try:
            f.set_variation_by_name(weight)   # DM Sans ships as a variable font
        except Exception:
            pass
        return f
    return _cached(("dmsans", size, weight), make)


def tiktoksans(size: int, weight: str = "SemiBold") -> ImageFont.FreeTypeFont:
    """TikTok Sans at a named instance; falls back to DM Sans if the file is absent."""
    def make():
        try:
            f = ImageFont.truetype(TIKTOKSANS, size)
            f.set_variation_by_name(weight)
            return f
        except Exception:
            return dmsans(size, "Bold")
    return _cached(("ttsans", size, weight), make)


def helvetica(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    def make():
        try:
            return ImageFont.truetype(HELVETICA_TTC, size, index=HELV_INDEX[weight])
        except Exception:
            # Fallback so the module still works off a Mac.
            return dmsans(size, "Medium" if weight in ("medium", "regular") else "Bold")
    return _cached(("helv", size, weight), make)


# ---------------------------------------------------------------------------
# text layout helpers (adapted from legacy/tools/render_demo_quiz.py:172-212)
# ---------------------------------------------------------------------------
_probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))


def text_width(d, text, font, tracking=0.0) -> float:
    w = d.textlength(text, font=font)
    if tracking:
        w += tracking * max(0, len(text) - 1)
    return w


def wrap(d, text, font, max_w, tracking=0.0):
    """Greedy word wrap. Honors explicit \\n. Never drops a too-long word."""
    lines = []
    for para in text.split("\n"):
        words = [w for w in para.split(" ") if w != ""]
        if not words:
            lines.append("")
            continue
        cur = ""
        for w in words:
            t = (cur + " " + w).strip()
            if text_width(d, t, font, tracking) <= max_w or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def line_h(font, leading: float = 1.0) -> float:
    asc, desc = font.getmetrics()
    return (asc + desc) * leading


def fit_font(d, text, max_w, max_h, sizes, font_factory, leading=1.0,
             extra_per_line=0, gap=0, tracking=0.0):
    """
    Largest size whose wrapped text fits (max_w, max_h).

    If even the smallest size overflows (absurd text vs. a tiny safe zone) the
    line list is TRUNCATED with an ellipsis so nothing is ever drawn outside the
    box. Returns (font, lines, size, truncated).
    """
    def block_h(f, n):
        return (line_h(f, leading) + extra_per_line) * n + gap * max(0, n - 1)

    for sz in sizes:
        f = font_factory(sz)
        lines = wrap(d, text, f, max_w, tracking)
        if block_h(f, len(lines)) <= max_h and all(
                text_width(d, ln, f, tracking) <= max_w for ln in lines):
            return f, lines, sz, False

    sz = sizes[-1]
    f = font_factory(sz)
    lines = wrap(d, text, f, max_w, tracking)
    keep = len(lines)
    while keep > 1 and block_h(f, keep) > max_h:
        keep -= 1
    if keep < len(lines):
        lines = lines[:keep]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
        while lines[-1] and text_width(d, lines[-1], f, tracking) > max_w:
            lines[-1] = lines[-1][:-2] + "…"
        return f, lines, sz, True
    return f, lines, sz, False


def _rrect(d, box, radius, fill=None, outline=None, width=0):
    x0, y0, x1, y1 = box
    r = int(max(0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2)))
    d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill, outline=outline, width=width)


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------
@dataclass
class OverlayResult:
    path: str
    style: str
    font_size: int
    lines: list
    bbox: tuple           # (x0, y0, x1, y1) of everything drawn, excl. the tag
    safe_zone: dict
    fits_safe_zone: bool
    truncated: bool = False


# ---------------------------------------------------------------------------
# "Dramatization" tag
# ---------------------------------------------------------------------------
def _draw_dramatization(img, style, corner="bottom-left", label="Dramatization"):
    d = ImageDraw.Draw(img)
    mx, my = 72, 200            # margin from frame edge (my keeps it off TikTok's caption)
    if style == "neo-brutalist":
        f = dmsans(28, "Bold")
        padx, pady = 20, 12
    else:
        f = dmsans(26, "Medium")
        padx, pady = 20, 11
    tw = text_width(d, label, f)
    asc, desc = f.getmetrics()
    th = asc + desc
    bw, bh = int(tw + 2 * padx), int(th + 2 * pady)

    if "left" in corner:
        x0 = mx
    else:
        x0 = W - mx - bw
    if "top" in corner:
        y0 = 240
    else:
        y0 = H - my - bh

    box = (x0, y0, x0 + bw, y0 + bh)
    if style == "white-bar":
        _rrect(d, box, bh // 2, fill=(0, 0, 0, 150))
        d.text((x0 + padx, y0 + pady), label, font=f, fill=(255, 255, 255, 235), anchor="la")
    elif style == "notes-app":
        _rrect(d, box, bh // 2, fill=(236, 236, 232, 240))
        d.text((x0 + padx, y0 + pady), label, font=f, fill=(90, 90, 96, 255), anchor="la")
    else:  # neo-brutalist
        sh = 6
        _rrect(d, (box[0] + sh, box[1] + sh, box[2] + sh, box[3] + sh), NB_RADIUS_SM,
               fill=TOKENS["ink"] + (255,))
        _rrect(d, box, NB_RADIUS_SM, fill=TOKENS["paper"] + (255,),
               outline=TOKENS["ink"] + (255,), width=NB_BORDER)
        d.text((x0 + padx, y0 + pady), label, font=f, fill=TOKENS["ink"] + (255,), anchor="la")
    return box


# ---------------------------------------------------------------------------
# style: white-bar
# ---------------------------------------------------------------------------
def _style_white_bar(img, text, sz, valign="top"):
    d = ImageDraw.Draw(img)
    PADX, PADY = 26, 14         # band padding around each line
    GAP = 14                    # vertical gap between bands
    RADIUS = 16
    LEADING = 1.06              # inside the band; GAP supplies the generous spacing
    inner_w = sz["w"] - 2 * PADX

    font, lines, size, truncated = fit_font(
        d, text, inner_w, sz["h"],
        sizes=list(range(64, 29, -2)),
        font_factory=lambda s: dmsans(s, "Bold"),
        leading=LEADING, extra_per_line=2 * PADY, gap=GAP,
    )

    asc, desc = font.getmetrics()
    band_h = int(round((asc + desc) * LEADING)) + 2 * PADY
    total_h = band_h * len(lines) + GAP * (len(lines) - 1)
    y0_all = _block_y(sz, total_h, valign)
    y = y0_all
    x0_all, x1_all = W, 0

    for ln in lines:
        if ln == "":
            y += band_h + GAP
            continue
        tw = text_width(d, ln, font)
        bx0 = sz["x"]
        bx1 = bx0 + tw + 2 * PADX
        _rrect(d, (bx0, y, bx1, y + band_h), RADIUS, fill=(0, 0, 0, 158))
        d.text((bx0 + PADX, y + PADY), ln, font=font, fill=(255, 255, 255, 255), anchor="la")
        x0_all, x1_all = min(x0_all, bx0), max(x1_all, bx1)
        y += band_h + GAP

    y1 = y - GAP
    return size, lines, (x0_all, y0_all, x1_all, y1), truncated


# ---------------------------------------------------------------------------
# style: tiktok-native
#
# Matches the in-app TikTok text tool: centred white bold text, no band, soft
# dark drop shadow for legibility, tight leading. Sentence case and punctuation
# are preserved verbatim - this style must never transform the copy.
# ---------------------------------------------------------------------------
TN_SHADOW_BLUR = 9          # gaussian radius of the soft shadow
TN_SHADOW_ALPHA = 168       # shadow opacity
TN_SHADOW_OFF = (0, 3)      # shadow offset (x, y)
# Measured off the live post: the app's line spacing is tight, ~1.05, not 1.14.
TN_LEADING = 1.06
# Measured against a real live post (/tmp/dani-live/post3.jpg, zoomed 3x): the
# app's own caption text is SemiBold, not Bold. Bold reads visibly heavier than
# the native tool and was the single biggest reason our renders looked off-brand.
# Medium is a touch too light.
TN_WEIGHT = "SemiBold"

# The `screen_style: confessional` arm, measured off a reference grid doing
# 3.5M-13.5M views per post. Its text is SMALLER and LONGER than a headline:
# 4-6 lines at roughly half the size, sitting over the middle of the face rather
# than below it. Medium not SemiBold, because at that size SemiBold reads shouty.
CONF_SIZES = list(range(46, 27, -2))
CONF_WEIGHT = "Medium"
CONF_LEADING = 1.20        # looser than a headline: more lines need more air
# Yellow, as used on roughly half the reference posts. Not the brand yellow —
# that one is a fill colour and goes muddy as small type over a dark photo.
TEXT_COLORS = {"white": (255, 255, 255), "yellow": (255, 233, 87)}


# The native text box has fixed side margins - measured at roughly 76% of frame
# width - independent of wherever the safe zone happens to end. Without this cap a
# wide zone wraps later than the real tool does and the line breaks give it away.
TN_MAX_W_FRAC = 0.76

# TikTok's own UI covers parts of every frame. Text placed under it is invisible in
# the app no matter how well it renders in the file. Measured against a real post:
#   bottom ~500px  caption + username + sound row
#   right  ~180px  like/comment/share/profile rail
#   top    ~230px  Following/For You tabs + search
# A manifest zone at y1445 h385 sat entirely inside the caption block and shipped.
UI_SAFE = {"top": 240, "bottom": 1420, "left": 60, "right": 900}

# The full box for an on-screen WALL. Face-avoiding zones exist to keep her eyes
# visible, which is right for a short line — but the format this arm is copying
# deliberately covers the face (@rareZuhair's posts put the whole body of text over
# it). So a wall gets the entire UI-safe region and the face goes under it. That is
# the arm, not a bug in the zone.
def full_ui_safe_zone() -> dict:
    return {"x": UI_SAFE["left"], "y": UI_SAFE["top"],
            "w": UI_SAFE["right"] - UI_SAFE["left"],
            "h": UI_SAFE["bottom"] - UI_SAFE["top"]}


def confessional_zone() -> dict:
    """A tall centre-weighted zone for the confessional arm.

    Deliberately sits OVER the middle of the face, which every other style avoids.
    That is the whole visual grammar of the reference posts: the text is the subject
    and the photo is texture behind it, so a zone tucked into a corner would render
    a confessional post as a headline post with more words.
    """
    z = full_ui_safe_zone()
    h = int(z["h"] * 0.62)
    return {"x": z["x"] + 30, "y": z["y"] + (z["h"] - h) // 2, "w": z["w"] - 60, "h": h}


def clamp_to_ui_safe(zone: dict) -> dict:
    """Pull a zone inside the region TikTok's chrome does not cover."""
    if not zone:
        return zone
    x = max(zone["x"], UI_SAFE["left"])
    y = max(zone["y"], UI_SAFE["top"])
    right = min(zone["x"] + zone["w"], UI_SAFE["right"])
    bottom = min(zone["y"] + zone["h"], UI_SAFE["bottom"])
    # a zone entirely below the fold gets moved up rather than collapsed to nothing
    if bottom - y < 160:
        h = min(zone["h"], UI_SAFE["bottom"] - UI_SAFE["top"])
        y = max(UI_SAFE["top"], UI_SAFE["bottom"] - h)
        bottom = y + h
    return {"x": x, "y": y, "w": max(120, right - x), "h": max(120, bottom - y)}


def _style_tiktok_native(img, text, sz, valign="center", text_color="light",
                         screen_style="headline"):
    # OPERATOR RULE 2026-08-01: white unless a caller asks for a NAMED colour.
    # Per-still dark text was an attempt to solve contrast against pale
    # backgrounds; the soft shadow handles that, and mixed colours made the feed
    # inconsistent. But "always white" was too strong: 2026-08-03 added a yellow
    # arm from the reference grid, so an explicit name now wins.
    if text_color not in TEXT_COLORS:
        text_color = "light"
    """text_color: "light" = white type / dark shadow (default, matches the app).
    "dark" = near-black type / soft WHITE halo, for pale blown-out backgrounds where
    white type is invisible. Several stills require this - see manifest text_color."""
    d = ImageDraw.Draw(img)
    inner_w = min(sz["w"], int(W * TN_MAX_W_FRAC))

    font, lines, size, truncated = fit_font(
        d, text, inner_w, sz["h"],
        # Capped at 62: the native tool has a default size users rarely change,
        # and letting autofit fill the safe zone produced type roughly 1.5x the
        # real thing. Measured against /tmp/dani-live/post3.jpg.
        sizes=(CONF_SIZES if screen_style == "confessional" else list(range(62, 33, -2))),
        font_factory=(lambda s: tiktoksans(s, CONF_WEIGHT)) if screen_style == "confessional"
                     else (lambda s: tiktoksans(s, TN_WEIGHT)),
        leading=(CONF_LEADING if screen_style == "confessional" else TN_LEADING),
    )

    lh = line_h(font, TN_LEADING)
    total_h = int(round(lh * len(lines)))
    y0_all = _block_y(sz, total_h, valign)
    cx = sz["x"] + sz["w"] / 2.0

    # Shadow pass on its own layer so the blur cannot eat the glyph edges.
    if text_color in TEXT_COLORS:
        body_rgb, halo_rgb, halo_alpha = TEXT_COLORS[text_color], (0, 0, 0), TN_SHADOW_ALPHA
    else:
        halo_rgb = (0, 0, 0) if text_color == "light" else (255, 255, 255)
        body_rgb = (255, 255, 255) if text_color == "light" else (17, 17, 19)
        halo_alpha = TN_SHADOW_ALPHA if text_color == "light" else 210
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ds = ImageDraw.Draw(shadow)
    y = float(y0_all)
    for ln in lines:
        if ln:
            ds.text((cx + TN_SHADOW_OFF[0], y + TN_SHADOW_OFF[1]), ln, font=font,
                    fill=halo_rgb + (halo_alpha,), anchor="ma")
        y += lh
    shadow = shadow.filter(ImageFilter.GaussianBlur(TN_SHADOW_BLUR))
    img.alpha_composite(shadow)

    # White text pass.
    y = float(y0_all)
    x0_all, x1_all = W, 0
    for ln in lines:
        if ln:
            tw = text_width(d, ln, font)
            x0_all = min(x0_all, cx - tw / 2.0)
            x1_all = max(x1_all, cx + tw / 2.0)
            d.text((cx, y), ln, font=font, fill=body_rgb + (255,), anchor="ma")
        y += lh

    return size, lines, (int(x0_all), y0_all, int(x1_all), int(round(y))), truncated


# ---------------------------------------------------------------------------
# style: notes-app
# ---------------------------------------------------------------------------
def _style_notes_app(img, text, sz, meta="Today at 9:41 AM", valign="center"):
    d = ImageDraw.Draw(img)
    PAD = 52
    RADIUS = 40
    LEADING = 1.34
    CARD = (252, 251, 247, 252)
    BODY = (28, 28, 30, 255)
    GRAY = (142, 142, 147, 255)

    meta_f = helvetica(28, "medium")
    meta_asc, meta_desc = meta_f.getmetrics()
    meta_h = meta_asc + meta_desc
    meta_gap = 26

    max_card_w = sz["w"]
    inner_w = max_card_w - 2 * PAD
    max_body_h = sz["h"] - 2 * PAD - meta_h - meta_gap

    font, lines, size, truncated = fit_font(
        d, text, inner_w, max_body_h,
        sizes=list(range(52, 25, -2)),
        font_factory=lambda s: helvetica(s, "regular"),
        leading=LEADING,
    )

    lh = line_h(font, LEADING)
    body_h = lh * len(lines)
    card_h = int(round(2 * PAD + meta_h + meta_gap + body_h))
    card_w = max_card_w
    cx0 = sz["x"]
    cy0 = int(round(_block_y(sz, card_h, valign)))
    box = (cx0, cy0, cx0 + card_w, cy0 + card_h)

    # subtle blurred drop shadow on its own layer, composited under the card
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    _rrect(sd, (box[0] + 4, box[1] + 14, box[2] - 4, box[3] + 18), RADIUS, fill=(0, 0, 0, 92))
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    img.alpha_composite(shadow)
    d = ImageDraw.Draw(img)

    _rrect(d, box, RADIUS, fill=CARD)
    _rrect(d, box, RADIUS, outline=(0, 0, 0, 26), width=2)

    ty = cy0 + PAD
    d.text((cx0 + PAD, ty), meta, font=meta_f, fill=GRAY, anchor="la")
    ty += meta_h + meta_gap
    asc, desc = font.getmetrics()
    slack = (lh - (asc + desc)) / 2.0
    for ln in lines:
        d.text((cx0 + PAD, ty + slack), ln, font=font, fill=BODY, anchor="la")
        ty += lh

    return size, lines, box, truncated


# ---------------------------------------------------------------------------
# style: neo-brutalist  (SFFS tokens)
# ---------------------------------------------------------------------------
def _style_neo_brutalist(img, text, sz, color="yellow", uppercase=True, valign="center"):
    d = ImageDraw.Draw(img)
    PADX, PADY = 46, 40
    LEADING = 1.08
    fill_rgb = TOKENS.get(color, TOKENS["yellow"])
    body = text.upper() if uppercase else text

    max_card_w = sz["w"] - NB_SHADOW           # leave room for the hard shadow
    inner_w = max_card_w - 2 * PADX
    max_body_h = sz["h"] - 2 * PADY - NB_SHADOW

    font, lines, size, truncated = fit_font(
        d, body, inner_w, max_body_h,
        sizes=list(range(112, 39, -2)),
        font_factory=anton,
        leading=LEADING,
    )

    lh = line_h(font, LEADING)
    body_h = lh * len(lines)
    card_w = max_card_w
    card_h = int(round(2 * PADY + body_h))
    cx0 = sz["x"]
    cy0 = int(round(_block_y(sz, card_h + NB_SHADOW, valign)))
    box = (cx0, cy0, cx0 + card_w, cy0 + card_h)

    # zero-blur hard shadow (tokens.css --shadow-card)
    _rrect(d, (box[0] + NB_SHADOW, box[1] + NB_SHADOW, box[2] + NB_SHADOW, box[3] + NB_SHADOW),
           NB_RADIUS, fill=TOKENS["ink"] + (255,))
    _rrect(d, box, NB_RADIUS, fill=fill_rgb + (255,),
           outline=TOKENS["ink"] + (255,), width=NB_BORDER)

    asc, desc = font.getmetrics()
    slack = (lh - (asc + desc)) / 2.0
    cx = cx0 + card_w / 2.0
    ty = cy0 + PADY
    for ln in lines:
        d.text((cx, ty + slack), ln, font=font, fill=TOKENS["ink"] + (255,), anchor="ma")
        ty += lh

    return size, lines, (box[0], box[1], box[2] + NB_SHADOW, box[3] + NB_SHADOW), truncated


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def render_overlay(text: str,
                   out_path: str,
                   style: str = DEFAULT_STYLE,
                   text_color: str = "light",
                   screen_style: str = "headline",
                   safe_zone: dict | None = None,
                   dramatization: bool = False,
                   dramatization_corner: str = "bottom-left",
                   nb_color: str = "yellow",
                   notes_meta: str = "Today at 9:41 AM",
                   valign: str | None = None) -> OverlayResult:
    """Render `text` to a transparent 1080x1920 RGBA PNG at `out_path`."""
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}; expected one of {STYLES}")
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")

    ensure_fonts()
    # Clamp inside TikTok's chrome BEFORE fitting text. Every zone in the manifest
    # was authored by eye against the raw image, and all 10 of them extended under
    # the caption block or the action rail — invisible in the app, fine in the file.
    sz = clamp_to_ui_safe(dict(DEFAULT_SAFE_ZONE if safe_zone is None else safe_zone))
    for k in ("x", "y", "w", "h"):
        if k not in sz:
            raise ValueError(f"safe_zone missing key {k!r}")
        sz[k] = int(sz[k])
    if sz["w"] < 200 or sz["h"] < 120:
        raise ValueError(f"safe_zone too small to hold text: {sz}")

    valign = valign or DEFAULT_VALIGN[style]
    if valign not in ("top", "center", "bottom"):
        raise ValueError(f"valign must be top|center|bottom, got {valign!r}")

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if style == "white-bar":
        size, lines, bbox, truncated = _style_white_bar(img, text, sz, valign=valign)
    elif style == "tiktok-native":
        size, lines, bbox, truncated = _style_tiktok_native(img, text, sz, valign=valign,
                                                              text_color=text_color,
                                                              screen_style=screen_style)
    elif style == "notes-app":
        size, lines, bbox, truncated = _style_notes_app(img, text, sz, meta=notes_meta,
                                                        valign=valign)
    else:
        size, lines, bbox, truncated = _style_neo_brutalist(img, text, sz, color=nb_color,
                                                            valign=valign)
    if truncated:
        print(f"WARNING: text too long for safe_zone {sz} even at the minimum size; "
              f"truncated with an ellipsis.", file=sys.stderr)

    if dramatization:
        _draw_dramatization(img, style, corner=dramatization_corner)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    img.save(out_path)

    fits = (bbox[0] >= sz["x"] - 1 and bbox[1] >= sz["y"] - 1
            and bbox[2] <= sz["x"] + sz["w"] + 1 and bbox[3] <= sz["y"] + sz["h"] + 1)

    return OverlayResult(path=os.path.abspath(out_path), style=style, font_size=size,
                         lines=lines, bbox=tuple(round(v, 1) for v in bbox),
                         safe_zone=sz, fits_safe_zone=fits, truncated=truncated)


def parse_safe_zone(s: str | None) -> dict | None:
    if not s:
        return None
    parts = [p for p in s.replace(" ", "").split(",") if p != ""]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--safe-zone must be x,y,w,h")
    x, y, w, h = (int(float(p)) for p in parts)
    return {"x": x, "y": y, "w": w, "h": h}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render hook text to a transparent 1080x1920 PNG.")
    ap.add_argument("--text", required=True)
    ap.add_argument("--style", default=DEFAULT_STYLE, choices=STYLES)
    ap.add_argument("--out", required=True)
    ap.add_argument("--safe-zone", default=None, help="x,y,w,h (default 72,320,936,1120)")
    ap.add_argument("--dramatization", action="store_true")
    ap.add_argument("--dramatization-corner", default="bottom-left",
                    choices=["bottom-left", "bottom-right", "top-left", "top-right"])
    ap.add_argument("--nb-color", default="yellow", choices=sorted(TOKENS))
    ap.add_argument("--notes-meta", default="Today at 9:41 AM")
    ap.add_argument("--valign", default=None, choices=["top", "center", "bottom"])
    a = ap.parse_args(argv)

    r = render_overlay(a.text, a.out, style=a.style, safe_zone=parse_safe_zone(a.safe_zone),
                       dramatization=a.dramatization,
                       dramatization_corner=a.dramatization_corner,
                       nb_color=a.nb_color, notes_meta=a.notes_meta, valign=a.valign)
    print(json.dumps(asdict(r), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
