import os, subprocess, json, hashlib, pytest
from PIL import Image
from render import captions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = {"caption": os.path.join(ROOT, "assets", "fonts", "TikTokSans-Variable.ttf"),
         "hook": os.path.join(ROOT, "assets", "fonts", "Anton-Regular.ttf")}
CFG = {"caption_weight": 900, "caption_size_px": 96, "hook_size_px": 118,
       "caption_center_y": 0.575,
       "safe_box": {"top": 0.10, "bottom": 0.18, "left": 0.06, "right": 0.14}}
SIZE = (360, 640)   # small canvas keeps the test fast
FPS = 30

WORDS = [{"word": "one", "start": 0.0, "end": 0.4},
         {"word": "two", "start": 0.5, "end": 0.9}]

# 10ms inter-word gaps: SHORTER than MIN_EVENT_S (1/60s), so no blank event is
# emitted between words and the gap has to be absorbed by the next word rather than
# dropped. The brief's WORDS fixture has 0.1s gaps, which is the other regime
# entirely -- a blank IS emitted there, so it never exercises this path.
SUB_FRAME_GAP_S = 0.010
GAPPY = [{"word": f"w{i}", "start": round(i * 0.21, 6), "end": round(i * 0.21 + 0.2, 6)}
         for i in range(20)]
HOOK_LEAD_S = 1.0
GAPPY_TOTAL_S = HOOK_LEAD_S + GAPPY[-1]["end"] + 1.0

# 12 back-to-back words SHORTER than one output frame (5ms against 33.3ms), then one
# normal-length word. Sub-frame words are the regime where a caption can be dropped
# from the track entirely, and the only lever that would show more of them -- a
# per-event duration floor -- pays for it by pushing every later caption late. TINY is
# back-to-back on purpose: with no inter-word gaps there is nothing for a floor to eat
# into, so its cost lands entirely on ANCHOR, which is 300ms and therefore always
# sampled. That makes the assertion un-vacuous whatever the tiny words do.
SUB_FRAME_WORD_S = 0.005
TINY_N = 12
ANCHOR_S = 0.30
TINY = [{"word": f"t{i}", "start": round(i * SUB_FRAME_WORD_S, 6),
         "end": round((i + 1) * SUB_FRAME_WORD_S, 6)} for i in range(TINY_N)]
TINY.append({"word": "anchor", "start": round(TINY_N * SUB_FRAME_WORD_S, 6),
             "end": round(TINY_N * SUB_FRAME_WORD_S + ANCHOR_S, 6)})
TINY_TOTAL_S = HOOK_LEAD_S + TINY[-1]["end"] + 0.5


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    return (next(s for s in data["streams"] if s["codec_type"] == "video"),
            data["format"])


def _decode_frames(path, size):
    """`(alpha digest, opaque pixel count)` per decoded frame, in one decode pass.

    The frame size is MEASURED off the stream and asserted against `size` before
    any byte slicing, because the slicing is what turns bytes into frames -- a
    probe that assumed the size it asked for would happily report a plausible
    fiction about a differently-sized track.
    """
    video, _ = _probe(path)
    measured = (int(video["width"]), int(video["height"]))
    assert measured == tuple(size), f"decoded {measured}, requested {tuple(size)}"
    nbytes = size[0] * size[1] * 4
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        stdout=subprocess.PIPE)
    frames = []
    try:
        while True:
            buf = proc.stdout.read(nbytes)
            if len(buf) < nbytes:
                break
            alpha = buf[3::4]
            frames.append((hashlib.sha256(alpha).hexdigest(),
                           len(alpha) - alpha.count(0)))
    finally:
        proc.stdout.close()
        proc.wait()
    return frames


def _alpha_digest(png_path):
    with Image.open(png_path) as im:
        return hashlib.sha256(im.convert("RGBA").getchannel("A").tobytes()).hexdigest()


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


