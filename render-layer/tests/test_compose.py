"""Tests for render/compose.py.

The first six tests are the brief's, unchanged. Everything after them exists
because those six were measured to survive nine destructive mutations of the
module -- including deleting the caption overlay outright, deleting the audio
ducking, and deleting loudnorm -- so they pinned no semantic behaviour at all.
Each added test names the mutation it kills.

One of the brief's own fixtures was wrong and is corrected here: see `_alpha`.
"""
import json, math, os, re, subprocess, pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont
from render import compose, footage

CFG = {"width": 360, "height": 640, "fps": 30, "crf": 28,
       "game_audio_duck": 0.12, "loudnorm_i": -14}

FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "assets", "fonts")
CAPTION_FONT = os.path.join(FONTS, "TikTokSans-Variable.ttf")
HOOK_FONT = os.path.join(FONTS, "Anton-Regular.ttf")


def _clip(path, w, h, secs, audio):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
           f"testsrc=size={w}x{h}:rate=30:duration={secs}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={secs}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(secs), path]
    subprocess.run(cmd, capture_output=True, check=True)


def _silent_mp3(path, secs):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={secs}", "-c:a", "libmp3lame", path],
                   capture_output=True, check=True)


def _alpha(path, w, h, secs):
    """A genuinely transparent qtrle track.

    The brief spelled this `-i color=c=black@0.0:...` with a SEPARATE `-vf
    format=rgba` stage, and that produces a fully OPAQUE black track: lavfi settles
    the input pixel format before the downstream conversion runs, drops the `@0.0`,
    and `format=rgba` then fills alpha with 255. MEASURED on the brief's exact
    command -- pix_fmt `argb`, alpha byte min 255, max 255 -- so every render test
    was compositing an opaque black rectangle over the whole canvas and asserting
    against a 100% black video. Moving `format=rgba` INSIDE the lavfi graph keeps
    the alpha: measured alpha min 0, max 0.
    """
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"color=c=black@0.0:s={w}x{h}:r=30:d={secs},format=rgba",
                    "-c:v", "qtrle", path],
                   capture_output=True, check=True)


def test_filtergraph_never_emits_a_text_filter():
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2, "layout": "fill", "has_audio": True}]
    g = compose.build_filtergraph(segs, 1, CFG, 1, 2, 3, 0.0)
    for banned in ("drawtext", "ass=", "subtitles"):
        assert banned not in g


def test_pillarbox_layout_uses_blur():
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2, "layout": "pillarbox", "has_audio": True}]
    assert "gblur" in compose.build_filtergraph(segs, 1, CFG, 1, 2, 3, 0.0)


def test_fill_layout_crops_without_blur():
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2, "layout": "fill", "has_audio": True}]
    g = compose.build_filtergraph(segs, 1, CFG, 1, 2, 3, 0.0)
    assert "crop=360:640" in g and "gblur" not in g


def test_renders_a_playable_portrait_mp4(tmp_path):
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 4, audio=True)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 3)
    narr = str(tmp_path / "n.mp3"); _silent_mp3(narr, 2)
    segs = [{"path": clip, "start_s": 0.5, "take_s": 3.0, "layout": "fill", "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "out.mp4"), CFG, total_s=3.0)
    data = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         "-show_format", out], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    a = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert (int(v["width"]), int(v["height"])) == (360, 640)
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert a["codec_name"] == "aac"
    assert 2.6 < float(data["format"]["duration"]) < 3.5


def test_silent_gameplay_clip_still_renders(tmp_path):
    clip = str(tmp_path / "s.mp4"); _clip(clip, 360, 640, 4, audio=False)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 2)
    narr = str(tmp_path / "n.mp3"); _silent_mp3(narr, 2)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill", "has_audio": False}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o2.mp4"), CFG, total_s=2.0)
    assert os.path.getsize(out) > 1000


def test_two_segments_concatenate(tmp_path):
    c1 = str(tmp_path / "1.mp4"); _clip(c1, 360, 640, 2, audio=True)
    c2 = str(tmp_path / "2.mp4"); _clip(c2, 640, 360, 2, audio=False)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 3)
    narr = str(tmp_path / "n.mp3"); _silent_mp3(narr, 2)
    segs = [{"path": c1, "start_s": 0, "take_s": 1.5, "layout": "fill", "has_audio": True},
            {"path": c2, "start_s": 0, "take_s": 1.5, "layout": "pillarbox", "has_audio": False}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o3.mp4"), CFG, total_s=3.0)
    assert os.path.getsize(out) > 1000


# ---------------------------------------------------------------------------
# Fixtures and probes that can see the picture and hear the mix.
# ---------------------------------------------------------------------------

RED, GREEN, BLUE, MAGENTA = (220, 20, 20), (20, 200, 20), (20, 20, 220), (255, 0, 255)
PALETTE = {"red": RED, "green": GREEN, "blue": BLUE, "magenta": MAGENTA}


def _png_clip(png, path, secs, audio_hz=300, rate=30):
    """A still PNG as a clip, so the picture is exactly known at every instant."""
    cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(rate), "-i", png]
    if audio_hz:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={audio_hz}:duration={secs}",
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(rate),
            "-t", str(secs), path]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


def _solid_png(path, w, h, rgb):
    Image.new("RGB", (w, h), rgb).save(path)
    return path


