#!/usr/bin/env python3
"""
crosspost.py — mirror scheduled TikTok stills to Instagram + Facebook as Reels.

    python post/crosspost.py --persona dani                  # dry run
    python post/crosspost.py --persona dani --go --limit 10

WHY A SEPARATE SCHEDULER ENTRY INSTEAD OF ADDING PROVIDERS TO THE TIKTOK POST:
Metricool can carry several networks on one entry, but changing an existing entry's
providers needs PUT, and PUT is a REPLACE that mints a NEW id. Every TikTok post id in
learn/assignments.json is the join key the A/B scoring depends on, so rewriting them
would silently orphan the entire experiment record. Mirrors are new entries; the
TikTok posts are never touched.

WHY REELS ARE VIDEO: a JPEG cannot be posted as a Reel. render/reel.py wraps each
still in a short MP4 with a slow push and a silent audio track. Source images are
pulled from the calendar's own hosted media URL rather than from local render output,
so this works for posts whose local files are long gone.

IDEMPOTENCY, two independent guards, because running this twice against a live account
would double-post to two networks:
  1. learn/crossposts.json maps tiktok_post_id -> mirror_post_id.
  2. Even with that file deleted, an existing IG/FB entry with the same caption inside
     the same day is treated as already mirrored.

AI DISCLOSURE: TikTok gets `isAigc` on every post. Meta exposes no equivalent field
through this API — verified, not assumed — so if a disclosure is wanted on IG/FB it
has to live in the caption. `--disclose "text"` appends it. Nothing is appended by
default, because silently editing the operator's captions is worse than asking.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import urllib.request

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

MIRROR_NETWORKS = ("instagram", "facebook")
OFFSET_MIN = 7           # keep the mirror off the exact TikTok minute


def state_path(root: str) -> str:
    return os.path.join(root, "learn", "crossposts.json")


def load_state(root: str) -> dict:
    try:
        return json.load(open(state_path(root)))
    except Exception:
        return {"mirrors": {}}


def save_state(root: str, obj: dict) -> None:
    f = state_path(root)
    tmp = f + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=2)
    os.replace(tmp, f)


def fetch(url: str, dest: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": mc.UA})
    with urllib.request.urlopen(req, context=mc._SSL, timeout=120) as r:
        blob = r.read()
    if len(blob) < 5000:
        raise RuntimeError(f"media at {url[:70]} was only {len(blob)} bytes")
    open(dest, "wb").write(blob)
    return dest


def statuses(post: dict) -> list[str]:
    return [(q.get("status") or "").upper() for q in (post.get("providers") or [])]


def networks(post: dict) -> set:
    return {(q.get("network") or "").lower() for q in (post.get("providers") or [])}


def gather(creds: dict, days: int) -> tuple[list, list]:
    d0 = datetime.date.today().strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    _, sc = mc._req("GET", "/v2/scheduler/posts", creds, params={"start": d0, "end": d1})
    rows = [x for x in (sc if isinstance(sc, list) else (sc or {}).get("data") or [])
            if isinstance(x, dict)]
    tiktok = [x for x in rows
              if "PENDING" in statuses(x) and networks(x) == {"tiktok"} and x.get("media")]
    mirrors = [x for x in rows if networks(x) & set(MIRROR_NETWORKS)]
    return tiktok, mirrors


def already(post: dict, mirrors: list, state: dict) -> str | None:
    pid = str(post.get("id"))
    if pid in state.get("mirrors", {}):
        return f"in crossposts.json as {state['mirrors'][pid]}"
    head = (post.get("text") or "").strip()[:60]
    day = ((post.get("publicationDate") or {}).get("dateTime") or "")[:10]
    for m in mirrors:
        if (m.get("text") or "").strip()[:60] == head and \
                ((m.get("publicationDate") or {}).get("dateTime") or "")[:10] == day:
            return f"an IG/FB entry with the same caption already exists on {day}"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--disclose", default="",
                    help="text appended to the IG/FB caption (AI disclosure). "
                         "Meta has no API field for this.")
    ap.add_argument("--seconds", type=float, default=reel.DEFAULT_SECONDS)
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args(argv)

    p = personas.resolve(a.persona)
    root = p["root"]
    creds = mc.load_creds()
    creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(p))
    state = load_state(root)

    tiktok, mirrors = gather(creds, a.days)
    tiktok.sort(key=lambda x: (x.get("publicationDate") or {}).get("dateTime") or "")
    todo, skipped = [], []
    for x in tiktok:
        why = already(x, mirrors, state)
        (skipped if why else todo).append((x, why))
    if a.limit:
        todo = todo[:a.limit]

    print(f"{len(tiktok)} pending TikTok post(s) in the next {a.days} days")
    print(f"  {len(skipped)} already mirrored, {len(todo)} to mirror"
          + (f" (limited to {a.limit})" if a.limit else ""))
    if a.disclose:
        print(f"  appending disclosure: {a.disclose!r}")
    else:
        print("  NO disclosure appended (Meta exposes no AI-label field via this API)")
    for x, _ in todo[:6]:
        when = ((x.get("publicationDate") or {}).get("dateTime") or "")[:16]
        print(f"    {when}  {(x.get('text') or '').splitlines()[0][:58]!r}")
    if len(todo) > 6:
        print(f"    ... and {len(todo)-6} more")
    if not a.go:
        print("\nDRY RUN — re-run with --go")
        return 0

    work = os.path.join(root, "render", "out", "reels")
    os.makedirs(work, exist_ok=True)
    ok = 0
    for x, _ in todo:
        pid = str(x.get("id"))
        src = (x.get("media") or [None])[0]
        stem = hashlib.sha256((src or pid).encode()).hexdigest()[:16]
        jpg, mp4 = os.path.join(work, stem + ".jpg"), os.path.join(work, stem + ".mp4")
        try:
            if not os.path.exists(mp4):
                fetch(src, jpg)
                reel.build(jpg, mp4, seconds=a.seconds)
            url = up.upload(mp4, verbose=False)
        except Exception as e:
            print(f"  SKIP {pid}: {type(e).__name__}: {str(e)[:110]}")
            continue

        dt = (x.get("publicationDate") or {}).get("dateTime") or ""
        try:
            when = (datetime.datetime.fromisoformat(dt)
                    + datetime.timedelta(minutes=OFFSET_MIN)).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            when = dt
        text = (x.get("text") or "").rstrip()
        if a.disclose:
            text = f"{text}\n\n{a.disclose}"

        body = {
            "providers": [{"network": n} for n in MIRROR_NETWORKS],
            "text": text, "autoPublish": True, "draft": False,
            "publicationDate": {"dateTime": when,
                                "timezone": personas.timezone(p)},
            "media": [url], "saveExternalMediaFiles": True,
            "instagramData": {"type": "REEL", "showReelOnFeed": True},
            "facebookData": {"type": "REEL"},
        }
        st, resp = mc._req("POST", "/v2/scheduler/posts", creds, body=body)
        d = (resp or {}).get("data") or resp or {}
        if st == 200 and isinstance(d, dict) and d.get("id"):
            state.setdefault("mirrors", {})[pid] = str(d["id"])
            save_state(root, state)          # after EVERY one, so a crash cannot double-post
            ok += 1
            print(f"  mirrored {pid} -> {d['id']}  {when[:16]}")
        else:
            print(f"  FAILED {pid}: {st} {str(resp)[:140]}")
    print(f"\n{ok} mirrored to {' + '.join(MIRROR_NETWORKS)} as Reels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
