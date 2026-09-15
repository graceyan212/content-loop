#!/usr/bin/env python3
"""
plan.py — read learnings.json + defaults.json + decisions_log, emit the NEXT
batch's variant plan. This is the point of the whole loop: everything else
(rollup.py's math, score.py's pull, promote.py's human gate) exists so this
command can answer "what should we post next?" without re-litigating anything
already settled.

Rules, in order:
  1. Never propose an arm that decisions_log has settled — a "drop_dimension"
     or "drop_arm" entry with status="applied" removes that dimension/arm from
     consideration permanently (append-only: the reason stays in the log, the
     arm just stops being offered).
  2. Every variant changes EXACTLY ONE axis from the current defaults
     (dimensions.py's one-axis-deviation invariant) — arms are "universe minus
     the current default" per dimension, so the reigning default is never
     tested against itself.
  3. Bias toward current front_runners: if a dimension already has a
     front-runner that is NOT the current default, that arm is queued first
     (it is the strongest lead to confirm past min_n).
  4. Round-robins across dimensions so a batch is not all one axis.
  5. `setting` never generates a variant (no default to deviate from) but
     IS listed as "needs a default set" in the notes.

    python plan.py                        # dani, default batch size
    python plan.py --persona chloe --count 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_ROOT = os.path.dirname(HERE)
for _p in (CODE_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import personas  # noqa: E402
from score import data_paths, _read_json  # noqa: E402
from dimensions import (build_arms, DEFAULTED_DIMENSIONS, undefaulted_dimensions,  # noqa: E402
                        dimensions_for)

DEFAULT_BATCH_SIZE = 4


def _settled(decisions_log: list[dict]) -> tuple[set[str], set[tuple[str, str]]]:
    """
    Arms a human has retired, so the loop stops re-running settled experiments.

    TWO BUGS LIVED HERE, both silent:

    1. WRONG FILE. This read learnings.json's decisions_log, which score.py writes
       as a NARRATIVE ({date, decision, rationale}) with no dimension or action
       field. The structured retirement decisions are written by promote.py into
       proposals.json. So the filter matched nothing, ever, and the one mechanism
       meant to stop the loop retesting a concluded experiment was dead code.

    2. WRONG ACTION NAMES. It looked for "drop_dimension"/"drop_arm"; promote.py
       writes "approve"/"reject". A rejected promotion means the operator has said
       no to that arm becoming the default, which is a settlement — so it retires.

    Both shapes are accepted now, so an explicit drop_* still works if anything
    ever writes one.
    """
    dims, arms = set(), set()
    for d in decisions_log or []:
        if d.get("status") != "applied":
            continue
        action, dim, arm = d.get("action"), d.get("dimension"), d.get("arm")
        if action == "drop_dimension" and dim:
            dims.add(dim)
        elif action in ("drop_arm", "reject") and dim and arm:
            arms.add((dim, arm))
    return dims, arms


def build_plan(persona_name: str, count: int) -> dict:
    persona = personas.resolve(persona_name)
    paths = data_paths(persona)
    defaults_doc = _read_json(paths["defaults"], None)
    learnings = _read_json(paths["learnings"], None)
    if not defaults_doc or not learnings:
        raise SystemExit("run score.py at least once before plan.py")

    defaults = defaults_doc["defaults"]
    # proposals.json, NOT learnings.json — see _settled's docstring
    proposals_doc = _read_json(paths["proposals"], {}) or {}
    dropped_dims, dropped_arms = _settled(proposals_doc.get("decisions_log", []))
    front_runners = learnings.get("front_runners", {})

    all_arms = [
        a for a in build_arms(defaults, dimensions_for(persona))
        if not a["baseline"]
        and a["dimension"] not in dropped_dims
        and (a["dimension"], a["arm"]) not in dropped_arms
    ]

    # Bias: front-runner arms (that aren't already the default) sort first
    # within their dimension; ties keep dimensions.py's declared arm order
    # (each arm's original position, captured before sorting disturbs it).
    def sort_key(indexed: tuple[int, dict]) -> tuple[int, int]:
        idx, a = indexed
        is_front_runner = front_runners.get(a["dimension"]) == a["arm"]
        return (0 if is_front_runner else 1, idx)
    ranked = [a for _, a in sorted(enumerate(all_arms), key=sort_key)]

    # Round-robin across dimensions so a 4-post batch isn't 4 slot arms.
    by_dim: dict[str, list[dict]] = {}
    for a in ranked:
        by_dim.setdefault(a["dimension"], []).append(a)
    order = [d for d in DEFAULTED_DIMENSIONS if d in by_dim]
    picked: list[dict] = []
    i = 0
    while len(picked) < count and any(by_dim.values()):
        dim = order[i % len(order)]
        if by_dim.get(dim):
            picked.append(by_dim[dim].pop(0))
        i += 1
        if i > 4 * count + len(order):
            break  # exhausted every available arm

    variants = []
    for n, a in enumerate(picked, start=1):
        # DERIVED, not enumerated. This was a hand-written dimension -> key map and
        # it KeyError'd the moment dimensions.py grew `list_style` — the same failure
        # shape as the parser regex that could not see `count_title`. Every rollup key
        # score.py writes is "by_<dimension>", so read it that way and treat a missing
        # rollup as no-data-yet rather than a crash: a brand new arm legitimately has
        # no history, which is exactly when the planner most needs to run.
        cells = learnings.get("rollups", {}).get(f"by_{a['dimension']}", {})
        seen = cells.get(a["arm"], {"n_posts": 0, "n_with_metrics": 0})
        variants.append({
            "batch_position": n,
            "dimension_under_test": a["dimension"],
            "arm": a["arm"],
            "variant": a["variant"],
            "rationale": a["rationale"],
            "is_front_runner": front_runners.get(a["dimension"]) == a["arm"],
            "already_seen": seen["n_posts"],
            "already_with_metrics": seen["n_with_metrics"],
        })

    single_arm = learnings.get("single_arm_dimensions", [])

    return {
        "persona": persona_name,
        "generated_at": defaults_doc.get("updated_at"),
        "current_defaults": defaults,
        "batch_size_requested": count,
        "batch_size_returned": len(variants),
        "variants": variants,
        "excluded": {
            "dropped_dimensions": sorted(dropped_dims),
            "dropped_arms": sorted(f"{d}:{a}" for d, a in dropped_arms),
            "no_default_to_deviate_from": sorted(undefaulted_dimensions().keys()),
        },
        "notes": (
            f"{learnings['status']} "
            f"Single-arm dimensions with zero comparison data: {single_arm or 'none'}. "
            "Every variant above changes exactly one axis from current_defaults; "
            "unset axes inherit the default. Front-runner bias only matters once "
            "front_runners stops being all-null (needs min_n per arm)."
        ),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", default=personas.DEFAULT_PERSONA)
    ap.add_argument("--count", type=int, default=DEFAULT_BATCH_SIZE)
    args = ap.parse_args(argv)

    plan = build_plan(args.persona, args.count)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
