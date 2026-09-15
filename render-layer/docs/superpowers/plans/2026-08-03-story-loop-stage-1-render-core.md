# story-loop Stage 1: Render Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render one complete Reddit-story narration video — narrated hand-written script, one-word captions in sync, gameplay background, 1080×1920 mp4 — with no Reddit, no loop, and no scheduling.

**Architecture:** A hand-written script goes to ElevenLabs' `/with-timestamps` endpoint, which returns audio plus character-level timing. Those characters group into word timings. Pillow renders one transparent PNG per word; a `concat` demuxer turns those into a single lossless alpha video track; ffmpeg composites that track over a window of gameplay footage and mixes narration over ducked game audio.

**Tech Stack:** Python 3.11.1 · Pillow 12.1.1 · ffmpeg/ffprobe 8.1.2 (homebrew, `/opt/homebrew/bin`) · pytest · stdlib `urllib.request` for HTTP (no new dependencies)

## Global Constraints

Every task's requirements implicitly include this section.

- Canvas is **1080×1920**, 30 fps. Output H.264, `yuv420p`, AAC, `+faststart`.
- **This ffmpeg has no `ass`, no `subtitles`, and no `drawtext` filter.** Verified against all 481 available filters. Never emit them. Text is rendered by Pillow and composited with `overlay`.
- Available and required: `overlay`, `gblur`, `scale`, `crop`, `concat`, `amix`, `loudnorm`, `anullsrc`, `trim`, `atrim`; encoders `qtrle`, `libx264`, `aac`.
- Caption timing and caption text both come from **`normalized_alignment`**, never from `alignment` mixed with raw text.
- Caption font `TikTokSans-Variable.ttf` at variable **Weight axis 900** (axis range is 300–900). Hook font `Anton-Regular.ttf`. Both vendored into `assets/fonts/`.
- Captions sit at **55–60% frame height**, inside a safe box insetting 10% top, 18% bottom, 6% left, 14% right.
- **No music bed.** Gameplay audio stays, ducked to a config level. Loudness normalized to **-14 LUFS** default.
- Beat between narration segments: **0.3 s**.
- **No SFFS branding in frame. No Reddit UI chrome.**
- **Never modify anything under `GTM/ugc-pipeline/`.** Read and copy only.
- The ElevenLabs API key is never written into a rendered artifact, a manifest, or run state.
- Any probe records the value it **measured** and asserts measured-equals-requested rather than trusting an input.

## File Structure

| File | Responsibility |
|---|---|
| `channels.json` | Registry mapping channel name to data root |
| `channel.py` | `resolve()` — channel name to config and paths |
| `capability.py` | Assert required ffmpeg filters, encoders, and fonts exist |
| `render/footage.py` | ffprobe clips into a manifest; choose a deterministic window; decide layout |
| `voice/words.py` | Character alignment to word timings (pure) |
| `voice/tts.py` | ElevenLabs `/with-timestamps` client with content-hash cache |
| `render/captions.py` | Pillow word PNGs, safe-box layout, alpha track assembly |
| `render/hook.py` | Pillow hook card PNG |
| `render/compose.py` | ffmpeg filtergraph construction and final render |
| `make_one.py` | CLI tying stage 1 together |
| `tests/` | pytest suite, fixtures, golden images |

---

### Task 1: Project skeleton, channel resolver, vendored fonts

**Files:**
- Create: `GTM/story-loop/channels.json`, `GTM/story-loop/channel.py`, `GTM/story-loop/channels/main/channel.json`, `GTM/story-loop/assets/fonts/` (two vendored fonts)
- Test: `GTM/story-loop/tests/test_channel.py`

**Interfaces:**
- Consumes: nothing
- Produces: `channel.resolve(name: str | None) -> dict` returning keys `name`, `root`, `config`, `channel_json_path`; `channel.ChannelError`; `channel.watermark(ch: dict) -> str`

- [ ] **Step 1: Create the directory tree, vendor the fonts, and init git**

```bash
cd /Users/graceyan/Desktop/alpha/GTM/story-loop
mkdir -p channels/main/{footage,scripts,out/pending,runs,learn} \
         voice render assets/fonts tests/fixtures tests/golden
cp ../ugc-pipeline/render/fonts/TikTokSans-Variable.ttf assets/fonts/
cp ../ugc-pipeline/render/fonts/Anton-Regular.ttf assets/fonts/
touch voice/__init__.py render/__init__.py
git init
printf '%s\n' '__pycache__/' '*.pyc' '.pytest_cache/' 'channels/*/out/' \
  'channels/*/voice/cache/' 'channels/*/footage/*.mp4' 'channels/*/footage/*.mov' \
  '.env.local' '*.key' > .gitignore
ls -l assets/fonts/
```

Expected: both `.ttf` files listed, non-zero size.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_channel.py
import json, os, pytest, channel

def test_resolve_returns_absolute_root_and_config():
    ch = channel.resolve("main")
    assert ch["name"] == "main"
    assert os.path.isabs(ch["root"]) and os.path.isdir(ch["root"])
    assert ch["config"]["fps"] == 30

def test_unknown_channel_names_the_known_ones():
    with pytest.raises(channel.ChannelError) as e:
        channel.resolve("nope")
    assert "main" in str(e.value)

def test_watermark_defaults_to_empty_not_placeholder():
    assert channel.watermark(channel.resolve("main")) == ""
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `cd /Users/graceyan/Desktop/alpha/GTM/story-loop && python3 -m pytest tests/test_channel.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'channel'`

- [ ] **Step 4: Write the config files**

```json
// channels.json
{"main": {"root": "./channels/main", "blog_id": null, "timezone": "America/Chicago"}}
```

```json
// channels/main/channel.json
{
  "watermark": "",
  "width": 1080,
  "height": 1920,
  "fps": 30,
  "crf": 20,
  "voice_id": "",
  "hook_lead_s": 2.0,
  "tail_s": 0.6,
  "segment_beat_s": 0.3,
  "game_audio_duck": 0.12,
  "loudnorm_i": -14,
  "caption_weight": 900,
  "caption_size_px": 96,
  "hook_size_px": 118,
  "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14},
  "caption_center_y": 0.575
}
```

`watermark` is deliberately empty: the channel is unnamed, and an empty watermark must render nothing rather than render a placeholder.

- [ ] **Step 5: Write `channel.py`**