def _stripes_png(path, w, h):
    """Three wide vertical stripes: red | green | blue."""
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w // 3, h], fill=RED)
    d.rectangle([w // 3, 0, 2 * w // 3, h], fill=GREEN)
    d.rectangle([2 * w // 3, 0, w, h], fill=BLUE)
    im.save(path)
    return path


def _fine_stripes_png(path, w, h, period=8):
    """Alternating 8px black/white columns -- high spatial frequency everywhere,
    so it survives a crop-to-fill and a blur has something to destroy."""
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for x in range(0, w, period):
        d.rectangle([x, 0, x + period // 2, h],
                    fill=(255, 255, 255) if (x // period) % 2 == 0 else (0, 0, 0))
    im.save(path)
    return path


def _timed_clip(tmp_path, path, w, h):
    """A 3s clip that is red on [0,1), green on [1,2), blue on [2,3)."""
    names = []
    for i, rgb in enumerate((RED, GREEN, BLUE)):
        names.append(_solid_png(str(tmp_path / f"t{i}.png"), w, h, rgb))
    manifest = str(tmp_path / "timed.ffconcat")
    lines = ["ffconcat version 1.0"]
    for n in names:
        lines += [f"file '{n}'", "duration 1.000000"]
    lines.append(f"file '{names[-1]}'")
    with open(manifest, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", manifest,
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
                    "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-r", "30", "-t", "3", path],
                   capture_output=True, check=True)
    return path


def _alpha_box(path, w, h, secs, rgb=MAGENTA):
    """An alpha track that is opaque `rgb` over the middle half and transparent
    everywhere else -- so one frame proves both that the overlay ran AND that its
    alpha channel was honoured."""
    png = os.path.splitext(path)[0] + "_box.png"
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([w // 4, h // 4, 3 * w // 4, 3 * h // 4],
                                 fill=rgb + (255,))
    im.save(png)
    subprocess.run(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", png,
                    "-vf", "format=rgba", "-c:v", "qtrle", "-r", "30",
                    "-t", str(secs), path], capture_output=True, check=True)
    return path


def _alpha_caption(path, w, h, secs, text="MOMENT"):
    """An alpha track carrying REAL caption ink: white glyphs with a black stroke at
    57% frame height, transparent everywhere else.

    `_alpha` (transparent everywhere) and `_alpha_box` (one hard-edged rectangle) both
    leave the caption band free of the thing captions are made of -- antialiased glyph
    edges. Any assertion about what happens to caption pixels needs those edges to
    exist or it holds for the wrong reason."""
    png = os.path.splitext(path)[0] + "_cap.png"
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).text(
        (w * 0.5, h * 0.57), text,
        font=ImageFont.truetype(CAPTION_FONT, max(int(h * 0.145), 12)),
        anchor="mm", fill=(255, 255, 255, 255), stroke_width=8,
        stroke_fill=(0, 0, 0, 255))
    im.save(png)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", "30",
                    "-i", png, "-vf", "format=rgba", "-c:v", "qtrle", "-r", "30",
                    "-t", str(secs), path], capture_output=True, check=True)
    return path


def _composite_png(segs, cap, narr, cfg, out, watermark_png=None):
    """The composited frame BEFORE libx264, straight out of `[vout]` as a PNG.

    Same inputs in the same order as `compose.render` builds them, and the graph comes
    from `compose.build_filtergraph`, so what this measures is the graph. The encoder
    is deliberately not in the path: h264 rate control responds to any change in the
    picture across the whole frame (MEASURED at 1080x1920 crf 20, adding a 179x53
    watermark moved 29009 caption-band pixels by up to 23 counts), which swamps every
    filter-level difference and makes an exact comparison impossible downstream of it.
    """
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", segs[0]["path"]]
    for seg in segs[1:]:
        cmd += ["-i", seg["path"]]
    cmd += ["-f", "lavfi", "-t", "6.0",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-i", cap, "-i", narr]
    wm_idx = None
    if watermark_png:
        cmd += ["-i", watermark_png]
        wm_idx = len(segs) + 3
    g = compose.build_filtergraph(segs, len(segs), cfg, len(segs), len(segs) + 1,
                                  len(segs) + 2, 0.0, watermark_idx=wm_idx)
    cmd += ["-filter_complex", g, "-map", "[vout]", "-frames:v", "1", out,
            "-map", "[aout]", "-f", "null", "-"]
    subprocess.run(cmd, capture_output=True, check=True)
    return Image.open(out).convert("RGB")


def _diff_pixels(a, b, rect=(0, 0, 0, 0)):
    """Coordinates where two same-size frames differ, split by whether they fall
    inside `rect` (x0, y0, x1, y1) or outside it."""
    x0, y0, x1, y1 = rect
    inside, outside = [], []
    for y in range(a.height):
        for x in range(a.width):
            if a.getpixel((x, y)) != b.getpixel((x, y)):
                (inside if (x0 <= x < x1 and y0 <= y < y1) else outside).append((x, y))
    return inside, outside


def _tone_mp3(path, secs, hz):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={hz}:duration={secs}", "-c:a", "libmp3lame",
                    path], capture_output=True, check=True)
    return path


def _frame(video, t, out):
    """One decoded frame. `-ss` AFTER `-i` so the seek is accurate rather than
    keyframe-snapped."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-ss", str(t),
                    "-frames:v", "1", out], capture_output=True, check=True)
    return Image.open(out).convert("RGB")


def _nearest(rgb):
    """Classify a decoded pixel against the palette. yuv420p round-tripping moves
    every channel by a few counts (measured: magenta 255,0,255 decodes as
    253,0,252), so nearest-of-four is the comparison, not equality."""
    return min(PALETTE, key=lambda k: sum((a - b) ** 2
                                          for a, b in zip(rgb, PALETTE[k])))


def _mad(img, y0, y1, step=4):
    """Mean absolute difference between horizontally adjacent pixels in a band.
    High for sharp detail, ~0 for a heavy blur."""
    tot = n = 0
    for y in range(y0, y1, step):
        row = [img.getpixel((x, y))[0] for x in range(img.width)]
        for a, b in zip(row, row[1:]):
            tot += abs(a - b)
            n += 1
    return tot / max(n, 1)


def _lufs(path):
    r = subprocess.run(["ffmpeg", "-nostats", "-i", path, "-map", "0:a",
                        "-filter:a", "ebur128=framelog=quiet", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", r.stderr)
    assert m, f"ebur128 reported no integrated loudness for {path}: {r.stderr[-400:]}"
    return float(m[-1])


def _band_db(path, af):
    r = subprocess.run(["ffmpeg", "-nostats", "-i", path, "-map", "0:a",
                        "-af", af + ",volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
    assert m, f"volumedetect reported no mean_volume for {path}: {r.stderr[-400:]}"
    return float(m.group(1))


def _nb_frames(path):
    d = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, check=True).stdout)
    return int(next(s for s in d["streams"] if s["codec_type"] == "video")["nb_frames"])


# ---------------------------------------------------------------------------
# The picture
# ---------------------------------------------------------------------------

def test_the_alpha_fixture_is_actually_transparent(tmp_path):
    """Guards the fixture the other picture tests stand on. The brief's spelling of
    this produced alpha 255 everywhere, which made a composited frame and an
    uncomposited one look identical to every assertion in the file."""
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 2)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", cap, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         capture_output=True, check=True).stdout
    alpha = raw[3::4]
    assert max(alpha) == 0, f"alpha fixture is not transparent: max alpha {max(alpha)}"


def test_caption_overlay_composites_and_honours_alpha(tmp_path):
    """Kills: caption overlay deleted (`[bgv]format=yuv420p[vout]`), and alpha
    silently dropped. Centre must be the caption's ink, edge must still be the
    footage underneath."""
    png = _solid_png(str(tmp_path / "g.png"), 360, 640, GREEN)
    clip = _png_clip(png, str(tmp_path / "g.mp4"), 3)
    cap = _alpha_box(str(tmp_path / "c.mov"), 360, 640, 3)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    img = _frame(out, 1.0, str(tmp_path / "f.png"))
    centre = img.getpixel((180, 320))
    edge = img.getpixel((8, 320))
    assert _nearest(centre) == "magenta", f"caption did not composite: centre {centre}"
    assert _nearest(edge) == "green", f"alpha not honoured: edge {edge}"


def test_fill_layout_crops_to_fill_instead_of_squashing(tmp_path):
    """Kills: `force_original_aspect_ratio=increase` dropped. A red|green|blue
    landscape source cropped to fill a portrait canvas shows only the middle
    stripe; squashed to fit, it shows all three."""
    png = _stripes_png(str(tmp_path / "s.png"), 640, 360)
    clip = _png_clip(png, str(tmp_path / "s.mp4"), 3)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 3)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    img = _frame(out, 1.0, str(tmp_path / "f.png"))
    seen = [_nearest(img.getpixel((x, 320))) for x in (8, 180, 351)]
    assert seen == ["green", "green", "green"], (
        f"aspect was squashed rather than cropped to fill: sampled {seen}")


def test_pillarbox_background_is_actually_blurred(tmp_path):
    """Kills: `gblur=sigma=24` neutered to `sigma=0`. The pillarbox bands are a
    scaled-up crop of the source, so with fine stripes in the source they carry
    high-frequency detail unless something blurs them."""
    png = _fine_stripes_png(str(tmp_path / "f.png"), 640, 360)
    clip = _png_clip(png, str(tmp_path / "f.mp4"), 3)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 3)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "pillarbox",
             "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    img = _frame(out, 1.0, str(tmp_path / "fr.png"))
    top = _mad(img, 40, 170)          # background band only
    mid = _mad(img, 300, 340)         # the sharp foreground sits here
    assert top < 4.0, f"pillarbox background is not blurred: band MAD {top:.2f}"
    assert mid > 20.0, (
        f"the sharp foreground is missing, so the blur test proves nothing: "
        f"centre MAD {mid:.2f}")


def test_start_s_selects_the_requested_window(tmp_path):
    """Kills: `start_s` ignored (`s = 0.0`), which throws away all of Task 4's
    window selection. The source is red/green/blue by second."""
    clip = _timed_clip(tmp_path, str(tmp_path / "t.mp4"), 360, 640)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 2)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 1, 3000)
    segs = [{"path": clip, "start_s": 2.0, "take_s": 1.0, "layout": "fill",
             "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=1.0)
    seen = [_nearest(_frame(out, t, str(tmp_path / f"f{t}.png")).getpixel((180, 320)))
            for t in (0.2, 0.8)]
    assert seen == ["blue", "blue"], (
        f"start_s=2.0 did not select the third second of the source: saw {seen}")


def test_segments_concatenate_in_order(tmp_path):
    """Kills: segment order reversed before `concat`."""
    a = _png_clip(_solid_png(str(tmp_path / "a.png"), 360, 640, RED),
                  str(tmp_path / "a.mp4"), 2)
    b = _png_clip(_solid_png(str(tmp_path / "b.png"), 360, 640, BLUE),
                  str(tmp_path / "b.mp4"), 2)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 3)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": a, "start_s": 0, "take_s": 1.5, "layout": "fill", "has_audio": True},
            {"path": b, "start_s": 0, "take_s": 1.5, "layout": "fill", "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=3.0)
    first = _nearest(_frame(out, 0.5, str(tmp_path / "f1.png")).getpixel((180, 320)))
    second = _nearest(_frame(out, 2.2, str(tmp_path / "f2.png")).getpixel((180, 320)))
    assert (first, second) == ("red", "blue"), (
        f"segments played out of order: {first} then {second}")


def _vfr_clip(tmp_path, path, rgb, secs=2.0):
    """A genuinely variable-frame-rate clip: stills held for irregular durations and
    muxed in passthrough mode, so the PTS are not on any grid. Measures
    r_frame_rate 25/1 against avg_frame_rate 100/23 over 8 frames."""
    png = _solid_png(str(tmp_path / "vfr.png"), 360, 640, rgb)
    lines = ["ffconcat version 1.0"]
    for dur in (0.31, 0.03, 0.62, 0.05, 0.44, 0.09, 0.33, 0.13):
        lines += [f"file '{png}'", f"duration {dur}"]
    lines.append(f"file '{png}'")
    manifest = str(tmp_path / "vfr.ffconcat")
    with open(manifest, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", manifest, "-f", "lavfi",
                    "-i", f"sine=frequency=300:duration={secs}", "-c:a", "aac",
                    "-fps_mode", "passthrough", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-t", str(secs), path],
                   capture_output=True, check=True)
    return path


def _join_frame(video, tmp_path):
    """Index of the first frame whose centre pixel is blue rather than red."""
    outdir = tmp_path / "join"
    outdir.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-fps_mode",
                    "passthrough", str(outdir / "f_%03d.png")],
                   capture_output=True, check=True)
    files = sorted(outdir.iterdir())
    idx = -1
    for i, f in enumerate(files):
        px = Image.open(f).convert("RGB").getpixel((180, 320))
        if _nearest(px) == "blue":
            idx = i
            break
    for f in files:
        f.unlink()
    return idx, len(files)


def test_a_variable_frame_rate_source_is_normalised_before_concat(tmp_path):
    """Kills: `fps={FPS}` dropped from the per-segment chain.

    `-r 30` on the output makes the per-segment `fps` filter look redundant, and for
    constant-frame-rate sources it IS -- MEASURED, baseline and the fps-less mutant
    are frame-identical for 24+30, 15+30, 12+60 and 25+30 fps joins. The difference
    only appears on a VARIABLE frame rate source, which is what a screen recorder
    produces: with the filter the 1.5s join lands on frame 47, without it on frame
    56, a 0.300s / 9-frame slip. Deterministic across three runs.
    """
    vfr = _vfr_clip(tmp_path, str(tmp_path / "a.mp4"), RED)
    blue = _png_clip(_solid_png(str(tmp_path / "b.png"), 360, 640, BLUE),
                     str(tmp_path / "b.mp4"), 2)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 3.5)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 3, 3000)
    segs = [{"path": vfr, "start_s": 0, "take_s": 1.5, "layout": "fill", "has_audio": True},
            {"path": blue, "start_s": 0, "take_s": 1.5, "layout": "fill", "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=3.0)
    assert _nb_frames(out) == 90, f"3.0s at 30fps is 90 frames, got {_nb_frames(out)}"
    idx, total = _join_frame(out, tmp_path)
    assert total == 90, f"decoded {total} frames, not 90"
    # 45 is the 1.5s join. The VFR source's own frames do not land on the 30fps
    # grid, so a couple of frames of offset is inherent; 9 is the mutant.
    assert abs(idx - 45) <= 4, (
        f"the variable-rate segment was not normalised to {CFG['fps']}fps before "
        f"concat: the join landed on frame {idx} ({idx / 30.0:.3f}s), not 45 (1.500s)")


def test_graph_pins_the_selected_window(tmp_path):
    """The cheap unit-level half of the two tests above: the numbers Task 4 chose
    must reach the trim, not half of them and not zero."""
    segs = [{"path": "/a.mp4", "start_s": 2.5, "take_s": 3.25, "layout": "fill",
             "has_audio": True}]
    g = compose.build_filtergraph(segs, 1, CFG, 1, 2, 3, 0.0)
    assert "trim=duration=3.25" in g
    assert "atrim=duration=3.25" in g


def test_the_window_start_is_an_input_seek_not_a_filter():
    """Kills: `trim=start=` coming back, and the graph half being read alone.

    The window is split across two places -- `-ss` before `-i` (start) and
    `trim=duration=` (length) -- so both halves are asserted in one test. A
    `trim=start=` anywhere in the graph would double the seek on top of `-ss`, i.e.
    render 2x the offset; `-ss` missing would render from 0.000s of every clip.
    """
    segs = [{"path": "/a.mp4", "start_s": 2.5, "take_s": 3.25, "layout": "fill",
             "has_audio": True},
            {"path": "/b.mp4", "start_s": 1515.0, "take_s": 4.0,
             "layout": "pillarbox", "has_audio": False}]
    assert compose.build_input_args(segs) == [
        "-ss", "2.500000", "-i", "/a.mp4",
        "-ss", "1515.000000", "-i", "/b.mp4"]
    g = compose.build_filtergraph(segs, 2, CFG, 2, 3, 4, 0.0)
    assert "trim=start=" not in g and "atrim=start=" not in g, g
    for d in ("trim=duration=3.25", "trim=duration=4.0"):
        assert d in g, f"{d} missing from {g}"


def _gop_clip(tmp_path, path, w, h, secs=40, gop=300):
    """A `secs`-long clip whose colour changes every second, keyframed only every
    `gop` frames, and the keyframe spacing is MEASURED rather than requested.

    The spacing is the whole point: with keyframes 10s apart, a seek that snaps to
    the preceding keyframe lands on a DIFFERENT colour than an accurate one, so the
    fixture can tell the two apart. x264 inserts a keyframe at every scene cut by
    default, which -- with a hard colour change every second -- would make every
    second its own keyframe and the distinction untestable, hence `scenecut=0`.
    """
    order = ["red", "green", "blue", "magenta"]
    pngs = [_solid_png(str(tmp_path / f"g{n}.png"), w, h, PALETTE[n]) for n in order]
    manifest = str(tmp_path / "gop.ffconcat")
    lines = ["ffconcat version 1.0"]
    for i in range(secs):
        lines += [f"file '{pngs[i % 4]}'", "duration 1.000000"]
    lines.append(f"file '{pngs[(secs - 1) % 4]}'")
    with open(manifest, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", manifest,
                    "-f", "lavfi", "-i", f"sine=frequency=300:duration={secs}",
                    "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-r", "30", "-g", str(gop),
                    "-x264-params", f"scenecut=0:keyint={gop}:min-keyint={gop}",
                    "-t", str(secs), path], capture_output=True, check=True)
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-skip_frame", "nokey", "-show_entries",
                        "frame=pts_time", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    keys = sorted(float(x.rstrip(",")) for x in r.stdout.split() if x.strip(", \n"))
    assert len(keys) >= 2 and min(b - a for a, b in zip(keys, keys[1:])) >= 5.0, (
        f"fixture is useless: keyframes MEASURED at {keys}, so a keyframe-snapped "
        f"seek would land on the right colour by accident")
    return path, keys, order


def test_a_deep_window_is_seeked_accurately_not_keyframe_snapped(tmp_path):
    """Kills: `-ss` without accurate seek (or `-noaccurate_seek`), which silently
    renders from the preceding keyframe.

    This is the test that makes input seeking safe to prefer over `trim=start=`.
    `test_start_s_selects_the_requested_window` cannot see the difference: its
    source changes colour every second and x264 keyframes every scene cut, so every
    second of it is its own keyframe. Here the keyframes are 10s apart (asserted in
    the fixture), and the window starts at 27.0s -- 7s past the keyframe at 20.0s,
    which carries a different colour.
    """
    clip, keys, order = _gop_clip(tmp_path, str(tmp_path / "g.mp4"), 360, 640)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 2)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 27.0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    seen = [_nearest(_frame(out, t, str(tmp_path / f"d{t}.png")).getpixel((180, 320)))
            for t in (0.2, 1.2)]
    want = [order[27 % 4], order[28 % 4]]
    snapped = order[int(max(k for k in keys if k <= 27.0)) % 4]
    assert seen == want, (
        f"a 27.0s window rendered {seen}, wanted {want}; the preceding keyframe at "
        f"{max(k for k in keys if k <= 27.0)}s is {snapped!r}")


# ---------------------------------------------------------------------------
# The mix
# ---------------------------------------------------------------------------

def _mix_fixture(tmp_path, duck, loudnorm_i, total_s=3.0):
    """Gameplay audio at 300Hz, narration at 3000Hz, so the two are separable in
    the finished mp4 by a pair of band filters."""
    png = _solid_png(str(tmp_path / "g.png"), 360, 640, GREEN)
    clip = _png_clip(png, str(tmp_path / "g.mp4"), total_s + 1, audio_hz=300)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, total_s)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), total_s, 3000)
    cfg = dict(CFG, game_audio_duck=duck, loudnorm_i=loudnorm_i)
    segs = [{"path": clip, "start_s": 0, "take_s": total_s, "layout": "fill",
             "has_audio": True}]
    return compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), cfg,
                          total_s=total_s)


def test_gameplay_audio_is_ducked_to_the_configured_level(tmp_path):
    """Kills: `volume={duck}` replaced by `volume=1.0`, i.e. no ducking at all.

    MEASURED, 300Hz gameplay under 3000Hz narration, band levels out of the
    finished mp4: duck=0.05 -> -23.00 dB, 0.12 -> -16.90, 0.30 -> -9.40,
    0.60 -> -3.40, 1.00 -> +1.00. Every one tracks 20*log10(duck) to within
    +1.0..+3.0 dB, so a 5 dB window round the theoretical figure holds across the
    whole range while the no-ducking case misses it by 19.4 dB. loudnorm applies
    one gain to the whole mix, so it cannot move the RATIO between the two bands.
    """
    out = _mix_fixture(tmp_path, duck=CFG["game_audio_duck"],
                       loudnorm_i=CFG["loudnorm_i"])
    game = _band_db(out, "lowpass=f=600")
    narration = _band_db(out, "highpass=f=2000")
    assert narration > -40.0, f"narration is not in the mix: {narration:.2f} dB"
    theory = 20 * math.log10(CFG["game_audio_duck"])
    assert abs((game - narration) - theory) < 5.0, (
        f"gameplay sits {game - narration:+.2f} dB under the narration; "
        f"duck={CFG['game_audio_duck']} means {theory:+.2f} dB")


def test_mix_is_normalised_to_the_configured_loudness(tmp_path):
    """Kills: `loudnorm=I=...` replaced by `anull`, and `loudnorm_i` hardcoded.

    MEASURED integrated loudness of the finished mp4: loudnorm_i=-14 -> -14.1
    LUFS, -20 -> -20.1, -9 -> -9.0. Dropping loudnorm entirely gave -18.4 against
    a -14 target, 4.4 dB outside this tolerance. Single-pass loudnorm is dynamic
    mode, so it approaches the target rather than hitting it exactly -- 1.0 LUFS
    is the band, not a claim of exactness.
    """
    for want in (CFG["loudnorm_i"], -20):
        out = _mix_fixture(tmp_path, duck=CFG["game_audio_duck"], loudnorm_i=want)
        got = _lufs(out)
        assert abs(got - want) <= 1.0, (
            f"loudnorm_i={want} produced {got} LUFS")


# ---------------------------------------------------------------------------
# The module measures what it ships
# ---------------------------------------------------------------------------

def test_footage_short_of_total_s_is_rejected(tmp_path):
    """2.0s of footage under a 6.0s narration.

    The caption track here is deliberately 6.0s -- long enough -- because that is
    the real path's shape (`build_timeline` guarantees the track spans `total_s`)
    and it is the shape in which no fact about the output file shows the defect.
    MEASURED with a 6.0s track and no coverage check: exit 0, nb_frames=180, video
    stream 6.000000s, and the decoded frame at 3.0s identical to the frame at 5.0s.
    A 4.0s frozen tail behind a perfect frame count."""
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 2.5, audio=True)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 6)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 6, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=6.0)
    msg = str(e.value)
    assert "cover 2.000s" in msg and "6.000s" in msg, msg
    assert "freeze" in msg, msg


def test_a_window_running_past_its_source_clip_is_rejected(tmp_path):
    """The manifest is not taken on trust: a window that overruns the clip's
    measured picture would render its tail as a held frame."""
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 2, audio=True)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 3)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 3, 3000)
    segs = [{"path": clip, "start_s": 1.0, "take_s": 3.0, "layout": "fill",
             "has_audio": True}]
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=3.0)
    assert "MEASURES" in str(e.value) and "1.000..4.000s" in str(e.value)


def _skewed_clip(path, secs_video, secs_audio):
    """A clip whose audio outruns its picture, which is what a screen recording does.
    MEASURED on 3.0s video / 3.2s audio: video stream duration 3.000000 (nb_frames
    90), audio stream 3.200000, format.duration 3.200000."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc=size=360x640:rate=30:duration={secs_video}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={secs_audio}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path],
        capture_output=True, check=True)
    return path


def test_a_window_probed_off_an_av_skewed_clip_is_one_check_footage_accepts(tmp_path):
    """The contract between task 3/4 and this module, end to end and in one place.

    `footage.probe_clip` decides what windows exist and `check_footage` decides which
    are allowed; when they measured different fields, every run on ordinary screen-
    recorded footage aborted. MEASURED before the fix, on a clip with 3.000s of video
    and 3.200s of audio: probe_clip duration_s=3.2 (it recorded format.duration),
    select_window take_s=3.2, and render raised "footage segment 0 asks for
    0.000..3.200s ... but its picture MEASURES 3.000s" -- tolerance 1/30+1e-3 =
    0.0343s, so 0.2s of a/v skew was fatal.

    Both sides now resolve it through `footage.picture_duration_s`, so this walks the
    real path: probe -> select -> check -> render."""
    clip = _skewed_clip(str(tmp_path / "skew.mp4"), 3.0, 3.2)
    info = footage.probe_clip(clip)
    assert info["duration_s"] == pytest.approx(3.0, abs=0.05), (
        f"the probe recorded {info['duration_s']}s of picture for a clip carrying "
        f"3.0s of it")
    assert info["container_duration_s"] == pytest.approx(3.2, abs=0.05)

    sel = footage.select_window({"clips": [info]}, 3.0, "run-skew")
    got = compose.check_footage(sel["segments"], sel["total_s"], CFG["fps"])
    assert got["covered_s"] == pytest.approx(3.0, abs=0.05)

    cap = _alpha_caption(str(tmp_path / "c.mov"), 360, 640, 3.0)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    out = compose.render(sel["segments"], cap, narr, str(tmp_path / "o.mp4"), CFG,
                         total_s=sel["total_s"])
    assert _nb_frames(out) == 90, f"3.0s at 30fps is 90 frames, got {_nb_frames(out)}"


def test_the_container_duration_is_still_refused_if_something_hands_it_over(tmp_path):
    """The positive control for the test above: the check did not become lenient. A
    window built off the CONTAINER duration of the same clip -- what a manifest from
    the old probe holds -- is still rejected, because those 0.2s really would be a
    frozen frame."""
    clip = _skewed_clip(str(tmp_path / "skew.mp4"), 3.0, 3.2)
    segs = [{"path": clip, "start_s": 0.0, "take_s": 3.2, "layout": "fill",
             "has_audio": True}]
    with pytest.raises(compose.ComposeError) as e:
        compose.check_footage(segs, 3.2, CFG["fps"])
    assert "MEASURES 3.000s" in str(e.value), str(e.value)


def test_the_probe_and_the_compositor_pick_the_same_video_stream(monkeypatch,
                                                                 tmp_path):
    """`picture_duration_s` promises the probe and the compositor "cannot disagree".
    That covered the FIELD they read, not the STREAM they read it off: this module
    took `next(s for s in streams if codec_type == 'video')` while `probe_clip` skips
    `disposition.attached_pic`, so on a container whose cover art lands at index 0 one
    measured the still and the other the footage.

    Unreachable is not the same as absent -- no locally available muxer writes that
    ordering (mp4/mov sort attached pics last, this mkv build drops the disposition) --
    so ffprobe's output is stubbed to the exact shape, the way
    `test_probe_ignores_leading_attached_pic_cover_art` already does in
    tests/test_footage_probe.py. Both callers now come through
    `footage.pick_video_stream`."""
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "width": 200,
         "height": 200, "r_frame_rate": "25/1", "duration": "9.000000",
         "pix_fmt": "yuvj420p", "disposition": {"attached_pic": 1}},
        {"index": 1, "codec_type": "video", "codec_name": "h264", "width": 640,
         "height": 360, "r_frame_rate": "30/1", "duration": "3.000000",
         "pix_fmt": "yuv420p", "disposition": {"attached_pic": 0}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac"},
    ]
    fake = json.dumps({"streams": streams, "format": {"duration": "3.200000"}})
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            args=cmd, returncode=0, stdout=fake, stderr=""))
    path = str(tmp_path / "cover_first.mkv")
    open(path, "w").close()          # never read; ffprobe itself is stubbed

    probed = footage.probe_clip(path)
    video, _audio, fmt = compose._probe(path, "footage segment 0")
    assert (int(video["width"]), int(video["height"])) == (640, 360), (
        f"the compositor measured stream {video.get('index')} "
        f"({video.get('width')}x{video.get('height')}), the cover art")
    assert (probed["width"], probed["height"]) == (640, 360)
    assert footage.picture_duration_s(video, fmt) == probed["duration_s"] == 3.0

    # Not vacuous: the two streams disagree by 6.0s, so picking the wrong one is
    # visible in the number rather than only in the index.
    first_video = next(s for s in streams if s["codec_type"] == "video")
    assert (first_video["width"], footage.picture_duration_s(first_video, fmt)) == \
        (200, 9.0), "the fixture no longer distinguishes the two streams"


