# story-loop Stage 1b: Reddit post card + two-segment narration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open every video on a Reddit-styled post card narrated with the story's title, then cut to the body with one-word captions — and fix the three composition defects the first trial render exposed.

**Architecture:** Extends Stage 1, which is complete (21 commits, 143 tests). Adds `render/post_card.py` rendering the card through the existing Pillow → alpha-track path, splits narration into two cached TTS calls (title, body) joined by the genre's `0.3s` beat, and adjusts the compositor's watermark and pillarbox for legibility.

**Tech Stack:** Python 3.11.1 · Pillow 12.1.1 · ffmpeg 8.1.2 · pytest 9.1.1 · stdlib only

## Global Constraints

Every task's requirements implicitly include this section.

- Canvas 1080×1920, 30 fps. H.264 `yuv420p`, AAC, `+faststart`.
- **No `ass`, `subtitles`, or `drawtext` filter exists in this ffmpeg.** Text is Pillow-rendered and composited with `overlay`.
- **The post card carries NO username and NO avatar identifying a real account.** Real subreddit, our retold title, upvote/comment chrome only. Reddit's wordmark and logo are never reproduced — the card is *styled* like a post, not a copy of Reddit's UI. A test must enforce the no-username rule.
- Caption timing and text both from `normalized_alignment`.
- Caption font `TikTokSans-Variable.ttf` @ Weight 900; title/card font `Anton-Regular.ttf`. Both in `assets/fonts/`.
- Safe box insets: 10% top, 18% bottom, 6% left, 14% right. Captions in the 55–60% height band.
- No music bed. Gameplay audio ducked (this footage has **no audio track** — `has_audio: False`).
- Loudness `-14 LUFS` default. `segment_beat_s` = 0.3.
- Never modify `GTM/ugc-pipeline/`.
- The ElevenLabs key never enters an artifact, manifest, or run state.
- **Do not break the 143 existing tests.** `build_timeline` keeps its current signature and behaviour; new structure goes in a new function.

## Measured facts this plan relies on

All printed from live runs, not estimated:

| Fact | Value |
|---|---|
| Narration rate, `eleven_multilingual_v2` | **16.25 chars/sec** (183.9 wpm) |
| Narration rate, `eleven_turbo_v2_5` | 17.32 chars/sec (196.0 wpm) |
| Footage | 1762×990, aspect 1.7798, 5237.5s, 24 fps, **no audio** |
| `cropdetect` on footage | `crop=1762:960:0:30` — only 30px of letterbox, so cropping is **not** the layout fix |
| Character quota remaining | 3,053,055 |

## File Structure

| File | Responsibility |
|---|---|
| `render/post_card.py` | **new** — Reddit-styled post card PNG |
| `render/captions.py` | **modify** — add `build_two_segment_timeline`, leave `build_timeline` untouched |
| `voice/narration.py` | **new** — synthesize title + body, join with the beat, return combined audio + both alignments |
| `render/compose.py` | **modify** — watermark legibility, `pillarbox_zoom` |
| `channels/main/channel.json` | **modify** — new config keys |
| `make_one.py` | **modify** — wire the two-segment path |

---

### Task 1: The Reddit post card

**Files:**
- Create: `render/post_card.py`
- Test: `tests/test_post_card.py`

**Interfaces:**
- Consumes: `captions.safe_box(width, height, cfg) -> dict` with keys `left`,`right`,`top`,`bottom`
- Produces: `post_card.render_post_card_png(subreddit, title, size, cfg, font_paths, out_path, upvotes=None, comments=None) -> str`; `post_card.PostCardError`

`font_paths` is `{"caption": <path>, "hook": <path>}` — the same dict shape `captions.build_alpha_track` already takes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_post_card.py
import inspect, os, pytest
from PIL import Image
from render import post_card, captions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = {"caption": os.path.join(ROOT, "assets", "fonts", "TikTokSans-Variable.ttf"),
         "hook": os.path.join(ROOT, "assets", "fonts", "Anton-Regular.ttf")}