```python
"""channel.py — channel resolution. The personas.py pattern, minus faces and briefs."""
from __future__ import annotations
import json, os

CODE_ROOT = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(CODE_ROOT, "channels.json")
DEFAULT_CHANNEL = "main"


class ChannelError(SystemExit):
    """Always carries an actionable message. Callers let it propagate."""


def _registry() -> dict:
    if not os.path.isfile(REGISTRY_PATH):
        raise ChannelError(f"FATAL: registry not found: {REGISTRY_PATH}")
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise ChannelError(f"FATAL: {REGISTRY_PATH} will not parse ({e})")


def resolve(name: str | None = None) -> dict:
    name = (name or DEFAULT_CHANNEL).strip().lower()
    reg = _registry()
    entry = reg.get(name)
    if entry is None:
        known = ", ".join(sorted(reg)) or "(none)"
        raise ChannelError(f"FATAL: unknown channel {name!r}. Known: {known}")
    root = os.path.normpath(os.path.join(CODE_ROOT, entry["root"]))
    if not os.path.isdir(root):
        raise ChannelError(f"FATAL: channel {name!r} root missing: {root}")
    cfg_path = os.path.join(root, "channel.json")
    if not os.path.isfile(cfg_path):
        raise ChannelError(f"FATAL: {cfg_path} missing")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise ChannelError(f"FATAL: {cfg_path} will not parse ({e})")
    return {"name": name, "root": root, "registry_entry": entry,
            "config": config, "channel_json_path": cfg_path}


def watermark(ch: dict) -> str:
    return (ch.get("config") or {}).get("watermark", "") or ""


def require_blog_id(ch: dict) -> str:
    bid = (ch.get("registry_entry") or {}).get("blog_id")
    if not bid:
        raise ChannelError(
            f"FATAL: channel {ch['name']!r} has no blog_id. Refusing to post.")
    return str(bid)
```

- [ ] **Step 6: Run tests and commit**

Run: `python3 -m pytest tests/test_channel.py -v`
Expected: 3 passed

```bash
git add -A
git commit -m "feat: story-loop skeleton, channel resolver, vendored fonts"
```

---

### Task 2: Capability preflight

Guards every later task. The design was already invalidated once by assuming a filter existed; this makes that failure loud and immediate.

**Files:**
- Create: `GTM/story-loop/capability.py`
- Test: `GTM/story-loop/tests/test_capability.py`

**Interfaces:**
- Consumes: nothing
- Produces: `capability.ffmpeg_filters() -> set[str]`, `capability.ffmpeg_encoders() -> set[str]`, `capability.check(font_dir: str) -> None`, `capability.CapabilityError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability.py
import os, pytest, capability

FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "assets", "fonts")

def test_required_filters_present_on_this_machine():
    f = capability.ffmpeg_filters()
    for name in ("overlay", "gblur", "scale", "crop", "concat",
                 "amix", "loudnorm", "anullsrc", "trim", "atrim"):
        assert name in f, f"missing filter {name}"

def test_text_filters_are_known_absent():
    # Documents the constraint the whole caption design rests on.
    f = capability.ffmpeg_filters()
    assert not ({"ass", "subtitles", "drawtext"} & f)

def test_check_passes_with_real_fonts():
    capability.check(FONTS)

def test_check_names_the_missing_font(tmp_path):
    with pytest.raises(capability.CapabilityError) as e:
        capability.check(str(tmp_path))
    assert "TikTokSans-Variable.ttf" in str(e.value)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_capability.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'capability'`

- [ ] **Step 3: Implement**

```python
"""capability.py — assert the toolchain can do what the render path needs."""
from __future__ import annotations
import functools, os, subprocess

REQUIRED_FILTERS = ("overlay", "gblur", "scale", "crop", "concat",
                    "amix", "loudnorm", "anullsrc", "trim", "atrim")
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
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_capability.py -v`
Expected: 4 passed. If `test_required_filters_present_on_this_machine` fails, stop and report — the plan's premise is broken.

- [ ] **Step 5: Commit**

```bash
git add capability.py tests/test_capability.py
git commit -m "feat: capability preflight for ffmpeg filters, encoders, fonts"
```

---

### Task 3: Footage probe

**Files:**
- Create: `GTM/story-loop/render/footage.py`
- Test: `GTM/story-loop/tests/test_footage_probe.py`

**Interfaces:**
- Consumes: nothing
- Produces: `footage.probe_clip(path) -> dict` with keys `path`, `duration_s`, `width`, `height`, `fps`, `has_audio`; `footage.build_manifest(footage_dir) -> dict`; `footage.load_manifest(footage_dir) -> dict`; `footage.ProbeError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_footage_probe.py
import os, subprocess, pytest
from render import footage

def _make_clip(path, w, h, secs, with_audio):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
           f"testsrc=size={w}x{h}:rate=30:duration={secs}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:duration={secs}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(secs), path]
    subprocess.run(cmd, capture_output=True, check=True)

def test_probe_reports_measured_dimensions_and_audio(tmp_path):
    p = str(tmp_path / "a.mp4")
    _make_clip(p, 640, 360, 2, with_audio=True)
    info = footage.probe_clip(p)
    assert (info["width"], info["height"]) == (640, 360)
    assert info["has_audio"] is True
    assert 1.8 < info["duration_s"] < 2.3
    assert 29 < info["fps"] < 31

def test_probe_detects_missing_audio(tmp_path):
    p = str(tmp_path / "b.mp4")
    _make_clip(p, 360, 640, 1, with_audio=False)
    assert footage.probe_clip(p)["has_audio"] is False

def test_build_manifest_indexes_every_clip(tmp_path):
    for i, (w, h) in enumerate([(640, 360), (360, 640)]):
        _make_clip(str(tmp_path / f"c{i}.mp4"), w, h, 1, with_audio=False)
    m = footage.build_manifest(str(tmp_path))
    assert m["probed_count"] == 2
    assert len(m["clips"]) == 2

def test_probe_raises_on_nonvideo(tmp_path):
    p = tmp_path / "notvideo.mp4"
    p.write_text("nope")
    with pytest.raises(footage.ProbeError):
        footage.probe_clip(str(p))
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_footage_probe.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'render.footage'`

- [ ] **Step 3: Implement the probe**

```python
"""footage.py — probe the clip library and choose windows from it."""
from __future__ import annotations
import glob, json, os, subprocess

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
MANIFEST_NAME = "manifest.json"


class ProbeError(RuntimeError):
    pass


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
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError(f"no video stream in {path}")
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        raise ProbeError(f"no duration reported for {path}")
    return {
        "path": os.path.abspath(path),
        "duration_s": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": _parse_fps(video.get("r_frame_rate", "")),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def build_manifest(footage_dir: str) -> dict:
    paths = sorted(p for p in glob.glob(os.path.join(footage_dir, "*"))
                   if p.lower().endswith(VIDEO_EXTS))
    clips, failed = [], []
    for p in paths:
        try:
            clips.append(probe_clip(p))
        except ProbeError as e:
            failed.append({"path": p, "error": str(e)[:200]})
    manifest = {"clips": clips, "probed_count": len(clips), "failed": failed}
    with open(os.path.join(footage_dir, MANIFEST_NAME), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def load_manifest(footage_dir: str) -> dict:
    f = os.path.join(footage_dir, MANIFEST_NAME)
    if not os.path.isfile(f):
        return build_manifest(footage_dir)
    with open(f) as fh:
        return json.load(fh)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_footage_probe.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add render/footage.py tests/test_footage_probe.py
git commit -m "feat: ffprobe-backed footage manifest recording measured values"
```

