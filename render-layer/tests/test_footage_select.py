import signal

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


def test_multi_clip_fallback_does_not_hang_on_small_tail_remainder():
    # Regression for a hang: after 4x 5.0s clips, remaining lands at 0.02s
    # (inside the old buggy (1e-6, 0.05] skip window, which used `continue`
    # to bypass both `remaining -= take` and the iteration-count bail-out,
    # leaving `remaining` permanently stuck above the loop's exit threshold).
    def _timeout(signum, frame):
        raise TimeoutError("select_window hung on a small tail remainder")

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        w = footage.select_window(SHORTS, 20.02, "r")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    total_take = sum(seg["take_s"] for seg in w["segments"])
    assert abs(total_take - 20.02) < 0.01


def test_used_windows_are_avoided_in_multi_clip_fallback():
    # The single-clip path consults `used` via _free_starts; the multi-clip
    # concatenation fallback must offer the same anti-repeat guarantee
    # instead of hard-coding start_s=0.0 for every segment regardless of
    # what has already been shown.
    used = [{"path": "/x/s0.mp4", "start_s": 0.0, "take_s": 5.0}]
    w = footage.select_window(SHORTS, 22.0, "r", used=used)
    assert all(seg["path"] != "/x/s0.mp4" for seg in w["segments"])
