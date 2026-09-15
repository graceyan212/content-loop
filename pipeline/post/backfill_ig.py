#!/usr/bin/env python3
"""
backfill_ig.py — give Instagram + Facebook the early TikTok posts they missed.

    python post/backfill_ig.py --persona dani                 # dry run
    python post/backfill_ig.py --persona dani --go

WHY THIS IS NOT crosspost.py: that mirrors PENDING TikTok posts, which is correct for
keeping the two platforms in step going forward. It deliberately ignores anything
already published, so the account's first weeks of TikTok content never reached
Instagram — the IG profile starts mid-conversation with no beginning.

WHAT IT DOES DIFFERENTLY:

  1. Sources from PUBLISHED TikTok posts, oldest first, so the Instagram grid ends up
     in the same order the story actually happened.

  2. Leads with an Instagram-specific intro from noschedule-intro-instagram.md. The TikTok
     intro says "starting a tiktok at 38" and would read as a copy-paste on another
     platform. That post has no scheduler entry to mirror (it was published by hand),
     so its image is rendered fresh here.

  3. DEDUPES BY CAPTION. "Twenty minutes of coffee before the noise starts" published
     twice on TikTok, on 2026-08-03 and again 2026-08-04. That cannot be undone there,
     but Instagram should not inherit the mistake.

  4. Uses its own time slots, offset from the ongoing mirror times, so a backfill post
     and a same-day mirror never land in the same window.

Idempotent via learn/backfill_ig.json, keyed by source post id, written after every
single schedule call — a crash mid-run cannot double-post.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "copy"), os.path.join(ROOT, "render"),
          os.path.join(ROOT, "learn")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas          # noqa: E402
import metricool as mc   # noqa: E402
import upload as up      # noqa: E402
import reel              # noqa: E402
import gallery as gal    # noqa: E402
import caption as cap    # noqa: E402
import compose as cp     # noqa: E402
import crosspost as xp   # noqa: E402
import schedule_batch as sb  # noqa: E402
import settings as cset  # noqa: E402
import gates             # noqa: E402
import respin            # noqa: E402

MIRROR_NETWORKS = ("instagram", "facebook")
# Deliberately clear of the mirror slots (which sit at the TikTok minute + 7, so
# roughly :2x past 06 / 12 / 17 / 22). These are the gaps between them.
BACKFILL_SLOTS = ["08:41", "10:18", "14:23", "19:37"]
MIN_GAP_MIN = 40   # no two IG posts closer than this, across mirror + backfill + fill
STATE = "backfill_ig.json"


def load_state(root):
    try:
        return json.load(open(os.path.join(root, "learn", STATE)))
    except Exception:
        return {"done": {}}


def save_state(root, obj):
    f = os.path.join(root, "learn", STATE)
    tmp = f + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=2)
    os.replace(tmp, f)


def published_tiktok(creds):
    _, sc = mc._req("GET", "/v2/scheduler/posts", creds,
                    params={"start": "2026-07-01T00:00:00",
                            "end": datetime.date.today().strftime("%Y-%m-%dT23:59:59")})
    rows = [x for x in (sc if isinstance(sc, list) else (sc or {}).get("data") or [])
            if isinstance(x, dict)]
    out = [x for x in rows
           if {(q.get("network") or "").lower() for q in (x.get("providers") or [])} == {"tiktok"}
           and "PUBLISHED" in {(q.get("status") or "").upper() for q in (x.get("providers") or [])}
           and x.get("media")]
    out.sort(key=lambda z: (z.get("publicationDate") or {}).get("dateTime") or "")
    return out


def ig_captions(creds, days=40):
    """Caption heads ALREADY scheduled or published on IG/FB.

    Dedupe inside the backfill queue is not enough. crosspost.py mirrors every pending
    TikTok post, so a post that TikTok published very recently is on the IG calendar
    already — and the backfill sources from published posts, so it picks the same one
    up again. That put two captions on Instagram twice.
    """
    d0 = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    _, sc = mc._req("GET", "/v2/scheduler/posts", creds, params={"start": d0, "end": d1})
    heads = set()
    for x in (sc if isinstance(sc, list) else (sc or {}).get("data") or []):
        if not isinstance(x, dict):
            continue
        if ({(q.get("network") or "").lower() for q in (x.get("providers") or [])}
                & set(MIRROR_NETWORKS)):
            heads.add((x.get("text") or "").strip()[:70])
    return heads


def occupied(creds, days=14):
    """Times already taken on IG/FB, per day, so backfill never crowds a mirror."""
    d0 = datetime.date.today().strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    _, sc = mc._req("GET", "/v2/scheduler/posts", creds, params={"start": d0, "end": d1})
    taken = {}
    for x in (sc if isinstance(sc, list) else (sc or {}).get("data") or []):
        if not isinstance(x, dict):
            continue
        if not ({(q.get("network") or "").lower() for q in (x.get("providers") or [])}
                & set(MIRROR_NETWORKS)):
            continue
        dt = (x.get("publicationDate") or {}).get("dateTime") or ""
        if len(dt) >= 16:
            taken.setdefault(dt[:10], set()).add(dt[11:16])
    return taken


def ig_only_posts(root):
    """Every post in copy/igonly-*.md, oldest file first.

    These never appear on TikTok — gal.NEVER_SCHEDULE keeps both library loaders away
    from them — so they are the only way Instagram carries more than TikTok's rate.
    """
    import glob as _glob
    out = []
    for path in sorted(_glob.glob(os.path.join(root, "copy", gal.IG_ONLY_PREFIX + "*.md"))):
        for post in gal.parse_posts(path):
            post.setdefault("setting", "any")
            post["uid"] = f"{os.path.basename(path)}::{post['id']}"
            out.append(post)
    return out


def render_library_post(persona, post, out_dir, recent):
    """Render a library/ig-only post to a fresh jpg. Returns (jpg_path, caption)."""
    stills = json.load(open(os.path.join(personas.character_dir(persona), "manifest.json")))
    post.setdefault("setting", cset.setting_of(post))
    still = sb.pick_still(post, stills, recent)
    recent.append(still["file"])
    dens = "wall" if len(post.get("items") or []) >= 3 else "short"
    zone = (still.get("safe_text_zone_wall" if dens == "wall" else "safe_text_zone")
            or still.get("safe_text_zone"))
    stem = os.path.join(out_dir, post["uid"].replace("::", "-").replace(".md", ""))
    cp.compose(still=os.path.join(personas.character_dir(persona), still["file"]),
               out=stem, text=gal.screen_text(post), style="tiktok-native", fmt="png",
               safe_zone=zone, text_color="white", screen_style="headline",
               # honour a declared dramatized-labeled posture, same as schedule_batch
               dramatization=(post.get("claim_posture") == gates.DRAMATIZED),
               jpeg=True)
    text = cap.build(post, numbered=bool(post.get("count_title")))
    ok, why = cap.validate(text)
    if not ok:
        raise RuntimeError(f"{post['uid']} caption failed validation: {why}")
    posture = post.get("claim_posture", "no-product")
    for blob in (gal.screen_text(post), text):
        ok, notes = gates.check(blob, claim_posture=posture)
        if not ok:
            raise RuntimeError(f"{post['uid']} failed the gate ({posture}): {notes}")
    return stem + ".jpg", text


def render_intro(persona, root, out_dir):
    """Render the Instagram-specific intro from noschedule-intro-instagram.md."""
    src = os.path.join(root, "copy", "noschedule-intro-instagram.md")
    posts = gal.parse_posts(src)
    if not posts:
        raise RuntimeError(f"no posts parsed from {src}")
    post = posts[0]
    post.setdefault("setting", "any")
    stills = json.load(open(os.path.join(personas.character_dir(persona), "manifest.json")))
    still = sb.pick_still(post, stills, [])
    stem = os.path.join(out_dir, "ig-intro")
    cp.compose(still=os.path.join(personas.character_dir(persona), still["file"]),
               out=stem, text=gal.screen_text(post), style="tiktok-native", fmt="png",
               safe_zone=still.get("safe_text_zone"), text_color="white",
               screen_style="headline", jpeg=True)
    text = cap.build(post)
    ok, why = cap.validate(text)
    if not ok:
        raise RuntimeError(f"intro caption failed validation: {why}")
    return stem + ".jpg", text


def ig_fill(persona, root, creds, tz, per_day, go, library_reserve=20):
    """Top IG/FB up to `per_day` posts a day with content TikTok never gets.

    SOURCE ORDER: igonly-*.md first (written for Instagram, so it should lead), then
    library posts that are on NEITHER calendar. A library post already scheduled on
    TikTok is deliberately skipped — crosspost.py will mirror it anyway, and
    scheduling it here too would put it on Instagram twice.
    """
    taken = occupied(creds, days=30)
    heads = ig_captions(creds)
    pool = [x for x in ig_only_posts(root) if (x.get("caption") or "").strip()[:70] not in heads]
    ig_only_n = len(pool)

    lib = respin.library_by_uid(root)
    dead = sb.killed_uids(root)
    seen = sb.already_posted(root) + sb.scheduled_captions(creds)
    from_library = []
    for x in lib.values():
        if x["uid"] in dead or sb.is_rejected_shape(x):
            continue
        mine = x.get("caption") or x.get("title") or x.get("on-screen") or ""
        if max((gal.similarity(mine, s) for s in seen), default=0.0) >= gal.MATCH_THRESHOLD:
            continue
        if mine.strip()[:70] in heads:
            continue
        from_library.append(x)
    # HOLD BACK a reserve. TikTok's filler draws from this same unposted pool, so
    # letting IG take all of it would leave the primary channel with nothing to
    # schedule once its current horizon runs out.
    usable = max(0, len(from_library) - library_reserve)
    pool += from_library[:usable]
    print(f"IG-only fill: target {per_day}/day  |  supply {len(pool)} post(s) "
          f"({ig_only_n} written for IG, {usable} from the library)")
    print(f"  holding back {min(library_reserve, len(from_library))} library post(s) "
          f"for TikTok's filler")

    # Drop posts whose caption duplicates ANOTHER post in this same pool before
    # placing anything. Checking only against the live calendar missed the case where
    # two library posts share an anecdote and both get scheduled in one run — which is
    # exactly what happened: the same Kayla story existed in events-01 and pov-01.
    uniq, run_heads = [], set(heads)
    for x in pool:
        h = (x.get("caption") or "").strip()[:70]
        if h in run_heads:
            print(f"  dropped {x['uid']}: caption duplicates another post in this run")
            continue
        run_heads.add(h)
        uniq.append(x)
    pool = uniq

    now = datetime.datetime.now()
    plan, pi = [], 0
    for d in range(0, 30):
        if pi >= len(pool):
            break
        day = datetime.date.today() + datetime.timedelta(days=d)
        have = len(taken.get(day.isoformat(), set()))
        for t in BACKFILL_SLOTS:
            if have >= per_day or pi >= len(pool):
                break
            # PROXIMITY, not equality. The mirror slots sit at the TikTok minute + 7,
            # so a TikTok pickup post at 14:35 mirrors to 14:42 — which is 19 minutes
            # from the 14:23 backfill slot and passed an exact-match check cleanly.
            mins = int(t[:2]) * 60 + int(t[3:])
            busy_mins = [int(u[:2]) * 60 + int(u[3:]) for u in taken.get(day.isoformat(), set())]
            if any(abs(m - mins) < MIN_GAP_MIN for m in busy_mins):
                continue
            when = datetime.datetime.combine(day, datetime.time(int(t[:2]), int(t[3:])))
            if when <= now + datetime.timedelta(minutes=12):
                continue
            taken.setdefault(day.isoformat(), set()).add(t)
            plan.append((pool[pi], when))
            pi += 1
            have += 1
    days = len({w.date() for _, w in plan})
    print(f"  places {len(plan)} post(s) across {days} day(s); "
          f"{len(pool)-len(plan)} left in supply")
    for post, when in plan[:8]:
        print(f"    {when:%Y-%m-%d %H:%M}  {post['uid']}")
    if len(plan) > 8:
        print(f"    ... and {len(plan)-8} more")
    if not go:
        print("  DRY RUN — re-run with --go")
        return 0

    work = os.path.join(root, "render", "out", "igonly")
    os.makedirs(work, exist_ok=True)
    recent, ok_n = [], 0
    for post, when in plan:
        try:
            jpg, text = render_library_post(persona, post, work, recent)
            mp4 = os.path.splitext(jpg)[0] + ".mp4"
            if not os.path.exists(mp4):
                reel.build(jpg, mp4)
            url = up.upload(mp4, verbose=False)
        except Exception as e:
            print(f"  SKIP {post['uid']}: {type(e).__name__}: {str(e)[:110]}")
            continue
        body = {"providers": [{"network": n} for n in MIRROR_NETWORKS],
                "text": text, "autoPublish": True, "draft": False,
                "publicationDate": {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "timezone": tz},
                "media": [url], "saveExternalMediaFiles": True,
                "instagramData": {"type": "REEL", "showReelOnFeed": True},
                "facebookData": {"type": "REEL"}}
        st, resp = mc._req("POST", "/v2/scheduler/posts", creds, body=body)
        d = (resp or {}).get("data") or resp or {}
        if st == 200 and isinstance(d, dict) and d.get("id"):
            ok_n += 1
            print(f"  ig-only {post['uid']:34} -> {d['id']}  {when:%Y-%m-%d %H:%M}")
        else:
            print(f"  FAILED {post['uid']}: {st} {str(resp)[:120]}")
    print(f"  {ok_n} IG-only post(s) scheduled\n")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True)
    ap.add_argument("--per-day", type=int, default=4)
    ap.add_argument("--start-day", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-intro", action="store_true")
    ap.add_argument("--library-reserve", type=int, default=20,
                    help="library posts held back for TikTok's own filler. IG-only "
                         "draws from the SAME unposted pool, so without this, doubling "
                         "Instagram starves the primary channel.")
    ap.add_argument("--ig-per-day", type=int, default=0,
                    help="top Instagram/Facebook up to this many posts a day using "
                         "igonly-*.md first, then library posts on NEITHER calendar. "
                         "0 disables. This is the only way IG exceeds TikTok's rate.")
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args(argv)

    p = personas.resolve(a.persona)
    root = p["root"]
    tz = personas.timezone(p)
    creds = mc.load_creds()
    creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(p))
    state = load_state(root)

    # ---- IG-only fill FIRST -------------------------------------------------
    # Must run before the catch-up queue: that block ends in `return 0` on a dry run,
    # so anything placed after it never executed and --ig-per-day silently did nothing.
    if a.ig_per_day:
        ig_fill(p, root, creds, tz, a.ig_per_day, a.go, a.library_reserve)

    pub = published_tiktok(creds)
    queue = []
    # Seeded with what IG/FB already carry, so the queue dedupes against the live
    # calendar and not merely against itself.
    seen_caps = ig_captions(creds)
    pre_existing = len(seen_caps)
    if not a.skip_intro and "ig-intro" not in state["done"]:
        queue.append({"key": "ig-intro", "kind": "intro"})
    dropped = 0
    for x in pub:
        key = str(x.get("id"))
        head = (x.get("text") or "").strip()[:70]
        if key in state["done"]:
            continue
        if head in seen_caps:
            dropped += 1
            continue
        seen_caps.add(head)
        queue.append({"key": key, "kind": "mirror", "post": x})
    if a.limit:
        queue = queue[:a.limit]

    print(f"{len(pub)} published TikTok post(s); {len(state['done'])} already backfilled")
    if dropped:
        print(f"  DROPPED {dropped} duplicate caption(s): already on the IG/FB calendar "
              f"({pre_existing} captions there) or repeated on TikTok")
    print(f"  {len(queue)} to schedule at {a.per_day}/day on {MIRROR_NETWORKS}")

    taken = occupied(creds)
    # Walk day by day and take the first FREE slot each time, counting placements per
    # day separately. The previous version kept an index into the per-day free-slot
    # list, but that list shrinks as slots get claimed, so the index skipped entries
    # and only ever placed 2 of the 4 slots a day.
    plan = []
    now = datetime.datetime.now()
    day_i, placed_today = a.start_day, 0
    for item in queue:
        when = None
        while when is None:
            day = datetime.date.today() + datetime.timedelta(days=day_i)
            if placed_today >= a.per_day:
                day_i += 1
                placed_today = 0
                continue
            busy = taken.get(day.isoformat(), set())
            free = [t for t in BACKFILL_SLOTS if t not in busy]
            cand = None
            for t in free:
                c = datetime.datetime.combine(
                    day, datetime.time(int(t[:2]), int(t[3:])))
                if c > now + datetime.timedelta(minutes=12):
                    cand = c
                    break
            if cand is None:                  # nothing usable left today
                day_i += 1
                placed_today = 0
                continue
            when = cand
            taken.setdefault(day.isoformat(), set()).add(when.strftime("%H:%M"))
            placed_today += 1
        plan.append((item, when))

    for item, when in plan:
        label = "IG INTRO" if item["kind"] == "intro" else item["key"]
        head = ("starting an instagram at 38" if item["kind"] == "intro"
                else (item["post"].get("text") or "").splitlines()[0][:54])
        print(f"    {when:%Y-%m-%d %H:%M}  {label:12} {head!r}")
    if not a.go:
        print("\nDRY RUN — re-run with --go")
        return 0

    work = os.path.join(root, "render", "out", "reels")
    os.makedirs(work, exist_ok=True)
    ok_n = 0
    for item, when in plan:
        try:
            if item["kind"] == "intro":
                jpg, text = render_intro(p, root, work)
                mp4 = os.path.join(work, "ig-intro.mp4")
            else:
                x = item["post"]
                src = (x.get("media") or [None])[0]
                stem = hashlib.sha256((src or item["key"]).encode()).hexdigest()[:16]
                jpg = os.path.join(work, stem + ".jpg")
                mp4 = os.path.join(work, stem + ".mp4")
                text = (x.get("text") or "").rstrip()
                if not os.path.exists(mp4):
                    xp.fetch(src, jpg)
            if not os.path.exists(mp4):
                reel.build(jpg, mp4)
            url = up.upload(mp4, verbose=False)
        except Exception as e:
            print(f"  SKIP {item['key']}: {type(e).__name__}: {str(e)[:120]}")
            continue

        body = {"providers": [{"network": n} for n in MIRROR_NETWORKS],
                "text": text, "autoPublish": True, "draft": False,
                "publicationDate": {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "timezone": tz},
                "media": [url], "saveExternalMediaFiles": True,
                "instagramData": {"type": "REEL", "showReelOnFeed": True},
                "facebookData": {"type": "REEL"}}
        st, resp = mc._req("POST", "/v2/scheduler/posts", creds, body=body)
        d = (resp or {}).get("data") or resp or {}
        if st == 200 and isinstance(d, dict) and d.get("id"):
            state["done"][item["key"]] = str(d["id"])
            save_state(root, state)
            ok_n += 1
            print(f"  scheduled {item['key']:12} -> {d['id']}  {when:%Y-%m-%d %H:%M}")
        else:
            print(f"  FAILED {item['key']}: {st} {str(resp)[:140]}")
    print(f"\n{ok_n} backfilled to {' + '.join(MIRROR_NETWORKS)} as Reels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
