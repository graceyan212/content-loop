#!/usr/bin/env python3
"""
respin.py — re-render and re-schedule PENDING posts in place, keeping their slots.

Needed because a render fix (UI-safe zones, white-only text) has to reach posts that
are already queued, without losing the operator's per-post approvals or reshuffling
times they already signed off on.

Per pending post it matches the live caption back to a library entry, then:
  killed in copy/killed.json  -> delete, do not replace
  replacement mapped          -> swap in the new copy at the SAME time
  otherwise                   -> re-render identical copy with the current fixes

There is no in-place edit on Metricool: PUT replaces the post and mints a new id
(see metricool.py). So every change here is delete-then-create, and the new id is
printed. Nothing is touched without --go.

    python post/respin.py            # plan only
    python post/respin.py --go
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "render"), os.path.join(ROOT, "copy")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas          # noqa: E402
import metricool as mc   # noqa: E402
import upload as up      # noqa: E402
import compose as cp     # noqa: E402
import settings as cset  # noqa: E402
import caption as cap        # noqa: E402  (copy/caption.py - one formatter)
import gallery as gal    # noqa: E402
import schedule_batch as sb  # noqa: E402

# live post id -> library uid to put in its place, at the same scheduled time
REPLACEMENTS = {
    "value-01-meals.md::9":        "ask-02-numbers.md::n-20",   # breakfast: value -> ask
    "value-02-school-tweens.md::17": "ask-02-numbers.md::n-21", # -> mentally preparing for school year
}


def library_by_uid(root: str) -> dict:
    out = {}
    for f in gal.library_files(root):
        for p in gal.parse_posts(f):
            p["setting"] = cset.setting_of(p)
            p["uid"] = f"{os.path.basename(f)}::{p['id']}"
            out[p["uid"]] = p
    return out


def caption_text(post: dict) -> str:
    return cap.build(post)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--only-lists", action="store_true",
                    help="only touch posts that have an items list (the ones a caption "
                         "formatting change actually affects)")
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args(argv)

    p = personas.resolve(a.persona)
    root = p["root"]
    creds = mc.load_creds(); creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(p))

    items, _ = __import__("review").fetch(a.persona)
    pending = [x for x in items
               if any((pr.get("status") or "").upper() == "PENDING"
                      for pr in (x.get("providers") or []))]
    lib = library_by_uid(root)
    dead = sb.killed_uids(root)
    stills = json.load(open(os.path.join(personas.character_dir(p), "manifest.json")))
    by_file = {s["file"]: s for s in stills}

    plan = []
    for live in pending:
        live_cap = (live.get("text") or "").strip()
        best, score = None, 0.0
        for uid, lp in lib.items():
            sc = gal.similarity(caption_text(lp), live_cap)
            if sc > score:
                best, score = uid, sc
        action = "rerender"
        target = best
        if best in dead:
            action = "delete" if best not in REPLACEMENTS else "replace"
            target = REPLACEMENTS.get(best)
        if a.only_lists and not (lib.get(best) or {}).get("items"):
            continue
        plan.append({"live": live, "uid": best, "match": round(score, 2),
                     "action": action, "target": target})

    when = lambda x: (x["live"].get("publicationDate") or {}).get("dateTime", "")[:16]
    print(f"{len(pending)} pending post(s)\n")
    for it in sorted(plan, key=when):
        tag = {"delete": "DELETE  ", "replace": "REPLACE ", "rerender": "rerender"}[it["action"]]
        print(f"  {when(it).replace('T','  ')}  {tag}  {it['uid']}  (match {it['match']})")
        if it["action"] == "replace":
            print(f"                              -> {it['target']}")
    if not a.go:
        print("\nPLAN ONLY — re-run with --go")
        return 0

    print()
    out_dir = os.path.join(root, "render", "out", "respin")
    os.makedirs(out_dir, exist_ok=True)
    for it in sorted(plan, key=when):
        live, act = it["live"], it["action"]
        pid = live.get("id")
        st, _ = mc._req("DELETE", f"/v2/scheduler/posts/{pid}", creds)
        if act == "delete":
            print(f"  {when(it)[-5:]}  deleted {pid} (killed, not replaced) -> {st}")
            continue
        uid = it["target"] if act == "replace" else it["uid"]
        post = lib[uid]
        screen = (post.get("on-screen") or post.get("title") or "").replace(" / ", "\n")
        still = sb.pick_still(post, stills, [])
        density = "wall" if len(post.get("items") or []) >= 3 else "short"
        zone = still.get("safe_text_zone_wall" if density == "wall" else "safe_text_zone")
        stem = os.path.join(out_dir, uid.replace("::", "-").replace(".md", ""))
        cp.compose(still=os.path.join(personas.character_dir(p), still["file"]),
                   out=stem, text=screen, style="tiktok-native", fmt="png",
                   safe_zone=zone, text_color="light", jpeg=True)
        url = up.upload(stem + ".jpg", verbose=False)
        body = {"providers": [{"network": "tiktok"}], "text": caption_text(post),
                "autoPublish": True, "draft": False,
                "publicationDate": live.get("publicationDate"),
                "media": [url], "saveExternalMediaFiles": True,
                "tiktokData": {"isAigc": True, "privacyOption": "PUBLIC_TO_EVERYONE",
                               "photoCoverIndex": 0, "disableComment": False,
                               "autoAddMusic": True}}
        # LAST CHECK before the API call. build() is a convention; this is the
        # enforcement. A wall-of-text caption shipped once because nothing checked.
        _ok, _why = cap.validate(body["text"])
        if not _ok:
            print(f"  BLOCKED (caption): {_why}")
            continue
        for _w in cap.readability_warnings(body["text"]):
            print(f"      note: {_w}")
        st2, resp = mc._req("POST", "/v2/scheduler/posts", creds, body=body)
        nd = (resp or {}).get("data") or resp or {}
        # Recreating mints a NEW id. Without migrating the decision, every post the
        # operator already approved reappears in the approval queue after any render
        # fix - which would make the queue useless.
        try:
            import approve as ap_mod
            db = ap_mod.load_decisions(root)
            prev = next((d for d in db["decisions"] if str(d.get("post_id")) == str(pid)), None)
            if prev and nd.get("id"):
                moved = dict(prev)
                moved["post_id"] = str(nd["id"])
                moved["migrated_from"] = str(pid)
                db["decisions"] = [d for d in db["decisions"]
                                   if str(d.get("post_id")) not in (str(pid), str(nd["id"]))]
                db["decisions"].append(moved)
                json.dump(db, open(ap_mod.decisions_path(root), "w"), indent=2)
        except Exception as e:
            print(f"      (could not migrate approval for {pid}: {e})")
        verb = "replaced" if act == "replace" else "re-rendered"
        print(f"  {when(it)[-5:]}  {verb} -> new id {nd.get('id')}  "
              f"{os.path.basename(still['file'])}  [{st2}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
