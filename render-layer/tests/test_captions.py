import json, os, pytest
from PIL import Image, ImageDraw, ImageFont
from render import captions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "assets", "fonts", "TikTokSans-Variable.ttf")
STATIC_FONT = os.path.join(ROOT, "assets", "fonts", "Anton-Regular.ttf")
CHANNEL_JSON = os.path.join(ROOT, "channels", "main", "channel.json")
CFG = {"caption_weight": 900, "caption_size_px": 96, "caption_center_y": 0.575,
       "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
SIZE = (1080, 1920)

# Global constraints this file is the last line of defence for.
BAND_LO, BAND_HI = 0.55, 0.60   # captions sit at 55-60% of frame height
REQUIRED_WEIGHT = 900           # variable Weight axis, range 300-900


def _cfg(**over):
    c = dict(CFG)
    c.update(over)
    return c


def _ink_px(path):
    """Count of non-transparent pixels -- a heavier weight lays down strictly more."""
    a = Image.open(path).getchannel("A")
    return sum(1 for v in a.get_flattened_data() if v > 0)


def _ink_centre_y(path):
    bbox = Image.open(path).getchannel("A").getbbox()
    assert bbox is not None, "expected ink"
    return (bbox[1] + bbox[3]) / 2.0

def test_safe_box_insets_match_config():
    b = captions.safe_box(1080, 1920, CFG)
    assert b["left"] == pytest.approx(64.8)
    assert b["right"] == pytest.approx(928.8)
    assert b["top"] == pytest.approx(192.0)
    assert b["bottom"] == pytest.approx(1574.4)

def test_word_png_is_transparent_rgba_of_canvas_size(tmp_path):
    p = captions.render_word_png("hello", SIZE, CFG, FONT, str(tmp_path / "w.png"))
    img = Image.open(p)
    assert img.mode == "RGBA"
    assert img.size == SIZE
    assert img.getpixel((5, 5))[3] == 0  # corner stays transparent

def test_word_ink_lands_inside_the_safe_box(tmp_path):
    p = captions.render_word_png("hello", SIZE, CFG, FONT, str(tmp_path / "w.png"))
    bbox = Image.open(p).getchannel("A").getbbox()
    b = captions.safe_box(*SIZE, CFG)
    assert bbox is not None
    assert bbox[0] >= b["left"] - 1 and bbox[2] <= b["right"] + 1
    assert bbox[1] >= b["top"] - 1 and bbox[3] <= b["bottom"] + 1

def test_long_word_is_shrunk_to_fit_rather_than_overflowing(tmp_path):
    p = captions.render_word_png("supercalifragilisticexpialidocious",
                                 SIZE, CFG, FONT, str(tmp_path / "l.png"))
    bbox = Image.open(p).getchannel("A").getbbox()
    b = captions.safe_box(*SIZE, CFG)
    assert bbox[0] >= b["left"] - 1 and bbox[2] <= b["right"] + 1

def test_blank_png_is_fully_transparent(tmp_path):
    p = captions.render_blank_png(SIZE, str(tmp_path / "b.png"))
    assert Image.open(p).getchannel("A").getbbox() is None

def test_render_is_deterministic(tmp_path):
    a = captions.render_word_png("same", SIZE, CFG, FONT, str(tmp_path / "a.png"))
    b = captions.render_word_png("same", SIZE, CFG, FONT, str(tmp_path / "b.png"))
    assert open(a, "rb").read() == open(b, "rb").read()

def test_empty_word_raises():
    with pytest.raises(captions.CaptionError):
        captions.render_word_png("   ", SIZE, CFG, FONT, "/tmp/x.png")


# --- the Weight-axis-900 constraint -----------------------------------------

def test_weight_900_lays_down_more_ink_than_weight_300(tmp_path):
    """Fails if the weight is never applied, or is hardcoded, or lands on the
    wrong axis -- all three collapse 900 onto the axis default of 300."""
    heavy = _ink_px(captions.render_word_png(
        "hello", SIZE, _cfg(caption_weight=900), FONT, str(tmp_path / "h.png")))
    light = _ink_px(captions.render_word_png(
        "hello", SIZE, _cfg(caption_weight=300), FONT, str(tmp_path / "l.png")))
    assert heavy > light * 1.15, (
        f"weight 900 inked {heavy}px vs weight 300 {light}px -- the Weight axis "
        f"is not reaching the font")


def test_weight_900_matches_the_fonts_own_black_named_instance(tmp_path):
    """Independent oracle for 'the 900 landed on the Weight axis'.

    Pillow offers no readback of applied variation coordinates -- get_variation_axes()
    keeps reporting the untouched fvar table -- so a positional-order bug cannot be
    caught by inspecting the font object. The font's own 'Black' named instance IS
    weight 900, and set_variation_by_name resolves it without any positional list,
    so it pins the axis identity rather than the argument order.
    """
    rendered = Image.open(captions.render_word_png(
        "hello", SIZE, CFG, FONT, str(tmp_path / "w.png"))).convert("RGBA")

    ref_font = ImageFont.truetype(FONT, int(CFG["caption_size_px"]))
    ref_font.set_variation_by_name("Black")
    box = captions.safe_box(*SIZE, CFG)
    ref = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(ref).text(
        ((box["left"] + box["right"]) / 2.0, SIZE[1] * CFG["caption_center_y"]),
        "hello", font=ref_font, fill=captions.FILL, anchor="mm",
        stroke_width=captions.STROKE_PX, stroke_fill=captions.STROKE)

    assert list(rendered.get_flattened_data()) == list(ref.get_flattened_data()), (
        "caption render does not match the font's own weight-900 named instance")


def test_static_font_raises_instead_of_silently_downgrading_weight(tmp_path):
    """A swallowed exception here would render the whole build at nominal weight.

    Anton-Regular is a static build: Pillow 12.1.1 raises OSError from
    set_variation_by_axes / get_variation_axes on it.
    """
    with pytest.raises(captions.CaptionError, match="variation font"):
        captions.render_word_png("hello", SIZE, CFG, STATIC_FONT,
                                 str(tmp_path / "s.png"))


def test_out_of_range_weight_raises_instead_of_clamping(tmp_path):
    with pytest.raises(captions.CaptionError, match="outside"):
        captions.render_word_png("hello", SIZE, _cfg(caption_weight=1200), FONT,
                                 str(tmp_path / "o.png"))


# --- the 55-60% frame-height constraint -------------------------------------

def test_caption_ink_centre_sits_in_the_mandated_55_to_60_percent_band(tmp_path):
    """The safe box spans 10%-82% of the frame, so the safe-box test above passes
    for any cy in a band 14x wider than the mandated one. This is the narrow check."""
    cy = _ink_centre_y(captions.render_word_png(
        "hello", SIZE, CFG, FONT, str(tmp_path / "w.png")))
    frac = cy / SIZE[1]
    assert BAND_LO <= frac <= BAND_HI, (
        f"caption ink centre measured at {cy:.1f}px = {frac:.4f} of frame height, "
        f"outside the mandated {BAND_LO}-{BAND_HI}")


def test_centre_y_tracks_config_rather_than_the_safe_box_midpoint(tmp_path):
    """Pins cy to caption_center_y. The safe-box midpoint is 46.0% of the frame,
    so a fallback to it reads as a distinct, detectable value."""
    box = captions.safe_box(*SIZE, CFG)
    midpoint_frac = ((box["top"] + box["bottom"]) / 2.0) / SIZE[1]
    assert not BAND_LO <= midpoint_frac <= BAND_HI, "midpoint no longer distinguishable"
    for want in (0.55, 0.575, 0.60):
        cy = _ink_centre_y(captions.render_word_png(
            "hello", SIZE, _cfg(caption_center_y=want), FONT,
            str(tmp_path / f"c{want}.png")))
        assert cy / SIZE[1] == pytest.approx(want, abs=0.01)


def test_shipped_channel_config_renders_inside_the_band(tmp_path):
    """The constraint binds the artifact that actually ships, not just this file's CFG."""
    with open(CHANNEL_JSON, encoding="utf-8") as fh:
        cfg = json.load(fh)
    assert int(cfg["caption_weight"]) == REQUIRED_WEIGHT
    size = (int(cfg["width"]), int(cfg["height"]))
    assert size == SIZE
    cy = _ink_centre_y(captions.render_word_png(
        "hello", size, cfg, FONT, str(tmp_path / "ch.png")))
    frac = cy / size[1]
    assert BAND_LO <= frac <= BAND_HI, (
        f"{CHANNEL_JSON} puts caption ink at {frac:.4f} of frame height")


# --- the remaining silent knobs ---------------------------------------------

def test_white_fill_is_outlined_in_black(tmp_path):
    """Captions composite over gameplay footage; without the stroke, white text on
    a bright frame is unreadable. No alpha-only assertion can see this."""
    img = Image.open(captions.render_word_png(
        "hello", SIZE, CFG, FONT, str(tmp_path / "w.png"))).convert("RGBA")
    black = white = 0
    for r, g, b, a in img.get_flattened_data():
        if a == 255:
            if r < 16 and g < 16 and b < 16:
                black += 1
            elif r > 240 and g > 240 and b > 240:
                white += 1
    assert white > 0, "no opaque white fill"
    assert black > 0, "no opaque black outline -- stroke_width/stroke_fill dropped"


def test_caption_size_px_changes_the_rendered_ink_height(tmp_path):
    big = Image.open(captions.render_word_png(
        "hi", SIZE, _cfg(caption_size_px=96), FONT,
        str(tmp_path / "b.png"))).getchannel("A").getbbox()
    small = Image.open(captions.render_word_png(
        "hi", SIZE, _cfg(caption_size_px=48), FONT,
        str(tmp_path / "s.png"))).getchannel("A").getbbox()
    assert (big[3] - big[1]) > (small[3] - small[1]) * 1.2, (
        f"caption_size_px ignored: heights {big[3]-big[1]} vs {small[3]-small[1]}")
