#!/usr/bin/env python3
"""
publish.py — render-to-scheduled in one step: upload the image, create the post.

    python publish.py --config live-car-questions.json --image ../render/out/live/x.jpg
    python publish.py --config ... --image ... --publish      # actually schedule it
    python publish.py --persona chloe --config ... --image ...   # -> FATAL, no blogId yet

Defaults to a DRAFT. `--publish` is the only thing that makes it real, and it prints
the post id so it can be found or deleted afterwards.

`--persona` defaults to "dani" so a zero-flag invocation behaves exactly as before.
It resolves that persona's Metricool blogId and REQUIRES one to be configured —
posting one persona's content under another persona's (or no) blogId is the worst
failure mode in this pipeline, so a persona with no blogId (e.g. a freshly
scaffolded one) fails loudly here instead of silently posting to the wrong brand.
`--config`, given as a relative path, resolves against that persona's own post/
directory (where its live-*.json configs live), not this script's directory.

Metricool can return 200 while scheduling nothing (the classic cause is `providers`
as bare strings instead of objects), so this always echoes the per-network status
from the response rather than trusting the HTTP code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))     # ugc-pipeline/post (CODE root)
CODE_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, CODE_ROOT)
import metricool as mc
import upload as up
import personas                # noqa: E402  (personas.py — persona registry + resolution)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--publish", action="store_true",
                    help="schedule for real (default: create a draft)")
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.")
    a = ap.parse_args(argv)

    persona = personas.resolve(a.persona)
    # Refuse before anything else happens (before uploading media, even) — the
    # worst failure mode here is shipping a persona's content under the wrong
    # (or no) Metricool brand, so this must be impossible, not just unlikely.
    blog_id = personas.require_blog_id(persona)

    persona_post_dir = os.path.join(persona["root"], "post")
    cfg_path = a.config if os.path.isabs(a.config) else os.path.join(persona_post_dir, a.config)
    cfg = json.load(open(cfg_path))

    print("uploading image...")
    cfg["media_url"] = up.upload(a.image)
    print(f"  hosted at {cfg['media_url']}")

    creds = mc.load_creds(blog_id=blog_id)
    (status, body), sent = mc.create_post(creds, cfg, publish=a.publish)

    redacted = dict(sent)
    print(f"\n--- request ({'SCHEDULED' if a.publish else 'DRAFT'}) ---")
    print(json.dumps(redacted, indent=2, ensure_ascii=False)[:1400])
    print(f"\n--- response {status} ---")
    if status not in (200, 201):
        print(json.dumps(body, indent=2)[:1200])
        return 1

    data = (body or {}).get("data") or body or {}
    print(f"post id: {data.get('id')}")
    print(f"draft flag: {data.get('draft')}   autoPublish: {data.get('autoPublish')}")
    pub = data.get("publicationDate") or {}
    print(f"scheduled: {pub.get('dateTime')} {pub.get('timezone')}")
    provs = data.get("providers") or []
    if not provs:
        print("WARNING: no providers on the created post - nothing will publish.")
    for p in provs:
        print(f"  {p.get('network')}: {p.get('status')} {p.get('publicUrl') or ''}")
    for d in (data.get("descendants") or []):
        for p in (d.get("providers") or []):
            print(f"  child {p.get('network')}: {p.get('status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