def test_a_clip_with_trailing_cover_art_is_measured_off_its_footage(tmp_path):
    """The same agreement on a REAL container, which is the shape that exists: the
    mp4/mov family sorts attached pics after the regular streams, so the cover art is
    a trailing 200x200 png beside 3.0s of 640x360 footage. `check_footage` must accept
    a window inside the footage's duration and measure the footage's dimensions, not
    the still's."""
    main = str(tmp_path / "main.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=640x360:rate=30:duration=3", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-t", "3", main],
                   capture_output=True, check=True)
    cover = _solid_png(str(tmp_path / "cover.png"), 200, 200, RED)
    clip = str(tmp_path / "with_cover.m4v")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", main, "-i", cover,
                    "-map", "0", "-map", "1", "-c:v:0", "copy", "-c:v:1", "png",
                    "-disposition:v:1", "attached_pic", clip],
                   capture_output=True, check=True)
    probed = footage.probe_clip(clip)
    video, _audio, fmt = compose._probe(clip, "footage segment 0")
    assert (int(video["width"]), int(video["height"])) == (probed["width"],
                                                           probed["height"])
    assert (int(video["width"]), int(video["height"])) == (640, 360), (
        f"measured the cover art: {video.get('width')}x{video.get('height')}")
    segs = [{"path": clip, "start_s": 0.0, "take_s": 2.0, "layout": "pillarbox",
             "has_audio": False}]
    assert compose.check_footage(segs, 2.0, CFG["fps"])["covered_s"] == 2.0


