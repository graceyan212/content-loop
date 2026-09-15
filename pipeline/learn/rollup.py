#!/usr/bin/env python3
"""
rollup.py — the pure A/B rollup math. NO I/O, NO network, NO persona resolution —
importable and testable completely offline. Ported faithfully from the reference
implementation's hermes/src/rollup.ts (see /tmp/sffs-ai-video-pipeline), adapted
for THIS pipeline's shape: one shared metric vocabulary (views/likes/comments/
shares) instead of a single eng_rate, because the primary metric here depends on
post `type` (see PRIMARY_METRIC below) and mixing metrics across types is exactly
the bug this pipeline already made once (see the comment on PRIMARY_METRIC).

A "cell" is the per-group summary the loop decides on: how many posts, how many
have MATURE (non-pending) metrics, and the MEDIAN of each raw metric — never the
mean. Median is deliberate: small samples with near-zero-reach outliers make
means meaningless (this is posts.json's whole justification for tracking `match`
confidence and refusing to treat every joined row as equally trustworthy).

Grouping by `type` additionally reports `median_primary` using that group's own
primary metric (comments for `ask`, likes for everything else) — safe, because
every post in a `by_type` cell shares one type and therefore one primary metric.
Grouping by any OTHER dimension (slot, audio, setting, density, first_comment,
text_color) mixes types together, so those cells deliberately do NOT report a
single "primary" figure — only the raw per-metric medians. Inventing one number
there would silently re-commit the corrected mistake: scoring an `ask` post's
comments against a `joke` post's likes as if they were the same yardstick.
"""

from __future__ import annotations

from statistics import median as _stat_median
from typing import Any, Callable, Iterable

# Primary metric per content `type` — established from the first 5 real posts,
# corrected once already. The original guess was that `value` posts earn SHARES
# (saved/sent rather than replied to). Two value posts in, shares were 0 and 0 —
# zero support — while the account's best post by far (a self-aware `joke`, 14
# likes) was being scored as a zero because it was typed `value` and judged on
# shares. Comments only move on `ask` posts (replying is the free, obvious
# response to a question); likes is the metric that discriminates everywhere
# else. Do not re-derive this from a handful of posts again without the same
# scrutiny — see learn/track.py's PRIMARY_METRIC for the original note.
PRIMARY_METRIC: dict[str, str] = {
    "intro": "likes",
    "ask": "comments",
    "value": "likes",
    "joke": "likes",
    "observe": "likes",
}
SECONDARY_METRIC = "comments"

RAW_METRICS = ("views", "likes", "comments", "shares")

# No dimension may name a front-runner below this many posts WITH metrics.
MIN_N = 3


def median(nums: Iterable[float]) -> float | None:
    vals = [float(n) for n in nums if isinstance(n, (int, float)) and n == n]  # n==n filters NaN
    if not vals:
        return None
    return _stat_median(vals)


def round2(n: float | None) -> float | None:
    return None if n is None else round(n * 100) / 100


def has_metrics(post: dict) -> bool:
    """A post counts toward metrics once it has non-pending, non-null numbers.
    Mirrors reference hasMatureMetrics(): source != 'pending' AND the metric
    is actually present, not just an empty shell."""
    m = (post or {}).get("metrics")
    if not m or m.get("source") == "pending":
        return False
    return m.get("views") is not None


def metric_value(post: dict, name: str) -> float | None:
    if not has_metrics(post):
        return None
    v = post["metrics"].get(name)
    return float(v) if isinstance(v, (int, float)) else None


def primary_metric_value(post: dict) -> float | None:
    """The value of THIS post's own primary metric, keyed by its type. None if
    the type is unknown or metrics are not mature."""
    t = (post.get("variant") or {}).get("type") or post.get("type")
    name = PRIMARY_METRIC.get(t)
    return metric_value(post, name) if name else None


def _cell(posts_in_group: list[dict], with_primary: bool) -> dict[str, Any]:
    mature = [p for p in posts_in_group if has_metrics(p)]
    cell: dict[str, Any] = {
        "n_posts": len(posts_in_group),
        "n_with_metrics": len(mature),
        "meaningful": len(mature) >= MIN_N,
    }
    for m in RAW_METRICS:
        cell[f"median_{m}"] = round2(median(metric_value(p, m) for p in mature))

    if with_primary:
        # Safe here ONLY because the caller guarantees every post in this group
        # shares one `type` (see group_rollup's `by_type` special-case).
        types = {(p.get("variant") or {}).get("type") or p.get("type") for p in posts_in_group}
        if len(types) == 1:
            primary_name = PRIMARY_METRIC.get(next(iter(types)))
            cell["primary_metric"] = primary_name
            cell["median_primary"] = round2(median(primary_metric_value(p) for p in mature)) if primary_name else None
        else:
            # Defensive: should never happen (group_rollup only sets
            # with_primary=True for the by_type grouping), but refuse to guess
            # rather than silently mixing metrics if it ever does.
            cell["primary_metric"] = None
            cell["median_primary"] = None
            cell["note"] = "mixed types in group; no single primary metric — see rollup.py docstring"
    return cell


def group_rollup(
    posts: list[dict],
    key_fn: Callable[[dict], str | None],
    with_primary: bool = False,
) -> dict[str, dict]:
    """Group posts by key_fn(post) (None/'' keys are dropped) and summarize each
    group into a cell. Pure — no I/O."""
    groups: dict[str, list[dict]] = {}
    for p in posts or []:
        k = key_fn(p)
        if not k:
            continue
        groups.setdefault(k, []).append(p)
    return {k: _cell(v, with_primary) for k, v in groups.items()}


def _variant_get(post: dict, field: str) -> str | None:
    v = (post.get("variant") or {})
    return v.get(field) if v.get(field) is not None else post.get(field)


def compute_rollups(posts: list[dict]) -> dict[str, dict]:
    """All decision rollups from ab-database posts[]. Pure. One key per
    dimension in dimensions.DIMENSIONS."""
    arr = posts or []
    return {
        "by_type": group_rollup(arr, lambda p: _variant_get(p, "type"), with_primary=True),
        "by_slot": group_rollup(arr, lambda p: _variant_get(p, "slot")),
        "by_audio": group_rollup(arr, lambda p: _variant_get(p, "audio")),
        "by_setting": group_rollup(arr, lambda p: _variant_get(p, "setting")),
        "by_density": group_rollup(arr, lambda p: _variant_get(p, "density")),
        "by_first_comment": group_rollup(arr, lambda p: _variant_get(p, "first_comment")),
        "by_text_color": group_rollup(arr, lambda p: _variant_get(p, "text_color")),
    }


def pick_front_runner(cells: dict[str, dict], metric: str = "median_primary", min_n: int = MIN_N) -> tuple[str | None, str]:
    """Highest `metric` among cells with n_with_metrics >= min_n. Returns
    (label_or_None, reason). Falls back to median_likes when a cell has no
    median_primary (e.g. every non-`type` dimension)."""
    best: str | None = None
    best_val = float("-inf")
    for label, cell in (cells or {}).items():
        if cell.get("n_with_metrics", 0) < min_n:
            continue
        val = cell.get(metric)
        if val is None:
            val = cell.get("median_likes")
        if val is None:
            continue
        if val > best_val:
            best, best_val = label, val
    if best is None:
        return None, f"no arm has n_with_metrics >= {min_n} yet"
    return best, f"median {metric}={best_val} at n={cells[best]['n_with_metrics']}"
