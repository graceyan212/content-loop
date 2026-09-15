"""capability.py — assert the toolchain can do what the render path needs."""
from __future__ import annotations
import functools, os, subprocess

REQUIRED_FILTERS = ("overlay", "gblur", "scale", "crop", "concat",
                    "amix", "loudnorm", "anullsrc", "trim", "atrim",
                    # `compose.check_caption_track` measures the caption track's
                    # alpha channel through these three, and raises when it cannot.
                    # A build without them fails every render, so it is named here
                    # rather than discovered inside the compositor.
                    "alphaextract", "signalstats", "metadata")
REQUIRED_ENCODERS = ("qtrle", "libx264", "aac")
REQUIRED_FONTS = ("TikTokSans-Variable.ttf", "Anton-Regular.ttf")


class CapabilityError(SystemExit):
    pass


def _names(kind: str) -> set[str]:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", f"-{kind}"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise CapabilityError(f"FATAL: cannot run ffmpeg -{kind}: {e}")
    # Real entries start with exactly one space: " TS aap  AA->A  ...".
    # Legend and separator lines start with two ("  T.. = Timeline support"),
    # and the sink-filter legend line is " | = Source or sink filter".
    names = set()
    for line in out.splitlines():
        if not line.startswith(" ") or line.startswith("  "):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "|" and parts[1] != "=":
            names.add(parts[1])
    return names


@functools.lru_cache(maxsize=1)
def ffmpeg_filters() -> set[str]:
    return _names("filters")


@functools.lru_cache(maxsize=1)
def ffmpeg_encoders() -> set[str]:
    return _names("encoders")


def check(font_dir: str) -> None:
    missing = []
    filters, encoders = ffmpeg_filters(), ffmpeg_encoders()
    missing += [f"filter:{n}" for n in REQUIRED_FILTERS if n not in filters]
    missing += [f"encoder:{n}" for n in REQUIRED_ENCODERS if n not in encoders]
    missing += [f"font:{n}" for n in REQUIRED_FONTS
                if not os.path.isfile(os.path.join(font_dir, n))]
    if missing:
        raise CapabilityError("FATAL: toolchain missing " + ", ".join(missing))