@pytest.mark.parametrize("w,h", [(100, 100), (720, 1280)])
def test_caption_track_that_does_not_match_the_canvas_is_rejected(tmp_path, w, h):
    """`overlay=0:0` neither scales nor centres. MEASURED before the check: a
    100x100 track over a 360x640 canvas rendered 360x640 with the captions
    confined to the top-left 100x100, exit 0."""
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 3, audio=True)
    cap = str(tmp_path / "c.mov"); _alpha(cap, w, h, 3)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    assert f"{w}x{h}" in str(e.value) and "360x640" in str(e.value)


def test_caption_track_shorter_than_the_video_is_rejected(tmp_path):
    """`overlay` defaults to eof_action=repeat, so a short track freezes its last
    caption on screen for the remainder instead of ending."""
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 4, audio=True)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 1)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 3, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 3.0, "layout": "fill",
             "has_audio": True}]
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=3.0)
    assert "frozen on" in str(e.value)


def test_an_opaque_caption_track_is_rejected(tmp_path):
    """The failure the pix_fmt check cannot see: a track that probes as `argb` but
    carries alpha 255 in every pixel composites as an opaque rectangle over the
    entire canvas, i.e. a 100% black video that still probes as a correct mp4.
    This is exactly what the brief's own fixture produced."""
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 3, audio=True)
    cap = str(tmp_path / "c.mov")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "color=c=black@0.0:s=360x640:r=30:d=3",
                    "-vf", "format=rgba", "-c:v", "qtrle", cap],
                   capture_output=True, check=True)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    assert "fully opaque" in str(e.value)


