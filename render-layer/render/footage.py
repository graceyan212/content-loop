"""footage.py — probe the clip library and choose windows from it."""
from __future__ import annotations
import glob, json, os, random, subprocess

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
MANIFEST_NAME = "manifest.json"

# Bumped whenever a recorded field changes MEANING rather than just appearing, so a
# manifest written by an older probe is re-measured instead of being read back with
# the wrong semantics. Schema 2: `duration_s` is the PICTURE duration; schema 1
# recorded `format.duration`, which on an a/v-skewed clip is the longer of the two
# streams (MEASURED on a 3.0s-video/3.2s-audio mp4: format.duration 3.200000,
# video stream 3.000000).
PROBE_SCHEMA = 2


class ProbeError(RuntimeError):
    pass


def picture_duration_s(video_stream: dict, fmt: dict) -> float | None:
    """How long the PICTURE lasts, resolved in one order for the whole pipeline.

    `format.duration` is the container's duration, i.e. the longest stream's, so on a
    clip whose audio outruns its video it is NOT how much picture exists. Trimming a
    window out of it yields a frozen last frame for the difference, and a/v skew of a
    couple of hundred milliseconds is ordinary in screen recordings.

    MEASURED on `testsrc duration=3` muxed with `sine duration=3.2`: video stream
    `duration` 3.000000 (nb_frames 90), audio stream 3.200000, `format.duration`
    3.200000. Selecting a 3.200s window off the container figure and then checking it
    against the video stream is a guaranteed abort, which is what this function
    exists to make impossible: `footage.probe_clip` (which decides what windows are
    available) and `compose.check_footage` (which asserts them before the encode)
    both resolve the duration THROUGH HERE, so they cannot disagree.

    "Cannot disagree" is a claim about the FIELD; it needs the STREAM to match too,
    and for a while it did not -- see `pick_video_stream`, which both callers now use
    to choose the stream this duration is read off.

    The video stream's own `duration` is preferred and `format.duration` is the
    fallback, because plenty of containers carry no per-stream duration at all
    (matroska/webm typically do not) -- in that case the container figure is the only
    measurement available and both sides use it, so they still agree.
    """
    for raw in ((video_stream or {}).get("duration"), (fmt or {}).get("duration")):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0.0:
            return v
    return None


def pick_video_stream(streams, what: str = "clip") -> dict:
    """The one stream that carries the PICTURE, chosen the same way everywhere.

    `picture_duration_s` promises that the probe which decides what windows exist
    and the compositor which asserts them "cannot disagree". That promise covers the
    FIELD they read; it only covers the STREAM as well if both pick the same one.
    They did not: this module skipped `disposition.attached_pic` streams while
    `compose._probe` took `next(s for s in streams if codec_type == 'video')`, so on
    a container whose cover art lands at index 0 the two would measure a 200x200
    still and a 640x360 clip respectively. That is the exact ordering
    `test_probe_ignores_leading_attached_pic_cover_art` stubs, because no locally
    available muxer writes it (mp4/mov sort attached pics last; this mkv build drops
    the disposition altogether), so the disagreement was unreachable rather than
    absent. Both callers now come through here.

    Attached pictures are cover art / poster frames muxed as a video stream, not
    footage. More than one real video stream is refused rather than resolved: taking
    "the first" would silently measure a stream that may not be the one a downstream
    filter picks.
    """
    all_video = [s for s in (streams or []) if s.get("codec_type") == "video"]
    picture = [s for s in all_video
               if not (s.get("disposition") or {}).get("attached_pic")]
    if not picture:
        if all_video:
            raise ProbeError(
                f"{what} has {len(all_video)} video stream(s) but every one of them "
                f"is disposed attached_pic (cover art), so there is no footage in it")
        raise ProbeError(f"no video stream in {what}")
    if len(picture) > 1:
        raise ProbeError(
            f"{what} has {len(picture)} video streams (excluding "
            f"attached pics) — ambiguous which is footage")
    return picture[0]


def _parse_fps(rate: str) -> float:
    num, _, den = (rate or "0/1").partition("/")
    try:
        d = float(den or 1) or 1.0
        return float(num) / d
    except ValueError:
        return 0.0


