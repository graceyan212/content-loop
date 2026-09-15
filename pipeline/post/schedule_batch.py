#!/usr/bin/env python3
"""
schedule_batch.py — pick N unposted ideas, render, upload, and schedule them all.

The missing link. batch.py renders and stops; publish.py posts exactly one thing.
This walks the whole path for a run of posts across several days:

    library post -> still (matched by setting) -> JPEG -> Metricool media -> scheduled

    # see the plan, touch nothing
    python post/schedule_batch.py --per-day 2 --days 1 --dry-run
    python post/schedule_batch.py --per-day 3 --days 3 --dry-run

    # actually schedule
    python post/schedule_batch.py --per-day 2 --days 1 --go

Nothing is scheduled without --go. A dry run prints the exact slot times, images
and copy so the whole plan is reviewable before anything reaches a live account.

WHY JPEG, ALWAYS: TikTok photo posts reject PNG, and they reject it at PUBLISH
time — Metricool accepts the media, schedules the post, holds it PENDING, then
fails. That cost a slot once. upload.py refuses PNG outright now; this module
only ever hands it the .jpg the renderer emits alongside the PNG.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import random
import re
import sys
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "render"), os.path.join(ROOT, "copy"),
          os.path.join(ROOT, "learn")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas                      # noqa: E402
import metricool as mc               # noqa: E402
import upload as up                  # noqa: E402
import compose as cp
import overlay as ov                 # noqa: E402
import settings as cset              # noqa: E402
import gates                         # noqa: E402
import caption as cap        # noqa: E402  (copy/caption.py - one formatter)
import gallery as gal                # noqa: E402  (parse_posts + similarity, one impl)
import timing as tmg                 # noqa: E402  (copy/timing.py — content -> time of day)

# Slots and content->time affinity both live in copy/timing.py so the dashboard,
# the planner and this scheduler cannot disagree about what "pickup" means.
SLOTS = tmg.SLOTS


def load_drafts_library(root: str) -> list[dict]:
    """
    drafts-*.md hooks, adapted into the library-post shape this module expects.

    WHY THIS EXISTS. gal.library_files() globs value-*.md and ask-*.md — the
    gallery format. batch.py renders from drafts-*.md instead. Those are two
    different libraries, and this module only ever read the first one.

    Dani has BOTH (11 gallery files, 3 drafts), so nobody noticed. Ray has only
    drafts, so `schedule_batch.py --persona ray` reported "0 in library · 0 not yet
    posted" and would have scheduled nothing at all: he could render and could not
    post, and the dry run was the only thing that surfaced it.

    text_density is carried through explicitly rather than re-derived. Downstream,
    density decides which safe_text_zone the renderer gets, and for a gallery post
    it is inferred from len(items) >= 3 — a drafts hook has no items, so every wall
    would have silently used the (smaller) short zone and overflowed onto his face.
    """
    import batch as bt

    copy_dir = os.path.join(root, "copy")
    # Honour the never-schedule prefix here too: this loader globs drafts-*.md
    # directly and does NOT go through gal.library_files().
    files = [f for f in bt.draft_files_in(copy_dir)
             if not os.path.basename(f).startswith(gal.NEVER_SCHEDULE)]
    if not files:
        return []
    out = []
    for h in bt.parse_hooks(copy_dir=copy_dir, files=files,
                            territories=_territories_for_root(root)):
        pid = f"{h['territory']}-{h['source_line']:03d}"
        post = {
            "id": pid,
            "src": h["source_file"],
            "on-screen": h["hook_text"],
            "text_density": h["text_density"],
            "territory": h["territory"],
            "claim_posture": h.get("claim_posture", "no-product"),
        }
        post["setting"] = cset.setting_of(post)
        post["uid"] = f"{h['source_file']}::{pid}"
        out.append(post)
    return out


def _territories_for_root(root: str) -> tuple[str, ...]:
    """This persona's territory whitelist, resolved from its own persona.json."""
    import batch as bt
    import json as _json
    pj = os.path.join(root, "persona.json")
    cfg = {}
    if os.path.isfile(pj):
        try:
            cfg = _json.load(open(pj, encoding="utf-8"))
        except Exception:
            cfg = {}
    return bt.persona_territories({"config": cfg})


