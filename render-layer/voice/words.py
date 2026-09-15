"""words.py — ElevenLabs character alignment to word timings. Pure, no I/O.

Always fed `normalized_alignment`, never `alignment`: the normalized track
reflects ElevenLabs' own expansion ($5 -> "five dollars", Dr. -> "Doctor").
Timing from one track while captioning the other desynchronises every word
after the first number or abbreviation.
"""
from __future__ import annotations


class AlignmentError(ValueError):
    pass


def _extract_arrays(alignment: dict) -> tuple[list, list, list]:
    """Pull the three parallel arrays out of an alignment dict and verify
    they're the same length. Shared by every function that reads an
    alignment, so a ragged/malformed response fails loud everywhere instead
    of only where someone remembered to check.
    """
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
    return chars, starts, ends


def words_from_alignment(alignment: dict) -> list[dict]:
    chars, starts, ends = _extract_arrays(alignment)

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
    chars, starts, ends = _extract_arrays(alignment)
    if not chars:
        raise AlignmentError("cannot measure rate from an empty alignment")
    span = float(ends[-1]) - float(starts[0])
    if span <= 0:
        raise AlignmentError(f"non-positive alignment span: {span}")
    return len(chars) / span
