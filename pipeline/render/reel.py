#!/usr/bin/env python3
"""
reel.py — turn a finished still into a Reel-shaped MP4.

    python render/reel.py in.jpg out.mp4 [--seconds 6.5]

WHY THIS EXISTS: Instagram and Facebook Reels are a VIDEO placement. A JPEG cannot be
posted as a Reel at all, so cross-posting the TikTok stills means wrapping each one in
a short video. Everything visual is already burned into the still by the Pillow
overlay, so this file adds no text and no layout decisions — it only animates.

THREE THINGS IT DOES THAT A NAIVE `-loop 1` WOULD NOT:

  1. A slow push (1.00 -> 1.06 scale). A totally static frame is the single clearest
     "this is a photo pretending to be a Reel" tell, and it reads as low effort to a
     viewer mid-scroll. The move is deliberately small: at 6.5s it is about 1% per
     second, which registers as alive rather than as a zoom.

  2. Zoom is computed at 3x resolution and scaled down. ffmpeg's zoompan snaps its
     crop origin to whole pixels, so zooming at output resolution produces a visible
     stutter every few frames. Oversampling makes each step sub-pixel at 1080 wide.

  3. A silent AAC track. Meta rejects or silently mangles video with no audio stream,
     and a Reel with no audio also loses the "original audio" attribution. Silence is
     a real stream; absent audio is not.

Output is H.264 High/yuv420p with +faststart, which is what both platforms want.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

W, H = 1080, 1920
FPS = 30
DEFAULT_SECONDS = 6.5
ZOOM_TO = 1.06
OVERSAMPLE = 3


class ReelError(RuntimeError):
    pass


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ReelError("ffmpeg not found on PATH")
    return exe


def build(still: str, out: str, seconds: float = DEFAULT_SECONDS,
          zoom_to: float = ZOOM_TO, verbose: bool = False) -> str:
    """Render `still` to a Reel-shaped MP4 at `out`. Returns the output path."""
    if not os.path.exists(still):
        raise ReelError(f"still not found: {still}")
    frames = max(1, int(round(seconds * FPS)))
    bigw, bigh = W * OVERSAMPLE, H * OVERSAMPLE

    # zoompan's `on` is the output frame index. Linear ramp to zoom_to over the clip,
    # clamped so the last frame cannot overshoot if rounding drifts.
    per_frame = (zoom_to - 1.0) / frames
    vf = (
        f"scale={bigw}:{bigh}:force_original_aspect_ratio=increase,"
        f"crop={bigw}:{bigh},scale=out_range=tv,"
        f"zoompan=z='min(1.0+{per_frame:.8f}*on,{zoom_to})'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={W}x{H}:fps={FPS},"
        f"format=yuv420p"
    )
    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", still,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", f"{seconds}",
        "-vf", vf,
        "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        "-movflags", "+faststart",
        out,
    ]
    if verbose:
        print("  " + " ".join(cmd[:6]) + " ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ReelError(f"ffmpeg failed: {(r.stderr or '').strip()[:400]}")
    verify(out, seconds)
    return out


def probe(path: str) -> dict:
    exe = shutil.which("ffprobe")
    if not exe:
        raise ReelError("ffprobe not found on PATH")
    def kv(*a):
        # key=value, NOT csv. ffprobe emits csv fields in stream-declaration order
        # rather than the order they were requested, so positional parsing silently
        # mislabels every field: codec_name landed in `width` and the verifier then
        # reported "expected 1080x1920, got h264x1080" on a perfectly good file.
        r = subprocess.run([exe, "-v", "error", *a, "-of",
                            "default=noprint_wrappers=1", path],
                           capture_output=True, text=True)
        out = {}
        for line in (r.stdout or "").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
        return out
    v = kv("-select_streams", "v:0", "-show_entries",
           "stream=width,height,codec_name,pix_fmt,r_frame_rate")
    a = kv("-select_streams", "a:0", "-show_entries", "stream=codec_name")
    d = kv("-show_entries", "format=duration")
    return {"width": v.get("width", ""), "height": v.get("height", ""),
            "vcodec": v.get("codec_name", ""), "pix_fmt": v.get("pix_fmt", ""),
            "fps": v.get("r_frame_rate", ""), "acodec": a.get("codec_name", ""),
            "duration": float(d.get("duration") or 0.0),
            "bytes": os.path.getsize(path)}


def verify(path: str, seconds: float) -> dict:
    """Assert the file is actually what both platforms require. A render that silently
    produced a 1-frame clip or dropped the audio stream would upload fine and fail or
    look broken only once it was live."""
    p = probe(path)
    problems = []
    if (p["width"], p["height"]) != (str(W), str(H)):
        problems.append(f"expected {W}x{H}, got {p['width']}x{p['height']}")
    if p["vcodec"] != "h264":
        problems.append(f"video codec {p['vcodec']!r}, expected h264")
    if p["pix_fmt"] not in ("yuv420p", "yuvj420p"):
        problems.append(f"pix_fmt {p['pix_fmt']!r}, expected yuv420p")
    if not p["acodec"]:
        problems.append("no audio stream (Meta needs one, even silent)")
    if abs(p["duration"] - seconds) > 0.6:
        problems.append(f"duration {p['duration']:.2f}s, expected ~{seconds}s")
    if p["bytes"] < 20_000:
        problems.append(f"suspiciously small file ({p['bytes']} bytes)")
    if problems:
        raise ReelError("reel verify failed: " + "; ".join(problems))
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("still")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    ap.add_argument("--zoom-to", type=float, default=ZOOM_TO)
    a = ap.parse_args(argv)
    p = build(a.still, a.out, a.seconds, a.zoom_to, verbose=True)
    info = probe(p)
    print(f"  {p}  {info['width']}x{info['height']} {info['vcodec']}/{info['acodec']} "
          f"{info['duration']:.2f}s {info['bytes']/1024:.0f}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
