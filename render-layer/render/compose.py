"""compose.py — build the ffmpeg filtergraph and render the final mp4.

No text filter appears anywhere in here: this ffmpeg has neither libass nor
libfreetype. Captions arrive as a pre-rendered alpha video and are composited
with a single overlay. The watermark takes the same route -- Pillow rasterises
it, `overlay` puts it in frame -- so there is one text mechanism in the build,
not two.
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, tempfile
from PIL import Image, ImageDraw, ImageFont
# One definition of "how long is the picture", shared with the probe that decides
# which windows exist. When the two modules resolved it separately they disagreed:
# footage.probe_clip took `format.duration` while this module measured the video
# stream, so an a/v-skewed clip (video 3.000s, audio 3.200s, format 3.200s) produced
# a window this file then refused. See `picture_duration_s`.
#
# The stream is shared for the same reason: resolving the same FIELD off a different
# STREAM is the same disagreement one level down. `pick_video_stream` skips
# `disposition.attached_pic`, which this module's `next(... codec_type == 'video')`
# did not.
from render.footage import ProbeError, picture_duration_s, pick_video_stream


class ComposeError(RuntimeError):
    pass


# ffmpeg prints its per-encoder summary (libx264 macroblock statistics, `Qavg`,
# `Conversion failed!`) AFTER the lines that say what went wrong, so a blind tail
# slice of stderr reliably returns everything except the diagnosis. MEASURED on a
# real rc=234 encode failure: 7158 bytes of stderr, the root-cause line
# `[aac] Input contains (near) NaN/+-Inf` at offset 5475 -- 1696 bytes from the
# end -- and the 800-byte tail the caller used to receive contained neither
# "Error" nor "NaN", only x264 statistics.
#
# So the level is asked for instead of guessed. `-loglevel level+info` keeps the
# default verbosity and additionally stamps every message with its own severity,
# which ffmpeg places after any component prefix:
#   `[aac @ 0x...] [error] Input contains (near) NaN/+-Inf`
# Selecting on that tag needs no list of error phrasings and cannot go stale when
# a message is reworded. `-nostats` drops the progress spam so the fallback tail
# (used only when nothing is tagged, e.g. a signal kill) is log lines rather than
# carriage returns.
FFMPEG_LOGLEVEL = "level+info"
_ERROR_TAGS = ("[error]", "[fatal]", "[panic]")

PIX_FMT = "yuv420p"
VIDEO_CODEC = "h264"
AUDIO_CODEC = "aac"

# The working format of the two ALPHA composites in the chain -- the caption overlay
# and the watermark overlay -- pinned rather than left at `overlay`'s `format=auto`, so
# which colour space antialiased glyph alpha is blended in is a decision in this file
# instead of an outcome of ffmpeg's format negotiation (which reads the whole graph, so
# it can change when a stage is added downstream).
#
# MEASURED on the lossless composite out of `[vout]` -- a qtrle/argb caption track
# carrying real glyph ink over an h264/yuv420p clip -- PER PATH, because negotiation
# resolves per path and "pinning rgb changes nothing" is not one claim but two:
#
#   FILL      pinning rgb reproduces auto EXACTLY: 0 of 2073600 px differ at
#             1080x1920, 0 of 230400 at 360x640.
#   PILLARBOX auto resolves to YUV420 there (an explicit `format=yuv420` differs from
#             auto in 0 of 2073600 px), so pinning rgb IS a picture change on this
#             path: 1947122 of 2073600 px differ, max channel delta 29, first at (0,0)
#             (204,218,247) -> (202,216,245).
#
# The pillarbox change is deliberate and it buys uniformity, not quality. Against a
# Pillow ground-truth alpha composite of the same caption PNG over the same rendered
# background, glyph-edge mean|max error moves 22.02|87 -> 21.62|88 and glyph-body
# 1.59|72 -> 1.63|72 -- a wash. What it fixes is that the same caption track was being
# blended in a different colour space depending on the footage's aspect ratio. After
# the pin, the same caption over the same flat background composites PIXEL-IDENTICALLY
# through both layouts (MEASURED 360x640: 0 of 230400 px differ pinned, 206627 differ
# unpinned); see `test_the_pillarbox_and_fill_paths_blend_the_captions_identically`,
# which is also what pins pillarbox pixels at all.
#
# What pinning does NOT do is change whether the watermark stage disturbs caption
# pixels: it never did. See `test_the_watermark_stage_composites_the_captions_byte_
# identically`.
OVERLAY_FORMAT = "rgb"

# The pillarbox background/foreground composite is the graph's third overlay and it is
# NOT an alpha blend: the foreground is the opaque scaled clip, so `overlay` copies
# there rather than blending, and the format option only moves the yuv<->rgb conversion
# boundary. It is pinned anyway -- a stage left at `auto` is a stage negotiation may
# re-decide -- but pinned at the value negotiation was already choosing, so it costs no
# picture: MEASURED 1080x1920 pillarbox, bg/fg auto vs an explicit `format=yuv420`,
# 0 of 2073600 px differ. `rgb` there differs in 69083 px (max delta 26) and
# yuv444/gbrp in ~1.96M px, for a copy that has no alpha to blend, and both are slower
# (0.58s -> 1.06s / 1.38s per 2s render).
PILLARBOX_OVERLAY_FORMAT = "yuv420"

# qtrle carries alpha as `argb`. This mirrors `captions.ALPHA_PIX_FMTS`.
ALPHA_PIX_FMTS = ("argb", "rgba", "abgr", "bgra")

# The concat demuxer feeds still images at a 25fps timebase whatever is asked for
# downstream, so `captions.build_alpha_track` accepts its own measured duration
# within (1/25 + 1/fps + 1e-3)s of the timeline span it clamped `-t` to. A track
# handed to `render` may therefore legitimately measure that much under `total_s`,
# and no more.
CONCAT_TB_S = 1.0 / 25.0

# Frames of slack on the "does the picture actually cover total_s" assertion.
# MEASURED across five shapes -- a single 3.0s take from a 30fps source, two 1.5s
# segments (fill + pillarbox), a 29.97fps source with a 2.734s take, three uneven
# segments totalling 2.7s, and a 24fps source -- a legitimate render landed on
# exactly round(total_s * fps) frames every time, delta +0. This is therefore pure
# slack, and it is deliberately small: the defect it exists to catch is a 4.0s
# frozen tail, 120 frames at 30fps.
FRAME_SLACK = 2

# Watermark defaults. The design spec fixes the intent ("channel name", "small and
# low-opacity") and puts the numbers in config, so both are cfg-overridable.
WATERMARK_OPACITY = 0.35
WATERMARK_HEIGHT_FRAC = 0.022        # font px as a fraction of canvas height
WATERMARK_MIN_PX = 12
WATERMARK_STROKE_PX = 2
# The watermark sits in the TOP-right corner of the caption safe box. The safe box
# is the one piece of frame geometry the brief actually specifies, so reusing it
# means the watermark cannot land under the platform's own bottom-edge chrome; and
# captions occupy 55-60% of frame height, so the top of the box is free. These three
# only apply when cfg carries no `safe_box`.
SAFE_TOP_DEFAULT = 0.10
SAFE_RIGHT_DEFAULT = 0.14
SAFE_LEFT_DEFAULT = 0.06


def _watermark_box(cfg: dict, W: int, H: int) -> dict:
    """Where the watermark may put pixels, in one place for both users of it.

    `build_filtergraph` places the ink at `x = W-w-mx`, so the width it is allowed to
    occupy is fixed by the same inset -- and by the safe box's LEFT edge, since the
    watermark is specified as sitting inside that box. Computing the position in the
    graph builder and the size in the rasteriser off two separate readings of cfg is
    how the ink came to be wider than the space: `render_watermark_png` refused only
    when the ink was larger than the whole canvas, so `x` went NEGATIVE and `overlay`
    clipped the left of the name off the frame with a zero exit code.
    """
    sb = cfg.get("safe_box") or {}
    mx = int(round(W * float(sb.get("right", SAFE_RIGHT_DEFAULT))))
    my = int(round(H * float(sb.get("top", SAFE_TOP_DEFAULT))))
    lx = int(round(W * float(sb.get("left", SAFE_LEFT_DEFAULT))))
    return {"mx": mx, "my": my, "lx": lx,
            "max_w": W - mx - lx, "max_h": H - my}


def _ffmpeg_error(stderr: str, limit: int = 14) -> str:
    """The error-level lines out of an ffmpeg log, not its tail.

    The first tagged line is the root cause and the ones after it are usually its
    consequences ("Task finished with error code"), so the head is kept when there
    are more than `limit`.
    """
    lines = [l.rstrip() for l in (stderr or "").splitlines()]
    picked = [l.strip() for l in lines if any(t in l for t in _ERROR_TAGS)]
    if picked:
        shown = picked[:limit]
        omitted = len(picked) - len(shown)
        tail = f"\n(+{omitted} further error line(s))" if omitted else ""
        return "\n".join(shown) + tail
    body = [l.strip() for l in lines if l.strip()][-limit:]
    return (f"ffmpeg logged no error-level line in {len(stderr or '')} bytes of "
            f"output; its last {len(body)} log line(s):\n" + "\n".join(body))


def _probe(path: str, what: str) -> tuple[dict, dict | None, dict]:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise ComposeError(
            f"could not probe {what} {path!r}: {res.stderr.strip()[-300:]}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ComposeError(f"ffprobe output for {what} {path!r} will not parse ({e})")
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    # Through footage.pick_video_stream, not `the first video stream`: an attached
    # picture (cover art) is a video stream that is not the picture, and the probe
    # that chose this clip's window skips them. Both sides measuring the same stream
    # is what makes `picture_duration_s`'s "they cannot disagree" true of the stream
    # as well as the field.
    try:
        video = pick_video_stream(streams, f"{what} {path!r}")
    except ProbeError as e:
        raise ComposeError(str(e)) from e
    return video, audio, data.get("format") or {}


def _min_alpha(path: str) -> int:
    """The lowest alpha byte in the caption track's first frame.

    `pix_fmt` is not evidence of a live alpha channel and this is the measurement
    that shows why. MEASURED: `color=c=black@0.0` piped through a SEPARATE `-vf
    format=rgba` stage encodes to qtrle/`argb` -- a pix_fmt in ALPHA_PIX_FMTS --
    with alpha 255 in every pixel, because lavfi settles the input format before
    the conversion runs and drops the `@0.0`. Composited by `overlay=0:0` that is
    an opaque black rectangle over the entire canvas, i.e. a 100% black video that
    still probes as a correct 360x640 h264/yuv420p/aac mp4.
    """
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostats", "-i", path, "-frames:v", "1", "-vf",
         "alphaextract,signalstats,"
         "metadata=print:key=lavfi.signalstats.YMIN:file=-", "-f", "null", "-"],
        capture_output=True, text=True)
    vals = [int(v) for v in re.findall(r"lavfi\.signalstats\.YMIN=(\d+)", res.stdout)]
    if not vals:
        raise ComposeError(
            f"could not measure the alpha channel of caption track {path!r} "
            f"(ffmpeg rc={res.returncode}): {_ffmpeg_error(res.stderr, 6)}")
    return min(vals)


def check_footage(segments, total_s: float, fps: int) -> dict:
    """Assert the footage windows cover the video, against MEASURED clip durations.

    This is the check that actually closes the frozen-tail defect, and it has to be
    a precondition rather than a probe of the output, because the output cannot show
    it. MEASURED: 2.0s of footage under a 6.0s narration with a 6.0s caption track
    -- the real path's shape, since `build_timeline` guarantees the track spans
    `total_s` -- renders nb_frames=180 and a 6.000000s video stream, a full and
    correct-looking frame count, while the picture is frozen from 2.0s onward (the
    decoded frame at 3.0s is identical to the frame at 5.0s). `overlay`'s framesync
    repeats the main input's last frame for as long as the secondary keeps
    delivering, so a short footage window is invisible in every container fact the
    finished file carries.

    Per-segment, the window is checked against the clip's own measured picture
    duration rather than the manifest's word, which is what the design spec asks
    for: "the manifest stores what was measured, and the compositor asserts against
    it rather than trusting a value it passed in."

    That duration is resolved through `footage.picture_duration_s` -- the same
    function `footage.probe_clip` records it with -- so the window the selector was
    allowed to offer and the window this function will accept cannot be measured off
    different fields. They were: the probe took `format.duration` while this measured
    the video stream, and on a clip whose audio outruns its picture (MEASURED: video
    3.000s, audio 3.200s, format.duration 3.200s) the selector allocated take_s=3.2
    and this raised on every run. Nothing about that abort was wrong except its
    inevitability -- a 3.2s window off 3.0s of picture really would freeze for 0.2s,
    which is why the fix is upstream in what gets offered, not a clamp here.
    """
    tol = 1.0 / float(fps) + 1e-3
    covered = 0.0
    for i, seg in enumerate(segments):
        start, take = float(seg["start_s"]), float(seg["take_s"])
        if take <= 0:
            raise ComposeError(
                f"footage segment {i} ({seg['path']!r}) has take_s {take!r}; a "
                f"non-positive window contributes no picture")
        video, _audio, fmt = _probe(seg["path"], f"footage segment {i}")
        src = picture_duration_s(video, fmt)
        if src is None:
            raise ComposeError(
                f"footage segment {i} ({seg['path']!r}) reports no duration, so the "
                f"requested {start:.3f}..{start + take:.3f}s window cannot be "
                f"checked against it")
        if start + take > src + tol:
            raise ComposeError(
                f"footage segment {i} asks for {start:.3f}..{start + take:.3f}s of "
                f"{seg['path']!r} but its picture MEASURES {src:.3f}s; trim would "
                f"yield {max(src - start, 0.0):.3f}s and the last frame would be "
                f"held for the remaining {start + take - src:.3f}s")
        covered += take
    if covered < float(total_s) - tol:
        raise ComposeError(
            f"footage windows cover {covered:.3f}s but total_s is "
            f"{float(total_s):.3f}s; the picture would freeze on its final frame "
            f"for the remaining {float(total_s) - covered:.3f}s while the narration "
            f"keeps playing. No fact about the output file shows this -- `overlay` "
            f"repeats the footage's last frame while the caption track runs on, so "
            f"the render still measures {int(round(float(total_s) * fps))} frames "
            f"and a {float(total_s):.3f}s video stream (MEASURED)")
    return {"covered_s": round(covered, 6), "segments": len(segments)}


def check_caption_track(caption_track: str, cfg: dict, total_s: float,
                       fps: int) -> dict:
    """Assert the caption track against the canvas it will be composited onto.

    Every failure here is silent in the artifact, which is why it is checked
    before the encode rather than eyeballed after it. MEASURED, all three exit 0
    with nothing raised before this existed:
      - a 100x100 track over a 360x640 canvas renders 360x640 with the captions
        confined to the top-left 100x100;
      - a 720x1280 track over the same canvas renders 360x640 with only the track's
        top-left quadrant ever visible;
      - a track shorter than the video holds its final frame for the remainder
        (`overlay` defaults to `eof_action=repeat`), so the last spoken word sits
        frozen on screen through the tail.
    """
    want = (int(cfg["width"]), int(cfg["height"]))
    video, _audio, fmt = _probe(caption_track, "caption track")
    measured = (int(video["width"]), int(video["height"]))
    if measured != want:
        raise ComposeError(
            f"caption track {caption_track!r} measured {measured[0]}x{measured[1]} "
            f"but the canvas is {want[0]}x{want[1]}; `overlay=0:0` neither scales "
            f"nor centres, so the captions would be "
            f"{'clipped to' if measured > want else 'confined to'} the top-left "
            f"{min(measured[0], want[0])}x{min(measured[1], want[1])} of frame")
    pix = video.get("pix_fmt")
    if pix not in ALPHA_PIX_FMTS:
        raise ComposeError(
            f"caption track {caption_track!r} is {pix!r}, which carries no alpha "
            f"channel; overlaying it would paint an opaque rectangle over the "
            f"whole gameplay clip")
    lo = _min_alpha(caption_track)
    if lo >= 255:
        raise ComposeError(
            f"caption track {caption_track!r} probes as {pix!r} but its first frame "
            f"is fully opaque (minimum alpha {lo}); compositing it would replace "
            f"every pixel of the gameplay footage. The alpha was lost when the "
            f"track was encoded, not here")
    dur = float(fmt.get("duration") or 0.0)
    tol = CONCAT_TB_S + 1.0 / float(fps) + 1e-3
    if dur < float(total_s) - tol:
        raise ComposeError(
            f"caption track {caption_track!r} measures {dur:.6f}s against a "
            f"{float(total_s):.6f}s video (tolerance {tol:.6f}s); `overlay` holds "
            f"the last caption frame at EOF, so its final word would sit frozen on "
            f"screen for the remaining {float(total_s) - dur:.3f}s")
    return {"width": measured[0], "height": measured[1], "pix_fmt": pix,
            "duration_s": dur, "min_alpha": lo}


def render_watermark_png(text: str, size, cfg: dict, font_path: str,
                         out_path: str) -> str:
    """Rasterise the channel name, small and low-opacity, on a transparent bed.

    Tight to the ink rather than canvas-sized, so `build_filtergraph` can place it
    with `overlay`'s own `W-w` expression and does not have to know its pixels.

    Sized to fit the space that expression leaves it, which is NOT the whole canvas.
    MEASURED at 360x640 with watermark_height_frac=0.05 and Anton-Regular on a
    22-character name: refusing only when the ink is bigger than the canvas (342x36
    against 360x640 -- it fits) let `overlay` place it at x = W-w-mx = -32, and the
    composited ink landed at x 0..305 with 32px of the name off the left edge of
    frame while `render` returned normally. Shrunk to fit the safe box it rasterises
    276x31 at x=34, ink at x 38..305, inside the box's x 22..310.
    """
    text = (text or "").strip()
    if not text:
        raise ComposeError("refusing to render an empty watermark")
    W, H = int(size[0]), int(size[1])
    frac = float(cfg.get("watermark_height_frac", WATERMARK_HEIGHT_FRAC))
    px = max(int(round(H * frac)), WATERMARK_MIN_PX)
    opacity = float(cfg.get("watermark_opacity", WATERMARK_OPACITY))
    if not 0.0 < opacity <= 1.0:
        raise ComposeError(
            f"watermark_opacity must be in (0.0, 1.0], got {opacity!r}; a "
            f"watermark at 0 is a watermark that silently is not there")
    alpha = max(1, int(round(255 * opacity)))
    if not font_path or not os.path.isfile(font_path):
        raise ComposeError(
            f"watermark {text!r} needs a font and {font_path!r} is not a file; "
            f"refusing to render a video whose configured watermark is missing")
    try:
        font = ImageFont.truetype(font_path, px)
    except OSError as e:
        raise ComposeError(f"watermark font {font_path!r} will not load ({e})") from e

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pad = WATERMARK_STROKE_PX + 2

    def measure(size_px):
        f = ImageFont.truetype(font_path, size_px)
        l, t, r, b = probe.textbbox((0, 0), text, font=f,
                                    stroke_width=WATERMARK_STROKE_PX)
        return f, l, t, int(r - l) + 2 * pad, int(b - t) + 2 * pad

    box = _watermark_box(cfg, W, H)
    max_w, max_h = box["max_w"], box["max_h"]
    if max_w < WATERMARK_MIN_PX or max_h < WATERMARK_MIN_PX:
        raise ComposeError(
            f"the safe box leaves {max_w}x{max_h}px for a watermark on a {W}x{H} "
            f"canvas, which is not a place a name can be drawn; check safe_box "
            f"left/right/top in config")

    font, l, t, iw, ih = measure(px)
    # Shrink to fit the box rather than let `overlay` clip the name. The font size is
    # OUR derived number (watermark_height_frac * H), not a pixel count the operator
    # asked for, so reducing it keeps the configured watermark visible and whole;
    # clipping it silently is the failure being fixed, and aborting a whole render
    # over a decorative element would be worse than either.
    while (iw > max_w or ih > max_h) and px > WATERMARK_MIN_PX:
        ratio = min(max_w / iw, max_h / ih)
        px = max(WATERMARK_MIN_PX, min(px - 1, int(px * ratio)))
        font, l, t, iw, ih = measure(px)
    if iw > max_w or ih > max_h:
        raise ComposeError(
            f"watermark {text!r} rasterises to {iw}x{ih} even at the {px}px floor, "
            f"and the safe box on a {W}x{H} canvas leaves {max_w}x{max_h}; "
            f"`overlay` would place it at x={W - iw - box['mx']} and clip "
            f"{max(iw - max_w, 0)}px of the name off the frame. Shorten the channel "
            f"name or widen safe_box")

    img = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - l, pad - t), text, font=font,
                             fill=(255, 255, 255, alpha),
                             stroke_width=WATERMARK_STROKE_PX,
                             stroke_fill=(0, 0, 0, alpha))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    # Measure the file that will actually be overlaid, rather than trust the size the
    # bed was allocated with: Pillow's own `textbbox` is what sized it, and this is
    # the last point before ffmpeg is handed a `W-w-mx` it cannot sanity-check.
    with Image.open(out_path) as saved:
        got = saved.size
    x = W - got[0] - box["mx"]
    if got != (iw, ih) or x < 0 or got[1] + box["my"] > H:
        raise ComposeError(
            f"watermark png {out_path!r} MEASURES {got[0]}x{got[1]} (expected "
            f"{iw}x{ih}); at overlay x={x}, y={box['my']} on a {W}x{H} canvas that "
            f"is not fully in frame")
    return out_path


def build_input_args(segments) -> list[str]:
    """`-ss <start_s>` BEFORE each `-i`: the window's START is an input seek.

    This is the half of the window that does NOT live in the filtergraph, and the
    two halves must be read together -- `build_filtergraph` emits only
    `trim=duration=`, so a caller that assembles the graph without these args
    renders from 0.000s of every clip. `render` is the only assembler; the pair is
    pinned together by `test_the_window_start_is_an_input_seek_not_a_filter`.

    Why not `trim=start=`, which is what this used to be: `trim` is a FILTER, so
    ffmpeg decodes every frame before the start point and throws it away. On the
    owner's library -- one 5237.542s clip (MEASURED), so `select_window` draws an
    offset anywhere in 0..5206s -- that is up to ~3 minutes of wasted decode per
    render, and it also delays the first encoded video frame until the discard is
    done. MEASURED at offset 1515.0s, same inputs, same 31.243s output of 937
    frames, standalone:

        trim=start=1515.0   88.13s / 92.49s / 87.13s / 89.51s wall
        -ss 1515.000        36.23s / 35.82s / 40.76s wall

    and at offset 4500.0s the filter shape took 194.59s. The stall is also the only
    condition under which this pipeline has ever been observed to ABORT: with the
    filter shape, `make_one`'s dry run failed with

        [out#0/mp4] Could not write header (incorrect codec parameters ?):
                    Operation timed out
        [fc#0] Error sending frames to consumers: Operation timed out
        [out#0/mp4] Nothing was written into output file, because at least one of
                    its streams received no packets.

    (MEASURED rc=196 at HEAD 56a45c9, and 2 of 2 in the prior report) while the
    audio path had long since finished. Two mechanisms were proposed for that abort
    and BOTH are falsified by measurement, so neither of their fixes is applied
    here: it is NOT the pre-mux packet queue (`-max_muxing_queue_size 4` still
    rendered rc=0 through both shapes, and a full queue reports "Too many packets
    buffered", not ETIMEDOUT), and it is NOT a fixed deadline on stream
    initialisation (the 194.59s offset-4500 render finished rc=0). What is
    established is that the abort has only ever appeared on the shape that stalls,
    the stall is 51s of pure waste, and it is gone.
    """
    args: list[str] = []
    for seg in segments:
        # Formatted at microsecond precision -- AV_TIME_BASE's own resolution --
        # rather than %.3f, so a start offset is never quietly truncated. Offsets
        # from `footage.select_window` land on a 1s grid, but this function does not
        # get to assume its caller.
        args += ["-ss", f"{float(seg['start_s']):.6f}", "-i", seg["path"]]
    return args


def build_filtergraph(segments, n_clip_inputs, cfg, silence_idx,
                      caption_idx, narration_idx, narration_delay_s,
                      watermark_idx=None) -> str:
    """The graph. It carries each window's DURATION only -- see `build_input_args`,
    which carries the start, and read the two together."""
    W, H, FPS = int(cfg["width"]), int(cfg["height"]), int(cfg["fps"])
    parts, vlabels, alabels = [], [], []

    for i, seg in enumerate(segments):
        # No `start` on the trim: input 'i' was opened with `-ss start_s`, so its
        # first frame IS the start of the window. `trim`'s `duration` is measured
        # from the first frame it sees (MEASURED: `-ss 1515.000` + `trim=duration=
        # 31.243` off a 24fps source renders 937 frames = round(31.243 * 30), the
        # same count the filter shape produced), and `setpts=PTS-STARTPTS` after it
        # rebases the window to 0 for `concat`.
        d = float(seg["take_s"])
        if seg["layout"] == "pillarbox":
            parts.append(
                f"[{i}:v]trim=duration={d},setpts=PTS-STARTPTS,split[b{i}][f{i}];"
                f"[b{i}]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},gblur=sigma=24[bg{i}];"
                f"[f{i}]scale={W}:-2[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2:"
                f"format={PILLARBOX_OVERLAY_FORMAT},fps={FPS},setsar=1[v{i}]")
        else:
            parts.append(
                f"[{i}:v]trim=duration={d},setpts=PTS-STARTPTS,"
                f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},fps={FPS},setsar=1[v{i}]")
        vlabels.append(f"[v{i}]")

        if seg["has_audio"]:
            parts.append(f"[{i}:a]atrim=duration={d},"
                         f"asetpts=PTS-STARTPTS,aresample=44100[a{i}]")
        else:
            parts.append(f"[{silence_idx}:a]atrim=duration={d},"
                         f"asetpts=PTS-STARTPTS,aresample=44100[a{i}]")
        alabels.append(f"[a{i}]")

    n = len(segments)
    if n > 1:
        parts.append("".join(vlabels) + f"concat=n={n}:v=1:a=0[bgv]")
        parts.append("".join(alabels) + f"concat=n={n}:v=0:a=1[gamea]")
    else:
        parts.append(f"{vlabels[0]}null[bgv]")
        parts.append(f"{alabels[0]}anull[gamea]")

    # The caption composite is spelled IDENTICALLY in both branches, and its working
    # format is pinned rather than left at `format=auto`. See OVERLAY_FORMAT. Every
    # `overlay` in this graph names a format -- the pillarbox bg/fg copy above included,
    # which was the one stage still left to negotiation -- and
    # `test_every_overlay_stage_in_the_graph_pins_its_working_format` enumerates them
    # instead of asserting `"format=auto" not in graph`, which a stage that spells no
    # format option at all satisfies for free.
    cap = f"[bgv][{caption_idx}:v]overlay=0:0:format={OVERLAY_FORMAT}"
    if watermark_idx is None:
        parts.append(cap + f",format={PIX_FMT}[vout]")
    else:
        # The design spec's video chain is: footage window -> layout filter ->
        # overlay of the alpha caption track -> watermark. The watermark input is a
        # single PNG frame; `overlay` defaults to eof_action=repeat, so that one
        # frame is held for the whole video and the main input still drives the
        # length.
        box = _watermark_box(cfg, W, H)
        parts.append(cap + "[capped]")
        parts.append(f"[capped][{watermark_idx}:v]"
                     f"overlay=x=W-w-{box['mx']}:y={box['my']}:"
                     f"format={OVERLAY_FORMAT},format={PIX_FMT}[vout]")

    duck = float(cfg["game_audio_duck"])
    delay_ms = int(round(float(narration_delay_s) * 1000))
    parts.append(f"[gamea]volume={duck}[gameduck]")
    parts.append(f"[{narration_idx}:a]aresample=44100,"
                 f"adelay={delay_ms}|{delay_ms}[narr]")
    parts.append(f"[gameduck][narr]amix=inputs=2:duration=longest:normalize=0,"
                 f"loudnorm=I={int(cfg['loudnorm_i'])}:TP=-1.5:LRA=11[aout]")
    return ";".join(parts)


def check_output(out_path: str, cfg: dict, total_s: float) -> dict:
    """Measure the artifact before handing back its path.

    `captions.build_alpha_track` already establishes the precedent and the reason:
    a value we passed in on the command line cannot certify the encode, because
    `-pix_fmt`, `-c:v`, `-c:a` and `crop=W:H` make the container facts right no
    matter what the graph did.

    The frame count here is NOT what guarantees the picture covers `total_s` --
    `check_footage` is, and its docstring records why the output cannot show that.
    What this catches is the complementary class: a run that ended up shorter (or
    longer) than asked for on the way out, e.g. a truncated write. MEASURED for the
    tolerance: a legitimate render landed on exactly round(total_s * fps) frames in
    all five shapes tried. Note also that `format.duration` is NOT the field to read
    -- it reported 6.000000s for a file carrying 2.0s of picture, because the
    narration supplied it.
    """
    fps = int(cfg["fps"])
    want = (int(cfg["width"]), int(cfg["height"]))
    video, audio, fmt = _probe(out_path, "rendered output")
    measured = (int(video["width"]), int(video["height"]))
    if measured != want:
        raise ComposeError(
            f"rendered {measured[0]}x{measured[1]} but the canvas is "
            f"{want[0]}x{want[1]}")
    if video.get("pix_fmt") != PIX_FMT:
        raise ComposeError(
            f"rendered pix_fmt is {video.get('pix_fmt')!r}, not {PIX_FMT!r}")
    if video.get("codec_name") != VIDEO_CODEC:
        raise ComposeError(
            f"rendered video codec is {video.get('codec_name')!r}, "
            f"not {VIDEO_CODEC!r}")
    if audio is None:
        raise ComposeError(
            "rendered output has no audio stream; the narration and the ducked "
            "gameplay mix are both missing")
    if audio.get("codec_name") != AUDIO_CODEC:
        raise ComposeError(
            f"rendered audio codec is {audio.get('codec_name')!r}, "
            f"not {AUDIO_CODEC!r}")
    try:
        frames = int(video["nb_frames"])
    except (KeyError, TypeError, ValueError):
        # No frame count in the container: fall back to the VIDEO stream's own
        # duration, never the format duration -- the format duration is the one
        # that reads 6.0s for 2.0s of picture.
        try:
            frames = int(round(float(video["duration"]) * fps))
        except (KeyError, TypeError, ValueError):
            raise ComposeError(
                f"rendered output {out_path!r} reports neither nb_frames nor a "
                f"video stream duration, so the picture cannot be measured")
    expect = int(round(float(total_s) * fps))
    if abs(frames - expect) > FRAME_SLACK:
        short = (expect - frames) / float(fps)
        raise ComposeError(
            f"rendered {frames} video frames but {float(total_s):.3f}s at {fps}fps "
            f"is {expect} (slack {FRAME_SLACK}); "
            + (f"the picture is {short:.3f}s short of the audio, so the last frame "
               f"is frozen for that long -- the footage window does not cover "
               f"total_s"
               if short > 0 else
               f"the picture runs {-short:.3f}s past total_s") +
            f". format.duration reads {fmt.get('duration')!r} and does not show it")
    return {"width": measured[0], "height": measured[1],
            "pix_fmt": video.get("pix_fmt"), "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"), "frames": frames,
            "expected_frames": expect,
            "format_duration_s": float(fmt.get("duration") or 0.0)}


def render(segments, caption_track, narration_mp3, out_path, cfg,
           total_s, narration_delay_s=0.0, watermark_text="",
           watermark_font=None, work_dir=None) -> str:
    if not segments:
        raise ComposeError("no footage segments to render")
    fps = int(cfg["fps"])
    check_footage(segments, total_s, fps)
    check_caption_track(caption_track, cfg, total_s, fps)

    wm_text = (watermark_text or "").strip()
    tmp_dir = None
    try:
        wm_png = None
        if wm_text:
            # An empty watermark renders nothing -- that is its configured
            # behaviour. A NON-empty one that cannot be rendered is an operator who
            # did the documented setup step and would otherwise get an unwatermarked
            # video and no warning, so it raises.
            if work_dir:
                os.makedirs(work_dir, exist_ok=True)
                wm_dir = work_dir
            else:
                tmp_dir = wm_dir = tempfile.mkdtemp(prefix="compose-wm-")
            wm_png = render_watermark_png(
                wm_text, (int(cfg["width"]), int(cfg["height"])), cfg,
                watermark_font, os.path.join(wm_dir, "watermark.png"))

        cmd = ["ffmpeg", "-y", "-nostats", "-loglevel", FFMPEG_LOGLEVEL]
        # `-ss start_s -i path` per segment. The graph then trims duration only --
        # `build_input_args` records what that buys and what it fixes.
        cmd += build_input_args(segments)
        silence_idx = len(segments)
        cmd += ["-f", "lavfi", "-t", f"{total_s + 1:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        caption_idx = silence_idx + 1
        cmd += ["-i", caption_track]
        narration_idx = caption_idx + 1
        cmd += ["-i", narration_mp3]
        watermark_idx = None
        if wm_png:
            watermark_idx = narration_idx + 1
            cmd += ["-i", wm_png]

        graph = build_filtergraph(segments, len(segments), cfg, silence_idx,
                                  caption_idx, narration_idx, narration_delay_s,
                                  watermark_idx=watermark_idx)
        cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "medium", "-crf", str(int(cfg["crf"])),
                "-pix_fmt", PIX_FMT, "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                "-t", f"{total_s:.3f}", out_path]

        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise ComposeError(
                f"render failed (ffmpeg rc={res.returncode}): "
                f"{_ffmpeg_error(res.stderr)}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    check_output(out_path, cfg, total_s)
    return out_path
