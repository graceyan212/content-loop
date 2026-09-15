#!/usr/bin/env python3
"""make_one.py — stage 1 entry point: one script in, one finished mp4 out.

    python3 make_one.py --script channels/main/scripts/hand-written-01.txt --voice <id>
    python3 make_one.py --script ...               # --voice may be omitted when the
                                                   # channel's channel.json sets voice_id
    python3 make_one.py --script ... --dry-run     # stub voice, no network, no spend
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import capability, channel                       # noqa: E402
from render import footage, captions, compose    # noqa: E402
from voice import tts, words                     # noqa: E402

STUB_RATE_IS_SYNTHETIC = True
STUB_CPS = 14.0   # dry-run only; never used for a real measurement

# One mp3 frame at 44.1kHz is 1152 samples = 0.026122s, so the stub narration's
# length can only be a whole number of those. The measured slack turned out to be
# smaller than that: MEASURED across seven requested durations (0.31, 1.00, 2.57,
# 5.13, 12.34, 28.64, 30.07s) libmp3lame reported the requested figure back
# EXACTLY, delta +0.000000s every time, because it writes a LAME/Xing header
# carrying the true sample count and ffprobe reads it. The tolerance below is
# therefore pure slack rather than an allowance for observed drift, and it is
# deliberately two frames wide: what it exists to catch is an encode that
# produced a mostly-empty or truncated file, which is off by seconds, not by
# milliseconds.
MP3_FRAME_S = 1152.0 / 44100.0
STUB_AUDIO_TOL_S = 2 * MP3_FRAME_S + 1e-3

# How far the narration mp3's MEASURED length may sit from the alignment's last
# stamp before the two are treated as disagreeing rather than rounding. An mp3
# carries whole frames of 1152 samples, and ElevenLabs' mp3 formats run down to
# 22.05kHz, so one frame of granularity there is 1152/22050s; four of those is the
# allowance. The defects on either side of it are seconds wide, not frames:
# trailing breath past the final character stamp (audio longer than the alignment,
# which `-t total_s` would cut off) and a truncated download (audio shorter than
# the alignment, which captions words that are never spoken).
MP3_FRAME_S_SLOWEST = 1152.0 / 22050.0
NARRATION_MATCH_TOL_S = 4 * MP3_FRAME_S_SLOWEST


class BuildError(SystemExit):
    """Carries an actionable message. `main` lets it propagate to the shell."""


def _stub_alignment(text: str) -> dict:
    step = 1.0 / STUB_CPS
    chars = list(text)
    return {"characters": chars,
            "character_start_times_seconds": [i * step for i in range(len(chars))],
            "character_end_times_seconds": [(i + 1) * step for i in range(len(chars))]}


def _measure_audio_s(path: str) -> float:
    """What ffprobe MEASURES in `path`, or raise. Never an assumed length."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise BuildError(f"FATAL: could not probe {path!r}: "
                         f"{res.stderr.strip()[-300:]}")
    try:
        return float(json.loads(res.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as e:
        raise BuildError(f"FATAL: {path!r} reports no duration ({e})")


def _stub_narration(out_path: str, duration_s: float) -> str:
    """Synthesise the dry run's stand-in narration, and MEASURE what came out.

    Deliberately not `os.system(... >/dev/null 2>&1)`: that discards both the exit
    status and the diagnosis, so a missing libmp3lame or an unwritable cache dir
    leaves no mp3 behind and the run continues into `compose.render`, which then
    fails on a missing input several hundred lines away from the cause. The length
    is measured rather than assumed for the same reason it is everywhere else in
    this pipeline -- the narration length is what `total_s` was computed from, and
    an input we passed on a command line cannot certify what the encoder wrote.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    want = float(f"{duration_s:.2f}")     # what lavfi is actually asked for
    res = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=220:duration={want:.2f}",
         "-c:a", "libmp3lame", out_path],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise BuildError(
            f"FATAL: could not write the dry-run stub narration {out_path!r} "
            f"(ffmpeg rc={res.returncode}): {res.stderr.strip()[-400:]}")
    got = _measure_audio_s(out_path)
    if abs(got - want) > STUB_AUDIO_TOL_S:
        raise BuildError(
            f"FATAL: stub narration {out_path!r} MEASURES {got:.6f}s but "
            f"{want:.6f}s was requested (tolerance {STUB_AUDIO_TOL_S:.6f}s); the "
            f"dry run's total_s was derived from the requested figure, so the "
            f"narration and the picture would not agree")
    return out_path


def resolve_voice_id(cfg: dict, voice_id: str) -> str:
    """`--voice` wins; the channel's configured `voice_id` is the fallback.

    This function is why `channel.json`'s `voice_id` field means anything. Without
    it the field had NO reader in the repo -- an operator who put their chosen voice
    in the only file that carries that key, and then ran without `--voice`, got
    `FATAL: --voice is required` and no mention of the value they had configured.
    That is the same "configured value that silently does nothing" defect that
    wiring `watermark` through `compose.render` fixed.
    """
    v = (voice_id or "").strip()
    if v:
        return v
    configured = str((cfg or {}).get("voice_id") or "").strip()
    if configured:
        return configured
    raise BuildError(
        "FATAL: no voice to synthesise with. Pass --voice <id>, or set "
        '"voice_id" in the channel\'s channel.json (the key is there and is '
        "currently empty). --dry-run needs neither: it stubs the narration with a "
        "tone, touches no network and spends nothing.")


def build(channel_name, script_path, run_id, voice_id, model_id, dry_run=False) -> dict:
    ch = channel.resolve(channel_name)
    cfg = ch["config"]
    fonts_dir = os.path.join(HERE, "assets", "fonts")
    capability.check(fonts_dir)

    # Named, like the two guards below it, instead of a bare FileNotFoundError
    # traceback out of `open` -- this is an operator typing a path on a command line.
    if not os.path.isfile(script_path):
        raise BuildError(f"FATAL: script not found: {script_path}")
    with open(script_path, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        raise BuildError(f"FATAL: {script_path} is empty")

    cache_dir = os.path.join(ch["root"], "voice", "cache")
    if dry_run:
        alignment = _stub_alignment(text)
        narration_mp3 = os.path.join(cache_dir, "_dryrun.mp3")
        _stub_narration(narration_mp3, len(text) / STUB_CPS)
        cached = False
        # NOT "normalized": the alignment this run measured its rate from was
        # generated from STUB_CPS a few lines up. Labelling it keeps the one thing
        # the stage exists to produce -- a real measured rate -- impossible to
        # confuse with a synthetic one, in the returned dict as well as on stdout.
        alignment_source = "stub"
    else:
        voice_resolved = resolve_voice_id(cfg, voice_id)
        if voice_resolved != (voice_id or "").strip():
            print(f"  voice_id from {ch['channel_json_path']}: {voice_resolved}")
        voice_id = voice_resolved
        key, source = tts.resolve_api_key([HERE, ch["root"]])
        print(f"  ElevenLabs key from {source}")
        res = tts.synthesize(text, voice_id, model_id, cache_dir, key)
        alignment, narration_mp3, cached = res["alignment"], res["mp3_path"], res["cached"]
        alignment_source = res["alignment_source"]

    word_list = words.words_from_alignment(alignment)
    cps = words.chars_per_second(alignment)
    aligned_s = words.total_duration(word_list)
    # MEASURE the narration that will actually be muxed, on BOTH paths. The stub
    # was already measured; the real mp3 -- the one input this module does not
    # generate and therefore the only one that can disagree with its alignment --
    # was not, which inverted the rule. `total_s` decides `-t` on the render and
    # the length of the caption track, so an mp3 whose speech runs past the last
    # character stamp (a trailing breath is ordinary) was silently CUT at
    # hook_lead + aligned_s + tail_s with rc=0 and every output assertion passing:
    # the artifact's audio stream is total_s long either way, because `amix` runs
    # against a total_s+1 silence bed.
    narration_measured_s = _measure_audio_s(narration_mp3)
    if narration_measured_s < aligned_s - NARRATION_MATCH_TOL_S:
        raise BuildError(
            f"FATAL: narration {narration_mp3!r} MEASURES "
            f"{narration_measured_s:.3f}s but its alignment's last word ends at "
            f"{aligned_s:.3f}s (tolerance {NARRATION_MATCH_TOL_S:.3f}s). The "
            f"captions would show {aligned_s - narration_measured_s:.3f}s of words "
            f"that are never spoken. Delete that cache entry and re-synthesise.")
    narration_s = max(aligned_s, narration_measured_s)
    if narration_s > aligned_s:
        print(f"  narration MEASURES {narration_measured_s:.3f}s against an "
              f"alignment that ends at {aligned_s:.3f}s; total_s covers the "
              f"measured length so the tail is not cut")
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
    # The watermark is a configured value with two defined behaviours -- empty
    # renders nothing, non-empty renders the channel name inside the safe box --
    # and this is the only caller in a position to supply it. Left unwired,
    # `channel.watermark` has no reader and setting `watermark` in channel.json
    # silently does nothing at all.
    compose.render(window["segments"], track, narration_mp3, out_path, cfg,
                   total_s, narration_delay_s=hook_lead,
                   watermark_text=channel.watermark(ch),
                   watermark_font=os.path.join(fonts_dir, "Anton-Regular.ttf"),
                   work_dir=work)

    result = {"out_path": out_path, "total_s": round(total_s, 2),
              "word_count": len(word_list), "chars_per_second": round(cps, 2),
              "alignment_source": alignment_source,
              "narration_aligned_s": round(aligned_s, 3),
              "narration_measured_s": round(narration_measured_s, 3),
              "cached": cached, "dry_run": dry_run}
    print(json.dumps(result, indent=2))
    if not dry_run:
        # Conditional on the alignment track the rate was measured FROM. `tts.py`
        # accepts the raw track as a last resort and warns that a rate off it is
        # understated ("do NOT record it as measured_cps"), because the raw track
        # holds "$5" for audio that speaks "five dollars". Printing "put this in
        # channel.json" underneath that warning would hand the operator the
        # opposite instruction, last and loudest, and the number they recorded
        # would mis-size every script stage 2 writes.
        if alignment_source == "normalized":
            print(f"\nMEASURED narration rate: {cps:.2f} chars/sec — "
                  f"put this in channel.json for stage 2's word-count target.")
        else:
            print(f"\nDO NOT RECORD: rate {cps:.2f} chars/sec was measured from the "
                  f"{alignment_source!r} alignment track, not the normalized one, so "
                  f"it is understated. Re-run once ElevenLabs returns a "
                  f"normalized_alignment before writing measured_cps.")
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
    voice = a.voice
    if not a.dry_run:
        # Resolved here as well so a run with no voice anywhere dies before the
        # capability preflight and the script read, and so the channel's configured
        # voice_id is honoured rather than reported missing. `build` resolves it
        # again; passing the resolved value makes that a no-op.
        voice = resolve_voice_id(channel.resolve(a.channel)["config"], a.voice)
    build(a.channel, a.script, a.run_id, voice, a.model, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