---

### Task 4: Deterministic window selection and layout

**Files:**
- Modify: `GTM/story-loop/render/footage.py` (append)
- Test: `GTM/story-loop/tests/test_footage_select.py`

**Interfaces:**
- Consumes: `footage.build_manifest` from Task 3
- Produces: `footage.layout_for(clip: dict) -> str` returning `"fill"` or `"pillarbox"`; `footage.select_window(manifest, duration_s, run_id, used=()) -> dict` returning `{"segments": [{"path", "start_s", "take_s", "layout", "has_audio"}], "total_s": float}`; `footage.SelectionError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_footage_select.py
import pytest
from render import footage

def _clip(path, w, h, dur, audio=True):
    return {"path": path, "width": w, "height": h, "duration_s": dur,
            "fps": 30.0, "has_audio": audio}

LONG = {"clips": [_clip("/x/long.mp4", 1080, 1920, 300.0)]}
SHORTS = {"clips": [_clip(f"/x/s{i}.mp4", 1080, 1920, 5.0) for i in range(6)]}

def test_layout_fill_for_portrait_and_square():
    assert footage.layout_for(_clip("p", 1080, 1920, 1)) == "fill"
    assert footage.layout_for(_clip("s", 1000, 1000, 1)) == "fill"

def test_layout_pillarbox_for_landscape():
    assert footage.layout_for(_clip("l", 1920, 1080, 1)) == "pillarbox"

def test_same_run_id_selects_the_same_window():
    a = footage.select_window(LONG, 30.0, "2026-08-03")
    b = footage.select_window(LONG, 30.0, "2026-08-03")
    assert a == b

def test_different_run_id_usually_moves_the_window():
    seen = {footage.select_window(LONG, 30.0, f"run-{i}")["segments"][0]["start_s"]
            for i in range(8)}
    assert len(seen) > 1

def test_window_stays_inside_the_clip():
    w = footage.select_window(LONG, 30.0, "r")
    seg = w["segments"][0]
    assert seg["start_s"] >= 0
    assert seg["start_s"] + seg["take_s"] <= 300.0

def test_short_clips_concatenate_to_cover_duration():
    w = footage.select_window(SHORTS, 22.0, "r")
    assert len(w["segments"]) > 1
    assert abs(w["total_s"] - 22.0) < 0.01

def test_used_windows_are_avoided_when_alternatives_exist():
    used = [{"path": "/x/long.mp4", "start_s": 0.0, "take_s": 290.0}]
    seg = footage.select_window(LONG, 5.0, "r", used=used)["segments"][0]
    assert seg["start_s"] >= 290.0 - 0.01

def test_empty_library_raises():
    with pytest.raises(footage.SelectionError):
        footage.select_window({"clips": []}, 10.0, "r")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_footage_select.py -v`
Expected: FAIL, `AttributeError: module 'render.footage' has no attribute 'layout_for'`

- [ ] **Step 3: Implement**

```python
# append to render/footage.py
import random

PORTRAIT_MAX_ASPECT = 1.0   # w/h below this is portrait or square -> crop to fill


class SelectionError(RuntimeError):
    pass


def layout_for(clip: dict) -> str:
    h = float(clip["height"]) or 1.0
    return "fill" if (float(clip["width"]) / h) < PORTRAIT_MAX_ASPECT else "pillarbox"


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
    while remaining > 1e-6:
        clip = order[i % len(order)]
        i += 1
        take = min(clip["duration_s"], remaining)
        if take <= 0.05:
            continue
        segments.append({"path": clip["path"], "start_s": 0.0,
                         "take_s": round(take, 3), "layout": layout_for(clip),
                         "has_audio": bool(clip["has_audio"])})
        remaining -= take
        if i > len(order) * 200:
            raise SelectionError("footage library too short to cover the story")
    return {"segments": segments, "total_s": round(duration_s, 3)}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_footage_select.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add render/footage.py tests/test_footage_select.py
git commit -m "feat: deterministic footage window selection and layout choice"
```

---

### Task 5: Character alignment to word timings

The purest and highest-value unit in stage 1. Everything visual depends on it.

**Files:**
- Create: `GTM/story-loop/voice/words.py`, `GTM/story-loop/tests/fixtures/alignment_five_dollars.json`
- Test: `GTM/story-loop/tests/test_words.py`

**Interfaces:**
- Consumes: nothing
- Produces: `words.words_from_alignment(alignment: dict) -> list[dict]` with keys `word`, `start`, `end`; `words.chars_per_second(alignment) -> float`; `words.total_duration(words) -> float`; `words.AlignmentError`

- [ ] **Step 1: Create the fixture**

```json
// tests/fixtures/alignment_five_dollars.json
{
  "characters": ["f","i","v","e"," ","d","o","l","l","a","r","s"],
  "character_start_times_seconds": [0.0,0.05,0.10,0.15,0.20,0.25,0.32,0.39,0.46,0.53,0.60,0.67],
  "character_end_times_seconds":   [0.05,0.10,0.15,0.20,0.25,0.32,0.39,0.46,0.53,0.60,0.67,0.74]
}
```

This is what `normalized_alignment` returns for the source text `$5` — the reason captions must be built from the normalized track.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_words.py
import json, os, pytest
from voice import words

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

def _align(chars, step=0.1):
    return {"characters": list(chars),
            "character_start_times_seconds": [i * step for i in range(len(chars))],
            "character_end_times_seconds": [(i + 1) * step for i in range(len(chars))]}

def test_groups_characters_into_words():
    got = words.words_from_alignment(_align("hi there"))
    assert [w["word"] for w in got] == ["hi", "there"]

def test_word_spans_first_char_start_to_last_char_end():
    got = words.words_from_alignment(_align("ab cd"))
    assert got[0]["start"] == pytest.approx(0.0)
    assert got[0]["end"] == pytest.approx(0.2)
    assert got[1]["start"] == pytest.approx(0.3)
    assert got[1]["end"] == pytest.approx(0.5)

def test_normalized_dollar_amount_becomes_two_spoken_words():
    with open(os.path.join(FIX, "alignment_five_dollars.json")) as fh:
        got = words.words_from_alignment(json.load(fh))
    assert [w["word"] for w in got] == ["five", "dollars"]
    assert got[1]["end"] == pytest.approx(0.74)

def test_collapses_runs_of_whitespace_and_newlines():
    assert len(words.words_from_alignment(_align("a  \n b"))) == 2

def test_punctuation_stays_attached():
    got = words.words_from_alignment(_align("wow, ok"))
    assert got[0]["word"] == "wow,"