def test_encode_failure_message_carries_the_error_lines(tmp_path):
    """A no-audio gameplay clip plus a digitally silent narration makes loudnorm
    emit NaN and the AAC encoder reject the frame, rc=234. MEASURED: 7158 bytes of
    stderr with the root cause 1696 bytes from the end, so the 800-byte tail the
    module used to report contained only libx264 macroblock statistics and
    `Conversion failed!` -- neither "Error" nor "NaN"."""
    clip = str(tmp_path / "s.mp4"); _clip(clip, 360, 640, 3, audio=False)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 2)
    narr = str(tmp_path / "n.mp3")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-t", "2", "-c:a", "libmp3lame", narr],
                   capture_output=True, check=True)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": False}]
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    msg = str(e.value)
    assert "rc=234" in msg, msg
    assert "NaN" in msg, f"the root cause is missing from the message:\n{msg}"
    assert "Error" in msg, msg
    assert "macroblock" not in msg and "consecutive B-frames" not in msg


def test_a_graph_binding_failure_still_reports(tmp_path):
    """The failure class that DID survive the old tail slice must survive the new
    selector too."""
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 3, audio=False)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 2)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    # has_audio lies: the clip has no audio stream, so [0:a] cannot bind.
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    assert "no error-level line" not in str(e.value)
    assert "matches no streams" in str(e.value) or "Error" in str(e.value)


