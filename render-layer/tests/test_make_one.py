"""Tests for make_one.py — the stage-1 entry point.

The brief's two tests are here in intent but not in body, because as written
neither one could fail for a reason that matters:

  - `test_dry_run_reports_a_measured_rate` asserted `STUB_RATE_IS_SYNTHETIC is
    True` against a module-level `= True`, i.e. only that the module imports. Delete
    `build` entirely, or hardcode `chars_per_second`, and it still passed — while
    its comment claimed "chars_per_second must come from the alignment, never from a
    constant". That invariant is now pinned by moving the constant: the rate a run
    reports has to follow `STUB_CPS`/the alignment it was handed, so a hardcoded 14.0
    fails.
  - `test_dry_run_builds_a_video_without_touching_the_network` asserted nothing
    about the network (no socket block, no stub — it held only because the caller
    passed `dry_run=True`), nothing about the artifact beyond "a file exists", and
    it rendered out of the LIVE footage library: it wrote its own clip into
    `channels/main/footage/` and then rendered whatever `select_window` picked from
    that directory, which on the owner's machine is a 5237.542s clip, not the
    fixture. Its cleanup also deleted the real library's `manifest.json`. Every test
    here runs against a channel rooted in `tmp_path`, and the network is blocked at
    `socket` rather than trusted.

Two levels are used deliberately. ONE test renders the whole chain for real and
measures the artifact (canvas, codecs, frame count) — that is the entry point's
end-to-end contract and it costs an encode. The rest replace `captions
.build_alpha_track` and `compose.render` with recorders, because what they pin is
make_one's own arithmetic — which `total_s` it derives from an alignment and a
MEASURED mp3, and what it hands the compositor. `test_compose.py` pins the
compositor.
"""
import json, os, socket, subprocess
import pytest

import make_one
from voice import words

ROOT = os.path.dirname(os.path.abspath(make_one.__file__))
SHIPPED_SCRIPT = os.path.join(ROOT, "channels", "main", "scripts",
                              "hand-written-01.txt")
REAL_CHANNEL_JSON = os.path.join(ROOT, "channels", "main", "channel.json")

# The first two lines of the shipped script: enough words to be a real timeline
# (21 of them, MEASURED) and short enough that rendering it at the configured
# 1080x1920 is one encode rather than four.
EXCERPT = ("My neighbour once sent me an invoice for breathing near his fence. "
           "Forty dollars, itemised, with a late fee already applied.")


def _no_network(monkeypatch):
    """Make "without touching the network" an assertion instead of a test name."""
    def boom(*a, **k):
        raise AssertionError(
            "this path opened a socket; the dry run must touch no network")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def _clip(path, secs, w=360, h=640, audio=True):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
           f"color=c=blue:s={w}x{h}:r=30:d={secs}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={secs}",
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-t", str(secs),
            path]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


def _tone_mp3(path, secs, hz=220):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={hz}:duration={secs}", "-c:a", "libmp3lame",
                    path], capture_output=True, check=True)
    return path


def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_streams", "-show_format", path],
                       capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return v, a, d["format"]


