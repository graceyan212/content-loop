#!/usr/bin/env python3
"""
adjust.py — let the loop change its own defaults when the evidence earns it.

    python learn/adjust.py --persona dani            # what it would change
    python learn/adjust.py --persona dani --go

THE GAP: score.py computes front_runners and plan.py biases toward them, but nothing
ever changed a DEFAULT. promote.py detects candidates and then waits for a human,
copied from Hermes where approve/reject is explicitly gated. So the loop has been
observing and proposing for days and adjusting nothing.

This closes it, with a bar high enough that it cannot be fooled by two lucky posts:

  1. BOTH arms need n >= MIN_ARM_N. A challenger with n=1 is an anecdote.
  2. The challenger's median must beat the incumbent by >= MIN_LIFT.
  3. The dimension must be ASSIGNED, not merely observed. Observed arms carry the
     scheduler's own selection bias — slots were picked by which slots the library
     had copy for, so "late-night wins" could just mean "we own late-night copy".
     Flipping a default on that would make the bias permanent.
  4. Comparison uses the primary metric for the TYPE, never a blended score.

Anything that passes 1, 2 and 4 but fails 3 is logged as a PROPOSAL for a human,
not applied. That is the honest split: the loop may act on experiments it ran, and
may only suggest from patterns it noticed.

Every change appends to decisions_log with the numbers that justified it, in the
shape promote.py writes and plan.py reads, so a flipped default retires the arm it
beat and the loop stops retesting it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "post"), os.path.join(ROOT, "copy")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas   # noqa: E402

MIN_ARM_N = 5        # per arm. Below this a "winner" is noise.
MIN_LIFT = 0.50      # challenger must beat incumbent by 50%
# Cohesion arms are judged on participation (comments per like), not reach.
# Judged on participation (comments per like), not reach. `register` is here because
# fear and provoke can farm comment volume from an audience that is arguing rather
# than joining — the ratio separates those.
COHESION_DIMENSIONS = {"community_move", "register"}

PRIMARY = {"ask": "comments", "value": "comments", "joke": "likes",
           "intro": "comments", "observe": "comments", "other": "comments",
           "pov": "comments",   # recognition -> "this is my house" replies
           # "Oddly specific mom events" are built to be SENT to someone, not replied
           # to, so shares are the honest success metric for them.
           #
           # MEASURED CAVEAT: this account has produced 3 shares TOTAL across 16 posts.
           # At that rate shares/1k will not accumulate enough signal to decide
           # anything, so this arm is effectively unfalsifiable for now. It stays
           # because scoring a share-format on comments would be actively misleading,
           # but treat an "event" verdict as pending indefinitely rather than as a
           # test in progress. If shares stay near zero, that is itself the result:
           # the format did not earn sends.
           "event": "shares"}

# Types whose success metric is not the comparison metric. A `type` flip involving one
# of these cannot be decided automatically: comparing an arm measured in shares against
# an arm measured in comments is not a comparison. These become proposals instead.
OFF_METRIC_TYPES = {t for t, m in PRIMARY.items() if m != "comments"}


def load(root, name, default=None):
    f = os.path.join(root, "learn", name)
    try:
        return json.load(open(f))
    except Exception:
        return default if default is not None else {}


def save(root, name, obj):
    f = os.path.join(root, "learn", name)
    tmp = f + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=2)
    os.replace(tmp, f)


def per_k(n, views):
    return (n / views * 1000) if views else 0.0


def arm_stats(posts, dim):
    """median comments-per-1k and likes-per-1k per arm, split by provenance."""
    by = {}
    for p in posts:
        v = p.get("variant") or {}
        arm = v.get(dim)
        if not arm or arm == "unknown":
            continue
        m = p.get("metrics") or {}
        views = m.get("views") or 0
        if not views:
            continue
        prov = p.get("variant_provenance") or "assigned"
        k = (arm, prov)
        by.setdefault(k, {"c": [], "l": [], "s": [], "p": [], "sv": [], "cp": [], "n": 0})
        by[k]["c"].append(per_k(m.get("comments") or 0, views))
        by[k]["l"].append(per_k(m.get("likes") or 0, views))
        by[k]["s"].append(per_k(m.get("shares") or 0, views))
        # Comments per LIKE, not per view. A like is a thumb-twitch; a comment is
        # someone deciding to say something. The ratio separates an audience that
        # consumes from one that participates, and participation is the closest
        # thing to "tight-knit" that a personal TikTok account can actually measure:
        # impressionSources (forYou vs follow) is null on non-business accounts and
        # Metricool's inbox does not carry TikTok, so repeat commenters are invisible.
        by[k]["p"].append((m.get("comments") or 0) / max(m.get("likes") or 0, 1))
        # Instagram-only, absent on every TikTok row: saves and completion. The
        # TikTok analytics payload has no field for either, which is a large part of
        # why judging this account on TikTok alone was judging it on its two weakest
        # signals. Collected as sparse lists so a pool with none of them simply has
        # no median rather than a misleading zero — a post nobody saved and a post on
        # a platform that does not report saves must not look like the same post.
        if m.get("saves") is not None:
            by[k]["sv"].append(per_k(m.get("saves") or 0, views))
        if m.get("completion") is not None:
            by[k]["cp"].append(m["completion"])
        by[k]["n"] += 1
    out = {}
    for (arm, prov), d in by.items():
        row = {"n": d["n"],
               "comments": statistics.median(d["c"]),
               "likes": statistics.median(d["l"]),
               "shares": statistics.median(d["s"]),
               "participation": statistics.median(d["p"])}
        if d["sv"]:
            row["saves"] = statistics.median(d["sv"])
        if d["cp"]:
            row["completion"] = statistics.median(d["cp"])
        out[(arm, prov)] = row
    return out


# PLATFORM POOLS. The key each platform's rows live under. These pools are NEVER
# unioned: TikTok "views" and Instagram "reach" are different measurements, so a
# median taken over both is a number with no referent.
PLATFORM_POOL = {"tiktok": "posts", "instagram": "ig_posts"}

# THE DECISION METRIC IS PER PLATFORM, and this is not a preference — on Instagram
# the TikTok metric is dead. Measured 2026-08-14 across 73 reels: 7 comments total,
# so comments-per-1k-reach rounds to 0.0 for EVERY arm. adjust.py ran clean against
# Instagram and reported "incumbent still leads" for six dimensions, which was not a
# finding — it was every arm tying at zero and max() returning the first one. A
# decision rule that cannot separate any pair of arms is the same as no decision rule,
# and it looks identical to a working one in the output.
#
# Completion (averageWatchTime / durationSeconds) is what Reels distribution actually
# runs on, it is populated on all 73, and it separates arms cleanly.
PLATFORM_METRIC = {"tiktok": "comments", "instagram": "completion"}

# RATIO METRICS NEED A DIFFERENT BAR THAN COUNT METRICS. MIN_LIFT is 50% relative,
# which is right for a per-1k count that can go from 2 to 20. Completion is bounded
# and clusters between about 0.4 and 0.9, so a 50% relative lift is close to
# unreachable — keeping one bar would have made the Instagram path permanently
# incapable of flipping anything while appearing to be armed.
#
# So ratio metrics use an ABSOLUTE floor in the metric's own units, stated as
# percentage points of completion.
RATIO_METRICS = {"completion"}
MIN_ABS_LIFT = {"completion": 0.10}


def evaluate(root, platform="tiktok"):
    db = load(root, "ab-database.json", {"posts": []})
    defaults_doc = load(root, "defaults.json", {"defaults": {}})
    defaults = defaults_doc.get("defaults", {})
    key = PLATFORM_POOL[platform]
    posts = [p for p in db.get(key, []) if (p.get("metrics") or {}).get("views")]

    applied, proposed, skipped = [], [], []
    for dim, incumbent in defaults.items():
        stats = arm_stats(posts, dim)
        if not stats:
            continue
        metric = PLATFORM_METRIC[platform]
        if platform == "tiktok" and dim in COHESION_DIMENSIONS:
            metric = "participation"
        # An arm with no value for this metric cannot be compared on it. Dropping the
        # arm is right; substituting 0 would rank "not measured" below "measured bad".
        stats = {k: v for k, v in stats.items() if v.get(metric) is not None}
        if not stats:
            skipped.append((dim, f"no arm has a {metric} value"))
            continue
        assigned = {a: s for (a, prov), s in stats.items() if prov == "assigned"}
        observed = {a: s for (a, prov), s in stats.items() if prov != "assigned"}
        pool, prov = (assigned, "assigned") if len(assigned) >= 2 else (observed, "observed")
        if len(pool) < 2:
            skipped.append((dim, f"only {len(pool)} arm(s) with data"))
            continue

        inc = pool.get(incumbent)
        if not inc:
            skipped.append((dim, f"incumbent {incumbent!r} has no data in this pool"))
            continue
        best_arm, best = max(pool.items(), key=lambda kv: kv[1][metric])
        if best_arm == incumbent:
            skipped.append((dim, f"incumbent {incumbent!r} still leads"))
            continue

        base = inc[metric] or 0.0
        lift = ((best[metric] - base) / base) if base else float("inf")
        row = {"dimension": dim, "from": incumbent, "to": best_arm, "metric": metric,
               "incumbent_median": round(base, 2), "challenger_median": round(best[metric], 2),
               "lift": None if lift == float("inf") else round(lift, 3),
               "n_incumbent": inc["n"], "n_challenger": best["n"], "provenance": prov}

        if inc["n"] < MIN_ARM_N or best["n"] < MIN_ARM_N:
            row["why_not"] = (f"need n>={MIN_ARM_N} on both arms "
                              f"(have {inc['n']} vs {best['n']})")
            skipped.append((dim, row["why_not"]))
            continue
        if metric in RATIO_METRICS:
            gain = (best[metric] or 0.0) - base
            row["abs_gain"] = round(gain, 3)
            floor = MIN_ABS_LIFT[metric]
            if gain < floor:
                row["why_not"] = (f"+{gain:.3f} {metric} below the "
                                  f"+{floor:.2f} absolute bar")
                skipped.append((dim, row["why_not"]))
                continue
            # A ratio metric may PROPOSE but never APPLY on its own. Completion is a
            # new decision surface the operator has not chosen yet, and swapping the
            # metric that governs the copy is an operator call — the same reason
            # --platform still defaults to tiktok. Auto-applying here would let a
            # metric nobody signed off on rewrite the content strategy quietly.
            row["why_not"] = (f"+{gain:.3f} {metric} clears the bar, but {metric} is "
                              f"not an operator-approved decision metric yet")
            proposed.append(row)
            continue
        if lift != float("inf") and lift < MIN_LIFT:
            row["why_not"] = f"lift {lift:.0%} below the {MIN_LIFT:.0%} bar"
            skipped.append((dim, row["why_not"]))
            continue
        if dim == "type" and (incumbent in OFF_METRIC_TYPES or best_arm in OFF_METRIC_TYPES):
            row["why_not"] = (f"{incumbent!r} vs {best_arm!r} are judged on different "
                              f"metrics ({PRIMARY.get(incumbent)} vs "
                              f"{PRIMARY.get(best_arm)}) — not an automatic comparison")
            proposed.append(row)
            continue
        if prov != "assigned":
            row["why_not"] = ("observed only — flipping a default on the scheduler's "
                              "own selection bias would make that bias permanent")
            proposed.append(row)
            continue
        applied.append(row)
    return defaults_doc, applied, proposed, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True)
    ap.add_argument("--go", action="store_true")
    # Default stays tiktok so the daily cycle's behaviour is unchanged by this flag
    # alone. Instagram is the better surface (100% delivery vs a measured 42%, and it
    # reports saves and watch time), but switching which platform decides the copy is
    # an operator call, not something to slip in as a default.
    ap.add_argument("--platform", choices=sorted(PLATFORM_POOL), default="tiktok")
    a = ap.parse_args(argv)
    p = personas.resolve(a.persona)
    root = p["root"]

    defaults_doc, applied, proposed, skipped = evaluate(root, a.platform)

    metric = PLATFORM_METRIC[a.platform]
    bar = (f"+{MIN_ABS_LIFT[metric]:.2f} absolute" if metric in RATIO_METRICS
           else f"+{MIN_LIFT:.0%} relative")
    print(f"platform: {a.platform}  ·  metric: {metric}  ·  bar: both arms "
          f"n>={MIN_ARM_N}, challenger {bar}, assigned provenance only\n")
    for r in applied:
        print(f"  FLIP  {r['dimension']}: {r['from']} -> {r['to']}  "
              f"({r['incumbent_median']} -> {r['challenger_median']} {r['metric']}/1k, "
              f"n={r['n_incumbent']}v{r['n_challenger']})")
    for r in proposed:
        print(f"  PROPOSE  {r['dimension']}: {r['from']} -> {r['to']}  ({r['why_not']})")
    for dim, why in skipped:
        print(f"  hold  {dim}: {why}")

    if not applied and not proposed:
        print("\nnothing to change")
        return 0
    if not a.go:
        print("\nDRY RUN — re-run with --go")
        return 0

    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if applied:
        for r in applied:
            defaults_doc["defaults"][r["dimension"]] = r["to"]
        defaults_doc["updated_at"] = now
        save(root, "defaults.json", defaults_doc)

    props = load(root, "proposals.json", {"proposals": [], "decisions_log": []})
    props.setdefault("decisions_log", [])
    for r in applied:
        props["decisions_log"].append({
            "date": now[:10], "dimension": r["dimension"], "arm": r["from"],
            "action": "reject", "status": "applied",
            "rationale": (f"auto-adjusted by learn/adjust.py: {r['to']} beat {r['from']} "
                          f"on {r['metric']}/1k ({r['challenger_median']} vs "
                          f"{r['incumbent_median']}) at n={r['n_challenger']}v"
                          f"{r['n_incumbent']}, assigned provenance"),
        })
    for r in proposed:
        # `platform` and `metric` are not decoration. The same dimension can have
        # opposite winners on the two surfaces (TikTok scores comments, Instagram
        # scores completion), so a proposal that does not say which surface produced
        # it cannot be acted on — and two contradictory rows would look like a bug
        # rather than the real finding they are.
        props.setdefault("proposals", []).append({
            "dimension": r["dimension"], "arm": r["to"], "status": "pending",
            "platform": a.platform, "rationale": r["why_not"],
            "detected_at": now, **r})
    save(root, "proposals.json", props)
    print(f"\napplied {len(applied)} flip(s), logged {len(proposed)} proposal(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
