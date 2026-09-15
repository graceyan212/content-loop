#!/usr/bin/env python3
"""
track.py — join what we posted to how it did.

WHY THIS EXISTS SEPARATELY FROM METRICOOL: Metricool knows the metrics but not the
*variant* — it cannot tell you that a post was a value-list in the car setting shipped
in the late-night slot. That metadata only lives here. And the metrics are perishable:
Metricool backfills ~30 days at connection time and TikTok caps video views at 60, so
anything not captured now is gone.

Deliberately small. This is not the full ab-database/learnings split from the spec;
it is the minimum that stops data being lost while that is unbuilt.

    python track.py pull                    # dani (default persona)
    python track.py add                     # register a post you just published (prompts)
    python track.py pull --persona chloe

Joining is on a normalized prefix of the caption, because posts published by hand in
the app carry no id we control. `match` records how confident that join is, rather
than pretending it is exact.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))     # ugc-pipeline/learn (CODE root)
CODE_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(CODE_ROOT, "post"))
sys.path.insert(0, CODE_ROOT)
import personas  # noqa: E402  (personas.py — persona registry + resolution)

# DATA path, "dani" default. A pure os.path.join (no FS access) so importing
# this module can never fail — main() always re-resolves the real persona via
# personas.resolve() and passes its posts.json path through explicitly;
# this constant only keeps load()/save()/pull() working unchanged when called
# with zero args, exactly as before this file moved.
_DANI_ROOT = os.path.normpath(os.path.join(CODE_ROOT, "..", "danielle"))
STORE = os.path.join(_DANI_ROOT, "learn", "posts.json")

# Metric to judge by, per content type. A question earns comments because replying is
# free; a list earns shares and saves because it is useful later. Comparing a list's
# comment count to a question's is measuring the wrong thing.
# Revised 2026-07-31 against the first 5 real posts. The original guess was that
# value lists earn SHARES (saved and sent rather than replied to). Two value posts
# in, shares are 0 and 0 — no support for that at all. Meanwhile the best post on
# the account (a self-aware joke, 14 likes) was being scored as a zero because it
# was typed `value` and measured on shares.
#
# What the 5 posts actually show: comments only move on questions, likes are the
# only metric that discriminates across all types. So likes is the default, and
# comments stays primary only where a reply is the explicit ask.
PRIMARY_METRIC = {
    "intro": "likes",
    "ask": "comments",      # the post asks a question; a reply is the success
    "value": "likes",
    "joke": "likes",
    "observe": "likes",
}
SECONDARY_METRIC = "comments"


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"#\w+", " ", s)              # hashtags drift between platforms
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load(store: str = STORE) -> dict:
    if os.path.exists(store):
        return json.load(open(store))
    return {"posts": [], "note": "Metrics are perishable; pull regularly."}


def save(db: dict, store: str = STORE) -> None:
    db["updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(store), exist_ok=True)
    json.dump(db, open(store, "w"), indent=2, ensure_ascii=False)


def pull(db: dict, persona: dict | None = None) -> dict:
    import metricool as mc
    p = persona or personas.resolve(personas.DEFAULT_PERSONA)
    blog_id = personas.require_blog_id(p)
    tz = personas.timezone(p)
    creds = mc.load_creds(blog_id=blog_id)
    d0 = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    status, body = mc._req("GET", "/v2/analytics/posts/tiktok", creds,
                           params={"from": d0, "to": d1, "timezone": tz})
    if status != 200:
        print(f"metricool returned {status}: {str(body)[:200]}", file=sys.stderr)
        return db
    live = [x for x in (body if isinstance(body, list) else body.get("data", []))
            if isinstance(x, dict)]

    by_norm = {norm(p.get("videoDescription", ""))[:70]: p for p in live}
    for rec in db["posts"]:
        key = norm(rec.get("caption", ""))[:70]
        hit = by_norm.get(key)
        match = "exact"
        if not hit:                              # fall back to a looser prefix
            for k, p in by_norm.items():
                if k and key and (k[:40] == key[:40]):
                    hit, match = p, "prefix"
                    break
        if not hit:
            rec["match"] = "none"
            continue
        rec["match"] = match
        rec["posted_at"] = hit.get("createTime")
        rec["metrics"] = {
            "views": hit.get("viewCount"), "likes": hit.get("likeCount"),
            "comments": hit.get("commentCount"), "shares": hit.get("shareCount"),
        }
        rec["url"] = hit.get("shareUrl")

    # Every live caption, normalized. The dashboard reads this to work out which
    # library posts have actually gone out, instead of relying on hand-ticked
    # checkboxes in one browser's localStorage.
    db["live_captions"] = sorted({norm(p.get("videoDescription", ""))[:70]
                                  for p in live if p.get("videoDescription")})
    db["live_pulled_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    # anything live that we have no record of
    known = {norm(r.get("caption", ""))[:70] for r in db["posts"]}
    db["unregistered_live_posts"] = [
        {"caption": p.get("videoDescription", "")[:90], "at": p.get("createTime"),
         "views": p.get("viewCount"), "comments": p.get("commentCount")}
        for k, p in by_norm.items() if k not in known
    ]
    return db


def table(db: dict) -> None:
    rows = [r for r in db["posts"] if r.get("metrics")]
    rows.sort(key=lambda r: r.get("posted_at") or "")
    if not rows:
        print("no matched posts yet"); return
    print(f"{'#':<3}{'type':<7}{'setting':<9}{'slot':<12}"
          f"{'views':>7}{'likes':>7}{'cmts':>6}{'shrs':>6} {'audio':<7} {'primary':<12} match")
    print("-" * 92)
    for i, r in enumerate(rows, 1):
        m = r["metrics"]
        pm = PRIMARY_METRIC.get(r.get("type", ""), "comments")
        print(f"{i:<3}{r.get('type',''):<7}{r.get('setting',''):<9}{r.get('slot',''):<12}"
              f"{m['views'] or 0:>7}{m['likes'] or 0:>7}{m['comments'] or 0:>6}"
              f"{m['shares'] or 0:>6} {(r.get('audio') or '?'):<7} "
              f"{pm}={m.get(pm) or 0:<6} {r.get('match','')}")
    # per-type medians, flagged as unreliable until there is enough of each
    print()
    from statistics import median
    for t in sorted({r.get("type") for r in rows if r.get("type")}):
        sub = [r for r in rows if r.get("type") == t]
        pm = PRIMARY_METRIC.get(t, "comments")
        vals = [r["metrics"].get(pm) or 0 for r in sub]
        warn = "  (n<3, not meaningful)" if len(sub) < 3 else ""
        print(f"  {t:<7} n={len(sub)}  median {pm} = {median(vals):.1f}{warn}")
    if db.get("unregistered_live_posts"):
        print(f"\n{len(db['unregistered_live_posts'])} live post(s) with no local record "
              f"- run `track.py add` so their variant is known:")
        for u in db["unregistered_live_posts"]:
            print(f"    {u['at'][:16] if u.get('at') else '?'}  {u['caption'][:64]!r}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull")
    a = sub.add_parser("add")
    # `audio` matters more than expected: the two worst-performing posts BOTH had
    # no sound, and both happened to be value lists, so content type and audio are
    # confounded in the first 5 posts. Track it or the confound persists.
    for f in ("caption", "type", "setting", "slot", "screen", "comment", "image", "audio"):
        a.add_argument(f"--{f}", default="")
    args = ap.parse_args(argv)

    p = personas.resolve(args.persona)
    store = os.path.join(p["root"], "learn", "posts.json")

    db = load(store)
    if args.cmd == "add":
        db["posts"].append({k: getattr(args, k) for k in
                            ("caption", "type", "setting", "slot", "screen", "comment",
                             "image", "audio")})
        save(db, store)
        print(f"registered. {len(db['posts'])} tracked.")
        return 0

    db = pull(db, persona=p)
    save(db, store)
    table(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