def _out_of_spec(path, w, h, secs, audio=True):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
           f"testsrc=size={w}x{h}:rate=30:duration={secs}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={secs}",
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-t", str(secs),
            path]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


def test_check_output_accepts_an_in_spec_artifact(tmp_path):
    """The positive control for the three rejections below -- without it they would
    also pass if `check_output` raised unconditionally."""
    p = _out_of_spec(str(tmp_path / "ok.mp4"), 360, 640, 2.0)
    got = compose.check_output(p, CFG, 2.0)
    assert got["width"] == 360 and got["height"] == 640
    assert got["pix_fmt"] == "yuv420p" and got["audio_codec"] == "aac"
    assert got["frames"] == got["expected_frames"] == 60


def test_check_output_rejects_the_wrong_canvas(tmp_path):
    p = _out_of_spec(str(tmp_path / "big.mp4"), 720, 1280, 2.0)
    with pytest.raises(compose.ComposeError) as e:
        compose.check_output(p, CFG, 2.0)
    assert "720x1280" in str(e.value) and "360x640" in str(e.value)


def test_check_output_rejects_a_missing_audio_stream(tmp_path):
    p = _out_of_spec(str(tmp_path / "mute.mp4"), 360, 640, 2.0, audio=False)
    with pytest.raises(compose.ComposeError) as e:
        compose.check_output(p, CFG, 2.0)
    assert "no audio stream" in str(e.value)


def test_render_measures_the_artifact_before_returning_its_path(tmp_path,
                                                                monkeypatch):
    """Pins the WIRING, which has no observable consequence on the happy path and
    so cannot be caught behaviourally: with the footage and caption preconditions
    satisfied, the encode always does produce an in-spec file, and deleting the
    output probe changes nothing about any artifact this suite can build. A spy is
    therefore the only thing that distinguishes "measured it" from "returned the
    path having measured nothing", which is the defect being fixed."""
    seen = []
    real = compose.check_output

    def spy(path, cfg, total_s):
        seen.append((path, total_s))
        return real(path, cfg, total_s)

    monkeypatch.setattr(compose, "check_output", spy)
    clip = str(tmp_path / "g.mp4"); _clip(clip, 360, 640, 3, audio=True)
    cap = str(tmp_path / "c.mov"); _alpha(cap, 360, 640, 2)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    out = compose.render(segs, cap, narr, str(tmp_path / "o.mp4"), CFG, total_s=2.0)
    assert seen == [(out, 2.0)], (
        "render returned a path without probing the file it points at")


def test_check_output_rejects_a_picture_shorter_than_total_s(tmp_path):
    """The complementary half of `check_footage`: whatever the reason, if the file
    on disk carries less picture than was asked for, the caller hears about it."""
    p = _out_of_spec(str(tmp_path / "short.mp4"), 360, 640, 1.0)
    with pytest.raises(compose.ComposeError) as e:
        compose.check_output(p, CFG, 3.0)
    assert "30 video frames" in str(e.value) and "90" in str(e.value)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------

def _wm_fixture(tmp_path, tag, text="MOMENT"):
    """Caption track carries real ink: see `_alpha_caption`. Every assertion below
    about caption pixels is void without it."""
    clip = _png_clip(_solid_png(str(tmp_path / "g.png"), 360, 640, GREEN),
                     str(tmp_path / "g.mp4"), 3)
    cap = _alpha_caption(str(tmp_path / "c.mov"), 360, 640, 3, text)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)
    segs = [{"path": clip, "start_s": 0, "take_s": 2.0, "layout": "fill",
             "has_audio": True}]
    return segs, cap, narr, str(tmp_path / f"{tag}.mp4")


def _caption_band_ink(img, thresh=200):
    """Pixels in the caption band bright enough on every channel to be caption ink."""
    return sum(1 for y in range(int(img.height * 0.52), int(img.height * 0.63))
               for x in range(img.width)
               if min(img.getpixel((x, y))) > thresh)


def test_a_configured_watermark_reaches_the_frame(tmp_path):
    """The design spec's video chain ends at the watermark. Before this, a
    configured watermark string had no consumer anywhere in the render path.

    The caption-band assertion here is a BOUND, not an equality, and the bound is the
    honest form: libx264 answers any change in the picture with a frame-wide change in
    its own output, so the caption band of the watermarked mp4 is never bit-identical
    to the plain one. MEASURED at 360x640 crf 28 with this fixture: 13989 caption-band
    pixels differ by up to 61 counts, while the number of pixels bright enough to BE
    caption ink moves by 4 in 6413 (0.06%). The first number is the encoder; the
    second is the thing the test is actually about, and a watermark landing on the
    captions would move it by thousands. Byte-equality is asserted where it is
    obtainable, on the composite before the encoder -- see
    `test_the_watermark_stage_composites_the_captions_byte_identically`."""
    segs, cap, narr, out_a = _wm_fixture(tmp_path, "plain")
    cfg = dict(CFG, watermark_height_frac=0.05)
    plain = compose.render(segs, cap, narr, out_a, cfg, total_s=2.0)
    img_plain = _frame(plain, 1.0, str(tmp_path / "fp.png"))
    marked = compose.render(segs, cap, narr, str(tmp_path / "marked.mp4"), cfg,
                            total_s=2.0, watermark_text="StoryLoop",
                            watermark_font=CAPTION_FONT,
                            work_dir=str(tmp_path / "work"))
    img_marked = _frame(marked, 1.0, str(tmp_path / "fm.png"))

    # The band the watermark is placed in: top of the safe box, right inset.
    y0, y1 = 40, 130
    changed = sum(1 for y in range(y0, y1) for x in range(180, 360)
                  if img_plain.getpixel((x, y)) != img_marked.getpixel((x, y)))
    assert changed > 200, f"no watermark ink in the top-right band ({changed} px changed)"

    # It cannot reach the caption band (55-60% of frame height) GEOMETRICALLY: the
    # ink's own measured height plus the safe box's top inset is the whole extent.
    box = compose._watermark_box(cfg, 360, 640)
    with Image.open(str(tmp_path / "work" / "watermark.png")) as wm:
        wm_w, wm_h = wm.size
    assert box["my"] + wm_h < int(640 * 0.52), (
        f"the watermark occupies y {box['my']}..{box['my'] + wm_h}, which reaches the "
        f"caption band starting at y {int(640 * 0.52)}")
    assert 0 <= 360 - wm_w - box["mx"], "the watermark is placed off the left of frame"

    # And the captions survive it: same amount of ink in the band, ±1%.
    before, after = _caption_band_ink(img_plain), _caption_band_ink(img_marked)
    assert before > 1000, (
        f"the fixture's caption track carries no ink ({before} px), so nothing below "
        f"is being tested")
    assert abs(after - before) <= before * 0.01, (
        f"caption ink in the band went from {before} to {after} px when the watermark "
        f"was added")