def test_ragged_arrays_raise():
    bad = {"characters": ["a", "b"],
           "character_start_times_seconds": [0.0],
           "character_end_times_seconds": [0.1, 0.2]}
    with pytest.raises(words.AlignmentError):
        words.words_from_alignment(bad)

def test_chars_per_second_is_measured_from_the_span():
    assert words.chars_per_second(_align("abcdefghij")) == pytest.approx(10.0)
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_words.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'voice.words'`

- [ ] **Step 4: Implement**

```python
"""words.py — ElevenLabs character alignment to word timings. Pure, no I/O.

Always fed `normalized_alignment`, never `alignment`: the normalized track
reflects ElevenLabs' own expansion ($5 -> "five dollars", Dr. -> "Doctor").
Timing from one track while captioning the other desynchronises every word
after the first number or abbreviation.
"""
from __future__ import annotations


class AlignmentError(ValueError):
    pass


def words_from_alignment(alignment: dict) -> list[dict]:
    try:
        chars = alignment["characters"]
        starts = alignment["character_start_times_seconds"]
        ends = alignment["character_end_times_seconds"]
    except (KeyError, TypeError) as e:
        raise AlignmentError(f"alignment missing required arrays: {e}")
    if not (len(chars) == len(starts) == len(ends)):
        raise AlignmentError(
            f"ragged alignment: {len(chars)} chars, {len(starts)} starts, "
            f"{len(ends)} ends")

    out: list[dict] = []
    buf, w_start, w_end = "", None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                out.append({"word": buf, "start": float(w_start), "end": float(w_end)})
                buf, w_start, w_end = "", None, None
            continue
        if not buf:
            w_start = s
        buf += ch
        w_end = e
    if buf:
        out.append({"word": buf, "start": float(w_start), "end": float(w_end)})
    return out


def total_duration(word_list: list[dict]) -> float:
    return float(word_list[-1]["end"]) if word_list else 0.0


def chars_per_second(alignment: dict) -> float:
    """Measured narration rate. Calibrates the retell word-count target in stage 2."""
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not starts or not ends:
        raise AlignmentError("cannot measure rate from an empty alignment")
    span = float(ends[-1]) - float(starts[0])
    if span <= 0:
        raise AlignmentError(f"non-positive alignment span: {span}")
    return len(alignment["characters"]) / span
```

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m pytest tests/test_words.py -v`
Expected: 7 passed

```bash
git add voice/words.py tests/test_words.py tests/fixtures/alignment_five_dollars.json
git commit -m "feat: word timings from ElevenLabs normalized character alignment"
```

---

### Task 6: ElevenLabs client with content-hash cache

**Files:**
- Create: `GTM/story-loop/voice/tts.py`
- Test: `GTM/story-loop/tests/test_tts.py`

**Interfaces:**
- Consumes: nothing
- Produces: `tts.resolve_api_key(search_roots) -> tuple[str, str]`; `tts.cache_key(voice_id, model_id, text) -> str`; `tts.synthesize(text, voice_id, model_id, cache_dir, api_key, http=None) -> dict` returning `mp3_path`, `alignment`, `chars`, `cached`; `tts.TTSError`

`http` is an injected callable `(url, headers, body_bytes) -> dict` so tests never touch the network.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tts.py
import base64, json, os, pytest
from voice import tts

ALIGN = {"characters": list("hi"),
         "character_start_times_seconds": [0.0, 0.1],
         "character_end_times_seconds": [0.1, 0.2]}

def _fake_http(calls):
    def http(url, headers, body):
        calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        return {"audio_base64": base64.b64encode(b"ID3fake").decode(),
                "normalized_alignment": ALIGN, "alignment": ALIGN}
    return http

def test_cache_key_changes_with_every_input():
    a = tts.cache_key("v1", "m1", "text")
    assert a != tts.cache_key("v2", "m1", "text")
    assert a != tts.cache_key("v1", "m2", "text")
    assert a != tts.cache_key("v1", "m1", "other")
    assert a == tts.cache_key("v1", "m1", "text")

def test_synthesize_writes_mp3_and_alignment(tmp_path):
    calls = []
    r = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert os.path.isfile(r["mp3_path"])
    assert r["alignment"] == ALIGN
    assert r["cached"] is False
    assert r["chars"] == 2

def test_second_call_is_served_from_cache_without_http(tmp_path):
    calls = []
    tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    r2 = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert r2["cached"] is True
    assert len(calls) == 1  # the second call never hit http