def test_a_sub_frame_gap_is_absorbed_rather_than_left_as_a_hole():
    """The gapless invariant, exercised in the regime that actually breaks it.

    A gap shorter than MIN_EVENT_S emits no blank event, so the next word event has
    to start where the previous one ended or the gap becomes a HOLE. A hole is not
    a blank frame -- `build_alpha_track` writes durations, so the hole time is
    simply missing and every later caption slides earlier by the running total.
    MEASURED on the pre-fix code, 40 words at 10ms gaps: 39 holes, 0.390s of
    missing time, and -0.010s of caption drift per word on the encoded track.
    """
    tl = captions.build_timeline(GAPPY, hook_lead_s=HOOK_LEAD_S,
                                 total_s=GAPPY_TOTAL_S)
    assert not any(e["kind"] == "blank" for e in tl[1:-1]), \
        "10ms gaps must be under the blank-emission threshold, or this tests nothing"
    for i, (a, b) in enumerate(zip(tl, tl[1:])):
        assert b["start"] == pytest.approx(a["end"]), f"hole after event {i}"
    span = tl[-1]["end"] - tl[0]["start"]
    assert sum(e["end"] - e["start"] for e in tl) == pytest.approx(span)


def test_absorbing_a_gap_never_moves_a_word_more_than_one_sub_frame_tick():
    """Absorption is a bounded, non-cumulative cost, paid once per word."""
    tl = captions.build_timeline(GAPPY, hook_lead_s=HOOK_LEAD_S,
                                 total_s=GAPPY_TOTAL_S)
    spoken = [e for e in tl if e["kind"] == "word"]
    assert len(spoken) == len(GAPPY)
    for w, ev in zip(GAPPY, spoken):
        early = (w["start"] + HOOK_LEAD_S) - ev["start"]
        assert 0 <= early <= captions.MIN_EVENT_S + 1e-9, \
            f"{ev['text']} moved {early:+.4f}s"