CFG = {"hook_size_px": 118, "caption_size_px": 96, "caption_weight": 900,
       "card_meta_size_px": 34,
       "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
SIZE = (1080, 1920)


def _render(tmp_path, title="my neighbour billed me for breathing near his fence",
            sub="tifu", **kw):
    return post_card.render_post_card_png(
        sub, title, SIZE, CFG, FONTS, str(tmp_path / "card.png"), **kw)


def test_card_is_rgba_canvas_sized_and_transparent_at_the_corners(tmp_path):
    img = Image.open(_render(tmp_path))
    assert img.mode == "RGBA" and img.size == SIZE
    assert img.getpixel((3, 3))[3] == 0


def test_all_ink_stays_inside_the_safe_box(tmp_path):
    bbox = Image.open(_render(tmp_path)).getchannel("A").getbbox()
    b = captions.safe_box(*SIZE, CFG)
    assert bbox is not None
    assert bbox[0] >= b["left"] - 1 and bbox[2] <= b["right"] + 1
    assert bbox[1] >= b["top"] - 1 and bbox[3] <= b["bottom"] + 1


def test_subreddit_is_prefixed_and_normalised(tmp_path):
    # r/ must be added exactly once whether or not the caller passes it
    a = _render(tmp_path, sub="tifu")
    b = post_card.render_post_card_png(
        "r/tifu", "same title here", SIZE, CFG, FONTS, str(tmp_path / "b.png"))
    assert open(a, "rb").read() == open(b, "rb").read()


def test_a_very_long_title_shrinks_to_fit_instead_of_overflowing(tmp_path):
    long_title = ("my neighbour sent me an itemised invoice for breathing near his "
                  "fence and then added a late fee and compound interest to it")
    bbox = Image.open(_render(tmp_path, title=long_title)).getchannel("A").getbbox()
    b = captions.safe_box(*SIZE, CFG)
    assert bbox[0] >= b["left"] - 1 and bbox[2] <= b["right"] + 1
    assert bbox[3] <= b["bottom"] + 1


def test_render_is_deterministic(tmp_path):
    a = _render(tmp_path)
    import shutil; shutil.move(a, str(tmp_path / "first.png"))
    b = _render(tmp_path)
    assert open(str(tmp_path / "first.png"), "rb").read() == open(b, "rb").read()


def test_counts_are_rendered_when_given_and_omitted_when_not(tmp_path):
    with_counts = _render(tmp_path, upvotes=48200, comments=1300)
    import shutil; shutil.move(with_counts, str(tmp_path / "with.png"))
    without = _render(tmp_path)
    assert open(str(tmp_path / "with.png"), "rb").read() != open(without, "rb").read()


def test_the_api_cannot_render_a_username(tmp_path):
    # The no-username rule is a project constraint, enforced structurally:
    # there must be no parameter through which a username could be supplied.
    params = set(inspect.signature(post_card.render_post_card_png).parameters)
    for banned in ("username", "user", "author", "u", "avatar", "account"):
        assert banned not in params


def test_empty_title_raises(tmp_path):
    with pytest.raises(post_card.PostCardError):
        _render(tmp_path, title="   ")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `cd /Users/graceyan/Desktop/alpha/GTM/story-loop && python3 -m pytest tests/test_post_card.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'render.post_card'`

- [ ] **Step 3: Implement**

Render a rounded light card centred in the safe box, holding: a meta row (`r/<sub>`), the wrapped title in `Anton-Regular.ttf`, and — when supplied — an upvote/comment row. Shrink the title until the whole card fits the safe box. Reuse `captions.safe_box`. Counts format compactly (`48.2k`). Normalise the subreddit by stripping a leading `r/` before prefixing exactly one. No username parameter exists at all — that is what makes the constraint enforceable rather than a convention.

Save with `img.save(out_path, "PNG")`. Raise `PostCardError` on an empty title.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_post_card.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add render/post_card.py tests/test_post_card.py
git commit -m "feat: Reddit-styled post card with no username by construction"
```

---

### Task 2: Two-segment narration

**Files:**
- Create: `voice/narration.py`, `tests/test_narration.py`
- Modify: `render/captions.py` (append `build_two_segment_timeline`), `tests/test_captions.py` (append)

**Interfaces:**
- Consumes: `tts.synthesize(text, voice_id, model_id, cache_dir, api_key, http=None) -> {mp3_path, alignment, chars, cached}`; `words.words_from_alignment(alignment) -> [{word,start,end}]`; `words.total_duration(list) -> float`
- Produces: `narration.narrate(title, body, voice_id, model_id, cache_dir, api_key, beat_s, out_path, http=None, ffmpeg=True) -> dict` with keys `audio_path`, `title_s`, `body_s`, `beat_s`, `total_s`, `body_words`, `chars`, `cached`; `captions.build_two_segment_timeline(title_s, beat_s, body_words, total_s) -> list[dict]`

- [ ] **Step 1: Write the failing timeline test**

```python
# append to tests/test_captions.py
import pytest
from render import captions

BODY = [{"word": "one", "start": 0.0, "end": 0.4},
        {"word": "two", "start": 0.5, "end": 0.9}]


def test_two_segment_timeline_opens_with_the_card_for_the_title_duration():
    tl = captions.build_two_segment_timeline(2.0, 0.3, BODY, 4.0)
    assert tl[0]["kind"] == "card"
    assert tl[0]["start"] == 0.0
    assert tl[0]["end"] == pytest.approx(2.0)


def test_two_segment_timeline_is_gapless_and_covers_total():
    tl = captions.build_two_segment_timeline(2.0, 0.3, BODY, 4.0)
    for a, b in zip(tl, tl[1:]):
        assert a["end"] == pytest.approx(b["start"])
    assert tl[-1]["end"] == pytest.approx(4.0)


def test_body_words_are_offset_by_title_plus_beat():
    tl = captions.build_two_segment_timeline(2.0, 0.3, BODY, 4.0)
    spoken = [e for e in tl if e["kind"] == "word"]
    assert spoken[0]["start"] == pytest.approx(2.3)
    assert spoken[1]["start"] == pytest.approx(2.8)


def test_the_beat_is_a_blank_event_not_a_held_card():
    tl = captions.build_two_segment_timeline(2.0, 0.3, BODY, 4.0)
    beat = [e for e in tl if e["start"] == pytest.approx(2.0)][0]
    assert beat["kind"] == "blank"


def test_existing_build_timeline_is_unchanged():
    tl = captions.build_timeline(BODY, 1.0, 3.0)
    assert tl[0]["kind"] == "hook"
```

- [ ] **Step 2: Write the failing narration test**

```python
# tests/test_narration.py
import base64, json, os, pytest
from voice import narration


def _align(text, cps=16.25):
    chars = list(text)
    step = 1.0 / cps
    return {"characters": chars,
            "character_start_times_seconds": [i * step for i in range(len(chars))],
            "character_end_times_seconds": [(i + 1) * step for i in range(len(chars))]}


def _http(calls):
    def http(url, headers, body):
        payload = json.loads(body)
        calls.append(payload["text"])
        return {"audio_base64": base64.b64encode(b"ID3fake").decode(),
                "normalized_alignment": _align(payload["text"])}
    return http


def test_narrate_makes_exactly_two_tts_calls_title_then_body(tmp_path):
    calls = []
    narration.narrate("Title here", "Body text here.", "v", "m",
                      str(tmp_path / "cache"), "KEY", 0.3,
                      str(tmp_path / "n.mp3"), http=_http(calls), ffmpeg=False)
    assert len(calls) == 2
    assert calls[0] == "Title here"
    assert calls[1] == "Body text here."


def test_total_is_title_plus_beat_plus_body(tmp_path):
    r = narration.narrate("Title here", "Body text here.", "v", "m",
                          str(tmp_path / "cache"), "KEY", 0.3,
                          str(tmp_path / "n.mp3"), http=_http([]), ffmpeg=False)
    assert r["total_s"] == pytest.approx(r["title_s"] + 0.3 + r["body_s"])
    assert r["beat_s"] == 0.3


def test_body_words_come_back_as_word_timings_relative_to_the_body(tmp_path):
    r = narration.narrate("T", "two words", "v", "m", str(tmp_path / "cache"),
                          "KEY", 0.3, str(tmp_path / "n.mp3"),
                          http=_http([]), ffmpeg=False)
    assert [w["word"] for w in r["body_words"]] == ["two", "words"]
    assert r["body_words"][0]["start"] == pytest.approx(0.0, abs=0.01)


def test_title_and_body_are_cached_independently(tmp_path):
    cache = str(tmp_path / "cache")
    calls1, calls2 = [], []
    narration.narrate("Same title", "First body.", "v", "m", cache, "KEY", 0.3,
                      str(tmp_path / "a.mp3"), http=_http(calls1), ffmpeg=False)
    narration.narrate("Same title", "Second body.", "v", "m", cache, "KEY", 0.3,
                      str(tmp_path / "b.mp3"), http=_http(calls2), ffmpeg=False)
    # title was cached from the first run; only the changed body is re-synthesised
    assert calls2 == ["Second body."]


def test_chars_counts_both_segments(tmp_path):
    r = narration.narrate("abc", "defg", "v", "m", str(tmp_path / "cache"), "KEY",
                          0.3, str(tmp_path / "n.mp3"), http=_http([]), ffmpeg=False)
    assert r["chars"] == 7
```

- [ ] **Step 3: Run both and confirm they fail**

Run: `python3 -m pytest tests/test_narration.py tests/test_captions.py -v`
Expected: `test_narration.py` fails on `ModuleNotFoundError: No module named 'voice.narration'`; the new caption tests fail on `AttributeError: ... has no attribute 'build_two_segment_timeline'`

- [ ] **Step 4: Implement `build_two_segment_timeline`**

Append to `render/captions.py`. Same gapless contract as `build_timeline`: a `card` event over `[0, title_s]`, a `blank` event over the beat, then `word` events offset by `title_s + beat_s`, then a trailing `blank` to `total_s`. Reuse `_check_gapless`. Leave `build_timeline` untouched.

- [ ] **Step 5: Implement `voice/narration.py`**

Two `tts.synthesize` calls (title, then body) against the same cache dir, so each is keyed by its own content hash. Derive `title_s` and `body_s` from each alignment via `words.total_duration(words.words_from_alignment(...))`. When `ffmpeg=True`, join `title.mp3 + <beat_s> silence + body.mp3` into `out_path` using the `concat` filter with an `anullsrc` segment for the beat, and set `audio_path` to it; when `ffmpeg=False` (tests) skip the join and leave `audio_path` as the title mp3 so no ffmpeg is required. Return the documented dict.

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_narration.py tests/test_captions.py -v`
Expected: all pass, including `test_existing_build_timeline_is_unchanged`

- [ ] **Step 7: Commit**

```bash
git add voice/narration.py tests/test_narration.py render/captions.py tests/test_captions.py
git commit -m "feat: two-segment narration (title over card, then body) with per-segment caching"
```

---

### Task 3: Composition fixes from the trial render

Three defects observed in a real 1080×1920 trial render, not hypothetical.

**Files:**
- Modify: `render/compose.py`, `tests/test_compose.py`, `channels/main/channel.json`, `render/captions.py` (alpha track must accept a `card` event)

**Interfaces:**
- Consumes: `compose.render(...)`, `compose.render_watermark_png(text, size, cfg, font_path, out_path)`
- Produces: unchanged signatures plus config keys `watermark_opacity`, `watermark_stroke_px`, `pillarbox_zoom`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_compose.py
import os
from PIL import Image
from render import compose

WCFG = {"width": 1080, "height": 1920, "fps": 30, "crf": 28,
        "game_audio_duck": 0.12, "loudnorm_i": -14,
        "watermark_opacity": 0.85, "watermark_stroke_px": 4,
        "caption_size_px": 96, "caption_weight": 900, "pillarbox_zoom": 1.0,
        "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
FONT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "assets", "fonts", "TikTokSans-Variable.ttf")


def test_watermark_is_legible_against_a_dark_background(tmp_path):
    """The trial render put a grey watermark over black — nearly invisible.
    It must carry a stroke, so dark-on-dark cannot happen."""
    p = compose.render_watermark_png("Petty Hours", (1080, 1920), WCFG, FONT,
                                     str(tmp_path / "wm.png"))
    img = Image.open(p).convert("RGBA")
    px = [img.getpixel((x, y)) for x in range(0, 1080, 3) for y in range(0, 1920, 3)]
    opaque = [p for p in px if p[3] > 200]
    assert opaque, "watermark rendered nothing"
    brightest = max(sum(p[:3]) for p in opaque)
    darkest = min(sum(p[:3]) for p in opaque)
    # both light glyph and dark stroke present => legible on any background
    assert brightest > 600, "no light pixels: invisible on dark footage"
    assert darkest < 200, "no dark stroke: invisible on light footage"


def test_pillarbox_zoom_above_one_enlarges_the_gameplay_band():
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2,
             "layout": "pillarbox", "has_audio": False}]
    flat = compose.build_filtergraph(segs, 1, dict(WCFG, pillarbox_zoom=1.0),
                                     1, 2, 3, 0.0)
    zoomed = compose.build_filtergraph(segs, 1, dict(WCFG, pillarbox_zoom=1.4),
                                       1, 2, 3, 0.0)
    assert flat != zoomed
    assert "1512" in zoomed or "1.4" in zoomed  # 1080 * 1.4


