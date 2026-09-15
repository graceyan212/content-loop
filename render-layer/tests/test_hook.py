import os
import pytest
from PIL import Image, ImageDraw, ImageFont
from render import hook, captions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "assets", "fonts", "Anton-Regular.ttf")
CFG = {"hook_size_px": 118, "caption_center_y": 0.5,
       "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
SIZE = (1080, 1920)


def test_hook_wraps_onto_multiple_lines_inside_the_safe_box(tmp_path):
    p = hook.render_hook_png(
        "my neighbour billed me for breathing near his fence",
        SIZE, CFG, FONT, str(tmp_path / "h.png"))
    img = Image.open(p)
    assert img.mode == "RGBA" and img.size == SIZE
    bbox = img.getchannel("A").getbbox()
    b = captions.safe_box(*SIZE, CFG)
    assert bbox[0] >= b["left"] - 1 and bbox[2] <= b["right"] + 1
    assert bbox[1] >= b["top"] - 1 and bbox[3] <= b["bottom"] + 1


def test_taller_than_one_caption_line(tmp_path):
    p = hook.render_hook_png("a b c d e f g h i j k l m n o p",
                             SIZE, CFG, FONT, str(tmp_path / "h2.png"))
    bbox = Image.open(p).getchannel("A").getbbox()
    assert (bbox[3] - bbox[1]) > 150   # wrapped, not a single line


def _cfg(**over):
    c = dict(CFG)
    c.update(over)
    return c


def _inside(bbox, b):
    return (bbox[0] >= b["left"] - 1 and bbox[2] <= b["right"] + 1
            and bbox[1] >= b["top"] - 1 and bbox[3] <= b["bottom"] + 1)


def test_a_word_too_wide_to_wrap_shrinks_instead_of_bleeding_off_frame(tmp_path):
    """A single token cannot be wrapped, so only shrinking keeps it in the box.

    Wrapping puts an over-wide word on a line of its own regardless of width; a
    fit test that only looks at height therefore passes while the glyphs run off
    both edges of the 1080px canvas and get clipped.
    """
    p = hook.render_hook_png("Antidisestablishmentarianismxx", SIZE, CFG, FONT,
                             str(tmp_path / "w.png"))
    bbox = Image.open(p).getchannel("A").getbbox()
    b = captions.safe_box(*SIZE, CFG)
    assert bbox[0] > 0 and bbox[2] < SIZE[0], f"clipped by the frame: {bbox}"
    assert _inside(bbox, b), f"{bbox} outside {b}"


def test_a_token_that_cannot_fit_even_at_the_floor_raises(tmp_path):
    with pytest.raises(hook.HookError, match="does not fit the safe box"):
        hook.render_hook_png("m" * 120, SIZE, CFG, FONT, str(tmp_path / "x.png"))


def test_text_far_too_long_raises_instead_of_overflowing(tmp_path):
    # Measured floor capacity is 884 four-letter words (49 chars per line over 53
    # lines), so ~4000 chars overruns it on height. Starting at 30px keeps the
    # shrink walk short: every stroked textbbox costs ~2ms and the walk measures
    # every line at every candidate size. The raise at the floor is what is under
    # test, not the distance travelled to reach it.
    with pytest.raises(hook.HookError, match="does not fit the safe box"):
        hook.render_hook_png(" ".join(["m" * 20] * 200), SIZE,
                             _cfg(hook_size_px=30), FONT, str(tmp_path / "x.png"))


@pytest.mark.parametrize("text", ["", "   ", "\n\t ", None])
def test_empty_hook_text_renders_a_transparent_card_instead_of_raising(tmp_path, text):
    """Empty hook text is a designed path and must return a frame.

    `captions.build_alpha_track(timeline, size, cfg, font_paths, work_dir,
    out_path, fps, hook_text="")` calls `render_hook_png(hook_text or "", ...)`
    for every `kind == "hook"` event, and `build_timeline` emits a hook event
    whenever `hook_lead_s > 0` -- so a caller that omits `hook_text` arrives here
    with `""` on the normal path. Raising would abort the whole alpha-track
    encode, not just the card.
    """
    p = hook.render_hook_png(text, SIZE, CFG, FONT, str(tmp_path / "e.png"))
    img = Image.open(p)
    assert img.mode == "RGBA" and img.size == SIZE
    assert img.getchannel("A").getbbox() is None, "a blank hook card must have no ink"


def test_the_empty_card_is_the_same_frame_render_blank_png_produces(tmp_path):
    """The lead-in slot is filled by the module that already owns 'blank frame',
    so a hook-with-no-text and a blank caption event are the same pixels."""
    a = hook.render_hook_png("", SIZE, CFG, FONT, str(tmp_path / "hook_empty.png"))
    b = captions.render_blank_png(SIZE, str(tmp_path / "blank.png"))
    ia, ib = Image.open(a), Image.open(b)
    assert (ia.mode, ia.size) == (ib.mode, ib.size)
    assert ia.tobytes() == ib.tobytes()


def test_the_task_9_alpha_track_call_shape_renders(tmp_path):
    """Regression for the exact downstream call, on Task 9's canvas and config.

    A previous revision raised `HookError("refusing to render an empty hook
    card")` here, which made `build_alpha_track` unable to produce a track at
    all whenever `hook_text` was defaulted. The 360x640 canvas and 0.575 caption
    centre are Task 9's, not this module's, on purpose: the raise was
    config-independent and so is the fix.
    """
    cfg9 = {"caption_weight": 900, "caption_size_px": 96, "hook_size_px": 118,
            "caption_center_y": 0.575,
            "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
    size9 = (360, 640)
    p = hook.render_hook_png("" or "", size9, cfg9, FONT, str(tmp_path / "_hook.png"))
    assert os.path.isfile(p)
    img = Image.open(p)
    assert img.mode == "RGBA" and img.size == size9
    assert img.getchannel("A").getbbox() is None

    # ...and a real hook on the same small canvas still fits its safe box.
    q = hook.render_hook_png("one two three four five six seven eight nine",
                             size9, cfg9, FONT, str(tmp_path / "_hook2.png"))
    bbox = Image.open(q).getchannel("A").getbbox()
    assert bbox is not None and _inside(bbox, captions.safe_box(*size9, cfg9))


@pytest.mark.parametrize("px", [20, 18, 1])
def test_a_configured_size_under_the_floor_renders_rather_than_crashing(tmp_path, px):
    p = hook.render_hook_png("my neighbour billed me", SIZE, _cfg(hook_size_px=px),
                             FONT, str(tmp_path / f"s{px}.png"))
    bbox = Image.open(p).getchannel("A").getbbox()
    assert bbox is not None and _inside(bbox, captions.safe_box(*SIZE, CFG))


def test_rendered_ink_matches_the_layout_that_was_measured(tmp_path):
    """The fit decision is made on `_layout`'s predicted ink, so that prediction
    has to bound what Pillow actually rasterises -- otherwise the safe-box check
    is verifying a number the pixels never honour."""
    text = "my neighbour billed me for breathing near his fence"
    p = hook.render_hook_png(text, SIZE, CFG, FONT, str(tmp_path / "m.png"))
    rendered = Image.open(p).getchannel("A").getbbox()

    draw = ImageDraw.Draw(Image.new("RGBA", SIZE))
    b = captions.safe_box(*SIZE, CFG)
    px = CFG["hook_size_px"]
    _, ink = hook._layout(draw, text, ImageFont.truetype(FONT, px), px, b)
    # The prediction is in float coordinates; the rasteriser lands on the pixel
    # grid, so a predicted top of 608.34 legitimately shows up as row 608. Compare
    # on the grid the pixels live on -- an escape of a whole pixel still fails.
    grid = (int(ink[0]), int(ink[1]), -(-ink[2] // 1), -(-ink[3] // 1))
    assert (rendered[0] >= grid[0] and rendered[1] >= grid[1]
            and rendered[2] <= grid[2] and rendered[3] <= grid[3]), \
        f"rendered {rendered} escapes measured ink {ink}"
    assert _inside(rendered, b)