def _channel(tmp_path, footage_secs=None, script_text=EXCERPT, **overrides):
    """A channel rooted in `tmp_path`, carrying the REAL channel.json.

    The config is the shipped one (so the canvas, hook_lead_s and tail_s under test
    are the ones that ship, not test-local inventions) with any overrides applied.
    Only the roots move, so nothing here can read or write the owner's library.
    """
    root = tmp_path / "ch"
    (root / "footage").mkdir(parents=True)
    (root / "scripts").mkdir()
    with open(REAL_CHANNEL_JSON, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update(overrides)
    cfg_path = root / "channel.json"
    cfg_path.write_text(json.dumps(cfg))
    if footage_secs:
        _clip(str(root / "footage" / "clip.mp4"), footage_secs)
    script = root / "scripts" / "s.txt"
    script.write_text(script_text)
    ch = {"name": "main", "root": str(root), "registry_entry": {}, "config": cfg,
          "channel_json_path": str(cfg_path)}
    return ch, str(script)


def _use(monkeypatch, ch):
    monkeypatch.setattr(make_one.channel, "resolve", lambda name=None: ch)


def _stub_pipeline(monkeypatch):
    """Recorders in place of the caption rasteriser and the compositor."""
    calls = {}

    def fake_track(timeline, size, cfg, font_paths, work_dir, out_path, fps,
                   hook_text=""):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        open(out_path, "wb").close()
        calls["track"] = {"size": size, "fps": fps, "hook_text": hook_text,
                          "events": len(timeline),
                          "end": timeline[-1]["end"] if timeline else None}
        return out_path

    def fake_render(segments, caption_track, narration_mp3, out_path, cfg, total_s,
                    **kw):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        open(out_path, "wb").close()
        calls["render"] = dict(kw, segments=segments, narration=narration_mp3,
                               total_s=total_s)
        return out_path

    monkeypatch.setattr(make_one.captions, "build_alpha_track", fake_track)
    monkeypatch.setattr(make_one.compose, "render", fake_render)
    return calls


def _uniform_alignment(text, cps):
    """A character alignment at an arbitrary rate, so a run's reported rate can be
    checked against a number that is NOT `STUB_CPS`."""
    step = 1.0 / cps
    return {"characters": list(text),
            "character_start_times_seconds": [i * step for i in range(len(text))],
            "character_end_times_seconds": [(i + 1) * step
                                            for i in range(len(text))]}


def _stub_tts(monkeypatch, alignment, mp3_path, source="normalized"):
    seen = {}

    def fake_synthesize(text, voice_id, model_id, cache_dir, api_key, http=None):
        seen.update(text=text, voice_id=voice_id, model_id=model_id,
                    api_key=api_key)
        return {"mp3_path": mp3_path, "alignment": alignment,
                "alignment_source": source, "chars": len(text), "cached": False}

    monkeypatch.setattr(make_one.tts, "synthesize", fake_synthesize)
    monkeypatch.setattr(make_one.tts, "resolve_api_key",
                        lambda roots: ("test-key", "test"))
    return seen


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------

def test_dry_run_builds_a_video_without_touching_the_network(tmp_path, monkeypatch):
    """The end-to-end one: the whole chain runs, and the mp4 is MEASURED.

    Kills, among others: an entry point that renders some other canvas than the
    configured 1080x1920 (the brief's version asserted only `os.path.isfile`), and a
    picture that does not cover the audio.
    """
    ch, script = _channel(tmp_path, footage_secs=20)
    _use(monkeypatch, ch)
    _no_network(monkeypatch)

    res = make_one.build(channel_name="main", script_path=script,
                         run_id="test-run", voice_id="stub", model_id="stub",
                         dry_run=True)

    assert os.path.isfile(res["out_path"])
    assert res["word_count"] == len(EXCERPT.split()) == 21
    assert res["total_s"] > 1.0
    # The rate and its label: the one number the stage exists to produce, and the
    # tag that says this one is not it.
    assert res["alignment_source"] == "stub"
    assert res["chars_per_second"] == round(
        words.chars_per_second(make_one._stub_alignment(EXCERPT)), 2)
    # total_s is hook_lead + the alignment's last stamp + tail, and the stub mp3
    # MEASURES that same narration length.
    want_total = round(2.0 + len(EXCERPT) / make_one.STUB_CPS + 0.6, 2)
    assert res["total_s"] == want_total
    assert res["narration_measured_s"] == pytest.approx(
        res["narration_aligned_s"], abs=make_one.STUB_AUDIO_TOL_S)

    v, a, fmt = _probe(res["out_path"])
    assert (int(v["width"]), int(v["height"])) == (1080, 1920)
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert a is not None and a["codec_name"] == "aac"
    assert int(v["nb_frames"]) == round(res["total_s"] * 30)


def test_the_shipped_script_is_the_one_the_stage_was_written_for(tmp_path):
    """Cheap pin on the deliverable file itself: the excerpt the fast tests use is
    really the head of it, and the whole thing is the size the gate was measured
    at."""
    with open(SHIPPED_SCRIPT, encoding="utf-8") as fh:
        text = fh.read().strip()
    assert len(text) == 401 and len(text.split()) == 76
    assert " ".join(text.split())[:len(EXCERPT)] == EXCERPT


# ---------------------------------------------------------------------------
# The rate — the thing the stage exists to measure
# ---------------------------------------------------------------------------

def test_dry_run_reports_a_measured_rate(tmp_path, monkeypatch):
    """chars_per_second must come from the alignment, never from a constant.

    Kills: `"chars_per_second": 14.0` (or any hardcoded figure), and a `total_s`
    that does not follow the alignment it was handed. STUB_CPS is moved to 7.0, so
    every number below is one no hardcoded 14.0 can produce.
    """
    ch, script = _channel(tmp_path, footage_secs=40)
    _use(monkeypatch, ch)
    _no_network(monkeypatch)
    calls = _stub_pipeline(monkeypatch)
    monkeypatch.setattr(make_one, "STUB_CPS", 7.0)

    res = make_one.build("main", script, "rate-run", "stub", "stub", dry_run=True)

    assert res["chars_per_second"] == 7.0
    assert res["alignment_source"] == "stub"
    assert res["narration_aligned_s"] == round(len(EXCERPT) / 7.0, 3)
    assert calls["render"]["total_s"] == pytest.approx(
        2.0 + len(EXCERPT) / 7.0 + 0.6, abs=1e-6)


def test_the_reported_rate_follows_a_real_alignment(tmp_path, monkeypatch):
    """The same invariant on the path that matters — the live TTS branch, which no
    test exercised at all. The alignment arrives at 9.0 chars/sec and is labelled
    `normalized`; both have to come back out."""
    ch, script = _channel(tmp_path, footage_secs=40)
    _use(monkeypatch, ch)
    calls = _stub_pipeline(monkeypatch)
    alignment = _uniform_alignment(EXCERPT, 9.0)
    mp3 = _tone_mp3(str(tmp_path / "n.mp3"), round(len(EXCERPT) / 9.0, 2))
    _stub_tts(monkeypatch, alignment, mp3)

    res = make_one.build("main", script, "live-run", "voice-x", "model-x")

    assert res["chars_per_second"] == 9.0
    assert res["alignment_source"] == "normalized"
    assert res["word_count"] == 21
    assert calls["render"]["total_s"] == pytest.approx(
        2.0 + len(EXCERPT) / 9.0 + 0.6, abs=0.03)


# ---------------------------------------------------------------------------
# The narration mp3 is MEASURED, not assumed
# ---------------------------------------------------------------------------

def test_narration_longer_than_its_alignment_is_not_cut_off(tmp_path, monkeypatch):
    """Kills: `total_s` derived from alignment stamps alone.

    ElevenLabs' mp3 can run past its last character stamp — a trailing breath is
    ordinary — and `compose.render` appends `-t total_s`, so the end of the speech
    was silently truncated with rc=0 and every output assertion passing (the
    artifact's audio stream is total_s long either way, because `amix` mixes against
    a total_s+1 silence bed).
    """
    ch, script = _channel(tmp_path, footage_secs=40)
    _use(monkeypatch, ch)
    calls = _stub_pipeline(monkeypatch)
    alignment = _uniform_alignment(EXCERPT, 14.0)      # last stamp 8.857s
    mp3 = _tone_mp3(str(tmp_path / "long.mp3"), 11.0)  # 2.14s of it past the stamps
    _stub_tts(monkeypatch, alignment, mp3)

    res = make_one.build("main", script, "long-run", "voice-x", "model-x")

    assert res["narration_aligned_s"] == round(len(EXCERPT) / 14.0, 3)
    assert res["narration_measured_s"] == pytest.approx(11.0, abs=0.03)
    assert calls["render"]["total_s"] == pytest.approx(2.0 + 11.0 + 0.6, abs=0.03)
    assert res["total_s"] == pytest.approx(13.6, abs=0.03)


def test_narration_shorter_than_its_alignment_is_refused(tmp_path, monkeypatch):
    """The other side of the same measurement: an mp3 that stops before the
    alignment does would caption words that are never spoken."""
    ch, script = _channel(tmp_path, footage_secs=40)
    _use(monkeypatch, ch)
    _stub_pipeline(monkeypatch)
    alignment = _uniform_alignment(EXCERPT, 14.0)     # last stamp 8.857s
    mp3 = _tone_mp3(str(tmp_path / "short.mp3"), 3.0)
    _stub_tts(monkeypatch, alignment, mp3)

    with pytest.raises(SystemExit) as e:
        make_one.build("main", script, "short-run", "voice-x", "model-x")
    msg = str(e.value)
    assert "MEASURES" in msg and "3.0" in msg and "8.857" in msg


# ---------------------------------------------------------------------------
# The configured voice_id
# ---------------------------------------------------------------------------

def test_the_configured_voice_id_is_used_when_no_voice_is_passed(tmp_path,
                                                                monkeypatch):
    """Kills: `cfg["voice_id"]` having no reader — an operator's configured voice
    ignored, and `FATAL: --voice is required` printed on top of it."""
    ch, script = _channel(tmp_path, footage_secs=40, voice_id="voice-from-config")
    _use(monkeypatch, ch)
    _stub_pipeline(monkeypatch)
    seen = _stub_tts(monkeypatch, _uniform_alignment(EXCERPT, 14.0),
                     _tone_mp3(str(tmp_path / "n.mp3"), 8.86))

    make_one.build("main", script, "cfg-voice-run", "", "model-x")
    assert seen["voice_id"] == "voice-from-config"

    # …and an explicit --voice still wins over it.
    make_one.build("main", script, "arg-voice-run", "voice-from-arg", "model-x")
    assert seen["voice_id"] == "voice-from-arg"


def test_no_voice_anywhere_is_a_named_error(tmp_path):
    with open(REAL_CHANNEL_JSON, encoding="utf-8") as fh:
        shipped = json.load(fh)
    assert "voice_id" in shipped, "the key this fallback reads must exist in config"
    with pytest.raises(SystemExit) as e:
        make_one.resolve_voice_id({"voice_id": ""}, "")
    assert "channel.json" in str(e.value) and "--voice" in str(e.value)


def test_main_resolves_the_configured_voice_before_it_renders(tmp_path,
                                                             monkeypatch):
    """The pre-flight in `main` reads the same config, so a run with a configured
    voice and no `--voice` gets past the guard instead of dying at it."""
    ch, script = _channel(tmp_path, footage_secs=None, voice_id="voice-from-config")
    _use(monkeypatch, ch)
    got = {}
    monkeypatch.setattr(make_one, "build",
                        lambda *a, **k: got.update(args=a) or {"out_path": "x"})

    assert make_one.main(["--script", script, "--run-id", "main-run"]) == 0
    assert got["args"] == ("main", script, "main-run", "voice-from-config",
                          "eleven_multilingual_v2", False)


def test_a_missing_script_is_a_named_error(tmp_path, monkeypatch):
    ch, _ = _channel(tmp_path, footage_secs=None)
    _use(monkeypatch, ch)
    with pytest.raises(SystemExit) as e:
        make_one.build("main", str(tmp_path / "nope.txt"), "r", "stub", "stub",
                       dry_run=True)
    assert "script not found" in str(e.value)