def load_library(root: str) -> list[dict]:
    posts = []
    for f in gal.library_files(root):
        posts += gal.parse_posts(f)
    for p in posts:
        p["setting"] = cset.setting_of(p)
        p["uid"] = f"{p['src']}::{p['id']}"
    # BOTH library formats. See load_drafts_library for why.
    posts += load_drafts_library(root)
    return posts


def scheduled_captions(creds: dict) -> list[str]:
    """
    Captions of posts already SITTING IN the Metricool calendar, published or not.

    Without this, two batch runs in a row both pick from the same "unposted" pool
    and schedule the same idea twice: ab-database only knows what has already gone
    live, not what is queued.
    """
    import datetime as _dt
    d0 = (_dt.date.today() - _dt.timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
    d1 = (_dt.date.today() + _dt.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    for params in ({"start": d0, "end": d1}, {"from": d0, "to": d1}):
        st, body = mc._req("GET", "/v2/scheduler/posts", creds, params=params)
        if st != 200:
            continue
        items = body if isinstance(body, list) else (body.get("data") or [])
        return [x.get("text") or "" for x in items if isinstance(x, dict)]
    return []


def killed_uids(root: str) -> set[str]:
    """
    Post uids the operator rejected, from copy/killed.json.

    Dashboard kills previously lived only in browser localStorage, so the scheduler
    happily re-queued a post the operator had X'd out. This file is the shared truth.
    """
    out = set()
    f = os.path.join(root, "copy", "killed.json")
    if os.path.exists(f):
        try:
            out |= {r["uid"] for r in json.load(open(f)).get("killed", []) if r.get("uid")}
        except Exception:
            pass
    # Anything the operator DELETED in the approval queue is a rejection of the
    # idea, not just of that one scheduled instance - otherwise the next fill
    # re-queues the same post and they have to reject it again.
    d = os.path.join(root, "learn", "decisions.json")
    if os.path.exists(d):
        try:
            out |= {r["uid"] for r in json.load(open(d)).get("decisions", [])
                    if r.get("action") == "delete" and r.get("uid")}
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Scheduled-uid ledger. The reliable dedup key.
# ---------------------------------------------------------------------------
# already_posted() + scheduled_captions() compare the LIBRARY'S on-screen text
# against METRICOOL'S caption. Those are different fields: the caption is written
# by the caption model from the hook, so for any drafts-*.md post they do not
# resemble each other and gal.similarity never clears MATCH_THRESHOLD. Measured on
# Ray: 12 posts on the calendar, 2 excluded, 135 of 137 still reported "not yet
# posted" — and two near-identical "The summary skips the part where..." posts
# landed a day apart because of it. The fuzzy matcher is a decent backstop for
# posts made before this ledger existed; it is not a dedup mechanism.
#
# The uid is exact, is known at schedule time, and needs no matching at all.
SCHED_LEDGER = "scheduled-uids.json"


def scheduled_uids(root: str) -> set[str]:
    f = os.path.join(root, "learn", SCHED_LEDGER)
    if not os.path.exists(f):
        return set()
    try:
        return {r["uid"] for r in json.load(open(f)).get("scheduled", []) if r.get("uid")}
    except Exception:
        return set()


def record_scheduled(root: str, uid: str, metricool_id, when: str) -> None:
    """Append one scheduled post. Written per-post rather than once at the end so
    a crash mid-batch cannot lose the posts that already went out."""
    f = os.path.join(root, "learn", SCHED_LEDGER)
    try:
        db = json.load(open(f)) if os.path.exists(f) else {}
    except Exception:
        db = {}
    db.setdefault("description",
                  "Post uids already scheduled to Metricool. Exact dedup key; the "
                  "caption similarity matcher cannot do this job. Never hand-edit "
                  "to force a repost -- delete the Metricool post instead.")
    db.setdefault("scheduled", []).append(
        {"uid": uid, "metricool_id": metricool_id, "scheduled_for": when})
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "w") as fh:
        json.dump(db, fh, indent=2)


def already_posted(root: str) -> list[str]:
    """Normalized captions of everything already live or already scheduled."""
    out = []
    for name in ("ab-database.json", "posts.json"):
        f = os.path.join(root, "learn", name)
        if not os.path.exists(f):
            continue
        try:
            db = json.load(open(f))
        except Exception:
            continue
        out += list(db.get("live_captions") or [])
        for r in db.get("posts", []):
            for k in ("caption", "screen", "on_screen", "text"):
                if r.get(k):
                    out.append(r[k])
    return out


# Shapes the operator has rejected outright. Recorded as a comment in the copy
# files first, which nothing reads - so it kept getting scheduled. Encoded here.
# 2026-08-01: one-number "be honest" questions went 0 kept / 4 cut, while
# asks-for-advice went 4/4. The shape earned 7 comments ONCE and was then repeated
# until it read as a formula.
# Widened 2026-08-07. The original tokens missed a whole Ray batch written in the
# same shape: "answer honestly" is not "be honest", "no judgement" is not "no
# judgment" (British spelling -- also a voice violation, every persona here is
# Americanized), and "give me the real time" is not "real number". Matching exact
# phrases when the shape is what was rejected means the next author rewrites round
# the regex without knowing the shape is the thing.
#
# NOTE this is a REPETITION guard, not a ban. The measured record is that this shape
# produced the account's best comments-per-1k (7.4 vs 1.4 for the next best) and was
# then run into the ground. So it stays available -- it just must not be the whole
# calendar. Keeping one or two and varying the rest is the correct use.
REJECTED_SHAPE = re.compile(
    r"\bbe honest\b|\banswer honestly\b|\bhonestly\b"
    r"|\bno judg(?:e)?ments?\b"
    r"|\breal number\b|\bthe real (?:time|age|answer|one)\b"
    r"|\bi will go first\b|\bgive me (?:the|a) number\b", re.I)


def is_rejected_shape(post: dict) -> bool:
    return bool(REJECTED_SHAPE.search(
        (post.get("on-screen") or post.get("title") or "")))


def pick_still(post: dict, stills: list[dict], recent: list[str]) -> dict | None:
    """Setting-compatible, and never the same image twice in a row."""
    ok = [s for s in stills if cset.compatible(post["setting"], s.get("setting_tag", "any"))]
    if not ok:
        ok = stills
    fresh = [s for s in ok if s["file"] not in recent[-1:]]
    pool = fresh or ok
    # least-recently-used first, so a small pool spreads instead of clustering
    pool.sort(key=lambda s: (recent[::-1].index(s["file"]) if s["file"] in recent else 999),
              reverse=True)
    return pool[0] if pool else None


def slot_time(day_offset: int, slot: str, tz, rng: random.Random) -> datetime.datetime:
    h1, m1, h2, m2 = SLOTS[slot]
    base = datetime.datetime.now(tz) + datetime.timedelta(days=day_offset)
    start = base.replace(hour=h1, minute=m1, second=0, microsecond=0)
    end = base.replace(hour=h2, minute=m2, second=0, microsecond=0)
    span = int((end - start).total_seconds() // 60)
    # jitter off the clock marks: everyone schedules on :00 and :30
    return start + datetime.timedelta(minutes=rng.randint(0, max(0, span)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--per-day", type=int, default=2)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--start-day", type=int, default=1,
                    help="0 = today, 1 = tomorrow (default)")
    ap.add_argument("--type", action="append", default=[],
                    help="restrict to these post types/files, e.g. --type ask")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--first", action="append", default=[],
                    help="uid(s) to place before anything else, e.g. "
                         "--first value-03-money-home-sanity.md::21")
    ap.add_argument("--fill-to", type=int, default=None,
                    help="top each day up to N posts, counting what is already scheduled. "
                         "Skips slots already past on the current day.")
    ap.add_argument("--go", action="store_true", help="actually schedule (default: dry run)")
    a = ap.parse_args(argv)

    p = personas.resolve(a.persona)
    root = p["root"]
    # personas.resolve() nests the persona file under ["config"], so reaching in
    # by hand gave None and tripped the no-blog_id guard on a persona that has one.
    # personas.blog_id()/timezone() are the accessors; use them.
    blog = personas.blog_id(p)
    if a.go and not blog:
        raise SystemExit(f"FATAL: persona {p['name']!r} has no Metricool blog_id. Refusing to post.")
    tz = zoneinfo.ZoneInfo(personas.timezone(p))
    rng = random.Random(a.seed)

    total = a.per_day * a.days
    if a.per_day > len(tmg.CHRONO):
        raise SystemExit(f"--per-day {a.per_day} exceeds the {len(tmg.CHRONO)} defined slots")

    stills = json.load(open(os.path.join(personas.character_dir(p), "manifest.json")))
    library = load_library(root)
    seen = already_posted(root)
    if blog:
        _c2 = mc.load_creds(); _c2["METRICOOL_BLOG_ID"] = str(blog)
        queued = scheduled_captions(_c2)
        if queued:
            print(f"excluding {len(queued)} post(s) already in the Metricool calendar")
            seen += queued

    dead = killed_uids(root)
    if dead:
        print(f"skipping {len(dead)} operator-killed post(s)")

    done = scheduled_uids(root)
    if done:
        print(f"skipping {len(done)} uid(s) already scheduled (exact, from the ledger)")

    # drop anything already live, using the same fuzzy matcher the dashboard uses
    fresh = []
    for post in library:
        if post["uid"] in dead or post["uid"] in done:
            continue
        mine = [post.get("caption") or "", post.get("title") or "",
                post.get("on-screen") or ""]
        if max((gal.similarity(m, s) for m in mine for s in seen), default=0.0) >= gal.MATCH_THRESHOLD:
            continue
        if a.type and not any(t.lower() in post["src"].lower() for t in a.type):
            continue
        if is_rejected_shape(post) and post["uid"] not in a.first:
            continue          # --first still overrides, so it can be forced
        fresh.append(post)

    # Bias toward the type the learning loop currently defaults to. Taking the
    # library in file order gave 2 value posts, and `value` is the type with 0
    # comments across 2 posts while `ask` is the only type that has earned any.
    pref = None
    dfile = os.path.join(root, "learn", "defaults.json")
    if not a.type and os.path.exists(dfile):
        try:
            pref = (json.load(open(dfile)).get("defaults") or {}).get("type")
        except Exception:
            pref = None
    if pref:
        fresh.sort(key=lambda x: 0 if pref in x["src"] else 1)
    if a.first:
        # operator-requested posts jump the queue, in the order given
        order = {u: i for i, u in enumerate(a.first)}
        fresh.sort(key=lambda x: order.get(x["uid"], len(order) + 1))
        missing = [u for u in a.first if u not in {x["uid"] for x in fresh}]
        if missing:
            print(f"WARNING: --first uid(s) not in the unposted pool: {missing}")
        print(f"biasing toward type={pref!r} (current default from learn/defaults.json)")

    print(f"persona {p['name']} · {len(library)} in library · {len(fresh)} not yet posted")
    print(f"scheduling {total} post(s): {a.per_day}/day for {a.days} day(s), "
          f"starting day +{a.start_day}\n")
    if len(fresh) < total:
        print(f"WARNING: only {len(fresh)} unposted ideas available for {total} slots.\n")
        total = len(fresh)

    # Assign each post to the slot its CONTENT is about. A post that says "we leave
    # at 7:38 for a house that has to leave at 7:20" belongs at 7am, not 10pm.
    for post in fresh:
        post["_affinity"] = tmg.affinity_of(post)

    # Choose slots by what the LIBRARY ACTUALLY HAS, not a fixed clock order.
    # Picking chronologically gave a `midmorning` slot that only 1 post in 98 is
    # about, so it got filled with a post about kids' phones at night — scheduled
    # for 9:38am. Supply-driven selection avoids that whole class of mismatch.
    import collections as _c
    supply = _c.Counter(x["_affinity"] for x in fresh if x["_affinity"] != "any")
    # late-night is the only slot with real performance history on this account,
    # so keep it in every rotation as the comparison baseline.
    keep = ["late-night"]
    ranked = [s for s, _ in supply.most_common() if s not in keep]
    slots_today = keep + ranked[:max(0, a.per_day - len(keep))]

    # TOP UP FROM THE PERSONA'S DECLARED SLOTS when supply cannot fill the day.
    #
    # Supply-ranking is an optimisation for WHICH declared slots to use, not a vote
    # on how many posts a day this account runs. tmg.affinity_of() reads content for
    # time-of-day cues, and its patterns are Dani-shaped (alarm, breakfast, the 5pm
    # meltdown). Ray's hooks are declarative and almost all resolve to "any", so his
    # supply Counter held exactly one slot — `evening` — and `--per-day 3` silently
    # produced TWO slots and two posts. He was running a third under his own stated
    # cadence with nothing reporting why.
    #
    # So: rank by supply, then fill any remaining slots from posting_slots in
    # persona.json. Dani has rich supply and is unaffected; a persona whose content
    # carries no time cues still posts at the rate it declares.
    declared = [s.get("name") for s in ((p.get("config") or {}).get("posting_slots") or [])
                if s.get("name") in tmg.SLOTS]
    topped_up = []
    for s in declared or tmg.CHRONO:
        if len(slots_today) >= a.per_day:
            break
        if s not in slots_today:
            slots_today.append(s)
            topped_up.append(s)

    slots_today = [s for s in tmg.CHRONO if s in slots_today]     # chronological within a day
    print("slots chosen by content supply: " +
          ", ".join(f"{s}({supply.get(s, 0)} posts)" for s in slots_today)
          + (f"  [topped up from persona.json: {', '.join(topped_up)}]" if topped_up else ""))
    # --fill-to: work out what each day is MISSING rather than adding blindly.
    # Without this, topping the calendar up means counting by hand and picking slots
    # that are already occupied or already in the past.
    existing_by_day: dict = {}
    if a.fill_to:
        _c3 = mc.load_creds(); _c3["METRICOOL_BLOG_ID"] = str(blog)
        st, body = mc._req("GET", "/v2/scheduler/posts", _c3, params={
            "start": datetime.datetime.now(tz).strftime("%Y-%m-%dT00:00:00"),
            "end": (datetime.datetime.now(tz) + datetime.timedelta(days=40)).strftime("%Y-%m-%dT00:00:00")})
        for x in (body if isinstance(body, list) else (body or {}).get("data") or []):
            if not isinstance(x, dict):
                continue
            # COUNT ONLY TIKTOK ENTRIES. Every TikTok post now has an
            # Instagram+Facebook Reel mirror as a SEPARATE scheduler entry, so
            # counting every entry makes a 4/day calendar look like 8/day. The
            # filler then refuses to add anything and the calendar silently
            # freezes — no error, just nothing scheduled ever again.
            if {(q.get("network") or "").lower() for q in (x.get("providers") or [])} \
                    != {"tiktok"}:
                continue
            dt = (x.get("publicationDate") or {}).get("dateTime") or ""
            if len(dt) >= 16:
                existing_by_day.setdefault(dt[:10], []).append(dt[11:16])

    plan, recent, used = [], [], set()

    def take(slot: str):
        """Best unposted post for this slot: exact affinity first, then 'any'."""
        for want in (slot, "any"):
            for post in fresh:
                if post["uid"] in used or post["_affinity"] != want:
                    continue
                used.add(post["uid"])
                return post, (want == slot)
        return None, False

    now_local = datetime.datetime.now(tz)
    for d in range(a.days):
        day = (now_local + datetime.timedelta(days=a.start_day + d)).strftime("%Y-%m-%d")
        have = existing_by_day.get(day, [])
        day_slots = list(slots_today)
        if a.fill_to:
            need = max(0, a.fill_to - len(have))
            # every slot not already used that day, and not already past if it is today
            # Preference order for a SPREAD across the day, not the earliest slots.
            # Walking CHRONO put 3 posts before 1pm on an empty day and left the
            # evening empty, which also meant no late-night content could be placed.
            # late-night leads because it is the only slot with performance history.
            SPREAD = ["late-night", "morning", "dinner", "lunch", "evening",
                      "pickup", "midmorning"]
            # THE PERSONA'S DECLARED SLOTS FIRST, in SPREAD order, and only fall back
            # to the full clock if it declares none. This picker is separate from the
            # supply-ranked one above and had the same blind spot: topping Ray up to
            # 3/day offered him `morning` at 06:35, a slot he does not declare, for a
            # persona whose posting_slots are dinner/evening/late-night and whose whole
            # subject is what happens after the kid is in bed. Nothing was wrong with
            # the time arithmetic; it was choosing from the wrong set.
            _declared = {s.get("name") for s in
                         ((p.get("config") or {}).get("posting_slots") or [])
                         if s.get("name") in tmg.SLOTS}
            # DECLARED FIRST, THEN TOP UP FROM THE FULL CLOCK.
            #
            # `or SPREAD` only covered the case of declaring NOTHING. Declaring FEWER
            # slots than posts_per_day hard-capped the day at the number declared:
            # Dani declares 3 (late-night, pickup, lunch) and runs 4/day, so every day
            # from Aug 26 on reported "3 scheduled, need 1 more -> nothing to add" with
            # 100 unposted posts sitting there. An empty day offered 3 slot names when
            # it needed 4. Silent, and it looks like a supply problem when it is not.
            #
            # This is the same blind spot the comment above records for the
            # supply-ranked picker — that one was taught to top up from posting_slots
            # and this one was left capping in the other direction.
            _pref = [s for s in SPREAD if s in _declared]
            _order = _pref + [s for s in SPREAD if s not in _declared]
            free = []
            for name in _order:
                h1, m1, _, _ = tmg.SLOTS[name]
                if any(abs(int(t[:2]) * 60 + int(t[3:]) - (h1 * 60 + m1)) < 70 for t in have):
                    continue
                if day == now_local.strftime("%Y-%m-%d") and \
                        (h1 * 60 + m1) <= (now_local.hour * 60 + now_local.minute + 25):
                    continue
                free.append(name)
            day_slots = [x for x in tmg.CHRONO if x in free[:need]]   # chronological
            print(f"  {day}: {len(have)} scheduled, need {need} more -> "
                  f"{', '.join(day_slots) if day_slots else 'nothing to add'}")
            if not day_slots:
                continue
        for s in day_slots:
            if not a.fill_to and len(plan) >= total:
                break
            post, matched = take(s)
            if post is None:
                break
            post["_slot_matched"] = matched
            still = pick_still(post, stills, recent)
            if recent and still["file"] == recent[-1]:
                print(f"    NOTE: reusing {os.path.basename(still['file'])} back-to-back — "
                      f"only one still matches setting {post['setting']!r}")
            recent.append(still["file"])
            when = slot_time(a.start_day + d, s, tz, rng)
            plan.append({"post": post, "still": still, "slot": s, "when": when,
                         "matched": matched})

    # gate everything BEFORE rendering or uploading anything
    for it in plan:
        for label in ("on-screen", "title", "caption", "comment"):
            t = it["post"].get(label)
            if not t:
                continue
            # WITH the post's declared posture, not the default. This used to be a
            # bare gates.check(t), so a dramatized-labeled post was judged as
            # no-product and could never ship through this path at all.
            ok, why = gates.check(t, it["post"].get("claim_posture", "no-product"))
            if not ok:
                raise SystemExit(f"GATE FAIL on {it['post']['uid']} ({label}): {why}")

    for i, it in enumerate(plan, 1):
        post, still = it["post"], it["still"]
        screen = (post.get("on-screen") or post.get("title") or "").replace(" / ", "\n")
        print(f"{i:>2}. {it['when']:%a %d %b %H:%M} {it['slot']:<11}{'' if it['matched'] else '~'} "
              f"{os.path.basename(still['file']):<24} [{post['setting']}] {post['uid']}")
        print(f"    screen: {screen[:76]!r}")
        if post.get("caption"):
            print(f"    caption: {post['caption'][:76]!r}")
        if not it["matched"]:
            print(f"    (no post about {it['slot']} left — filled from the 'any' pool)")

    if not a.go:
        print(f"\nDRY RUN — nothing scheduled. Re-run with --go to schedule these {len(plan)}.")
        return 0

    print()
    out_dir = os.path.join(root, "render", "out", f"sched-{datetime.date.today()}")
    os.makedirs(out_dir, exist_ok=True)
    creds = mc.load_creds()
    creds["METRICOOL_BLOG_ID"] = str(blog)
    ok_n = 0

    for i, it in enumerate(plan, 1):
        post, still = it["post"], it["still"]
        screen = (post.get("on-screen") or post.get("title") or "").replace(" / ", "\n")
        # A drafts-*.md hook knows its own density from its `### short|wall`
        # heading; carry it. Only fall back to inferring from items for gallery
        # posts, which have no density of their own. Without this every adapted
        # wall hook would be treated as "short", handed the smaller zone, and
        # overflow into the above-the-chin fallback across his face.
        density = post.get("text_density") or (
            "wall" if len((post.get("items") or [])) >= 3 else "short")
        # A post's own `screen_style:` wins here. This path fills whatever days the
        # designed experiment did not claim, so without this a confessional post drawn
        # as ordinary filler would render as a 62px headline — visually identical to
        # baseline, and it would land in the same arm score.py rolls up, quietly
        # diluting the one arm it was written to test.
        sstyle = (post.get("screen_style") or "headline").strip().lower()
        if sstyle == "confessional":
            zone = ov.confessional_zone()
        else:
            zone = still.get("safe_text_zone_wall" if density == "wall" else "safe_text_zone") \
                or still.get("safe_text_zone")
        stem = os.path.join(out_dir, f"{i:02d}-{post['id']}")
        cp.compose(still=os.path.join(personas.character_dir(p), still["file"]),
                   out=stem, text=screen, style="tiktok-native", fmt="png",
                   safe_zone=zone, text_color="white",   # operator instruction: always white
                   screen_style=sstyle, jpeg=True,
                   # The other half of the posture. Without this the gate grants the
                   # 16 CFR 465.2 exemption and the video ships with no tag on it --
                   # the exemption without the mitigation. batch.py already did this;
                   # this path did not.
                   dramatization=(post.get("claim_posture") == gates.DRAMATIZED))
        jpg = stem + ".jpg"          # ALWAYS the jpg — TikTok rejects PNG at publish

        # A drafts-*.md hook carries only its on-screen line. cap.build() composes
        # the posted description out of `caption`/`items`, which only gallery posts
        # have — so an adapted hook produced text == "" and cap.validate() blocked it
        # AFTER the image had already been rendered and uploaded. Generate the
        # missing atoms here, lazily: only the 1-3 posts actually in the plan, not
        # all 108 in the library.
        if not post.get("caption"):
            import captions as capmod
            atoms = capmod.build_atoms(
                post.get("on-screen") or "", post.get("territory") or "",
                density, index=i, claim_posture=post.get("claim_posture", "no-product"),
                persona_brief=personas.brief(p), persona=p)
            post["caption"] = atoms["caption"]
            # cap.build() expects a STRING here; build_atoms returns a list. The
            # gallery format stores them as one space-separated line, so match that.
            post["hashtags"] = " ".join(atoms["hashtags"])
            print(f"    caption ({atoms['source']}): {atoms['caption'][:72]!r}")

        url = up.upload(jpg, verbose=False)

        body = {
            "providers": [{"network": "tiktok"}],
            "text": cap.build(post),
            "autoPublish": True, "draft": False,
            "publicationDate": {"dateTime": it["when"].strftime("%Y-%m-%dT%H:%M:%S"),
                                "timezone": str(tz)},
            "media": [url], "saveExternalMediaFiles": True,
            "tiktokData": {"isAigc": True, "privacyOption": "PUBLIC_TO_EVERYONE",
                           "photoCoverIndex": 0, "disableComment": False,
                           "autoAddMusic": True},
        }
        # LAST CHECK before the API call. build() is a convention; this is the
        # enforcement. A wall-of-text caption shipped once because nothing checked.
        _ok, _why = cap.validate(body["text"])
        if not _ok:
            print(f"  BLOCKED (caption): {_why}")
            continue
        for _w in cap.readability_warnings(body["text"]):
            print(f"      note: {_w}")
        st, resp = mc._req("POST", "/v2/scheduler/posts", creds, body=body)
        d = (resp or {}).get("data") or resp or {}
        if st == 200 and d.get("id"):
            ok_n += 1
            record_scheduled(root, it["post"]["uid"], d.get("id"),
                             it["when"].isoformat())
            print(f"{i:>2}. scheduled id={d.get('id')} for {it['when']:%a %H:%M} "
                  f"({it['slot']})")
        else:
            print(f"{i:>2}. FAILED {st}: {str(resp)[:150]}")

    print(f"\n{ok_n}/{len(plan)} scheduled. Renders in {out_dir}")
    print("No first comment is set: the image carries text and the description "
          "carries text, so a third block from the same account adds nothing.")
    return 0 if ok_n == len(plan) else 1


if __name__ == "__main__":
    sys.exit(main())
