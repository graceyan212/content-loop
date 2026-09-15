#!/usr/bin/env python3
"""
compose.py — still photo + hook text -> a flattened 1080x1920 PNG/JPEG (TikTok
Photo post) and/or a silent 1080x1920 MP4 with a slow Ken Burns push.

`--format png` is the DEFAULT, because the live posts this pipeline feeds are
TikTok *Photo* posts. The PNG path is pure Pillow: cover-fit the still, alpha-
composite the overlay, flatten, save. **ffmpeg is never invoked for it** — not
even to probe — so it works on a machine with no ffmpeg at all.

The MP4 path is unchanged. The text is NOT drawn by ffmpeg: this ffmpeg build
has no libfreetype, so no `drawtext` filter (verify:
`ffmpeg -filters | grep -c drawtext` -> 0). overlay.py rasterizes the type to a
transparent RGBA PNG and ffmpeg composites it with `overlay`.

CLI:
  python compose.py --still path.png --text "..." --format png --out out.png
  python compose.py --still path.png --text "..." --format mp4 \
      --duration 8 --out out.mp4 [--safe-zone x,y,w,h] [--dramatization]

Python:
  from compose import compose
  compose(still="a.png", text="...", out="a.png")                  # png (default)
  compose(still="a.png", text="...", out="a.mp4", fmt="mp4")       # mp4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict, field

import overlay as ov

W, H = 1080, 1920
FPS = 30
SS = 2                      # supersample factor for the Ken Burns source plate
ZOOM_END = 1.08             # slow push: 1.00 -> 1.08 over the clip
MIN_DUR, MAX_DUR = 1.0, 30.0

# Output formats. `png` is the default: the account posts TikTok Photo posts, and
# photo-mode carousels measured +81% engagement / +82% likes vs video on TikTok
# (-33% shares). `both` emits the still AND the video from one overlay render.
FORMATS = ("png", "mp4", "both")
DEFAULT_FORMAT = "png"

# Some upload paths prefer JPEG, so the PNG arm also writes a q92 JPEG sibling.
JPEG_QUALITY = 92

# The style matching the live posts: centred white text, soft shadow, no bands.
# overlay.DEFAULT_STYLE is left at white-bar for overlay.py's own CLI; this is
# the default for everything composed through here.
DEFAULT_STYLE = "tiktok-native"

# Locked encode settings (identical for every render).
VENC = ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-preset", "medium", "-crf", "18", "-r", str(FPS), "-fps_mode", "cfr",
        "-movflags", "+faststart",
        # tag the stream as limited-range bt709 so players don't guess
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709"]


def _bin(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} not found on PATH")
    return p


def sh(cmd):
    """Run a command; raise with stderr attached (adapted from render_cogat_round_15.sh)."""
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("CMD FAILED: " + " ".join(map(str, cmd)) + "\n" + p.stderr[-4000:])
    return p


# ---------------------------------------------------------------------------
def build_filter(duration: float, zoom_end: float = ZOOM_END) -> str:
    """
    Cover-fit -> supersample -> zoompan Ken Burns -> composite the RGBA overlay.

    zoompan runs on a 2x plate so the integer x/y crop origin quantizes at half
    an output pixel; that is what keeps the push from visibly stair-stepping.
    """
    n = max(2, int(round(duration * FPS)))
    dz = zoom_end - 1.0
    return (
        f"[0:v]scale={W*SS}:{H*SS}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={W*SS}:{H*SS},setsar=1,format=rgb24,"
        f"zoompan=z='1+{dz:.6f}*on/{n-1}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={W}x{H}:fps={FPS}[bg];"
        f"[1:v]format=rgba[ov];"
        # composite in RGB (clean alpha, no chroma subsampling on the type), then
        # convert once to limited-range bt709 yuv420p -- without out_range=tv
        # libx264 tags the file yuvj420p (full range) and players wash it out.
        f"[bg][ov]overlay=0:0:format=rgb:eof_action=repeat,"
        f"scale=in_range=full:out_range=tv:out_color_matrix=bt709,"
        f"format=yuv420p[vout]"
    )


def build_command(still: str, overlay_png: str, duration: float, out: str,
                  zoom_end: float = ZOOM_END, ffmpeg: str | None = None) -> list:
    ffmpeg = ffmpeg or _bin("ffmpeg")
    return [
        ffmpeg, "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", still,
        "-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", overlay_png,
        "-filter_complex", build_filter(duration, zoom_end),
        "-map", "[vout]", "-an",
        *VENC,
        "-t", f"{duration:.3f}", out,
    ]


def probe(path: str) -> dict:
    ffprobe = _bin("ffprobe")
    p = sh([ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", path])
    j = json.loads(p.stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    a = [s for s in j["streams"] if s["codec_type"] == "audio"]
    return {
        "width": v and v["width"],
        "height": v and v["height"],
        "codec": v and v["codec_name"],
        "profile": v and v.get("profile"),
        "pix_fmt": v and v.get("pix_fmt"),
        "r_frame_rate": v and v.get("r_frame_rate"),
        "nb_frames": v and v.get("nb_frames"),
        "duration": float(j["format"]["duration"]),
        "audio_streams": len(a),
        "size_bytes": int(j["format"]["size"]),
    }


# ---------------------------------------------------------------------------
# still (PNG / JPEG) path — no ffmpeg, ever
# ---------------------------------------------------------------------------
def cover_fit(path: str, w: int = W, h: int = H):
    """
    Load `path` and cover-fit it to exactly w x h: scale up so both axes are
    covered (aspect preserved, Lanczos), then centre-crop the overflow.

    Same geometry as the ffmpeg arm's
    `scale=...:force_original_aspect_ratio=increase,crop=...`, minus the 2x
    supersample plate (which only exists to keep zoompan from stair-stepping —
    there is no zoom on a still, so it would be wasted work).
    """
    from PIL import Image
    img = Image.open(path)
    img = img.convert("RGB")
    s = max(w / img.width, h / img.height)
    nw, nh = max(w, int(round(img.width * s))), max(h, int(round(img.height * s)))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def render_still(still: str, overlay_png: str, out_png: str,
                 jpeg: bool = True, jpeg_quality: int = JPEG_QUALITY) -> dict:
    """
    Composite the overlay RGBA onto the cover-fit still and flatten to a
    1080x1920 PNG (+ an optional q92 JPEG sibling).

    Returns {"png", "png_bytes", "jpg", "jpg_bytes", "size"}. No ffmpeg is run
    and no ffprobe is needed: Pillow already knows the exact pixel dimensions.
    """
    from PIL import Image
    bg = cover_fit(still).convert("RGBA")
    ov_img = Image.open(overlay_png).convert("RGBA")
    if ov_img.size != (W, H):
        raise RuntimeError(f"overlay {overlay_png} is {ov_img.size}, expected {(W, H)}")
    bg.alpha_composite(ov_img)
    flat = bg.convert("RGB")                      # flatten: no alpha in the output
    if flat.size != (W, H):
        raise RuntimeError(f"composited still is {flat.size}, expected {(W, H)}")

    os.makedirs(os.path.dirname(os.path.abspath(out_png)) or ".", exist_ok=True)
    flat.save(out_png, format="PNG", optimize=True)
    res = {"png": os.path.abspath(out_png),
           "png_bytes": os.path.getsize(out_png),
           "jpg": None, "jpg_bytes": None,
           "size": [flat.width, flat.height]}
    if jpeg:
        out_jpg = os.path.splitext(out_png)[0] + ".jpg"
        flat.save(out_jpg, format="JPEG", quality=jpeg_quality,
                  subsampling=0, optimize=True, progressive=True)
        res["jpg"] = os.path.abspath(out_jpg)
        res["jpg_bytes"] = os.path.getsize(out_jpg)
    return res


@dataclass
class ComposeResult:
    out: str
    overlay_png: str
    ffmpeg_cmd: str
    duration: float
    style: str
    fmt: str = DEFAULT_FORMAT
    png: str | None = None
    jpg: str | None = None
    png_bytes: int | None = None
    jpg_bytes: int | None = None
    mp4: str | None = None
    overlay: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
def compose(still: str,
            out: str,
            text: str | None = None,
            style: str = DEFAULT_STYLE,
            text_color: str = "light",
            screen_style: str = "headline",
            duration: float = 8.0,
            fmt: str = DEFAULT_FORMAT,
            jpeg: bool = True,
            overlay_png: str | None = None,
            safe_zone: dict | None = None,
            dramatization: bool = False,
            dramatization_corner: str = "bottom-left",
            nb_color: str = "yellow",
            notes_meta: str = "Today at 9:41 AM",
            valign: str | None = None,
            zoom_end: float = ZOOM_END,
            keep_overlay: bool = True,
            verify: bool = True) -> ComposeResult:
    """
    Render a 1080x1920 still (PNG + JPEG) and/or a silent 1080x1920 MP4 from a
    still + hook text.

    `fmt` is one of png | mp4 | both (default png). `out`'s extension is
    ignored for choosing what gets written — the stem is reused, so
    out="v01.mp4" with fmt="png" writes v01.png / v01.jpg.

    Either pass `text` (overlay.py renders the PNG) or a pre-made `overlay_png`.
    Returns a ComposeResult with the file sizes, and — for the mp4 arms — the
    exact ffmpeg command and an ffprobe report.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS}")
    want_png = fmt in ("png", "both")
    want_mp4 = fmt in ("mp4", "both")

    still = os.path.abspath(os.path.expanduser(still))
    out = os.path.abspath(os.path.expanduser(out))
    if not os.path.isfile(still):
        raise FileNotFoundError(f"still not found: {still}")
    if want_mp4 and not (MIN_DUR <= duration <= MAX_DUR):
        raise ValueError(f"duration {duration} out of range [{MIN_DUR}, {MAX_DUR}]")
    if not text and not overlay_png:
        raise ValueError("pass --text (or text=) or --overlay-png")

    stem = os.path.splitext(out)[0]
    png_out = stem + ".png"
    mp4_out = stem + ".mp4"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    ov_meta = {}
    tmp_overlay = None
    if overlay_png:
        overlay_png = os.path.abspath(os.path.expanduser(overlay_png))
        if not os.path.isfile(overlay_png):
            raise FileNotFoundError(f"overlay png not found: {overlay_png}")
    else:
        if keep_overlay:
            overlay_png = stem + ".overlay.png"
        else:
            fd, overlay_png = tempfile.mkstemp(suffix=".png", prefix="hookov_")
            os.close(fd)
            tmp_overlay = overlay_png
        r = ov.render_overlay(text, overlay_png, style=style, safe_zone=safe_zone,
                          text_color=text_color, screen_style=screen_style,
                              dramatization=dramatization,
                              dramatization_corner=dramatization_corner,
                              nb_color=nb_color, notes_meta=notes_meta, valign=valign)
        ov_meta = asdict(r)
        if not r.fits_safe_zone:
            print(f"WARNING: rendered text bbox {r.bbox} escapes safe zone {r.safe_zone}",
                  file=sys.stderr)

    # -- still arm: pure Pillow, ffmpeg is not touched at all -----------------
    still_res = {}
    if want_png:
        still_res = render_still(still, overlay_png, png_out, jpeg=jpeg)

    # -- video arm: unchanged ------------------------------------------------
    cmd = []
    pr = {}
    if want_mp4:
        cmd = build_command(still, overlay_png, duration, mp4_out, zoom_end=zoom_end)
        sh(cmd)

        pr = probe(mp4_out) if verify else {}
        if verify:
            problems = []
            if (pr["width"], pr["height"]) != (W, H):
                problems.append(f"dimensions {pr['width']}x{pr['height']} != {W}x{H}")
            if pr["audio_streams"] != 0:
                problems.append(f"{pr['audio_streams']} audio stream(s) present; expected 0")
            if abs(pr["duration"] - duration) > 0.2:
                problems.append(f"duration {pr['duration']:.3f}s != requested {duration}s")
            if pr["pix_fmt"] != "yuv420p":
                problems.append(f"pix_fmt {pr['pix_fmt']} != yuv420p")
            if problems:
                raise RuntimeError("output failed verification: " + "; ".join(problems))

    if tmp_overlay:
        try:
            os.unlink(tmp_overlay)
        except OSError:
            pass

    primary = still_res.get("png") if want_png else mp4_out
    return ComposeResult(out=primary, overlay_png=overlay_png,
                         ffmpeg_cmd=" ".join(cmd), duration=duration, style=style,
                         fmt=fmt,
                         png=still_res.get("png"), jpg=still_res.get("jpg"),
                         png_bytes=still_res.get("png_bytes"),
                         jpg_bytes=still_res.get("jpg_bytes"),
                         mp4=mp4_out if want_mp4 else None,
                         overlay=ov_meta, probe=pr)