def probe_clip(path: str) -> dict:
    """Record what ffprobe MEASURED. Nothing here is taken on trust."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ProbeError(f"ffprobe failed on {path}: {e}")
    if res.returncode != 0:
        raise ProbeError(f"ffprobe rejected {path}: {res.stderr.strip()[:200]}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ProbeError(f"ffprobe output for {path} will not parse ({e})")
    streams = data.get("streams") or []
    # Attached pictures (cover art / thumbnails muxed as a video stream, e.g.
    # an mjpeg/png stream on an .mkv or .m4v) are not footage — ffprobe marks
    # them via disposition.attached_pic. `pick_video_stream` excludes those, and
    # `compose._probe` calls the SAME function, so the two modules cannot end up
    # measuring different streams of the same file.
    video = pick_video_stream(streams, path)
    fmt = data.get("format") or {}
    # `duration_s` is what `select_window` allocates windows out of, so it is the
    # PICTURE duration, not the container's. Both are recorded: their difference is
    # the a/v skew, and a manifest that only kept the number it used could not show
    # which of the two it was.
    duration = picture_duration_s(video, fmt)
    if duration is None:
        raise ProbeError(f"no duration reported for {path}")
    try:
        container = float(fmt.get("duration"))
    except (TypeError, ValueError):
        container = None
    return {
        "path": os.path.abspath(path),
        "duration_s": duration,
        "picture_duration_s": duration,
        "container_duration_s": container,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": _parse_fps(video.get("r_frame_rate", "")),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def probe_dir(footage_dir: str) -> dict:
    """Measure every clip in the directory. Reads only — nothing is written."""
    if not os.path.isdir(footage_dir):
        raise ProbeError(f"footage_dir does not exist: {footage_dir}")
    paths = sorted(p for p in glob.glob(os.path.join(footage_dir, "*"))
                   if p.lower().endswith(VIDEO_EXTS))
    clips, failed = [], []
    for p in paths:
        try:
            clips.append(probe_clip(p))
        except ProbeError as e:
            failed.append({"path": p, "error": str(e)[:200]})
    return {"schema": PROBE_SCHEMA, "clips": clips,
            "probed_count": len(clips), "failed": failed}


def _persist(footage_dir: str, manifest: dict) -> None:
    """Write the manifest, atomically.

    Via a temp file and `os.replace` because the write is now allowed to FAIL and be
    carried on from (see `_measure_without_requiring_a_write`): `open(path, "w")`
    truncates before it writes, so a failure part-way through -- a full disk, a
    disappearing mount -- would leave a truncated manifest.json behind, and the next
    `load_manifest` would refuse it with "will not parse". Nothing replaces the
    manifest until a complete one exists.
    """
    final = os.path.join(footage_dir, MANIFEST_NAME)
    tmp = f"{final}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            json.dump(manifest, fh, indent=2)
        os.replace(tmp, final)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ProbeError(f"could not write manifest into {footage_dir}: {e}")


def build_manifest(footage_dir: str) -> dict:
    """Measure and persist. A caller of THIS function asked for the write, so a
    directory it cannot write into is an error, not something to work around."""
    manifest = probe_dir(footage_dir)
    _persist(footage_dir, manifest)
    return manifest


def _measure_without_requiring_a_write(footage_dir: str) -> dict:
    """What `load_manifest` does when the manifest on disk is missing or stale.

    Persisting is best-effort here, deliberately. `load_manifest` is a READ of the
    clip library and it must not turn a readable footage directory into an abort:
    once the stale-schema re-probe was added, a schema-1 manifest sitting in a
    read-only directory (a mounted archive, a shared library owned by someone else)
    made a load that previously succeeded raise `could not write manifest into ...`
    instead of returning perfectly good measurements. The measurements are the point;
    the file is a cache of them. A failed write is recorded rather than swallowed, so
    a caller can see why the next load will pay for the probes again.
    """
    manifest = probe_dir(footage_dir)
    try:
        _persist(footage_dir, manifest)
    except ProbeError as e:
        manifest["manifest_write_error"] = str(e)
    return manifest


def load_manifest(footage_dir: str) -> dict:
    f = os.path.join(footage_dir, MANIFEST_NAME)
    if not os.path.isfile(f):
        return _measure_without_requiring_a_write(footage_dir)
    try:
        with open(f) as fh:
            m = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise ProbeError(f"{f} will not parse ({e})")
    # Clip records from an older schema carry durations that MEAN something else --
    # schema 1's `duration_s` was `format.duration`, so on an a/v-skewed clip it
    # over-reports the picture and every window selected from it aborts in
    # `compose.check_footage`. Re-probing is cheap (one ffprobe per clip) and the
    # alternative is a manifest that reads fine and fails every run. A manifest with
    # no clip records has nothing stale in it, so it is returned as found.
    if (m.get("clips") or []) and m.get("schema") != PROBE_SCHEMA:
        return _measure_without_requiring_a_write(footage_dir)
    return m


PORTRAIT_MAX_ASPECT = 1.0   # w/h below this is portrait or square -> crop to fill


class SelectionError(RuntimeError):
    pass


def layout_for(clip: dict) -> str:
    h = float(clip["height"]) or 1.0
    return "fill" if (float(clip["width"]) / h) <= PORTRAIT_MAX_ASPECT else "pillarbox"


def _free_starts(clip: dict, take: float, used) -> list[float]:
    """Candidate start offsets on a 1s grid that do not overlap a used window."""
    span = clip["duration_s"] - take
    if span < 0:
        return []
    blocked = [(u["start_s"], u["start_s"] + u["take_s"])
               for u in used if u.get("path") == clip["path"]]
    out = []
    step = 1.0
    t = 0.0
    while t <= span + 1e-9:
        if not any(t < b_end and (t + take) > b_start for b_start, b_end in blocked):
            out.append(round(t, 3))
        t += step
    return out


def select_window(manifest: dict, duration_s: float, run_id: str, used=()) -> dict:
    """Deterministic for a given run_id, so a resumed run picks the same footage."""
    clips = list(manifest.get("clips") or [])
    if not clips:
        raise SelectionError("footage library is empty — add clips and re-probe")
    rng = random.Random(f"{run_id}|{duration_s}")

    long_enough = [c for c in clips if c["duration_s"] >= duration_s]
    rng.shuffle(long_enough)
    for clip in long_enough:
        starts = _free_starts(clip, duration_s, used)
        if starts:
            return {"segments": [{"path": clip["path"],
                                  "start_s": rng.choice(starts),
                                  "take_s": round(duration_s, 3),
                                  "layout": layout_for(clip),
                                  "has_audio": bool(clip["has_audio"])}],
                    "total_s": round(duration_s, 3)}

    order = list(clips)
    rng.shuffle(order)
    segments, remaining, i = [], duration_s, 0
    max_iters = len(order) * 200
    while remaining > 1e-6:
        # Checked unconditionally, before any `continue` below, so a run of
        # skip-iterations (e.g. every clip's free offsets exhausted, or a
        # tail remainder no clip can currently serve) can never bypass this
        # bail-out and spin forever.
        if i >= max_iters:
            raise SelectionError("footage library too short to cover the story")
        clip = order[i % len(order)]
        i += 1
        take = min(clip["duration_s"], remaining)
        take_r = round(take, 3)
        if take_r <= 0:
            # Sub-millisecond float dust left over after rounding — not a
            # usable segment length, and remaining is effectively spent.
            continue
        # Respect `used` here too: the single-clip path above avoids
        # replaying a previously-used window via _free_starts, and this
        # fallback must offer the same guarantee rather than hard-coding
        # start_s to 0.0 regardless of what's already been shown.
        starts = _free_starts(clip, take, used)
        if not starts:
            continue
        segments.append({"path": clip["path"],
                         "start_s": rng.choice(starts),
                         "take_s": take_r, "layout": layout_for(clip),
                         "has_audio": bool(clip["has_audio"])})
        remaining -= take
    return {"segments": segments, "total_s": round(duration_s, 3)}