def test_an_empty_watermark_renders_nothing(tmp_path):
    """`channel.watermark()` returns "" for an unnamed channel and that must mean
    render nothing, not render a placeholder."""
    segs, cap, narr, out = _wm_fixture(tmp_path, "a")
    a = compose.render(segs, cap, narr, out, CFG, total_s=2.0)
    fa = _frame(a, 1.0, str(tmp_path / "fa.png"))
    b = compose.render(segs, cap, narr, str(tmp_path / "b.mp4"), CFG, total_s=2.0,
                       watermark_text="   ")
    fb = _frame(b, 1.0, str(tmp_path / "fb.png"))
    assert fa.tobytes() == fb.tobytes()
    assert "overlay=x=W-w-" not in compose.build_filtergraph(
        [{"path": "/a.mp4", "start_s": 0, "take_s": 2, "layout": "fill",
          "has_audio": True}], 1, CFG, 1, 2, 3, 0.0)


def test_a_configured_watermark_with_no_font_raises(tmp_path):
    """The failure this whole stage exists to prevent is a silent one: an operator
    does the documented setup step, the render succeeds, and the mp4 has no
    watermark. A non-empty watermark that cannot be drawn must not be discarded."""
    segs, cap, narr, out = _wm_fixture(tmp_path, "x")
    with pytest.raises(compose.ComposeError) as e:
        compose.render(segs, cap, narr, out, CFG, total_s=2.0,
                       watermark_text="StoryLoop")
    assert "needs a font" in str(e.value)


def test_the_watermark_path_emits_no_text_filter_either():
    """The brief's version of this test only builds the graph WITHOUT a watermark.
    The constraint is absolute, and the watermark is the one stage that is about
    text, so the branch that carries it gets the same assertion."""
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2, "layout": "pillarbox",
             "has_audio": True}]
    g = compose.build_filtergraph(segs, 1, CFG, 1, 2, 3, 0.0, watermark_idx=4)
    for banned in ("drawtext", "ass=", "subtitles", "libass"):
        assert banned not in g
    assert "[4:v]overlay=" in g, "the watermark input is not composited"


def test_watermark_png_is_small_and_low_opacity(tmp_path):
    p = compose.render_watermark_png(
        "StoryLoop", (1080, 1920), {"watermark_opacity": 0.35}, CAPTION_FONT,
        str(tmp_path / "wm.png"))
    im = Image.open(p).convert("RGBA")
    alpha = im.tobytes()[3::4]
    assert max(alpha) == round(255 * 0.35), (
        f"watermark ink is not at the configured opacity: max alpha {max(alpha)}")
    assert min(alpha) == 0, "watermark bed is not transparent"
    assert im.height <= int(1920 * 0.06), f"watermark is not small: {im.size}"


def test_watermark_opacity_of_zero_is_refused(tmp_path):
    with pytest.raises(compose.ComposeError) as e:
        compose.render_watermark_png("X", (360, 640), {"watermark_opacity": 0.0},
                                     CAPTION_FONT, str(tmp_path / "wm.png"))
    assert "silently is not there" in str(e.value)


def test_the_watermark_stage_composites_the_captions_byte_identically(tmp_path):
    """Adding the watermark stage must not change a caption pixel, and this is the
    level at which that can be asserted exactly.

    MEASURED on the lossless composite out of `[vout]`, 360x640, an argb caption track
    with real glyph edges over an h264 clip: 1326 pixels change and every one of them
    is inside the 67x27 watermark rect -- 0 outside it. Same result on the pillarbox
    path and at 1080x1920 (6886 changed, 0 outside).

    The finished mp4 cannot carry this assertion: MEASURED at 1080x1920 crf 20, the
    same pair of renders differs in 29009 caption-band pixels by up to 23 counts,
    because libx264's rate control and adaptive quantisation respond to the extra ink
    across the whole frame. That is the encoder, not the graph -- the two graph shapes
    were already producing identical composites outside the watermark box, with
    `format=auto` as well as with the pinned OVERLAY_FORMAT (both measured).

    What this test does NOT pin: the rect it excuses comes from `_watermark_box`, so
    moving the watermark on top of the captions moves the rect with it and this test
    still passes (verified -- mutation M21 survives here). Where the box may be is
    pinned against the caption band's own numbers in
    `test_a_configured_watermark_reaches_the_frame`, which kills M21."""
    segs, cap, narr, _ = _wm_fixture(tmp_path, "unused")
    cfg = dict(CFG, watermark_height_frac=0.05)
    wm = compose.render_watermark_png("StoryLoop", (360, 640), cfg, HOOK_FONT,
                                      str(tmp_path / "wm.png"))
    with Image.open(wm) as im:
        wm_w, wm_h = im.size
    box = compose._watermark_box(cfg, 360, 640)
    rect = (360 - wm_w - box["mx"], box["my"], 360 - box["mx"], box["my"] + wm_h)

    a = _composite_png(segs, cap, narr, cfg, str(tmp_path / "plain.png"))
    b = _composite_png(segs, cap, narr, cfg, str(tmp_path / "marked.png"),
                       watermark_png=wm)
    assert _caption_band_ink(a) > 1000, (
        "the caption track carries no ink, so 'the captions are untouched' would hold "
        "for a track that had no captions in it")
    inside, outside = _diff_pixels(a, b, rect)
    assert len(inside) > 200, (
        f"the watermark left no ink in its own rect {rect} ({len(inside)} px changed)")
    assert outside == [], (
        f"{len(outside)} composited pixels outside the watermark rect {rect} changed "
        f"when the watermark stage was added, first at {outside[:5]}")


def test_a_long_channel_name_is_fitted_to_the_safe_box_not_clipped(tmp_path):
    """The failure shape of the finding above, one layer down: the rasteriser used to
    refuse only ink wider than the whole CANVAS, while `build_filtergraph` places it
    at `x = W-w-mx`, so a name wider than the safe box got a negative x and `overlay`
    cut its left off with a zero exit code.

    MEASURED at 360x640, watermark_height_frac=0.05, Anton-Regular, a 22-character
    name: 342x36 ink, overlay x=-32, composited ink at x 0..305 -- 32px of the name
    gone. Fitted: 276x31 at x=34, ink at x 38..305, inside the safe box's 22..310."""
    cfg = dict(CFG, watermark_height_frac=0.05)
    box = compose._watermark_box(cfg, 360, 640)
    wm = compose.render_watermark_png("x" * 22, (360, 640), cfg, HOOK_FONT,
                                      str(tmp_path / "wm.png"))
    with Image.open(wm) as im:
        wm_w, wm_h = im.size
    x = 360 - wm_w - box["mx"]
    assert wm_w <= box["max_w"], (
        f"a 22-char name rasterised {wm_w}px wide into a {box['max_w']}px box")
    assert x >= box["lx"], f"overlay x={x} puts the name outside the safe box"

    segs, cap, narr, _ = _wm_fixture(tmp_path, "unused")
    a = _composite_png(segs, cap, narr, cfg, str(tmp_path / "plain.png"))
    b = _composite_png(segs, cap, narr, cfg, str(tmp_path / "marked.png"),
                       watermark_png=wm)
    _inside, outside = _diff_pixels(a, b)   # every changed pixel, no rect
    assert outside, "the long watermark left no ink at all"
    left, right = min(p[0] for p in outside), max(p[0] for p in outside)
    assert left >= box["lx"] and right <= 360 - box["mx"], (
        f"watermark ink spans x {left}..{right}, outside the safe box "
        f"{box['lx']}..{360 - box['mx']}; a clipped name reads as ink starting at x 0")


