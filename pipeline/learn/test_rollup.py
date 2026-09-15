#!/usr/bin/env python3
"""
test_rollup.py — unit tests for the pure rollup math (rollup.py) and the
one-axis-deviation arm generator (dimensions.py). No I/O, no network, no
persona resolution — everything here runs against in-memory fixtures.

Run:  python -m pytest learn/test_rollup.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rollup import (  # noqa: E402
    median, round2, has_metrics, metric_value, primary_metric_value,
    group_rollup, compute_rollups, pick_front_runner, MIN_N, PRIMARY_METRIC,
)
from dimensions import (  # noqa: E402
    DIMENSIONS, DEFAULTED_DIMENSIONS, FALLBACK_DEFAULTS, build_arms,
    deviations, validate_defaults, undefaulted_dimensions,
)


def _post(type_="ask", views=100, likes=5, comments=2, shares=0, source="api", **variant_extra):
    variant = {"type": type_, "slot": "late-night", "audio": "none",
               "setting": "car", "density": "short", "first_comment": "no",
               "text_color": "light"}
    variant.update(variant_extra)
    metrics = None if source == "pending" else {
        "views": views, "likes": likes, "comments": comments, "shares": shares, "source": source,
    }
    return {"variant": variant, "metrics": metrics}


# ---------------------------------------------------------------------------
# median vs mean
# ---------------------------------------------------------------------------
def test_median_ignores_outlier_that_would_skew_a_mean():
    # 0, 0, 3, 14 -> mean is 4.25 (dragged up by the 14-like outlier); the
    # median (1.5) is what the "median, not mean" convention is protecting.
    vals = [0, 0, 3, 14]
    assert median(vals) == 1.5
    assert sum(vals) / len(vals) != median(vals)


def test_median_odd_length():
    assert median([5, 1, 3]) == 3


def test_median_empty_is_none():
    assert median([]) is None


def test_median_ignores_non_numeric_and_nan():
    assert median([1, 2, float("nan")]) == 1.5


def test_round2():
    assert round2(1.005) in (1.0, 1.01)  # binary float rounding tolerance
    assert round2(None) is None
    assert round2(2.0) == 2.0


# ---------------------------------------------------------------------------
# has_metrics / pending exclusion
# ---------------------------------------------------------------------------
def test_pending_post_excluded_from_metrics():
    p = _post(source="pending")
    assert not has_metrics(p)
    assert metric_value(p, "likes") is None
    assert primary_metric_value(p) is None


def test_mature_post_included():
    p = _post(type_="joke", likes=14, source="api")
    assert has_metrics(p)
    assert metric_value(p, "likes") == 14
    assert primary_metric_value(p) == 14  # joke's primary is likes


def test_primary_metric_is_comments_for_ask_and_likes_for_joke():
    ask = _post(type_="ask", likes=5, comments=8)
    joke = _post(type_="joke", likes=14, comments=1)
    # The corrected mapping: an `ask` post is judged on comments, a `joke` on
    # likes. Getting this backwards is the exact bug that made a 14-like joke
    # post score as a zero once already.
    assert primary_metric_value(ask) == 8
    assert primary_metric_value(joke) == 14
    assert PRIMARY_METRIC["ask"] == "comments"
    assert PRIMARY_METRIC["value"] == "likes"  # not "shares" — see rollup.py docstring


# ---------------------------------------------------------------------------
# min_n gating
# ---------------------------------------------------------------------------
def test_cell_below_min_n_is_flagged_not_meaningful():
    posts = [_post(likes=1), _post(likes=2)]  # n=2 < MIN_N (3)
    cells = group_rollup(posts, lambda p: p["variant"]["audio"])
    cell = cells["none"]
    assert cell["n_with_metrics"] == 2
    assert cell["meaningful"] is False


def test_cell_at_min_n_is_meaningful():
    posts = [_post(likes=1), _post(likes=2), _post(likes=3)]
    cells = group_rollup(posts, lambda p: p["variant"]["audio"])
    assert cells["none"]["n_with_metrics"] == MIN_N
    assert cells["none"]["meaningful"] is True


def test_pick_front_runner_refuses_below_min_n():
    posts = [_post(type_="joke", likes=100), _post(type_="joke", likes=100)]  # n=2
    rollups = compute_rollups(posts)
    best, reason = pick_front_runner(rollups["by_type"])
    assert best is None
    assert "min_n" in reason or str(MIN_N) in reason


def test_pick_front_runner_finds_best_once_min_n_met():
    posts = (
        [_post(type_="joke", likes=14)] * 3
        + [_post(type_="value", likes=1)] * 3
    )
    rollups = compute_rollups(posts)
    best, _ = pick_front_runner(rollups["by_type"])
    assert best == "joke"


# ---------------------------------------------------------------------------
# empty groups
# ---------------------------------------------------------------------------
def test_empty_post_list_yields_empty_rollups():
    rollups = compute_rollups([])
    for key in ("by_type", "by_slot", "by_audio", "by_setting", "by_density",
                "by_first_comment", "by_text_color"):
        assert rollups[key] == {}


def test_missing_dimension_key_excluded_not_crashed():
    # A post missing a variant field entirely (key_fn returns None) must be
    # dropped from that grouping, not raise or silently become a "None" bucket.
    posts = [{"variant": {"type": "ask"}, "metrics": None}]  # no "slot" key
    cells = group_rollup(posts, lambda p: p["variant"].get("slot"))
    assert cells == {}


# ---------------------------------------------------------------------------
# by_type carries a primary metric; every other dimension does not
# ---------------------------------------------------------------------------
def test_by_type_has_primary_metric_other_dimensions_do_not():
    posts = [_post(type_="ask", likes=1, comments=8), _post(type_="joke", likes=14, comments=1)]
    rollups = compute_rollups(posts)
    assert rollups["by_type"]["ask"]["primary_metric"] == "comments"
    assert rollups["by_type"]["joke"]["primary_metric"] == "likes"
    # by_audio mixes an `ask` and a `joke` post together (both audio="none")
    # -- it must NOT invent a single primary metric for that mixed group.
    assert "primary_metric" not in rollups["by_audio"]["none"]


# ---------------------------------------------------------------------------
# one-axis-deviation invariant (dimensions.py)
# ---------------------------------------------------------------------------
def test_control_arm_deviates_nothing():
    arms = build_arms(FALLBACK_DEFAULTS)
    control = next(a for a in arms if a["dimension"] == "control")
    assert deviations(FALLBACK_DEFAULTS, control["variant"]) == []


def test_every_non_control_arm_deviates_exactly_one_axis():
    arms = build_arms(FALLBACK_DEFAULTS)
    for a in arms:
        if a["baseline"]:
            continue
        devs = deviations(FALLBACK_DEFAULTS, a["variant"])
        assert devs == [a["dimension"]], f"{a} deviates on {devs}, expected only [{a['dimension']}]"


def test_current_default_never_listed_as_its_own_challenger():
    arms = build_arms(FALLBACK_DEFAULTS)
    for dim in DEFAULTED_DIMENSIONS:
        challengers = [a["arm"] for a in arms if a["dimension"] == dim]
        assert FALLBACK_DEFAULTS[dim] not in challengers


def test_arm_universe_is_exactly_all_arms_minus_current_default():
    arms = build_arms(FALLBACK_DEFAULTS)
    for dim in DEFAULTED_DIMENSIONS:
        challengers = {a["arm"] for a in arms if a["dimension"] == dim}
        assert challengers == set(DIMENSIONS[dim]) - {FALLBACK_DEFAULTS[dim]}


def test_promoting_a_new_default_makes_the_old_one_a_challenger_again():
    # Simulate promote.py flipping audio's default from autoAddMusic -> none.
    new_defaults = dict(FALLBACK_DEFAULTS)
    new_defaults["audio"] = "none"
    arms = build_arms(new_defaults)
    audio_challengers = {a["arm"] for a in arms if a["dimension"] == "audio"}
    assert "autoAddMusic" in audio_challengers  # old default is testable again
    assert "none" not in audio_challengers      # new default is never its own challenger


def test_setting_has_no_default_and_generates_no_arms():
    arms = build_arms(FALLBACK_DEFAULTS)
    assert all(a["dimension"] != "setting" for a in arms)
    assert "setting" in undefaulted_dimensions()
    assert "setting" not in DEFAULTED_DIMENSIONS


def test_validate_defaults_rejects_unknown_arm():
    bad = dict(FALLBACK_DEFAULTS)
    bad["audio"] = "not-a-real-arm"
    with pytest.raises(ValueError):
        validate_defaults(bad)


def test_validate_defaults_accepts_fallback_defaults():
    validate_defaults(FALLBACK_DEFAULTS)  # must not raise
