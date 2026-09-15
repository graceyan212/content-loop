"""captions.py — one-word caption images.

ffmpeg here has no libass and no libfreetype, so text cannot be drawn by a video
filter. Pillow draws it instead, which also makes captions golden-file testable.
"""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont

STROKE_PX = 8
FILL = (255, 255, 255, 255)
STROKE = (0, 0, 0, 255)
MIN_PX = 12
WEIGHT_AXIS = "Weight"


class CaptionError(ValueError):
    pass


def safe_box(width: int, height: int, cfg: dict) -> dict:
    sb = cfg["safe_box"]
    return {"left": width * sb["left"], "right": width * (1.0 - sb["right"]),
            "top": height * sb["top"], "bottom": height * (1.0 - sb["bottom"])}


def _axis_name(axis: dict) -> str:
    name = axis.get("name")
    if isinstance(name, bytes):
        name = name.decode("utf-8", "replace")
    return (name or "").strip()


def _font(font_path: str, px: int, weight: int) -> ImageFont.FreeTypeFont:
    """Build the caption font with the Weight axis actually set to `weight`.

    Every failure here is raised, never swallowed. The weight is not cosmetic: the
    caption font is specified as TikTokSans-Variable at Weight 900, and the axis
    default is 300, so any silently-skipped `set_variation_by_axes` renders every
    caption in the build at Light and nothing downstream can tell.

    The axis is located BY NAME rather than by position. `set_variation_by_axes`
    takes a positional coordinate list, so a hardcoded `[36, 100, weight, 0]`
    against a font whose fvar table is ordered differently puts the weight on some
    other axis and raises nothing at all -- e.g. sending 900 to `Optical size`
    (max 36) and 36 to `Weight` (min 300) clamps back to the 300 default and
    renders ink pixel-identical to an untouched font.
    """
    f = ImageFont.truetype(font_path, px)
    short = os.path.basename(font_path)
    try:
        axes = f.get_variation_axes()
    except OSError as e:
        raise CaptionError(
            f"caption font {short!r} is not a variation font ({e}); it cannot honour "
            f"the required Weight axis {weight} and would render at its nominal "
            f"weight instead") from e
    idx = next((i for i, a in enumerate(axes)
                if _axis_name(a).lower() == WEIGHT_AXIS.lower()), -1)
    if idx < 0:
        raise CaptionError(
            f"caption font {short!r} exposes no {WEIGHT_AXIS!r} axis (found "
            f"{[_axis_name(a) for a in axes]}); required weight {weight} is unreachable")
    lo, hi = float(axes[idx]["minimum"]), float(axes[idx]["maximum"])
    want = float(weight)
    if not lo <= want <= hi:
        raise CaptionError(
            f"caption_weight {want:g} is outside the {short!r} {WEIGHT_AXIS} axis "
            f"range {lo:g}-{hi:g}; FreeType would clamp it silently")
    coords = [float(a["default"]) for a in axes]
    coords[idx] = want
    try:
        f.set_variation_by_axes(coords)
    except OSError as e:
        raise CaptionError(
            f"could not set {short!r} {WEIGHT_AXIS} axis to {want:g} "
            f"(coords={coords}): {e}") from e
    return f


def render_word_png(word: str, size, cfg: dict, font_path: str, out_path: str) -> str:
    text = (word or "").strip()
    if not text:
        raise CaptionError("refusing to render an empty caption")
    width, height = size
    box = safe_box(width, height, cfg)
    max_w = box["right"] - box["left"]

    px = max(int(cfg["caption_size_px"]), MIN_PX)
    while True:
        font = _font(font_path, px, int(cfg["caption_weight"]))
        l, t, r, b = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox(
            (0, 0), text, font=font, stroke_width=STROKE_PX)
        if (r - l) <= max_w or px <= MIN_PX:
            break
        px = max(px - 4, MIN_PX)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = (box["left"] + box["right"]) / 2.0
    cy = height * float(cfg["caption_center_y"])
    cy = min(max(cy, box["top"] + (b - t) / 2.0), box["bottom"] - (b - t) / 2.0)
    draw.text((cx, cy), text, font=font, fill=FILL, anchor="mm",
              stroke_width=STROKE_PX, stroke_fill=STROKE)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG", optimize=False)
    return out_path