def test_a_name_too_long_for_the_box_even_at_the_size_floor_raises(tmp_path):
    """Shrinking has a floor, and below it the honest answer is a refusal rather than
    an unreadable or clipped watermark. MEASURED: 60 'x's at 360x640 still rasterise
    372px wide at the 12px floor against a 288px box."""
    with pytest.raises(compose.ComposeError) as e:
        compose.render_watermark_png("x" * 60, (360, 640),
                                     dict(CFG, watermark_height_frac=0.05),
                                     HOOK_FONT, str(tmp_path / "wm.png"))
    msg = str(e.value)
    assert "12px floor" in msg and "288x576" in msg, msg
    assert "clip" in msg, msg


def _overlay_stages(graph):
    """Every `overlay` filter instance in a filtergraph, options included.

    A filter instance runs to the next `,`, `;` or `[`, so this cannot miss one the
    way `"format=auto" not in graph` can: that assertion is satisfied by a stage
    spelling NO format option at all, which is how the pillarbox background/foreground
    composite sat unpinned underneath a test named for pinning them all."""
    return re.findall(r"overlay=[^,;\[]*", graph)


@pytest.mark.parametrize("layout,watermark,expected", [
    ("fill", False, 1),        # caption
    ("fill", True, 2),         # caption, watermark
    ("pillarbox", False, 2),   # pillarbox bg/fg, caption
    ("pillarbox", True, 3),    # pillarbox bg/fg, caption, watermark
])
def test_every_overlay_stage_in_the_graph_pins_its_working_format(
        layout, watermark, expected):
    """`format=auto` makes the colour space a composite happens in an outcome of
    ffmpeg's format negotiation, which reads the whole graph. MEASURED, the same
    caption track over the same clip: on the fill path negotiation chose rgb, on the
    PILLARBOX path yuv420 -- 1947122 of 2073600 pixels apart at 1080x1920. Same
    captions, different blend, decided by the footage's aspect ratio.

    This test used to be named for "both overlay stages" while the graph had THREE,
    and its `"format=auto" not in g` assertion passed for the third one because that
    stage named no format at all. So the stages are enumerated and counted here, and
    the count is asserted per shape: a fourth overlay added without a format cannot
    slip through, and neither can a stage disappearing."""
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2, "layout": layout,
             "has_audio": True}]
    g = compose.build_filtergraph(segs, 1, CFG, 1, 2, 3, 0.0,
                                  watermark_idx=4 if watermark else None)
    stages = _overlay_stages(g)
    assert len(stages) == expected, (
        f"{layout}{' +watermark' if watermark else ''} should have {expected} "
        f"overlay stage(s), found {len(stages)}: {stages}")
    for stage in stages:
        # The pillarbox bg/fg copy is the one overlay that is not an alpha blend (its
        # secondary input is the opaque scaled clip), so it pins a different value --
        # the one negotiation already chose, measured at 0 pixels of change.
        want = (compose.PILLARBOX_OVERLAY_FORMAT if "(W-w)/2" in stage
                else compose.OVERLAY_FORMAT)
        assert f":format={want}" in stage, (
            f"overlay stage does not pin its working format (wanted "
            f"format={want}): {stage}")
    assert "format=auto" not in g, g
    assert g.count(f"format={compose.PIX_FMT}[vout]") == 1, (
        f"the chain must end in exactly one conversion to {compose.PIX_FMT}: {g}")


def _differing_pixel_count(a, b):
    """How many pixels of two same-size frames differ in ANY channel.

    `_diff_pixels` walks the frame in Python, which is fine for the small excused
    rects it is used on and too slow for a whole 360x640 composite. `ImageChops`
    keeps the comparison exact: the per-band differences are combined with `lighter`,
    so the mask holds each pixel's MAX channel delta and a nonzero count is the
    answer. A per-channel delta of 1 survives here where `convert("L")` would average
    it away to 0."""
    assert a.size == b.size, (a.size, b.size)
    bands = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).split()
    mask = bands[0]
    for band in bands[1:]:
        mask = ImageChops.lighter(mask, band)
    return sum(n for value, n in enumerate(mask.histogram()) if value)


GREY = (96, 96, 96)


def test_the_pillarbox_and_fill_paths_blend_the_captions_identically(tmp_path):
    """Pins PILLARBOX pixels, which nothing in this file did.

    `OVERLAY_FORMAT` is not merely a determinism change: MEASURED at 1080x1920 with a
    landscape source, pinning it moves 1947122 of 2073600 composited pixels on the
    pillarbox path (max channel delta 29) because negotiation was choosing yuv420
    there and rgb on the fill path. The property that buys is the one asserted here --
    one caption track blends the same way whatever the footage's aspect ratio -- and
    it is asserted as pixels rather than as a graph string.

    A FLAT source makes the two paths comparable: scaled up, blurred and centred, flat
    grey is still flat grey, so a pillarbox frame and a fill frame of the same colour
    differ only in how the captions were composited onto them. MEASURED 360x640:
    0 of 230400 pixels differ with the format pinned, 206627 differ with both stages
    at `format=auto`."""
    land = _png_clip(_solid_png(str(tmp_path / "l.png"), 640, 360, GREY),
                     str(tmp_path / "l.mp4"), 3)
    port = _png_clip(_solid_png(str(tmp_path / "p.png"), 360, 640, GREY),
                     str(tmp_path / "p.mp4"), 3)
    cap = _alpha_caption(str(tmp_path / "c.mov"), 360, 640, 3)
    narr = _tone_mp3(str(tmp_path / "n.mp3"), 2, 3000)

    def composite(layout, src, tag):
        segs = [{"path": src, "start_s": 0, "take_s": 2.0, "layout": layout,
                 "has_audio": True}]
        return _composite_png(segs, cap, narr, CFG, str(tmp_path / f"{tag}.png"))

    pb = composite("pillarbox", land, "pb")
    fi = composite("fill", port, "fi")

    # Equality of two blank frames would also be equality, so the fixture is checked
    # first: caption ink present in both, flat grey where the captions are not.
    for name, img in (("pillarbox", pb), ("fill", fi)):
        ink = _caption_band_ink(img)
        assert ink > 200, f"{name} frame carries no caption ink ({ink} px)"
        corner = img.getpixel((4, 4))
        assert max(abs(c - g) for c, g in zip(corner, GREY)) <= 4, (
            f"{name} frame is not the flat grey source outside the captions: "
            f"corner {corner}")

    differ = _differing_pixel_count(pb, fi)
    assert differ == 0, (
        f"the same caption track blends differently on the two layout paths: "
        f"{differ} of {pb.width * pb.height} pixels differ")
