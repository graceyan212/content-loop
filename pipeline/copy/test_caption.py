"""
Regression tests for the caption gate.

The first case is the exact caption that published on 2026-08-01 as an unreadable
wall. It must never pass again.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import caption as cap


SHIPPED_BROKEN = """Making friends at 38 is so much harder than anyone warned me about. These are the only places it has actually worked for me. What places am I missing?

The bleachers at practice. Same four women every Tuesday, and it took about six weeks before anyone said a word.
The 20 minutes in the pickup line. Roll the window down. That is genuinely the whole trick.
A rec league I signed up for by myself at 38, which was terrifying for exactly one week.

#momfriends #momsoftiktok #momlife"""


def test_the_caption_that_actually_shipped_is_blocked():
    ok, why = cap.validate(SHIPPED_BROKEN)
    assert not ok
    assert "unmarked list" in why


def test_build_output_passes_its_own_gate():
    post = {"title": "Where I have actually made mom friends",
            "caption": "Making friends at 38 is harder than anyone warned me. What am I missing?",
            "items": ["The bleachers at practice. Same four women every Tuesday.",
                      "The 20 minutes in the pickup line. Roll the window down.",
                      "A rec league I signed up for alone at 38."],
            "hashtags": "#momfriends #momsoftiktok #momlife"}
    ok, why = cap.validate(cap.build(post))
    assert ok, why


def test_items_without_blank_lines_are_blocked():
    ok, why = cap.validate("Lead line here.\n\n• one thing\n• two thing\n\n#tag")
    assert not ok
    assert "blank line" in why


def test_numbered_when_title_promises_a_count():
    post = {"title": "3 things I stopped doing", "caption": "A lead.",
            "items": ["one", "two", "three"], "hashtags": "#a"}
    out = cap.build(post)
    assert "1. one" in out and "2. two" in out


def test_bulleted_when_count_does_not_match():
    post = {"title": "5 things I stopped doing", "caption": "A lead.",
            "items": ["one", "two"], "hashtags": "#a"}
    out = cap.build(post)
    assert "• one" in out and "1. one" not in out


def test_long_item_warns_but_does_not_block():
    long_item = "x" * 140
    post = {"title": "Things", "caption": "A lead.",
            "items": [long_item, "short one"], "hashtags": "#a"}
    text = cap.build(post)
    ok, _ = cap.validate(text)
    assert ok, "length must not block — it would take the unattended loop down"
    assert cap.readability_warnings(text), "but it must warn"


def test_prose_caption_with_no_list_passes():
    ok, why = cap.validate("He said none tonight. There is a folder in that "
                           "backpack. What is your move here.\n\n#momsoftiktok")
    assert ok, why


def test_empty_is_blocked():
    assert not cap.validate("")[0]
