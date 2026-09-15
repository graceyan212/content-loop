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

def test_chars_per_second_raises_on_ragged_arrays():
    bad = {"characters": ["a", "b", "c"],
           "character_start_times_seconds": [0.0, 0.1],
           "character_end_times_seconds": [0.1, 0.2]}
    with pytest.raises(words.AlignmentError):
        words.chars_per_second(bad)