def render_blank_png(size, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    Image.new("RGBA", tuple(size), (0, 0, 0, 0)).save(out_path, "PNG")
    return out_path


import hashlib, json, subprocess

MIN_EVENT_S = 1.0 / 60.0

# The concat demuxer feeds still images through the image2 sub-demuxer, whose
# timebase is 25fps whatever we ask for downstream -- ffmpeg prints it itself:
# `Stream #0:0: Video: png, rgba(pc, gbr/unknown/unknown), 360x640, 25 fps, 25 tbr,
# 25 tbn`. So every event boundary lands on a 1/25s grid, which is the irreducible
# slop in a caption onset. MEASURED on the encoded track: max onset error 0.033s
# (one output frame at 30fps) at 20, 40 and 150 words alike, and the head-third mean
# +0.0035s against the tail-third mean +0.0037s at 150 words -- so it is bounded and
# it does NOT accumulate, because `build_alpha_track` differences absolute
# boundaries instead of summing lengths. It does set the floor for any duration
# tolerance here.
CONCAT_TB_S = 1.0 / 25.0

# Adjacency tolerance for the gapless precondition: 0.1ms. ~300x tighter than one
# frame at 30fps, and orders of magnitude looser than float noise, so it accepts a
# timeline assembled by accumulating floats and rejects every hole big enough to
# move a caption (the defect this guard replaced left 10ms holes -- 100x this).
GAP_EPS_S = 1e-4

# qtrle carries alpha as `argb`. Anything outside this set means the alpha channel
# was dropped somewhere in the encode, which under Task 10's `overlay=0:0` would
# paint an opaque rectangle over the whole gameplay clip.
ALPHA_PIX_FMTS = ("argb", "rgba", "abgr", "bgra")


def build_timeline(word_list, hook_lead_s: float, total_s: float) -> list[dict]:
    """A gapless list of events covering 0..total_s. One image is on screen at
    every instant, so the alpha track needs no compositing of its own.

    "Gapless" is structural here, not incidental: every event starts at the cursor
    the previous one ended on, so `events[i]["start"] == events[i-1]["end"]`
    exactly, for every i, by construction. It has to be. `build_alpha_track`
    writes a concat manifest, and a concat manifest carries DURATIONS -- absolute
    positions are reconstructed by summing them -- so a hole in this list is not a
    momentary blank frame, it is a permanent shift of every caption after it.

    The earlier revision only emitted a blank when the inter-word gap exceeded
    MIN_EVENT_S and then started the word event at its own `start`, which left the
    sub-MIN_EVENT_S gaps as holes: MEASURED 39 holes totalling 0.390s on a 40-word
    list with 10ms gaps, and on the encoded track that was -0.010s of caption drift
    per word, growing linearly (word 2 -0.020s, word 10 -0.100s). A sub-frame gap
    is therefore ABSORBED, by bringing the next word's image up at the cursor
    instead of at its own start -- at most MIN_EVENT_S (16.7ms, half a frame at
    30fps) early, once, never accumulating.

    A word whose `end` precedes the cursor (non-monotonic alignment -- nothing
    upstream promises monotonicity) is clamped to a zero-length event rather than
    emitted with a negative duration. It keeps the list gapless and keeps every
    later word on time; the clamped word gets no frame of its own.
    """
    events: list[dict] = []
    t = 0.0

    def emit(kind, text, end):
        nonlocal t
        end = max(end, t)          # never let an event run backwards
        events.append({"kind": kind, "text": text, "start": t, "end": end})
        t = end

    if hook_lead_s > 0:
        emit("hook", None, hook_lead_s)
    for w in word_list:
        start = w["start"] + hook_lead_s
        if start - t > MIN_EVENT_S:
            emit("blank", None, start)
        emit("word", w["word"], w["end"] + hook_lead_s)
    if total_s - t > MIN_EVENT_S:
        emit("blank", None, total_s)
    if events:
        events[-1]["end"] = max(events[-1]["end"], total_s)
    return events


def _check_gapless(timeline) -> float:
    """Validate the precondition the concat manifest silently depends on.

    Returns the span. Raises `CaptionError` on the first hole or overlap, naming
    it, because the alternative is what this replaced: the track takes its LENGTH
    from the span and its CONTENT from the durations, those two agree only if the
    events are adjacent, and when they disagree nothing raises. MEASURED on a
    hand-built word-only timeline `[(alpha,0,0.5),(bravo,2,2.5),(delta,4,4.5)]`:
    `-t` was emitted as the 4.5s span while the encoded track was 2.033s / 61
    frames, "bravo" -- declared at 2.0s -- rendered at 0.533s (1.467s early) and
    "delta" -- declared at 4.0s -- rendered at 1.000s (3.000s early).
    """
    for i, ev in enumerate(timeline):
        if ev["end"] - ev["start"] < -GAP_EPS_S:
            raise CaptionError(
                f"caption event {i} ({ev['kind']} {ev['text']!r}) runs backwards: "
                f"start {ev['start']:.6f} > end {ev['end']:.6f}")
    for i, (a, b) in enumerate(zip(timeline, timeline[1:])):
        d = b["start"] - a["end"]
        if abs(d) > GAP_EPS_S:
            what = "hole" if d > 0 else "overlap"
            raise CaptionError(
                f"caption timeline is not gapless: {what} of {abs(d) * 1000:.1f}ms "
                f"between event {i} ({a['kind']} {a['text']!r} ending {a['end']:.6f}) "
                f"and event {i + 1} ({b['kind']} {b['text']!r} starting "
                f"{b['start']:.6f}). A concat manifest carries durations, not "
                f"absolute times, so a {what} here mis-times every caption after "
                f"it. Build the timeline with build_timeline, or make the events "
                f"adjacent, rather than shipping a silently drifted track.")
    return timeline[-1]["end"] - timeline[0]["start"]


def _asset_png(work_dir: str, prefix: str, identity: list, size, render):
    """Content-addressed rasterisation cache, keyed on everything that changes the
    pixels -- not just the text.

    Keying on `sha256(text)` alone (and on the constant filename `_hook.png`) meant
    a second call into an existing `work_dir` reused the first call's frames.
    MEASURED: with `hook_text` changed between calls the `_hook.png` digest was
    unchanged (081ca13ec239 both times), so the second track carried the first
    call's hook card; and with the canvas changed from 360x640 to 1080x1920 the
    encode SUCCEEDED and returned a 360x640 track, because the cached word PNGs set
    the concat stream size. That path is reachable from Task 11's normal loop, which
    re-runs with a fixed `--run-id` into the same work dir.

    The reuse is also verified rather than assumed: a cache hit is only honoured if
    the file on disk MEASURES the requested canvas, so a work dir left over from
    any earlier revision heals instead of contaminating the encode.
    """
    payload = json.dumps(identity, sort_keys=True, default=repr)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(work_dir, f"{prefix}_{digest}.png")
    if os.path.isfile(path):
        try:
            with Image.open(path) as im:
                measured = im.size
        except OSError:
            measured = None
        if measured == size:
            return path
    return render(path)


def _probe(path: str) -> tuple[dict, dict]:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise CaptionError(f"could not probe {path!r}: {res.stderr[-300:]}")
    data = json.loads(res.stdout)
    video = next((s for s in data.get("streams", ())
                  if s.get("codec_type") == "video"), None)
    if video is None:
        raise CaptionError(f"{path!r} has no video stream")
    return video, data.get("format", {})


def build_alpha_track(timeline, size, cfg, font_paths, work_dir,
                      out_path, fps: int, hook_text: str = "") -> str:
    """One PNG per distinct event, a concat manifest carrying the durations, and a
    single lossless qtrle encode. One image per word, not one per frame.

    `timeline` must be gapless (what `build_timeline` returns). That is checked, not
    assumed -- see `_check_gapless`.
    """
    if not timeline:
        raise CaptionError("refusing to encode an alpha track from an empty timeline")
    span = _check_gapless(timeline)
    size = (int(size[0]), int(size[1]))
    os.makedirs(work_dir, exist_ok=True)

    def image_for(ev):
        kind = ev["kind"]
        if kind == "blank":
            return _asset_png(work_dir, "blank", ["blank", size], size,
                              lambda p: render_blank_png(size, p))
        if kind == "hook":
            text = hook_text or ""

            def draw_hook(p):
                from render.hook import render_hook_png
                return render_hook_png(text, size, cfg, font_paths["hook"], p)

            return _asset_png(work_dir, "hook",
                              ["hook", text, size, cfg, font_paths["hook"]],
                              size, draw_hook)
        if kind == "word":
            return _asset_png(
                work_dir, "w", ["word", ev["text"], size, cfg, font_paths["caption"]],
                size,
                lambda p: render_word_png(ev["text"], size, cfg,
                                          font_paths["caption"], p))
        raise CaptionError(f"unknown caption event kind {kind!r}")

    # Each `duration` is the difference between two ABSOLUTE boundaries, not the
    # event's own length. Those are the same number for adjacent events, but they
    # fail differently: summing per-event lengths makes every rounding error and
    # every non-adjacency permanent for the rest of the track, whereas differencing
    # absolute boundaries re-anchors on every event, so nothing can accumulate.
    # Boundaries are rounded BEFORE differencing, so the manifest's running total
    # is exact to 1e-6s no matter how long the narration is.
    #
    # There is deliberately NO `max(dur, MIN_EVENT_S)` floor. An earlier revision had
    # one, and dropping it is the single behaviour this file traded away, so the price
    # is priced here rather than left for someone to rediscover as a bug.
    # A word shorter than one output frame can fall between 30fps samples and never be
    # displayed. MEASURED, 12 words of one duration each, 360x640, fps=30: 40ms words
    # 12/12 visible at every spacing tried; 10ms words anywhere from 0/12 to 6/12
    # depending ONLY on spacing (6/12 at 20ms, 3/12 at 210ms, 0/12 at 100ms). So the
    # survival count is a property of where the words land against the frame grid, not
    # of this code, and no single count characterises it -- do not write one down.
    # The floor did not fix that. MEASURED with `max(dur, 1/60)` restored, the same
    # words went to a flat 5/12: two or three more words, still not all of them, and
    # bought by lengthening every short event, which pushes every LATER caption late.
    # MEASURED on 12 back-to-back 5ms words followed by a 300ms word, that later word
    # lands +0.1400s late with the floor -- 4.2 frames, 1.88x the tolerance asserted at
    # the bottom of this function -- against +0.0067s without it. Partial,
    # phase-dependent visibility is not worth buying with drift that is neither, so
    # the position stays exact and the loss stays visible.
    # Off the real path, but with less headroom than it looks: `words_from_alignment`
    # spans a word's first character start to its last character end, so a
    # ONE-character word ("a", "I") is exactly one character long. MEASURED on
    # tests/fixtures/alignment_five_dollars.json, the only real alignment in the repo,
    # the shortest character is 0.0500s against a 0.0333s frame -- 1.5 frames of
    # headroom, not orders of magnitude.
    lines = ["ffconcat version 1.0"]
    t0 = timeline[0]["start"]
    cursor, last = 0.0, None
    for ev in timeline:
        end = round(ev["end"] - t0, 6)
        dur = round(end - cursor, 6)
        if dur <= 0:
            continue          # zero-length event: no frame, and no shift either
        last = image_for(ev)
        lines.append(f"file '{last}'")
        lines.append(f"duration {dur:.6f}")
        cursor = end
    if last is None:
        raise CaptionError(
            f"caption timeline has no event with a positive duration "
            f"({len(timeline)} events spanning {span:.6f}s)")
    lines.append(f"file '{last}'")  # concat demuxer drops the final duration
    manifest = os.path.join(work_dir, "captions.ffconcat")
    with open(manifest, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    if abs(cursor - round(span, 6)) > GAP_EPS_S:
        raise CaptionError(
            f"manifest covers {cursor:.6f}s but the timeline spans {span:.6f}s")

    # The repeated trailing entry is what makes the LAST real event last its full
    # duration -- without it the concat demuxer drops that duration and the final
    # caption collapses to a single frame (measured: a 3.0s timeline encodes to
    # 2.333s / 70 frames). Giving that trailing entry its own `duration` does not
    # help; the demuxer ignores it (measured: still 4.067s / 122 frames). But the
    # trailing entry is not free either: at EOF the tail is held for roughly the
    # PREVIOUS event's duration rather than one frame, so the same 3.0s timeline
    # runs 4.067s / 122 frames -- a caption track a full second longer than the
    # audio it belongs to, which would silently survive into compositing.
    # So the span is clamped explicitly. `-t` is derived from the timeline rather
    # than passed in, so the track cannot disagree with the events it was built
    # from (measured with the clamp: 3.000s / 90 frames).
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", manifest,
           "-vf", f"fps={fps},format=rgba", "-c:v", "qtrle",
           "-t", f"{span:.6f}", out_path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise CaptionError(f"alpha track encode failed: {res.stderr[-400:]}")

    # Measure the artifact instead of trusting the inputs that produced it. `-t`
    # makes the LENGTH right by construction, which is exactly why it cannot be the
    # thing that certifies the encode: a track built from stale cached PNGs comes
    # out at the wrong resolution, and a track that lost its alpha channel comes out
    # fully opaque, and `-t` reports neither.
    video, fmt = _probe(out_path)
    measured = (int(video["width"]), int(video["height"]))
    if measured != size:
        raise CaptionError(
            f"alpha track measured {measured[0]}x{measured[1]} but {size[0]}x{size[1]} "
            f"was requested -- {work_dir!r} most likely holds frames rasterised at "
            f"another canvas size, and the concat stream took its size from those")
    if video.get("pix_fmt") not in ALPHA_PIX_FMTS:
        raise CaptionError(
            f"alpha track encoded as {video.get('pix_fmt')!r}, which carries no alpha "
            f"channel; overlaying it would hide the footage underneath")
    # Tolerance is the two quantisations that are genuinely out of our hands: the
    # 1/25s concat-demuxer grid and one output frame (`-t` truncates at the last
    # frame START, so the reported duration overshoots by up to one frame).
    tol = CONCAT_TB_S + 1.0 / float(fps) + 1e-3
    dur = float(fmt.get("duration", 0.0))
    if abs(dur - span) > tol:
        raise CaptionError(
            f"alpha track measured {dur:.6f}s against a {span:.6f}s timeline "
            f"(tolerance {tol:.6f}s)")
    return out_path