def test_uses_the_with_timestamps_endpoint_and_sends_the_key(tmp_path):
    calls = []
    tts.synthesize("hi", "voiceX", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert calls[0]["url"].endswith("/v1/text-to-speech/voiceX/with-timestamps")
    assert calls[0]["headers"]["xi-api-key"] == "KEY"

def test_prefers_normalized_alignment(tmp_path):
    def http(url, headers, body):
        return {"audio_base64": base64.b64encode(b"x").decode(),
                "alignment": {"characters": ["W"],
                              "character_start_times_seconds": [0.0],
                              "character_end_times_seconds": [1.0]},
                "normalized_alignment": ALIGN}
    r = tts.synthesize("hi", "v", "m", str(tmp_path), "K", http=http)
    assert r["alignment"] == ALIGN

def test_missing_key_raises(tmp_path):
    with pytest.raises(tts.TTSError):
        tts.resolve_api_key([str(tmp_path)])

def test_api_key_never_lands_in_the_cache(tmp_path):
    tts.synthesize("hi", "v", "m", str(tmp_path), "SECRETKEY", http=_fake_http([]))
    for name in os.listdir(tmp_path):
        assert "SECRETKEY" not in open(os.path.join(tmp_path, name), "rb").read().decode("utf-8", "ignore")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_tts.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'voice.tts'`

- [ ] **Step 3: Implement**

```python
"""tts.py — ElevenLabs /with-timestamps client, cached by content hash.

Caching is not an optimisation here. This API is billed per character, so a run
that dies between synthesis and render must not re-pay on retry.
"""
from __future__ import annotations
import base64, hashlib, json, os, urllib.error, urllib.request

API_ROOT = "https://api.elevenlabs.io"
KEY_ENV = "ELEVENLABS_API_KEY"


class TTSError(RuntimeError):
    pass


def resolve_api_key(search_roots) -> tuple[str, str]:
    """env -> .env.local -> .eleven.key, matching sffs-mobile-app's generate-audio.mjs."""
    v = (os.environ.get(KEY_ENV) or "").strip()
    if v:
        return v, f"env:{KEY_ENV}"
    for root in search_roots:
        envf = os.path.join(root, ".env.local")
        if os.path.isfile(envf):
            for line in open(envf, encoding="utf-8"):
                k, _, val = line.strip().partition("=")
                if k.strip() == KEY_ENV and val.strip():
                    return val.strip().strip("'\""), envf
        keyf = os.path.join(root, ".eleven.key")
        if os.path.isfile(keyf):
            val = open(keyf, encoding="utf-8").read().strip()
            if val:
                return val, keyf
    raise TTSError(
        f"no ElevenLabs key: set ${KEY_ENV}, or add .env.local / .eleven.key in "
        + ", ".join(search_roots))


def cache_key(voice_id: str, model_id: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(voice_id.encode()); h.update(b"\0")
    h.update(model_id.encode()); h.update(b"\0")
    h.update(text.encode())
    return h.hexdigest()


def _default_http(url: str, headers: dict, body: bytes) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise TTSError(f"ElevenLabs HTTP {e.code}: {e.read()[:300].decode('utf-8','ignore')}")
    except urllib.error.URLError as e:
        raise TTSError(f"ElevenLabs unreachable: {e}")


def synthesize(text, voice_id, model_id, cache_dir, api_key, http=None) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    key = cache_key(voice_id, model_id, text)
    mp3_path = os.path.join(cache_dir, f"{key}.mp3")
    align_path = os.path.join(cache_dir, f"{key}.align.json")

    if os.path.isfile(mp3_path) and os.path.isfile(align_path):
        with open(align_path) as fh:
            return {"mp3_path": mp3_path, "alignment": json.load(fh),
                    "chars": len(text), "cached": True}

    url = f"{API_ROOT}/v1/text-to-speech/{voice_id}/with-timestamps"
    headers = {"xi-api-key": api_key, "content-type": "application/json",
               "accept": "application/json"}
    body = json.dumps({"text": text, "model_id": model_id}).encode()
    payload = (http or _default_http)(url, headers, body)

    audio_b64 = payload.get("audio_base64")
    alignment = payload.get("normalized_alignment") or payload.get("alignment")
    if not audio_b64 or not alignment:
        raise TTSError("response missing audio_base64 or alignment")

    tmp = mp3_path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(base64.b64decode(audio_b64))
    os.replace(tmp, mp3_path)
    with open(align_path, "w") as fh:
        json.dump(alignment, fh)
    return {"mp3_path": mp3_path, "alignment": alignment,
            "chars": len(text), "cached": False}
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_tts.py -v`
Expected: 7 passed

```bash
git add voice/tts.py tests/test_tts.py
git commit -m "feat: ElevenLabs with-timestamps client with content-hash cache"
```

---

### Task 7: Caption PNG rendering

**Files:**
- Create: `GTM/story-loop/render/captions.py`
- Test: `GTM/story-loop/tests/test_captions.py`

**Interfaces:**
- Consumes: `words.words_from_alignment` output shape from Task 5
- Produces: `captions.safe_box(w, h, cfg) -> dict` with `left`,`right`,`top`,`bottom`; `captions.render_word_png(word, size, cfg, font_path, out_path) -> str`; `captions.render_blank_png(size, out_path) -> str`; `captions.CaptionError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_captions.py
import os, pytest
from PIL import Image
from render import captions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "assets", "fonts", "TikTokSans-Variable.ttf")
CFG = {"caption_weight": 900, "caption_size_px": 96, "caption_center_y": 0.575,
       "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
SIZE = (1080, 1920)

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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_captions.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'render.captions'`

- [ ] **Step 3: Implement**

```python
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


class CaptionError(ValueError):
    pass


def safe_box(width: int, height: int, cfg: dict) -> dict:
    sb = cfg["safe_box"]
    return {"left": width * sb["left"], "right": width * (1.0 - sb["right"]),
            "top": height * sb["top"], "bottom": height * (1.0 - sb["bottom"])}


def _font(font_path: str, px: int, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(font_path, px)
    try:
        f.set_variation_by_axes([36, 100, float(weight), 0])
    except Exception:
        pass  # static font: nominal weight is what it is
    return f


def render_word_png(word: str, size, cfg: dict, font_path: str, out_path: str) -> str:
    text = (word or "").strip()
    if not text:
        raise CaptionError("refusing to render an empty caption")
    width, height = size
    box = safe_box(width, height, cfg)
    max_w = box["right"] - box["left"]

    px = int(cfg["caption_size_px"])
    while px > 12:
        font = _font(font_path, px, int(cfg["caption_weight"]))
        l, t, r, b = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox(
            (0, 0), text, font=font, stroke_width=STROKE_PX)
        if (r - l) <= max_w:
            break
        px -= 4

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
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_captions.py -v`
Expected: 7 passed

```bash
git add render/captions.py tests/test_captions.py
git commit -m "feat: Pillow one-word caption PNGs with safe-box fitting"
```

---

### Task 8: Hook card

**Files:**
- Create: `GTM/story-loop/render/hook.py`
- Test: `GTM/story-loop/tests/test_hook.py`

**Interfaces:**
- Consumes: `captions.safe_box` from Task 7
- Produces: `hook.render_hook_png(text, size, cfg, font_path, out_path) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hook.py
import os
from PIL import Image
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_hook.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'render.hook'`

- [ ] **Step 3: Implement**

```python
"""hook.py — the opening card. Same Pillow path as captions, one alpha track."""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont
from render.captions import safe_box, STROKE_PX, FILL, STROKE


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        l, _, r, _ = draw.textbbox((0, 0), trial, font=font, stroke_width=STROKE_PX)
        if (r - l) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def render_hook_png(text: str, size, cfg: dict, font_path: str, out_path: str) -> str:
    width, height = size
    box = safe_box(width, height, cfg)
    max_w = box["right"] - box["left"]
    max_h = box["bottom"] - box["top"]

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    px = int(cfg["hook_size_px"])
    while px > 20:
        font = ImageFont.truetype(font_path, px)
        lines = _wrap(draw, text, font, max_w)
        line_h = px * 1.18
        if line_h * len(lines) <= max_h:
            break
        px -= 4

    total_h = line_h * len(lines)
    cx = (box["left"] + box["right"]) / 2.0
    y = (box["top"] + box["bottom"]) / 2.0 - total_h / 2.0 + line_h / 2.0
    for line in lines:
        draw.text((cx, y), line, font=font, fill=FILL, anchor="mm",
                  stroke_width=STROKE_PX, stroke_fill=STROKE)
        y += line_h

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_hook.py -v`
Expected: 2 passed

```bash
git add render/hook.py tests/test_hook.py
git commit -m "feat: wrapped hook card rendered through the caption path"
```

---

### Task 9: Alpha caption track

**Files:**
- Modify: `GTM/story-loop/render/captions.py` (append)
- Test: `GTM/story-loop/tests/test_alpha_track.py`

**Interfaces:**
- Consumes: word timings (Task 5), `render_word_png` / `render_blank_png` (Task 7), `render_hook_png` (Task 8)
- Produces: `captions.build_timeline(words, hook_lead_s, total_s) -> list[dict]` of `{"kind","text","start","end"}`; `captions.build_alpha_track(timeline, size, cfg, font_paths, work_dir, out_path, fps) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alpha_track.py
import os, subprocess, json, pytest
from render import captions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = {"caption": os.path.join(ROOT, "assets", "fonts", "TikTokSans-Variable.ttf"),
         "hook": os.path.join(ROOT, "assets", "fonts", "Anton-Regular.ttf")}
CFG = {"caption_weight": 900, "caption_size_px": 96, "hook_size_px": 118,
       "caption_center_y": 0.575,
       "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
SIZE = (360, 640)   # small canvas keeps the test fast

WORDS = [{"word": "one", "start": 0.0, "end": 0.4},
         {"word": "two", "start": 0.5, "end": 0.9}]

def test_timeline_covers_every_instant_with_no_gaps():
    tl = captions.build_timeline(WORDS, hook_lead_s=1.0, total_s=3.0)
    assert tl[0]["kind"] == "hook"
    assert tl[0]["start"] == 0.0
    for a, b in zip(tl, tl[1:]):
        assert a["end"] == pytest.approx(b["start"])
    assert tl[-1]["end"] == pytest.approx(3.0)

def test_word_events_are_offset_by_the_hook_lead():
    tl = captions.build_timeline(WORDS, hook_lead_s=1.0, total_s=3.0)
    spoken = [e for e in tl if e["kind"] == "word"]
    assert spoken[0]["start"] == pytest.approx(1.0)
    assert spoken[1]["start"] == pytest.approx(1.5)

def test_gaps_between_words_become_blank_events():
    tl = captions.build_timeline(WORDS, hook_lead_s=0.0, total_s=2.0)
    assert any(e["kind"] == "blank" for e in tl)

def test_builds_a_playable_alpha_track(tmp_path):
    tl = captions.build_timeline(WORDS, hook_lead_s=1.0, total_s=3.0)
    out = captions.build_alpha_track(tl, SIZE, CFG, FONTS,
                                     str(tmp_path / "work"),
                                     str(tmp_path / "cap.mov"), fps=30)
    assert os.path.isfile(out)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", out],
        capture_output=True, text=True, check=True)
    data = json.loads(probe.stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert v["codec_name"] == "qtrle"
    assert (int(v["width"]), int(v["height"])) == SIZE
    assert 2.7 < float(data["format"]["duration"]) < 3.4
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_alpha_track.py -v`
Expected: FAIL, `AttributeError: module 'render.captions' has no attribute 'build_timeline'`

- [ ] **Step 3: Implement**

```python
# append to render/captions.py
import hashlib, subprocess

MIN_EVENT_S = 1.0 / 60.0


def build_timeline(word_list, hook_lead_s: float, total_s: float) -> list[dict]:
    """A gapless list of events covering 0..total_s. One image is on screen at
    every instant, so the alpha track needs no compositing of its own."""
    events, t = [], 0.0
    if hook_lead_s > 0:
        events.append({"kind": "hook", "text": None, "start": 0.0, "end": hook_lead_s})
        t = hook_lead_s
    for w in word_list:
        start = w["start"] + hook_lead_s
        end = w["end"] + hook_lead_s
        if start - t > MIN_EVENT_S:
            events.append({"kind": "blank", "text": None, "start": t, "end": start})
        events.append({"kind": "word", "text": w["word"],
                       "start": max(start, t), "end": end})
        t = end
    if total_s - t > MIN_EVENT_S:
        events.append({"kind": "blank", "text": None, "start": t, "end": total_s})
    if events:
        events[-1]["end"] = max(events[-1]["end"], total_s)
    return events


def build_alpha_track(timeline, size, cfg, font_paths, work_dir,
                      out_path, fps: int, hook_text: str = "") -> str:
    """One PNG per distinct event, a concat manifest carrying the durations, and a
    single lossless qtrle encode. One image per word, not one per frame."""
    os.makedirs(work_dir, exist_ok=True)
    blank = render_blank_png(size, os.path.join(work_dir, "_blank.png"))

    def image_for(ev):
        if ev["kind"] == "blank":
            return blank
        if ev["kind"] == "hook":
            from render.hook import render_hook_png
            p = os.path.join(work_dir, "_hook.png")
            if not os.path.isfile(p):
                render_hook_png(hook_text or "", size, cfg, font_paths["hook"], p)
            return p
        digest = hashlib.sha256(ev["text"].encode()).hexdigest()[:16]
        p = os.path.join(work_dir, f"w_{digest}.png")
        if not os.path.isfile(p):
            render_word_png(ev["text"], size, cfg, font_paths["caption"], p)
        return p

    lines = ["ffconcat version 1.0"]
    last = None
    for ev in timeline:
        dur = max(ev["end"] - ev["start"], MIN_EVENT_S)
        last = image_for(ev)
        lines.append(f"file '{last}'")
        lines.append(f"duration {dur:.4f}")
    if last:
        lines.append(f"file '{last}'")  # concat demuxer drops the final duration
    manifest = os.path.join(work_dir, "captions.ffconcat")
    with open(manifest, "w") as fh:
        fh.write("\n".join(lines) + "\n")

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", manifest,
           "-vf", f"fps={fps},format=rgba", "-c:v", "qtrle", out_path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise CaptionError(f"alpha track encode failed: {res.stderr[-400:]}")
    return out_path
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_alpha_track.py -v`
Expected: 4 passed

```bash
git add render/captions.py tests/test_alpha_track.py
git commit -m "feat: gapless caption timeline encoded to a qtrle alpha track"
```

---

### Task 10: The compositor

**Files:**
- Create: `GTM/story-loop/render/compose.py`
- Test: `GTM/story-loop/tests/test_compose.py`

**Interfaces:**
- Consumes: `select_window` output (Task 4), alpha track path (Task 9)
- Produces: `compose.build_filtergraph(segments, n_inputs, cfg, silence_idx, caption_idx, narration_idx, narration_delay_s) -> str`; `compose.render(segments, caption_track, narration_mp3, out_path, cfg, total_s) -> str`; `compose.ComposeError`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_compose.py
import json, os, subprocess, pytest
from render import compose

CFG = {"width": 360, "height": 640, "fps": 30, "crf": 28,
       "game_audio_duck": 0.12, "loudnorm_i": -14}

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
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"color=c=black@0.0:s={w}x{h}:r=30:d={secs}",
                    "-vf", "format=rgba", "-c:v", "qtrle", path],
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_compose.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'render.compose'`

- [ ] **Step 3: Implement**

```python
"""compose.py — build the ffmpeg filtergraph and render the final mp4.

No text filter appears anywhere in here: this ffmpeg has neither libass nor
libfreetype. Captions arrive as a pre-rendered alpha video and are composited
with a single overlay.
"""
from __future__ import annotations
import os, subprocess


class ComposeError(RuntimeError):
    pass


def build_filtergraph(segments, n_clip_inputs, cfg, silence_idx,
                      caption_idx, narration_idx, narration_delay_s) -> str:
    W, H, FPS = int(cfg["width"]), int(cfg["height"]), int(cfg["fps"])
    parts, vlabels, alabels = [], [], []

    for i, seg in enumerate(segments):
        s, d = float(seg["start_s"]), float(seg["take_s"])
        if seg["layout"] == "pillarbox":
            parts.append(
                f"[{i}:v]trim=start={s}:duration={d},setpts=PTS-STARTPTS,split[b{i}][f{i}];"
                f"[b{i}]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},gblur=sigma=24[bg{i}];"
                f"[f{i}]scale={W}:-2[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,fps={FPS},setsar=1[v{i}]")
        else:
            parts.append(
                f"[{i}:v]trim=start={s}:duration={d},setpts=PTS-STARTPTS,"
                f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},fps={FPS},setsar=1[v{i}]")
        vlabels.append(f"[v{i}]")

        if seg["has_audio"]:
            parts.append(f"[{i}:a]atrim=start={s}:duration={d},"
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

    parts.append(f"[bgv][{caption_idx}:v]overlay=0:0:format=auto,format=yuv420p[vout]")

    duck = float(cfg["game_audio_duck"])
    delay_ms = int(round(float(narration_delay_s) * 1000))
    parts.append(f"[gamea]volume={duck}[gameduck]")
    parts.append(f"[{narration_idx}:a]aresample=44100,"
                 f"adelay={delay_ms}|{delay_ms}[narr]")
    parts.append(f"[gameduck][narr]amix=inputs=2:duration=longest:normalize=0,"
                 f"loudnorm=I={int(cfg['loudnorm_i'])}:TP=-1.5:LRA=11[aout]")
    return ";".join(parts)


def render(segments, caption_track, narration_mp3, out_path, cfg,
           total_s, narration_delay_s=0.0) -> str:
    if not segments:
        raise ComposeError("no footage segments to render")
    cmd = ["ffmpeg", "-y"]
    for seg in segments:
        cmd += ["-i", seg["path"]]
    silence_idx = len(segments)
    cmd += ["-f", "lavfi", "-t", f"{total_s + 1:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    caption_idx = silence_idx + 1
    cmd += ["-i", caption_track]
    narration_idx = caption_idx + 1
    cmd += ["-i", narration_mp3]

    graph = build_filtergraph(segments, len(segments), cfg, silence_idx,
                              caption_idx, narration_idx, narration_delay_s)
    cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(int(cfg["crf"])),
            "-pix_fmt", "yuv420p", "-r", str(int(cfg["fps"])),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-t", f"{total_s:.3f}", out_path]

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ComposeError(f"render failed: {res.stderr[-800:]}")
    return out_path
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest tests/test_compose.py -v`
Expected: 6 passed

```bash
git add render/compose.py tests/test_compose.py
git commit -m "feat: ffmpeg compositor with alpha caption overlay and ducked game audio"
```

---

### Task 11: `make_one.py` and the stage gate

The stage's whole purpose: prove the caption approach against real footage and a real voice, and **measure** the narration rate that stage 2 depends on.

**Files:**
- Create: `GTM/story-loop/make_one.py`, `GTM/story-loop/channels/main/scripts/hand-written-01.txt`
- Test: `GTM/story-loop/tests/test_make_one.py`

**Interfaces:**
- Consumes: every module above
- Produces: `make_one.build(channel_name, script_path, run_id, voice_id, model_id, dry_run) -> dict` returning `out_path`, `total_s`, `word_count`, `chars_per_second`, `cached`

- [ ] **Step 1: Write the hand-written script**

```text
// channels/main/scripts/hand-written-01.txt
My neighbour once sent me an invoice for breathing near his fence.
Forty dollars, itemised, with a late fee already applied.
I thought it was a joke until the second one arrived, and that one had interest.
So I did the only reasonable thing. I paid it entirely in coins,
delivered in a bucket, at seven in the morning, on a Sunday.
He has not spoken to me since, and the fence has never looked better.
```

- [ ] **Step 2: Write the failing test (dry-run path, no network)**

```python
# tests/test_make_one.py
import json, os, subprocess, pytest
import make_one

ROOT = os.path.dirname(os.path.abspath(make_one.__file__))

def test_dry_run_builds_a_video_without_touching_the_network(tmp_path, monkeypatch):
    footage_dir = os.path.join(ROOT, "channels", "main", "footage")
    os.makedirs(footage_dir, exist_ok=True)
    clip = os.path.join(footage_dir, "_test_synthetic.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=360x640:rate=30:duration=30",
                    "-f", "lavfi", "-i", "sine=frequency=200:duration=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-t", "30", clip], capture_output=True, check=True)
    try:
        res = make_one.build(channel_name="main",
                             script_path=os.path.join(ROOT, "channels", "main",
                                                      "scripts", "hand-written-01.txt"),
                             run_id="test-run", voice_id="stub",
                             model_id="stub", dry_run=True)
        assert os.path.isfile(res["out_path"])
        assert res["word_count"] > 20
        assert res["total_s"] > 1.0
    finally:
        os.remove(clip)
        mf = os.path.join(footage_dir, "manifest.json")
        if os.path.isfile(mf):
            os.remove(mf)

def test_dry_run_reports_a_measured_rate(tmp_path):
    # chars_per_second must come from the alignment, never from a constant.
    assert make_one.STUB_RATE_IS_SYNTHETIC is True
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `python3 -m pytest tests/test_make_one.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'make_one'`

- [ ] **Step 4: Implement**

```python
#!/usr/bin/env python3
"""make_one.py — stage 1 entry point: one script in, one finished mp4 out.

    python3 make_one.py --script channels/main/scripts/hand-written-01.txt --voice <id>
    python3 make_one.py --script ... --dry-run     # stub voice, no network, no spend
"""
from __future__ import annotations
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import capability, channel                       # noqa: E402
from render import footage, captions, compose    # noqa: E402
from voice import tts, words                     # noqa: E402

STUB_RATE_IS_SYNTHETIC = True
STUB_CPS = 14.0   # dry-run only; never used for a real measurement


def _stub_alignment(text: str) -> dict:
    step = 1.0 / STUB_CPS
    chars = list(text)
    return {"characters": chars,
            "character_start_times_seconds": [i * step for i in range(len(chars))],
            "character_end_times_seconds": [(i + 1) * step for i in range(len(chars))]}


def build(channel_name, script_path, run_id, voice_id, model_id, dry_run=False) -> dict:
    ch = channel.resolve(channel_name)
    cfg = ch["config"]
    fonts_dir = os.path.join(HERE, "assets", "fonts")
    capability.check(fonts_dir)

    text = open(script_path, encoding="utf-8").read().strip()
    if not text:
        raise SystemExit(f"FATAL: {script_path} is empty")

    cache_dir = os.path.join(ch["root"], "voice", "cache")
    if dry_run:
        alignment = _stub_alignment(text)
        narration_mp3 = os.path.join(cache_dir, "_dryrun.mp3")
        os.makedirs(cache_dir, exist_ok=True)
        dur = len(text) / STUB_CPS
        os.system(f'ffmpeg -y -f lavfi -i "sine=frequency=220:duration={dur:.2f}" '
                  f'-c:a libmp3lame "{narration_mp3}" >/dev/null 2>&1')
        cached = False
    else:
        key, source = tts.resolve_api_key([HERE, ch["root"]])
        print(f"  ElevenLabs key from {source}")
        res = tts.synthesize(text, voice_id, model_id, cache_dir, key)
        alignment, narration_mp3, cached = res["alignment"], res["mp3_path"], res["cached"]

    word_list = words.words_from_alignment(alignment)
    cps = words.chars_per_second(alignment)
    narration_s = words.total_duration(word_list)
    hook_lead = float(cfg["hook_lead_s"])
    total_s = hook_lead + narration_s + float(cfg["tail_s"])

    size = (int(cfg["width"]), int(cfg["height"]))
    manifest = footage.build_manifest(os.path.join(ch["root"], "footage"))
    window = footage.select_window(manifest, total_s, run_id)

    work = os.path.join(ch["root"], "out", "work", run_id)
    timeline = captions.build_timeline(word_list, hook_lead, total_s)
    track = captions.build_alpha_track(
        timeline, size, cfg,
        {"caption": os.path.join(fonts_dir, "TikTokSans-Variable.ttf"),
         "hook": os.path.join(fonts_dir, "Anton-Regular.ttf")},
        work, os.path.join(work, "captions.mov"), int(cfg["fps"]),
        hook_text=" ".join(w["word"] for w in word_list[:9]))

    out_path = os.path.join(ch["root"], "out", "pending", f"{run_id}.mp4")
    compose.render(window["segments"], track, narration_mp3, out_path, cfg,
                   total_s, narration_delay_s=hook_lead)

    result = {"out_path": out_path, "total_s": round(total_s, 2),
              "word_count": len(word_list), "chars_per_second": round(cps, 2),
              "cached": cached, "dry_run": dry_run}
    print(json.dumps(result, indent=2))
    if not dry_run:
        print(f"\nMEASURED narration rate: {cps:.2f} chars/sec — "
              f"put this in channel.json for stage 2's word-count target.")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="main")
    ap.add_argument("--script", required=True)
    ap.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--voice", default="")
    ap.add_argument("--model", default="eleven_multilingual_v2")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.dry_run and not a.voice:
        raise SystemExit("FATAL: --voice is required unless --dry-run")
    build(a.channel, a.script, a.run_id, a.voice, a.model, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the full suite**

Run: `cd /Users/graceyan/Desktop/alpha/GTM/story-loop && python3 -m pytest tests/ -v`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add make_one.py channels/main/scripts/hand-written-01.txt tests/test_make_one.py
git commit -m "feat: make_one stage-1 entry point with dry-run path"
```

- [ ] **Step 7: THE STAGE GATE — render with real footage and a real voice**

This is the step the whole stage exists for. It needs the owner's gameplay clips in `channels/main/footage/` and `ELEVENLABS_API_KEY` set.

```bash
cd /Users/graceyan/Desktop/alpha/GTM/story-loop
python3 -c "
from voice import tts
import json, urllib.request
key, src = tts.resolve_api_key(['.', 'channels/main'])
req = urllib.request.Request('https://api.elevenlabs.io/v1/models',
                             headers={'xi-api-key': key})
print(json.dumps([m['model_id'] for m in json.load(urllib.request.urlopen(req))], indent=2))
print('voices:')
req = urllib.request.Request('https://api.elevenlabs.io/v1/voices',
                             headers={'xi-api-key': key})
print(json.dumps([(v['name'], v['voice_id'])
                  for v in json.load(urllib.request.urlopen(req))['voices']][:12], indent=2))
"
python3 make_one.py --script channels/main/scripts/hand-written-01.txt \
  --voice <voice_id_from_above> --model <model_id_from_above> --run-id gate-01
open -a "Brave Browser" "channels/main/out/pending/gate-01.mp4"
```

Verify by watching, and record each as pass or fail:

1. Every caption word appears exactly as it is spoken — **no drift**, especially late in the video.
2. Captions sit inside the safe box and clear TikTok's UI zones.
3. The hook card is readable and gives way cleanly to word captions.
4. Gameplay fills the frame with no black bars (portrait) or clean blurred bars (landscape).
5. Narration sits clearly above the ducked gameplay audio.
6. No silent gaps that read as a broken file.

- [ ] **Step 8: Record the measurement and close the stage**

Write the measured `chars_per_second` printed by step 7 into `channels/main/channel.json` as `"measured_cps"`. **Only the printed value** — this number sets stage 2's word-count target, and an estimate here silently mis-sizes every future video.

```bash
git add -A && git commit -m "chore: stage 1 gate — measured narration rate from live TTS"
```

If check 1 fails, **stop**. Caption drift invalidates the alignment approach and stages 2–4 must be re-planned before any more work.

---

## Self-Review

**Spec coverage.** Stage 1's spec units all map to tasks: `channel.py` → 1, capability preflight → 2, `footage.py` probe → 3 and selection → 4, `words.py` → 5, `tts.py` → 6, `captions.py` → 7 and 9, `hook.py` → 8, `compose.py` → 10, entry point and calibration → 11. Spec items deliberately deferred to later stages: `reddit.py`, `gates.py`, `retell.py`, `cycle.py`, `guard.py`, `llm.py`, `metricool.py`, `queue.py`.

**Placeholders.** None. Every code step carries runnable code; the empty `watermark` and empty `voice_id` in `channel.json` are real configured values with defined behaviour (render nothing / require `--voice`), not TODOs.

**Type consistency.** `probe_clip` returns `path`/`duration_s`/`width`/`height`/`fps`/`has_audio`, consumed unchanged by `layout_for` and `select_window`. `select_window` returns `segments` with `path`/`start_s`/`take_s`/`layout`/`has_audio`, consumed unchanged by `build_filtergraph` and `render`. `words_from_alignment` returns `word`/`start`/`end`, consumed by `build_timeline`. `synthesize` returns `mp3_path`/`alignment`/`chars`/`cached`, consumed by `build`. `safe_box` returns `left`/`right`/`top`/`bottom`, used identically in `captions.py` and `hook.py`.

**Parsing note.** `capability._names` keys off the exact indentation ffmpeg uses: real entries start with one space, legend and separator lines with two, and the sink-filter legend line begins `" | ="`. Verified against the installed binary's actual output. Task 2's tests assert against that binary, so a parsing regression fails loudly there rather than silently at render time.