def test_word_onsets_do_not_drift_late_in_the_track(tmp_path):
    """The Stage 1 gate check, measured on the encoded artifact.

    Every word's onset is compared against the timeline event it came from by
    fingerprinting the alpha channel of every decoded frame and hashing it back to
    the PNG the caption was rasterised from. Tolerance is the two quantisations we
    do not control (the 1/25s concat-demuxer grid plus one output frame); the point
    of the test is the SECOND assertion, that the error at the end of the track is
    no worse than at the start. MEASURED on the pre-fix code, -0.010s per word:
    -0.190s on the last of 20 words, -0.390s on the last of 40, -1.490s on the last
    of 150 (~45 frames). MEASURED on the fixed code: 0.033s worst case at all three
    lengths, and +0.0002s of growth from head-third to tail-third at 150 words.
    """
    tl = captions.build_timeline(GAPPY, hook_lead_s=HOOK_LEAD_S,
                                 total_s=GAPPY_TOTAL_S)
    work = str(tmp_path / "work")
    out = captions.build_alpha_track(tl, SIZE, CFG, FONTS, work,
                                     str(tmp_path / "cap.mov"), fps=FPS)
    frames = _decode_frames(out, SIZE)
    assert len(frames) > 0

    ref = {}
    for ev in (e for e in tl if e["kind"] == "word"):
        png = captions.render_word_png(ev["text"], SIZE, CFG, FONTS["caption"],
                                       str(tmp_path / "ref" / f"{ev['text']}.png"))
        ref[_alpha_digest(png)] = ev["text"]

    onset = {}
    for i, (digest, _) in enumerate(frames):
        if digest in ref:
            onset.setdefault(ref[digest], i / float(FPS))

    tol = captions.CONCAT_TB_S + 1.0 / FPS + 1e-3
    drift = []
    for ev in (e for e in tl if e["kind"] == "word"):
        assert ev["text"] in onset, f"{ev['text']} never appears in the track"
        d = onset[ev["text"]] - ev["start"]
        assert abs(d) <= tol, f"{ev['text']} drifted {d:+.3f}s (tolerance {tol:.3f}s)"
        drift.append(d)
    third = max(len(drift) // 3, 1)
    head = sum(drift[:third]) / third
    tail = sum(drift[-third:]) / third
    assert abs(tail - head) <= 1.0 / FPS, \
        f"drift grows through the track: {head:+.3f}s at the head, {tail:+.3f}s at the tail"


def test_a_sub_frame_word_is_dropped_without_displacing_the_word_after_it(tmp_path):
    """The one behaviour traded away, pinned so it cannot be traded back silently.

    A word shorter than one output frame may fall between 30fps samples and never be
    displayed. The obvious lever -- restoring the old `max(dur, MIN_EVENT_S)` floor --
    is measured and rejected in `build_alpha_track`'s comment, because it lengthens
    every short event and so pushes every later caption late.

    This asserts the half of the tradeoff that is a property of the CODE. How many
    sub-frame words survive is not: MEASURED at 30fps on 12 words of 10ms, survival
    ran 0/12 to 6/12 across seven spacings with the code untouched (6/12 at 20ms
    spacing, 3/12 at 210ms, 0/12 at 100ms), so no count is asserted here and none
    belongs in a report without its fixture beside it. What IS a property of the code
    is that dropping a word costs the words after it nothing. MEASURED on this
    fixture: `anchor` drifts +0.0067s as shipped, and +0.1400s (4.2 frames, 1.88x the
    tolerance) with the floor restored -- the floor's 5/12 visibility bought with 20.9x
    the error on every following caption.

    Note the span guard in `build_alpha_track` does NOT catch this and cannot: with
    the floor reinstated, differencing absolute boundaries re-anchors the running
    total, so `cursor` still lands on the span exactly while the onsets in between are
    already displaced. MEASURED -- both floor shapes (floored duration only, and the
    faithful pre-fix floor-plus-summed-cursor) reach the encode and fail here at
    +0.1400s rather than raising. This assertion is the only thing that sees it.
    """
    tl = captions.build_timeline(TINY, hook_lead_s=HOOK_LEAD_S, total_s=TINY_TOTAL_S)
    span = tl[-1]["end"] - tl[0]["start"]
    assert sum(e["end"] - e["start"] for e in tl) == pytest.approx(span), \
        "a floor on event durations would break this before it broke the timing"

    out = captions.build_alpha_track(tl, SIZE, CFG, FONTS, str(tmp_path / "work"),
                                     str(tmp_path / "cap.mov"), fps=FPS)
    frames = _decode_frames(out, SIZE)

    ref = {}
    for ev in (e for e in tl if e["kind"] == "word"):
        png = captions.render_word_png(ev["text"], SIZE, CFG, FONTS["caption"],
                                       str(tmp_path / "ref" / f"{ev['text']}.png"))
        ref[_alpha_digest(png)] = ev["text"]
    onset = {}
    for i, (digest, _) in enumerate(frames):
        if digest in ref:
            onset.setdefault(ref[digest], i / float(FPS))

    tol = captions.CONCAT_TB_S + 1.0 / FPS + 1e-3
    anchor = next(e for e in tl if e.get("text") == "anchor")
    assert "anchor" in onset, \
        "the 300ms anchor word is longer than a frame and must always be sampled"
    d = onset["anchor"] - anchor["start"]
    assert abs(d) <= tol, (
        f"the word after {TINY_N} sub-frame words drifted {d:+.4f}s "
        f"(tolerance {tol:.4f}s) -- a per-event duration floor is back")

    # Whichever sub-frame words do survive must also be where they were declared;
    # absorbing or dropping one must never shift the ones that made it.
    for ev in (e for e in tl if e["kind"] == "word" and e["text"] in onset):
        d = onset[ev["text"]] - ev["start"]
        assert abs(d) <= tol, f"{ev['text']} drifted {d:+.4f}s (tolerance {tol:.4f}s)"


def test_the_track_keeps_its_alpha_channel_and_carries_real_ink(tmp_path):
    """Codec, size and duration all pass for a track of 90 empty frames.

    Both gutting experiments were run and both passed the four assertions above
    verbatim: blanking `render_word_png`/`render_hook_png` gave 0 opaque pixels
    across all 90 frames (codec qtrle, 360x640, duration 3.000000, pix_fmt argb),
    and re-encoding the same manifest with `format=rgb24` instead of `format=rgba`
    gave 20736000/20736000 opaque pixels -- 100.0% of the track -- which under Task
    10's `overlay=0:0` would cover the gameplay for the whole clip. So the ink and
    the alpha channel are asserted here, on the artifact.
    """
    tl = captions.build_timeline(WORDS, hook_lead_s=1.0, total_s=3.0)
    out = captions.build_alpha_track(tl, SIZE, CFG, FONTS, str(tmp_path / "work"),
                                     str(tmp_path / "cap.mov"), fps=FPS,
                                     hook_text="my neighbour billed me")
    v, _ = _probe(out)
    assert v["pix_fmt"] in captions.ALPHA_PIX_FMTS, \
        f"{v['pix_fmt']} carries no alpha; overlaying it would hide the footage"

    frames = _decode_frames(out, SIZE)
    total_px = SIZE[0] * SIZE[1]

    def at(t):
        return frames[int(round(t * FPS))][1]

    assert at(0.5) > 0, "the hook card is on screen at 0.5s and must have ink"
    assert at(1.2) > 0, "the word 'one' is on screen at 1.2s and must have ink"
    assert at(1.45) == 0, "the inter-word gap must be fully transparent"
    assert at(2.5) == 0, "the tail must be fully transparent"
    assert max(at(0.5), at(1.2)) < total_px, \
        "no frame may be fully opaque -- the footage has to show through"


def test_frames_are_not_reused_for_a_different_hook_or_a_different_canvas(tmp_path):
    """A second build into an existing work dir must not inherit the first one.

    Task 11 re-runs with a fixed `--run-id`, and `work` is derived from the run id,
    so re-running after an edit lands in a populated work dir on the normal path.
    MEASURED pre-fix: the `_hook.png` digest was identical (081ca13ec239) after a
    second call with completely different `hook_text`, because the cache keyed the
    hook on a constant filename; and a call at 1080x1920 into a 360x640 work dir
    returned a 360x640 track without raising, because the stale word PNGs set the
    concat stream size.
    """
    work = str(tmp_path / "work")
    tl = captions.build_timeline(WORDS, hook_lead_s=1.0, total_s=3.0)
    lead = int(0.5 * FPS)

    first = captions.build_alpha_track(tl, SIZE, CFG, FONTS, work,
                                       str(tmp_path / "a.mov"), fps=FPS,
                                       hook_text="my neighbour billed me")
    second = captions.build_alpha_track(tl, SIZE, CFG, FONTS, work,
                                        str(tmp_path / "b.mov"), fps=FPS,
                                        hook_text="a completely different hook card")
    a = _decode_frames(first, SIZE)[lead]
    b = _decode_frames(second, SIZE)[lead]
    assert a[1] > 0 and b[1] > 0
    assert a[0] != b[0], "the second track carried the first call's hook card"

    smaller = (180, 320)
    third = captions.build_alpha_track(tl, smaller, CFG, FONTS, work,
                                       str(tmp_path / "c.mov"), fps=FPS)
    v, _ = _probe(third)
    assert (int(v["width"]), int(v["height"])) == smaller


@pytest.mark.parametrize("bad,why", [
    ([{"kind": "word", "text": "alpha", "start": 0.0, "end": 0.5},
      {"kind": "word", "text": "bravo", "start": 2.0, "end": 2.5},
      {"kind": "word", "text": "delta", "start": 4.0, "end": 4.5}], "hole"),
    ([{"kind": "word", "text": "alpha", "start": 0.0, "end": 0.5},
      {"kind": "word", "text": "bravo", "start": 0.2, "end": 0.9}], "overlap"),
])
def test_a_non_gapless_timeline_is_rejected_instead_of_mis_timed(tmp_path, bad, why):
    """The precondition is checked, not assumed.

    `build_alpha_track` takes the track LENGTH from the timeline span and the track
    CONTENT from the durations; those agree only if the events are adjacent. MEASURED
    on the word-only timeline above pre-fix: `-t` was emitted as the 4.5s span, the
    encoded track was 2.033s, and "bravo" -- declared at 2.0s -- rendered at 0.533s,
    with no exception and no warning.
    """
    with pytest.raises(captions.CaptionError, match=f"not gapless: {why}"):
        captions.build_alpha_track(bad, SIZE, CFG, FONTS, str(tmp_path / "work"),
                                   str(tmp_path / "cap.mov"), fps=FPS)


def test_an_empty_timeline_is_rejected(tmp_path):
    with pytest.raises(captions.CaptionError, match="empty timeline"):
        captions.build_alpha_track([], SIZE, CFG, FONTS, str(tmp_path / "work"),
                                   str(tmp_path / "cap.mov"), fps=FPS)