def test_zoom_of_one_still_fits_the_full_width():
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2,
             "layout": "pillarbox", "has_audio": False}]
    g = compose.build_filtergraph(segs, 1, dict(WCFG, pillarbox_zoom=1.0), 1, 2, 3, 0.0)
    assert "scale=1080:-2" in g


def test_no_text_filter_ever_appears():
    segs = [{"path": "/a.mp4", "start_s": 0, "take_s": 2,
             "layout": "pillarbox", "has_audio": False}]
    g = compose.build_filtergraph(segs, 1, WCFG, 1, 2, 3, 0.0)
    for banned in ("drawtext", "ass=", "subtitles"):
        assert banned not in g
```

Plus one test that the alpha track renders a `card` event:

```python
# append to tests/test_alpha_track.py
import os
from render import captions

def test_alpha_track_renders_a_card_event(tmp_path):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fonts = {"caption": os.path.join(ROOT, "assets/fonts/TikTokSans-Variable.ttf"),
             "hook": os.path.join(ROOT, "assets/fonts/Anton-Regular.ttf")}
    cfg = {"caption_weight": 900, "caption_size_px": 96, "hook_size_px": 118,
           "card_meta_size_px": 34, "caption_center_y": 0.575,
           "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
    tl = captions.build_two_segment_timeline(
        1.0, 0.3, [{"word": "hi", "start": 0.0, "end": 0.4}], 2.0)
    out = captions.build_alpha_track(tl, (360, 640), cfg, fonts,
                                     str(tmp_path / "w"), str(tmp_path / "c.mov"),
                                     fps=30, card={"subreddit": "tifu",
                                                   "title": "a short title"})
    assert os.path.isfile(out)
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m pytest tests/test_compose.py tests/test_alpha_track.py -v`
Expected: the four new compose tests and the card test fail; existing tests still pass.

- [ ] **Step 3: Fix the watermark**

Render it white with a dark stroke at `watermark_stroke_px`, alpha from `watermark_opacity`. The trial render's grey-on-black watermark was effectively invisible; a stroked glyph is legible over both dark and light footage, which is why the test asserts both a light and a dark pixel population exist.

- [ ] **Step 4: Add `pillarbox_zoom`**

In the pillarbox branch, scale the sharp foreground to `round(width * zoom)` instead of `width`, then centre-crop back to `width`. `zoom=1.0` must produce exactly today's `scale=1080:-2` graph so nothing regresses. Values above 1 trade side crop for a taller gameplay band — the trial render left gameplay at only ~32% of frame height.

- [ ] **Step 5: Teach `build_alpha_track` the `card` event**

Add an optional `card=None` kwarg: `{"subreddit": str, "title": str, "upvotes": int|None, "comments": int|None}`. On a `card` event, render via `post_card.render_post_card_png` (import locally, as the module already does for `hook`). Keep `hook_text` working so existing tests pass.

- [ ] **Step 6: Add config keys**

In `channels/main/channel.json`: `"watermark_opacity": 0.85`, `"watermark_stroke_px": 4`, `"pillarbox_zoom": 1.0`, `"card_meta_size_px": 34`. Leave `watermark` as-is (the channel is still unnamed; empty renders nothing).

- [ ] **Step 7: Run the full suite and commit**

Run: `python3 -m pytest tests/ -v`
Expected: all previous tests plus the new ones pass.

```bash
git add render/compose.py render/captions.py tests/test_compose.py tests/test_alpha_track.py channels/main/channel.json
git commit -m "fix: legible stroked watermark, pillarbox_zoom, card events on the alpha track"
```

---

### Task 4: Wire it into `make_one.py`

**Files:**
- Modify: `make_one.py`, `tests/test_make_one.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_make_one.py
import make_one

def test_build_accepts_a_title_and_uses_the_two_segment_path(tmp_path):
    assert "title" in make_one.build.__code__.co_varnames
    assert hasattr(make_one, "build")
```

- [ ] **Step 2: Implement**

Split the script file on its first blank line: first paragraph is the title, remainder the body (documented at the top of the script file). Call `narration.narrate(...)`, build the timeline with `build_two_segment_timeline`, pass `card={"subreddit": ..., "title": ...}` into `build_alpha_track`, and set `narration_delay_s=0.0` — narration now starts at t=0 because the title *is* the first audio, which removes the dead-air problem the silent footage created.

Add `--subreddit` (default `tifu`) and `--title` (overrides the script's first paragraph).

- [ ] **Step 3: Run the dry-run test, then commit**

Run: `python3 -m pytest tests/test_make_one.py -v`

```bash
git add make_one.py tests/test_make_one.py
git commit -m "feat: make_one renders card + title + body through the two-segment path"
```

---

## Self-Review

**Spec coverage.** Revised spec decisions map to tasks: post card → Task 1; two-segment narration → Task 2; watermark/pillarbox defects from the trial render → Task 3; CLI wiring → Task 4. The no-username constraint is enforced structurally in Task 1 (`test_the_api_cannot_render_a_username` asserts no such parameter exists) rather than by convention.

**Placeholders.** None. Every step carries runnable code or an exact, checkable instruction.

**Type consistency.** `render_post_card_png(subreddit, title, size, cfg, font_paths, out_path, upvotes, comments)` is called only from `build_alpha_track`'s `card` branch with the `card` dict's keys. `narrate()` returns `body_words` in the `{word,start,end}` shape `build_two_segment_timeline` consumes. `font_paths` keeps the `{"caption","hook"}` shape already used by `build_alpha_track`. `build_timeline` is untouched and pinned by `test_existing_build_timeline_is_unchanged`.

**Dead air resolved.** `hook_lead_s` is no longer used by the two-segment path — narration starts at t=0 with the title. The silent-footage dead-air problem disappears as a consequence of this change rather than needing a separate config decision.