def make_placeholder_still(path: str, top=(24, 30, 48), bottom=(96, 60, 52)) -> str:
    """Vertical-gradient 1080x1920 PNG, for testing when no real photo exists yet."""
    from PIL import Image
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(round(top[i] + (bottom[i] - top[i]) * t)) for i in range(3))
        for x in range(W):
            px[x, y] = c
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    img.save(path)
    return os.path.abspath(path)


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Still photo + hook text -> 1080x1920 PNG/JPEG (default) "
                    "and/or a silent 1080x1920 MP4 with Ken Burns.")
    ap.add_argument("--make-placeholder", metavar="PATH", default=None,
                    help="write a 1080x1920 gradient placeholder PNG and exit "
                         "(for testing before real stills exist)")
    if argv is None:
        argv = sys.argv[1:]
    if "--make-placeholder" in argv:
        i = argv.index("--make-placeholder")
        if i + 1 >= len(argv):
            ap.error("--make-placeholder needs a PATH")
        print(make_placeholder_still(argv[i + 1]))
        return 0
    ap.add_argument("--still", required=True)
    ap.add_argument("--text", default=None)
    ap.add_argument("--overlay-png", default=None,
                    help="use a pre-rendered transparent 1080x1920 PNG instead of --text")
    ap.add_argument("--style", default=DEFAULT_STYLE, choices=ov.STYLES)
    ap.add_argument("--text-color", default="light",
                    choices=["light", "dark", "white", "yellow"])
    ap.add_argument("--screen-style", default="headline",
                    choices=["headline", "confessional"])
    ap.add_argument("--format", dest="fmt", default=DEFAULT_FORMAT, choices=FORMATS,
                    help="png (default; TikTok Photo post, no ffmpeg) | mp4 | both")
    ap.add_argument("--no-jpeg", action="store_true",
                    help="skip the q92 JPEG sibling on the png arm")
    ap.add_argument("--duration", type=float, default=8.0)
    ap.add_argument("--out", required=True,
                    help="output path; the extension is replaced per --format "
                         "(.png/.jpg and/or .mp4 off the same stem)")
    ap.add_argument("--safe-zone", default=None, help="x,y,w,h (default 72,320,936,1120)")
    ap.add_argument("--dramatization", action="store_true")
    ap.add_argument("--dramatization-corner", default="bottom-left",
                    choices=["bottom-left", "bottom-right", "top-left", "top-right"])
    ap.add_argument("--nb-color", default="yellow", choices=sorted(ov.TOKENS))
    ap.add_argument("--notes-meta", default="Today at 9:41 AM")
    ap.add_argument("--valign", default=None, choices=["top", "center", "bottom"])
    ap.add_argument("--zoom-end", type=float, default=ZOOM_END)
    ap.add_argument("--no-keep-overlay", action="store_true",
                    help="delete the intermediate overlay PNG")
    ap.add_argument("--print-cmd", action="store_true", help="print the ffmpeg command only")
    a = ap.parse_args(argv)

    r = compose(still=a.still, out=a.out, text=a.text, style=a.style, duration=a.duration,
                text_color=a.text_color, screen_style=a.screen_style,
                fmt=a.fmt, jpeg=not a.no_jpeg,
                overlay_png=a.overlay_png, safe_zone=ov.parse_safe_zone(a.safe_zone),
                dramatization=a.dramatization,
                dramatization_corner=a.dramatization_corner,
                nb_color=a.nb_color, notes_meta=a.notes_meta, valign=a.valign,
                zoom_end=a.zoom_end,
                keep_overlay=not a.no_keep_overlay)
    if a.print_cmd:
        print(r.ffmpeg_cmd or "(no ffmpeg command — --format png does not use ffmpeg)")
    else:
        print(json.dumps(asdict(r), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
