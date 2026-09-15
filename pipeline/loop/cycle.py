#!/usr/bin/env python3
"""
cycle.py — one autonomous cycle: read the numbers, decide, make, schedule.

Adapted from Hermes's `hermes/src/cycle.ts`. Same skeleton, different body: that
pipeline renders video with Remotion and narrates it with a cloned voice; this one
puts text on a photo. Kept: the phase order, the resumable run-state, the
do-not-touch bracket, deterministic-decides / LLM-writes. Dropped: Remotion, voice,
lip-sync, question banks, and `gitCommitPush()` — an unattended job should not also
be a code-deployment vector.

PHASES

  preflight        kill switch, lock, credentials, budget
  snapshot         record every pre-existing scheduled post          [do-not-touch]
  pull_and_score   Metricool analytics -> ab-database -> learnings
  generate         Opus 5 writes new posts when the pool runs low
  plan             decide the batch, deterministically
  adjust           flip a default when an assigned arm has earned it
  experiment       schedule the planned A/B arms FIRST, recording each assignment
  make             top up remaining gaps with ordinary posts
  verify           prove nothing pre-existing moved                  [do-not-touch]

State is one JSON file per run, written after EVERY phase, atomically. A cycle that
dies halfway is resumable: re-running the same run id skips posts already scheduled
rather than duplicating them. That property is why the per-post loop records status
before moving on.

The LLM never decides WHAT to test. Dimension rotation is deterministic and seeded
by run id, exactly as in Hermes. The LLM writes copy and judges quality; it does not
choose the experiment.

    python loop/cycle.py --dry-run      # decide and render, schedule nothing
    python loop/cycle.py                # full cycle
    python loop/cycle.py --run-id 2026-08-02
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "post"), os.path.join(ROOT, "copy"),
          os.path.join(ROOT, "render"), os.path.join(ROOT, "learn")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas            # noqa: E402
import metricool as mc     # noqa: E402
import guard               # noqa: E402

HORIZON_DAYS = 3          # keep this many days of calendar filled ahead
# per-persona, see personas.posts_per_day(). Kept only as the fallback.
POSTS_PER_DAY = 3
# Generate when the unposted pool drops below this. A fixed library drains at
# 3/day; without generation the loop is a scheduler with a queue, not a loop.
LOW_WATER = 25
GENERATE_BATCH = 8


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# run state
# ---------------------------------------------------------------------------
def state_path(root: str, run_id: str) -> str:
    return os.path.join(root, "learn", "runs", f"{run_id}.json")


def load_state(root: str, run_id: str) -> dict:
    f = state_path(root, run_id)
    if os.path.exists(f):
        try:
            return json.load(open(f))
        except Exception:
            pass
    return {"run_id": run_id, "started": now_iso(), "phases": {}, "posts": [],
            "snapshot": None, "verify": None}


def save_state(root: str, st: dict) -> None:
    """Atomic: tmp + rename, so a crash mid-write cannot corrupt the run record."""
    f = state_path(root, st["run_id"])
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f + ".tmp"
    json.dump(st, open(tmp, "w"), indent=2)
    os.replace(tmp, f)


def phase(st: dict, name: str, root: str, ok: bool = True, **info):
    st["phases"][name] = {"at": now_iso(), "ok": ok, **info}
    save_state(root, st)
    flag = "ok " if ok else "FAIL"
    print(f"  [{flag}] {name}" + (f"  {info}" if info else ""))


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------
def do_pull_and_score(persona: dict) -> dict:
    """
    Refresh ab-database + learnings from live Metricool analytics.

    MUST pass --persona. This used to call score.main([]) with an empty argv, and
    score.py's --persona defaults to personas.DEFAULT_PERSONA ("dani") — so EVERY
    persona's cycle pulled Dani's analytics and REWROTE Dani's ab-database.json and
    learnings.json, then read learnings from its own (empty) root and silently
    reported nothing. It is the only cross-persona leak found that mutates another
    persona's state rather than just reading it, and --dry-run did not stop it: a
    dry run of Ray's cycle overwrote Dani's live learning data.
    """
    root = persona["root"]
    import score
    try:
        res = score.main(["--persona", persona["name"]]) if hasattr(score, "main") else None
    except SystemExit:
        res = None
    lp = os.path.join(root, "learn", "learnings.json")
    learn = json.load(open(lp)) if os.path.exists(lp) else {}
    return {"status": str(learn.get("status", ""))[:120],
            "front_runners": learn.get("front_runners", {})}


def do_mirror(st: dict, root: str, run_id: str, creds: dict,
              persona: dict, dry: bool) -> list[str]:
    """Mirror unmirrored TikTok posts to Instagram + Facebook as Reels.

    Returns the new scheduler ids. They MUST be passed to guard.verify as
    expected_new, or the do-not-touch bracket reports every mirror as an unexpected
    calendar mutation and the cycle reads as though someone tampered with it.

    Never raises: TikTok is the primary surface and is already scheduled by the time
    this runs, so a Meta-side failure is logged and the cycle still succeeds.
    """
    if dry:
        print("  [dry] would mirror unmirrored TikTok posts to instagram+facebook")
        phase(st, "mirror", root, ok=True, note="dry")
        return []
    try:
        import crosspost
        before = set(guard.snapshot(creds))
        rc = crosspost.main(["--persona", persona["name"], "--days", str(HORIZON_DAYS), "--go"])
        made = sorted(set(guard.snapshot(creds)) - before)
        if made:
            guard.ledger_append(root, "post", len(made), run_id=run_id)
        st["mirrors"] = made
        phase(st, "mirror", root, ok=(rc == 0), created=len(made))
        return made
    except Exception as e:
        phase(st, "mirror", root, ok=False,
              error=f"{type(e).__name__}: {str(e)[:120]}")
        return []


def do_plan(root: str, run_id: str, creds: dict, persona: dict) -> list[dict]:
    """
    What to post, decided deterministically.

    Seeded by run id so the same day always plans the same batch — that is what
    makes a resumed run pick up where it left off instead of choosing differently.
    """
    import schedule_batch as sb
    rng = random.Random(run_id)
    # what the calendar is missing over the horizon
    d0 = datetime.date.today().strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=HORIZON_DAYS + 1)).strftime("%Y-%m-%dT00:00:00")
    st, body = mc._req("GET", "/v2/scheduler/posts", creds, params={"start": d0, "end": d1})
    have: dict = {}
    for x in (body if isinstance(body, list) else (body or {}).get("data") or []):
        if isinstance(x, dict):
            # TikTok only. IG/FB mirrors are separate entries; counting them makes
            # every day look twice as full and do_plan returns no gaps forever.
            if {(q.get("network") or "").lower() for q in (x.get("providers") or [])} \
                    != {"tiktok"}:
                continue
            dt = (x.get("publicationDate") or {}).get("dateTime") or ""
            if len(dt) >= 10:
                have[dt[:10]] = have.get(dt[:10], 0) + 1
    need = {}
    for d in range(HORIZON_DAYS):
        day = (datetime.date.today() + datetime.timedelta(days=d)).isoformat()
        gap = max(0, personas.posts_per_day(persona) - have.get(day, 0))
        if gap:
            need[day] = gap
    return [{"day": d, "count": n} for d, n in sorted(need.items())]


def run(persona_name: str | None, run_id: str, dry: bool) -> int:
    p = personas.resolve(persona_name)
    root = p["root"]
    st = load_state(root, run_id)
    print(f"cycle {run_id}  persona={p['name']}  dry_run={dry}")

    # ---- preflight -------------------------------------------------------
    guard.check_kill_switch(p["name"])
    creds = mc.load_creds()
    creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(p))
    guard.check_budget(root, "post", 0)
    phase(st, "preflight", root, blog=creds["METRICOOL_BLOG_ID"])

    # ---- snapshot (do-not-touch) ----------------------------------------
    if not st.get("snapshot"):
        st["snapshot"] = guard.snapshot(creds)
        phase(st, "snapshot", root, pre_existing=len(st["snapshot"]))
    else:
        print(f"  [ok ] snapshot  (resumed, {len(st['snapshot'])} pre-existing)")

    # ---- pull and score --------------------------------------------------
    try:
        scored = do_pull_and_score(p)
        phase(st, "pull_and_score", root, **{k: v for k, v in scored.items() if k == "status"})
    except Exception as e:
        # analytics are nice to have; a stale learnings file should not stop posting
        phase(st, "pull_and_score", root, ok=False, error=str(e)[:200])

    # ---- generate --------------------------------------------------------
    # Only when the pool is low. Generating every cycle would pile up unposted
    # copy and burn tokens for nothing.
    try:
        import generate as gen
        import respin
        import schedule_batch as sb
        import gallery as gal
        import batch as bt

        dead = sb.killed_uids(root)
        seen = sb.already_posted(root)

        def _unposted(texts) -> int:
            n = 0
            for t in texts:
                if not t:
                    continue
                if max((gal.similarity(t, s2) for s2 in seen), default=0.0) >= gal.MATCH_THRESHOLD:
                    continue
                n += 1
            return n

        # BOTH library formats. respin.library_by_uid() globs only value-*.md and
        # ask-*.md — the gallery.py format. Ray's library is drafts-*.md, the
        # batch.py format, so his 74 hooks counted as a pool of ZERO and the loop
        # generated a fresh batch every single cycle, burning tokens forever while
        # a full library sat unused. Counting both is also simply more correct:
        # every unposted line in either format is available to post.
        lib = respin.library_by_uid(root)
        gallery_pool = _unposted(
            (lp.get("caption") or lp.get("title") or lp.get("on-screen") or "")
            for uid, lp in lib.items() if uid not in dead
        )
        copy_dir = os.path.join(root, "copy")
        drafts_pool = _unposted(
            h["hook_text"] for h in bt.parse_hooks(
                copy_dir=copy_dir,
                files=bt.draft_files_in(copy_dir),
                territories=bt.persona_territories(p))
        )
        pool = gallery_pool + drafts_pool

        if pool < LOW_WATER:
            guard.check_budget(root, "llm", 1)
            made_copy = gen.generate(p, GENERATE_BATCH, dry=dry)
            phase(st, "generate", root, pool_was=pool, gallery=gallery_pool,
                  drafts=drafts_pool, generated=len(made_copy))
        else:
            phase(st, "generate", root, pool=pool, gallery=gallery_pool,
                  drafts=drafts_pool, skipped="pool above low water")
    except Exception as e:
        # A generation failure must not stop the cycle posting from the existing
        # library. Make nothing new; ship what is already written.
        phase(st, "generate", root, ok=False, error=str(e)[:200])

    # ---- adjust ----------------------------------------------------------
    # Runs AFTER scoring and BEFORE planning, so a flipped default steers this
    # cycle's batch rather than waiting for tomorrow's. It sat after `plan` first,
    # which meant it never ran at all on a full calendar — plan early-returns. Bar is deliberately high
    # (n>=5 both arms, +50% lift, assigned provenance only) — see learn/adjust.py.
    try:
        import adjust
        rc = adjust.main(["--persona", p["name"], "--go"])
        phase(st, "adjust", root, ok=(rc == 0))
    except Exception as e:
        phase(st, "adjust", root, ok=False, error=str(e)[:200])

    # ---- adjust, instagram -----------------------------------------------
    # A SECOND PASS ON THE SURFACE THAT ACTUALLY DELIVERS. TikTok ran a measured 42%
    # of submitted posts live while Instagram ran 100%, and TikTok's payload has no
    # saves and no watch time — so the pass above is judging arms on the weaker
    # surface and on its two weakest signals.
    #
    # This pass CANNOT flip a default: Instagram scores on completion, completion is
    # a ratio metric, and adjust.py only ever proposes from a ratio metric. So the
    # effect is that Instagram findings accumulate in proposals.json every day for
    # the operator, instead of only existing when someone runs the tool by hand.
    # Non-fatal and separate from the TikTok phase, so a reels outage cannot take
    # down the pass that does flip defaults.
    try:
        rc = adjust.main(["--persona", p["name"], "--platform", "instagram", "--go"])
        phase(st, "adjust_instagram", root, ok=(rc == 0))
    except Exception as e:
        phase(st, "adjust_instagram", root, ok=False, error=str(e)[:200])

    # ---- plan ------------------------------------------------------------
    gaps = do_plan(root, run_id, creds, p)
    st["gaps"] = gaps
    phase(st, "plan", root, gaps=gaps)
    if not gaps:
        # A full calendar means nothing new to SCHEDULE. It does not mean nothing to
        # MIRROR: experiment.py and manual runs add TikTok posts outside this loop, and
        # the calendar is full most days, so returning here left mirroring effectively
        # dead — it would only ever run on a day the loop also happened to fill a gap.
        print("  calendar already full over the horizon; nothing to make")
        mirrored = do_mirror(st, root, run_id, creds, p, dry)
        st["verify"] = guard.verify(creds, st["snapshot"], set(mirrored))
        phase(st, "verify", root, **{k: v for k, v in st["verify"].items()
                                     if k in ("before", "after", "unexpected_added")})
        st["finished"] = now_iso()
        save_state(root, st)
        return 0

    # ---- experiment ------------------------------------------------------
    # ORDER MATTERS. The designed arms claim gaps before the generic fill does,
    # so an experiment displaces filler rather than adding a 4th post to the day.
    # Run the other way round and the calendar is full before the experiment
    # gets a look in, which is exactly why plan.py used to be ignored.
    if dry:
        print("  [dry] would schedule planned A/B arms before generic fill")
    else:
        try:
            import experiment as exp
            rc = exp.main(["--persona", p["name"], "--count", "4", "--go"])
            phase(st, "experiment", root, ok=(rc == 0))
        except Exception as e:
            # A failed experiment must not stop the calendar being filled.
            phase(st, "experiment", root, ok=False, error=str(e)[:200])

    # ---- make ------------------------------------------------------------
    made: list[str] = []
    if dry:
        print(f"  [dry] would fill {sum(g['count'] for g in gaps)} slot(s): {gaps}")
    else:
        total = sum(g["count"] for g in gaps)
        guard.check_budget(root, "post", total)
        import schedule_batch as sb
        argv = ["--persona", p["name"], "--fill-to", str(personas.posts_per_day(p)),
                "--days", str(HORIZON_DAYS), "--start-day", "0", "--go"]
        before_ids = set(guard.snapshot(creds))
        rc = sb.main(argv)
        after_ids = set(guard.snapshot(creds))
        made = sorted(after_ids - before_ids)
        guard.ledger_append(root, "post", len(made), run_id=run_id)
        st["posts"] = made
        phase(st, "make", root, ok=(rc == 0), created=len(made))

    mirrored = do_mirror(st, root, run_id, creds, p, dry)

    # ---- verify (do-not-touch) ------------------------------------------
    st["verify"] = guard.verify(creds, st["snapshot"], expected_new=set(made) | set(mirrored))
    phase(st, "verify", root, **{k: v for k, v in st["verify"].items()
                                 if k in ("before", "after", "unexpected_added")})
    st["finished"] = now_iso()
    save_state(root, st)
    print(f"cycle {run_id} complete: {len(made)} post(s) scheduled, "
          f"{len(mirrored)} mirrored to instagram+facebook")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # REQUIRED, unlike every other script's --persona. This is the unattended
    # entry point: it schedules real posts to a real brand with no human watching.
    # personas.DEFAULT_PERSONA is "dani", so a Ray timer that omitted the flag
    # would silently run Dani's cycle against Dani's blog_id. Everywhere else a
    # default is a convenience; here it is a way to post as the wrong person.
    ap.add_argument("--persona", required=True,
                    help="persona name from personas.json. Required: an unattended "
                         "cycle must never infer which brand it is posting to.")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    run_id = a.run_id or datetime.date.today().isoformat()

    # Resolve before taking the lock: an unknown persona should fail loudly rather
    # than sit holding a lock. One lock per persona, under that persona's own root
    # (see guard.lock_file on why not /tmp), so Ray's cycle and Dani's no longer
    # block each other.
    p = personas.resolve(a.persona)

    try:
        with guard.RunLock(persona_name=p["name"], root=p["root"]):
            return run(a.persona, run_id, a.dry_run)
    except guard.Halt as h:
        print(f"HALT: {h}", file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
