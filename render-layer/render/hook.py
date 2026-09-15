"""hook.py — the opening card. Same Pillow path as captions, one alpha track."""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont
from render.captions import safe_box, render_blank_png, STROKE_PX, FILL, STROKE

LINE_SPACING = 1.18
MIN_PX = 22
SHRINK_STEP = 4
TOL_PX = 1.0


class HookError(ValueError):
    pass


def _text_w(draw, text, font) -> float:
    l, _, r, _ = draw.textbbox((0, 0), text, font=font, stroke_width=STROKE_PX)
    return r - l


def _wrap(draw, text, font, max_w):
    """Greedy word wrap at `max_w`.

    A single word wider than `max_w` still has to go on a line of its own -- there
    is nowhere else to put it -- so wrapping ALONE never guarantees the block fits.
    The caller must measure the result and shrink; see `_layout`.
    """
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if _text_w(draw, trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _layout(draw, text, font, px: float, box: dict):
    """Wrap at this size and return `(placements, ink)`.

    `ink` is the exact box the drawn block will occupy, measured with the same
    anchor, stroke width and coordinates the `draw.text` calls use -- so the fit
    test is made against the ink that actually lands, not against an em-box
    estimate of it. At large sizes the em box overstates the ink (Anton's caps
    are ~0.72em) and at small sizes the fixed 8px stroke makes it understate,
    which is exactly the band where an estimate would pass and the pixels
    would not.
    """
    lines = _wrap(draw, text, font, box["right"] - box["left"])
    line_h = px * LINE_SPACING
    cx = (box["left"] + box["right"]) / 2.0
    y = (box["top"] + box["bottom"]) / 2.0 - line_h * len(lines) / 2.0 + line_h / 2.0

    placements, ink = [], None
    for line in lines:
        placements.append((cx, y, line))
        bb = draw.textbbox((cx, y), line, font=font, anchor="mm",
                           stroke_width=STROKE_PX)
        ink = bb if ink is None else (min(ink[0], bb[0]), min(ink[1], bb[1]),
                                      max(ink[2], bb[2]), max(ink[3], bb[3]))
        y += line_h
    return placements, ink


def _outside(ink, box) -> list[str]:
    if ink is None:
        return ["no ink at all"]
    over = []
    if ink[0] < box["left"] - TOL_PX:
        over.append(f"left by {box['left'] - ink[0]:.0f}px")
    if ink[2] > box["right"] + TOL_PX:
        over.append(f"right by {ink[2] - box['right']:.0f}px")
    if ink[1] < box["top"] - TOL_PX:
        over.append(f"top by {box['top'] - ink[1]:.0f}px")
    if ink[3] > box["bottom"] + TOL_PX:
        over.append(f"bottom by {ink[3] - box['bottom']:.0f}px")
    return over


def render_hook_png(text: str, size, cfg: dict, font_path: str, out_path: str) -> str:
    """Render the hook card to a full-canvas RGBA PNG and return `out_path`.

    Empty (or whitespace-only) text is a DESIGNED path, not bad input, and must
    return a frame rather than raise. `captions.build_alpha_track` takes
    `hook_text: str = ""` and calls `render_hook_png(hook_text or "", ...)` for
    every `kind == "hook"` event, and `build_timeline` emits a hook event
    whenever `hook_lead_s > 0` -- so a caller that wants a silent lead-in (or
    simply does not pass `hook_text`) reaches this function with `""` on the
    normal path. Raising there takes out the whole alpha track, not just the
    card. The lead still needs an image for its slot in the concat manifest, so
    empty text renders the same fully-transparent card `render_blank_png`
    produces: gameplay footage shows through for the lead and nothing is
    clipped. A blank hook is therefore a signal about the CALLER's `hook_text`,
    and belongs to whoever assembles it -- not to the rasteriser.
    """
    text = " ".join((text or "").split())
    if not text:
        return render_blank_png(size, out_path)
    width, height = size
    box = safe_box(width, height, cfg)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Shrink until the MEASURED block sits inside the safe box. The floor is
    # clamped rather than used as a loop guard so that a small configured size
    # renders instead of leaving the layout locals unbound.
    px = max(int(cfg["hook_size_px"]), MIN_PX)
    while True:
        font = ImageFont.truetype(font_path, px)
        placements, ink = _layout(draw, text, font, px, box)
        over = _outside(ink, box)
        if not over or px <= MIN_PX:
            break
        px = max(px - SHRINK_STEP, MIN_PX)

    if over:
        # `ink is None` is unreachable now that empty text returns early, but the
        # message must not itself raise a TypeError if that ever changes.
        shown = "none" if ink is None else str(tuple(round(v) for v in ink))
        raise HookError(
            f"hook text does not fit the safe box at the {MIN_PX}px floor: ink "
            f"{shown} overruns "
            f"{{left {box['left']:.0f}, top {box['top']:.0f}, right "
            f"{box['right']:.0f}, bottom {box['bottom']:.0f}}} "
            f"({', '.join(over)}); {len(placements)} lines, "
            f"{len(text)} chars. Shorten the hook rather than letting it bleed "
            f"off frame.")

    for cx, y, line in placements:
        draw.text((cx, y), line, font=font, fill=FILL, anchor="mm",
                  stroke_width=STROKE_PX, stroke_fill=STROKE)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
