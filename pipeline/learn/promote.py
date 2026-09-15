#!/usr/bin/env python3
"""
promote.py — propose/approve DEFAULT flips. Human-gated, mirroring the
reference implementation's content-defaults.json + proposals.json split
(/tmp/sffs-ai-video-pipeline/ab-testing/) and its promotion engine
(hermes-nous/sffs/promote.py): the autonomous loop may only DETECT and LIST
proposals; only a human can APPROVE or REJECT one. Approval is the only thing
that ever writes defaults.json's `defaults` — score.py and plan.py only read it.

`min_sample` (defaults.json's promotion.min_sample, default 5) is deliberately
STRICTER than learnings.json's front-runner min_n (3): naming a transient
front-runner is cheap to reverse next week; flipping a sticky default that
every future post inherits is a bigger decision.

The `type` dimension is EXCLUDED from automatic promotion on purpose. Its
arms (ask/value/joke/intro/observe) do not share one metric — ask is judged on
comments, everything else on likes (see rollup.py's PRIMARY_METRIC and the
correction it documents). Comparing "joke's median_likes" against "ask's
median_likes" to decide whether to promote `ask` -> `joke` as the default type
would silently re-commit exactly the cross-metric mistake that was already
caught and fixed once. `setting` is excluded too — it has no current default
to promote FROM (see dimensions.py).

    python promote.py detect                  # scan learnings.json, write pending proposals
    python promote.py list                    # show pending proposals
    python promote.py approve --id P-1         # human: apply a proposal
    python promote.py reject  --id P-1 --reason "..."
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_ROOT = os.path.dirname(HERE)
for _p in (CODE_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import personas  # noqa: E402
from score import data_paths, _read_json, _write_json, _now_iso  # noqa: E402
from dimensions import DIMENSIONS, dimensions_for  # noqa: E402

# Dimensions eligible for automatic default-flip DETECTION. `type` and
# `setting` are excluded — see module docstring.
PROMOTABLE_DIMENSIONS = ("slot", "audio", "density", "first_comment", "text_color")

DIM_TO_ROLLUP_KEY = {
    "slot": "by_slot", "audio": "by_audio", "density": "by_density",
    "first_comment": "by_first_comment", "text_color": "by_text_color",
}

PROMOTION_METRIC = "median_likes"


def detect(persona_name: str) -> list[dict]:
    persona = personas.resolve(persona_name)
    paths = data_paths(persona)
    learnings = _read_json(paths["learnings"], None)
    defaults_doc = _read_json(paths["defaults"], None)
    if not learnings or not defaults_doc:
        raise SystemExit("run score.py at least once before promote.py detect")

    promo_cfg = defaults_doc.get("promotion", {})
    min_sample = promo_cfg.get("min_sample", 5)
    min_abs = promo_cfg.get("min_abs_improvement", 2.0)
    min_rel = promo_cfg.get("min_rel_improvement", 0.2)
    defaults = defaults_doc["defaults"]

    proposals_doc = _read_json(paths["proposals"], {"proposals": [], "decisions_log": []})
    existing_pending = {(p["dimension"], p["arm"]) for p in proposals_doc["proposals"] if p["status"] == "pending"}
    next_id = 1 + max((int(p["id"].split("-")[1]) for p in proposals_doc["proposals"]), default=0)

    new_proposals = []
    for dim in PROMOTABLE_DIMENSIONS:
        cells = learnings["rollups"].get(DIM_TO_ROLLUP_KEY[dim], {})
        incumbent_arm = defaults.get(dim)
        incumbent = cells.get(incumbent_arm)
        if not incumbent or incumbent["n_with_metrics"] < min_sample or incumbent.get(PROMOTION_METRIC) is None:
            continue
        for arm, cell in cells.items():
            if arm == incumbent_arm or arm not in dimensions_for(persona).get(dim, ()):
                continue
            if cell["n_with_metrics"] < min_sample or cell.get(PROMOTION_METRIC) is None:
                continue
            delta = cell[PROMOTION_METRIC] - incumbent[PROMOTION_METRIC]
            rel = (delta / incumbent[PROMOTION_METRIC]) if incumbent[PROMOTION_METRIC] else None
            if delta >= min_abs and rel is not None and rel >= min_rel:
                if (dim, arm) in existing_pending:
                    continue
                new_proposals.append({
                    "id": f"P-{next_id}",
                    "date": _now_iso()[:10],
                    "dimension": dim,
                    "arm": arm,
                    "incumbent_arm": incumbent_arm,
                    "incumbent_cell": incumbent,
                    "challenger_cell": cell,
                    "metric": PROMOTION_METRIC,
                    "delta": round(delta, 2),
                    "rel_improvement": round(rel, 3),
                    "status": "pending",
                })
                next_id += 1

    proposals_doc["proposals"].extend(new_proposals)
    proposals_doc["updated_at"] = _now_iso()
    _write_json(paths["proposals"], proposals_doc)
    return new_proposals


def list_proposals(persona_name: str, status: str | None = "pending") -> list[dict]:
    persona = personas.resolve(persona_name)
    paths = data_paths(persona)
    doc = _read_json(paths["proposals"], {"proposals": []})
    return [p for p in doc["proposals"] if status is None or p["status"] == status]


def _decide(persona_name: str, proposal_id: str, approve: bool, reason: str | None) -> dict:
    persona = personas.resolve(persona_name)
    paths = data_paths(persona)
    proposals_doc = _read_json(paths["proposals"], None)
    if not proposals_doc:
        raise SystemExit("no proposals.json yet — run `promote.py detect` first")
    match = next((p for p in proposals_doc["proposals"] if p["id"] == proposal_id), None)
    if not match:
        raise SystemExit(f"no proposal {proposal_id!r}")
    if match["status"] != "pending":
        raise SystemExit(f"proposal {proposal_id!r} is already {match['status']!r}")

    match["status"] = "approved" if approve else "rejected"
    match["decided_at"] = _now_iso()
    if reason:
        match["decision_reason"] = reason
    proposals_doc.setdefault("decisions_log", []).append({
        "date": _now_iso()[:10], "proposal_id": proposal_id, "dimension": match["dimension"],
        "arm": match["arm"], "action": "approve" if approve else "reject", "status": "applied",
    })
    proposals_doc["updated_at"] = _now_iso()
    _write_json(paths["proposals"], proposals_doc)

    if approve:
        defaults_doc = _read_json(paths["defaults"], None)
        old = defaults_doc["defaults"][match["dimension"]]
        defaults_doc["defaults"][match["dimension"]] = match["arm"]
        defaults_doc.setdefault("history", []).append({
            "date": _now_iso()[:10], "dimension": match["dimension"],
            "from": old, "to": match["arm"], "proposal_id": proposal_id,
            "rationale": f"median_{PROMOTION_METRIC.split('_',1)[1]} {match['challenger_cell'][PROMOTION_METRIC]} "
                         f"vs incumbent {match['incumbent_cell'][PROMOTION_METRIC]} "
                         f"(n={match['challenger_cell']['n_with_metrics']} / {match['incumbent_cell']['n_with_metrics']})",
        })
        defaults_doc["updated_at"] = _now_iso()
        _write_json(paths["defaults"], defaults_doc)

        learnings = _read_json(paths["learnings"], None)
        if learnings is not None:
            learnings.setdefault("decisions_log", []).append({
                "date": _now_iso()[:10],
                "decision": f"DEFAULT PROMOTED: {match['dimension']} {old} -> {match['arm']} (proposal {proposal_id}).",
                "rationale": "Human-approved via promote.py approve. The old default "
                             "is automatically a testable challenger again from here on.",
                "dimension": match["dimension"], "action": "promote", "status": "applied",
            })
            learnings["updated_at"] = _now_iso()
            _write_json(paths["learnings"], learnings)
    return match


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect")
    lp = sub.add_parser("list")
    lp.add_argument("--status", default="pending")
    ap_ = sub.add_parser("approve"); ap_.add_argument("--id", required=True); ap_.add_argument("--reason", default=None)
    rj = sub.add_parser("reject"); rj.add_argument("--id", required=True); rj.add_argument("--reason", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "detect":
        new = detect(args.persona)
        print(f"{len(new)} new proposal(s)." if new else "no arm clears the promotion bar yet.")
        for p in new:
            print(f"  {p['id']}: {p['dimension']} {p['incumbent_arm']} -> {p['arm']} "
                  f"(+{p['delta']} {p['metric']}, {p['rel_improvement']:.0%})")
        return 0
    if args.cmd == "list":
        status = None if args.status == "all" else args.status
        for p in list_proposals(args.persona, status):
            print(f"  {p['id']} [{p['status']}] {p['dimension']} {p['incumbent_arm']} -> {p['arm']} "
                  f"(+{p['delta']} {p['metric']})")
        return 0
    if args.cmd == "approve":
        m = _decide(args.persona, args.id, True, args.reason)
        print(f"approved: {m['dimension']} -> {m['arm']}")
        return 0
    if args.cmd == "reject":
        m = _decide(args.persona, args.id, False, args.reason)
        print(f"rejected: {m['dimension']} -> {m['arm']}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
