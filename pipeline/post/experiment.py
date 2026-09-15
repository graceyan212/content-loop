#!/usr/bin/env python3
"""
experiment.py — make the scheduler execute the planned experiment.

THE GAP THIS CLOSES: learn/plan.py proposed proper one-axis-deviation variants
("post an ask, at lunch, short, with audio") and post/schedule_batch.py ignored
them completely — it picked by content supply and time affinity. score.py then
inferred each post's dimensions AFTER the fact from whatever happened to go out.

That is observational, not experimental. It measures correlations in a convenience
sample. Worse, the confound is systematic rather than random: the scheduler picks
slots by which slots the LIBRARY has content for, so "late-night wins" could just
mean "we own more late-night copy". No amount of data fixes a biased assignment.

What this does instead:
  1. ask plan.py what to test
  2. find a library post that MATCHES each variant (right type, right density)
  3. schedule it in the variant's slot, with the variant's audio setting
  4. RECORD the assignment — post id -> the arm it was assigned to

Step 4 is the one that matters. With an assignment recorded at design time, score.py
joins on a known arm instead of guessing from the artifact. That is the same reason
Hermes mints its variant id up front rather than back-filling from caption text.

    python post/experiment.py                 # show the assignment plan
    python post/experiment.py --go            # schedule it
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "copy"), os.path.join(ROOT, "render"),
          os.path.join(ROOT, "learn")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas            # noqa: E402
import metricool as mc     # noqa: E402
import upload as up        # noqa: E402
import compose as cp       # noqa: E402
import caption as cap      # noqa: E402
import gates               # noqa: E402
import timing as tmg       # noqa: E402
import gallery as gal      # noqa: E402
import respin              # noqa: E402
import overlay as ov       # noqa: E402
import schedule_batch as sb  # noqa: E402
import plan as planner     # noqa: E402


def assignments_path(root: str) -> str:
    return os.path.join(root, "learn", "assignments.json")


def load_assignments(root: str) -> dict:
    f = assignments_path(root)
    if os.path.exists(f):
        try:
            return json.load(open(f))
        except Exception:
            pass
    return {"_note": "post_id -> the experiment arm it was ASSIGNED at design time. "
                     "score.py must prefer this over inferring dimensions from the "
                     "finished post: an inferred dimension is a description, an "
                     "assigned one is an experiment.",
            "assignments": []}


def record_assignment(root: str, entry: dict) -> None:
    db = load_assignments(root)
    db["assignments"] = [a for a in db["assignments"]
                         if str(a.get("post_id")) != str(entry["post_id"])]
    db["assignments"].append(entry)
    os.makedirs(os.path.dirname(assignments_path(root)), exist_ok=True)
    json.dump(db, open(assignments_path(root), "w"), indent=2)


# filename prefix -> the `type` arm. Every value here MUST be in
# dimensions.DIMENSIONS["type"], or a variant can never match a post: the old
# fallback returned "other", which is not in the universe, so every post in an
# unrecognised file was silently unschedulable by the experiment path.
SRC_TYPE = {"ask-": "ask", "value-": "value", "events-": "event",
            "confess-": "observe", "community-": "ask", "pov-": "pov"}


def post_type(post: dict) -> str:
    """The `type` dimension, derived from which library file a post lives in."""
    src = post.get("src", "")
    if "fillblank" in src:
        return "joke"
    for prefix, t in SRC_TYPE.items():
        if src.startswith(prefix):
            return t
    print(f"  WARNING unmapped library file {src!r} -> defaulting type to 'observe'; "
          f"add a prefix to SRC_TYPE", file=sys.stderr)
    return "observe"


def post_density(post: dict) -> str:
    return "wall" if len(post.get("items") or []) >= 3 else "short"


def post_screen_style(post: dict) -> str:
    """The post's OWN declared screen_style. Not inferable — a confessional and a
    headline can both be a single `on-screen:` line; what differs is the register the
    copy was written in, which only the library file knows."""
    return (post.get("screen_style") or "headline").strip().lower()


def post_cta(post: dict) -> str:
    """The explicit ask, declared in the library. Not inferable from the text."""
    return (post.get("cta") or "none").strip().lower()


def post_register(post: dict) -> str:
    """Emotional register, declared in the library. Not inferable from the text."""
    return (post.get("register") or "neutral").strip().lower()


def post_framing(post: dict) -> str:
    """Loss vs gain framing, declared in the library. Not inferable: the same
    sentence can read either way depending on the detail it carries."""
    return (post.get("framing") or "negative").strip().lower()


def post_community_move(post: dict) -> str:
    """The cohesion mechanic a post uses, declared in the library. Not inferable: a
    callback and a plain ask look identical in structure and differ only in whether
    the text names something the account actually posted before."""
    return (post.get("community_move") or "none").strip().lower()


def post_list_style(post: dict) -> str:
    """caption-list is the default for anything with items; numbered-count needs a
    `count_title:` to promise a number the items then deliver."""
    if not (post.get("items") or []):
        return "caption-list"
    return "numbered-count" if post.get("count_title") else "caption-list"


# Axes this executor can actually RENDER differently. Anything outside this set gets
# refused rather than scheduled.
#
# WHY THIS EXISTS: the publish path hardcoded text_color="light", never passed
# screen_style, and always called cap.build() with default formatting — so a
# `text_color=yellow` or `screen_style=confessional` arm was rendered byte-identical
# to baseline and then RECORDED IN assignments.json AS TESTED. Three of seven arms
# were manufacturing experimental evidence for experiments that never ran, and every
# downstream consumer (adjust.py, dash.py, score.py) trusts assignments.json as
# design-time ground truth. A silent no-op is the one failure mode this file cannot
# tolerate, so an unimplementable axis is now a loud SKIP.
# "cta" was here and is deliberately gone. Dropping a retired axis from
# dimensions.py is not enough: an axis left in this set is one an assignment can
# still claim to have tested, and this module's contract is that it REFUSES an axis
# it cannot render rather than recording it as tested. Retire in both places, in the
# same edit.
HONOURED_AXES = frozenset({"type", "slot", "audio", "density", "list_style",
                           "screen_style", "text_color", "community_move", "framing",
                           "register"})


# Axes that CANNOT be isolated from each other, and why.
#
# `screen_style` is entangled with `type`. A confessional is a thought that ends
# unresolved; an ask ends with a question. There is no such thing as a confessional
# ask, so a "change only screen_style" variant is not constructible from real copy —
# the register and the content type are one decision made once.
#
# The choice is between refusing to test screen_style at all, or testing it and
# labelling the confound. Refusing is worse: it leaves the highest-signal idea the
# operator has brought in untested on a purity rule. So it runs, and the assignment
# carries `confounded_with` so adjust.py and the dashboard can never read the result
# as clean. An acknowledged confound is evidence. An unacknowledged one is a false
# conclusion with a number attached to it.
CONFOUNDED = {"screen_style": ("type",)}


def matches(post: dict, variant: dict, dim: str | None = None) -> bool:
    """Does this library post satisfy the variant's content dimensions?

    `dim` is the axis under test. Axes in CONFOUNDED[dim] are not enforced, because
    for that particular test they are not free to vary independently.
    """
    free = set(CONFOUNDED.get(dim or "", ()))
    if variant.get("type") and "type" not in free and post_type(post) != variant["type"]:
        return False
    if variant.get("density") and post_density(post) != variant["density"]:
        return False
    # Both directions. A confessional variant must not land on a headline post, and
    # a headline baseline must not accidentally pick up the confessional copy.
    if variant.get("screen_style") and post_screen_style(post) != variant["screen_style"]:
        return False
    # Both directions, like screen_style: a `none` baseline must not quietly pick up
    # a callback post, or the baseline stops being a baseline.
    if variant.get("community_move") \
            and post_community_move(post) != variant["community_move"]:
        return False
    if variant.get("framing") and post_framing(post) != variant["framing"]:
        return False
    if variant.get("register") and post_register(post) != variant["register"]:
        return False
    if variant.get("cta") and post_cta(post) != variant["cta"]:
        return False
    if variant.get("list_style") and variant["list_style"] != "on-screen-wall" \
            and post_list_style(post) != variant["list_style"]:
        return False
    return True


def build(persona_name: str | None, count: int) -> list[dict]:
    p = personas.resolve(persona_name)
    root = p["root"]
    tz = zoneinfo.ZoneInfo(personas.timezone(p))
    creds = mc.load_creds()
    creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(p))

    plan_doc = planner.build_plan(p["name"], count)
    lib = respin.library_by_uid(root)
    dead = sb.killed_uids(root)
    seen = sb.already_posted(root) + sb.scheduled_captions(creds)

    def unposted(post):
        if post["uid"] in dead or sb.is_rejected_shape(post):
            return False
        mine = post.get("caption") or post.get("title") or post.get("on-screen") or ""
        return max((gal.similarity(mine, s) for s in seen), default=0.0) < gal.MATCH_THRESHOLD

    pool = [x for x in lib.values() if unposted(x)]

    # Only place into days that are UNDER quota. Running the experiment and the
    # generic fill independently pushed the calendar to 4/day; the experiment
    # claims the gaps first and schedule_batch tops up whatever is left, so the
    # designed arms displace filler rather than adding to it.
    POSTS_PER_DAY = personas.posts_per_day(p)
    d0 = datetime.datetime.now(tz).strftime("%Y-%m-%dT00:00:00")
    # The read window MUST cover the placement horizon. It was 14 days while placement
    # walked 21, so every day past day 14 came back empty: the placer saw no posts
    # there, ignored the per-day quota, and stacked arms on top of each other at the
    # SAME MINUTE (two posts at 14:35 and two at 22:35 on one day). The collision check
    # cannot fire on data that was never fetched. +2 days of slack so an off-by-one in
    # timezone rollover cannot reintroduce it.
    PLACE_HORIZON_DAYS = 21
    d1 = (datetime.datetime.now(tz)
          + datetime.timedelta(days=PLACE_HORIZON_DAYS + 2)).strftime("%Y-%m-%dT00:00:00")
    st, body = mc._req("GET", "/v2/scheduler/posts", creds, params={"start": d0, "end": d1})
    per_day: dict = {}
    taken_slots: dict = {}
    for x in (body if isinstance(body, list) else (body or {}).get("data") or []):
        if not isinstance(x, dict):
            continue
        # TikTok entries only — see the note in schedule_batch: IG/FB Reel mirrors
        # are separate scheduler entries and double every day's apparent count,
        # which makes the quota check reject every day forever.
        if {(q.get("network") or "").lower() for q in (x.get("providers") or [])} \
                != {"tiktok"}:
            continue
        dt = (x.get("publicationDate") or {}).get("dateTime") or ""
        if len(dt) >= 16:
            per_day[dt[:10]] = per_day.get(dt[:10], 0) + 1
            taken_slots.setdefault(dt[:10], []).append(dt[11:16])

    used, out = set(), []
    day = 1
    for v in plan_doc["variants"]:
        variant = v["variant"]
        if v["dimension_under_test"] not in HONOURED_AXES:
            out.append({"variant": v, "post": None,
                        "why": f"axis {v['dimension_under_test']!r} is not implemented in "
                               f"this executor — refusing to record an assignment for an "
                               f"experiment that would render as baseline"})
            continue
        cands = [x for x in pool if x["uid"] not in used
                 and matches(x, variant, v["dimension_under_test"])]
        if not cands:
            out.append({"variant": v, "post": None,
                        "why": f"no unposted post matching type="
                               f"{variant.get('type')}/density={variant.get('density')}"
                               f"/screen_style={variant.get('screen_style')}"
                               f"/list_style={variant.get('list_style')}"
                               + (f" (type free: confounded with "
                                  f"{v['dimension_under_test']})"
                                  if 'type' in CONFOUNDED.get(v['dimension_under_test'], ())
                                  else "")})
            continue
        slot = variant.get("slot") or "late-night"
        h1, m1, h2, m2 = tmg.slot_window(slot)

        # walk forward to the first day that is under quota AND has this slot free
        placed = None
        for d in range(day, day + PLACE_HORIZON_DAYS):
            cand_day = datetime.datetime.now(tz) + datetime.timedelta(days=d)
            key = cand_day.strftime("%Y-%m-%d")
            if per_day.get(key, 0) >= POSTS_PER_DAY:
                continue
            mins = h1 * 60 + m1
            if any(abs(int(t[:2]) * 60 + int(t[3:]) - mins) < 70
                   for t in taken_slots.get(key, [])):
                continue
            placed = cand_day.replace(hour=h1, minute=(m1 + m2) // 2 % 60,
                                      second=0, microsecond=0)
            per_day[key] = per_day.get(key, 0) + 1
            taken_slots.setdefault(key, []).append(placed.strftime("%H:%M"))
            day = d
            break
        if placed is None:
            out.append({"variant": v, "post": None,
                        "why": f"no free {slot} slot in the next 21 days under "
                               f"{POSTS_PER_DAY}/day"})
            continue
        post_pick = cands[0]
        used.add(post_pick["uid"])
        out.append({"variant": v, "post": post_pick, "when": placed, "slot": slot})
    return p, root, creds, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args(argv)

    p, root, creds, plan_rows = build(a.persona, a.count)
    stills = json.load(open(os.path.join(personas.character_dir(p), "manifest.json")))

    print(f"experiment plan for {p['name']} — {len(plan_rows)} arm(s)\n")
    for r in plan_rows:
        v = r["variant"]
        head = f"  test {v['dimension_under_test']:<9} arm={v['arm']:<10}"
        if not r["post"]:
            print(f"{head}  SKIPPED — {r['why']}")
            continue
        print(f"{head}  {r['when']:%a %d %b %H:%M} ({r['slot']})")
        print(f"      {r['post']['uid']}  {(r['post'].get('on-screen') or r['post'].get('title'))[:58]!r}")
    if not a.go:
        print("\nPLAN ONLY — re-run with --go")
        return 0

    print()
    out_dir = os.path.join(root, "render", "out", "experiment")
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    # Threaded across the loop so pick_still's least-recently-used sort can actually
    # work. It was passed a fresh [] on every iteration, which made `fresh` and the LRU
    # key no-ops and returned pool[0] every time: 6 of 8 arms drew the same car selfie,
    # six days running. schedule_batch.py threads this correctly at line 347; this file
    # was a copy that dropped the accumulator.
    recent: list[str] = []
    for r in plan_rows:
        if not r["post"]:
            continue
        post, v, variant = r["post"], r["variant"], r["variant"]["variant"]
        sstyle = variant.get("screen_style") or "headline"
        lstyle = variant.get("list_style") or "caption-list"
        if lstyle == "on-screen-wall":
            screen = cap.on_screen_wall(post)
        else:
            screen = (post.get("on-screen") or post.get("title") or "").replace(" / ", "\n")
        still = sb.pick_still(post, stills, recent)
        recent.append(still["file"])
        if sstyle == "confessional":
            zone = ov.confessional_zone()
        elif lstyle == "on-screen-wall":
            zone = ov.full_ui_safe_zone()
        else:
            zone = still.get("safe_text_zone_wall" if post_density(post) == "wall"
                             else "safe_text_zone")
        stem = os.path.join(out_dir, post["uid"].replace("::", "-").replace(".md", ""))
        cp.compose(still=os.path.join(personas.character_dir(p), still["file"]),
                   out=stem, text=screen, style="tiktok-native", fmt="png",
                   safe_zone=zone, text_color=variant.get("text_color") or "white",
                   screen_style=sstyle,
                   # A post that DECLARES dramatized-labeled must actually get the tag.
                   # schedule_batch honours this; this path did not, and 3 of the 4
                   # product posts also pass under no-product — so they would have
                   # shipped a first-person product claim with no disclosure on it.
                   dramatization=(post.get("claim_posture") == gates.DRAMATIZED),
                   jpeg=True)
        url = up.upload(stem + ".jpg", verbose=False)

        text = (cap.wall_caption(post) if lstyle == "on-screen-wall"
                else cap.build(post, numbered=(lstyle == "numbered-count")))
        posture = post.get("claim_posture", "no-product")
        for _blob in (screen, text):
            _ok, _why = gates.check(_blob, claim_posture=posture)
            if not _ok:
                print(f"  BLOCKED (gate, posture={posture}): {_why[:100]}")
                break
        else:
            pass
        if not all(gates.check(b, claim_posture=posture)[0] for b in (screen, text)):
            continue
        ok, why = cap.validate(text)
        if not ok:
            print(f"  BLOCKED (caption): {why}")
            continue

        body = {"providers": [{"network": "tiktok"}], "text": text,
                "autoPublish": True, "draft": False,
                "publicationDate": {"dateTime": r["when"].strftime("%Y-%m-%dT%H:%M:%S"),
                                    "timezone": str(r["when"].tzinfo)},
                "media": [url], "saveExternalMediaFiles": True,
                "tiktokData": {"isAigc": True, "privacyOption": "PUBLIC_TO_EVERYONE",
                               "photoCoverIndex": 0, "disableComment": False,
                               # the audio arm is a REAL experimental variable here
                               "autoAddMusic": variant.get("audio") == "autoAddMusic"}}
        st, resp = mc._req("POST", "/v2/scheduler/posts", creds, body=body)
        d = (resp or {}).get("data") or resp or {}
        if st == 200 and d.get("id"):
            record_assignment(root, {
                # caption_head IS THE JOIN KEY, not post_id. assignments are keyed by
                # the Metricool scheduler id; ab-database.json is keyed by the TikTok
                # videoId. Those never match, so score.py falls back to matching on
                # caption text — and it matched NOTHING for 64 assignments because this
                # field was missing. The loop could not connect what it chose to what
                # happened, which is the entire point of the loop.
                "caption_head": text[:70],
                "post_id": str(d["id"]), "uid": post["uid"],
                "dimension_under_test": v["dimension_under_test"], "arm": v["arm"],
                "variant": variant, "scheduled_for": r["when"].isoformat(),
                "rendered": {"screen_style": sstyle, "list_style": lstyle,
                             "text_color": variant.get("text_color") or "white",
                             "actual_type": post_type(post)},
                "confounded_with": [c for c in CONFOUNDED.get(v["dimension_under_test"], ())
                                    if variant.get(c) and variant[c] != post_type(post)],
                "assigned_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            n += 1
            print(f"  scheduled id={d['id']}  testing {v['dimension_under_test']}={v['arm']}")
        else:
            print(f"  FAILED {st}: {str(resp)[:130]}")
    print(f"\n{n} arm(s) scheduled and recorded in learn/assignments.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
