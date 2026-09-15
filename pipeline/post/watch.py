#!/usr/bin/env python3
"""
watch.py — poll a scheduled Metricool post until it reaches a terminal state.

Exists because `autoPublish: true` is not a promise. A post can sit PENDING and then
fail at publish time with a network-specific reason that only appears in
`providers[].detailedStatus` — e.g. "does not specified privacy options", which is
how we learned TikTok photo posts require `tiktokData.privacyOption`.

    python watch.py 355679991 [--timeout-min 20]

Exits 0 on PUBLISHED, 1 on ERROR or timeout, printing the reason either way.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metricool as mc

# A successfully published TikTok post does NOT land on "PUBLISHED". It sits at
# AWAITING_CONFIRMATION with detailedStatus "Published:<tiktok_id>" - TikTok has
# accepted it and returned an id, but Metricool has not yet reconciled the post
# back from the platform. Treating only PUBLISHED as terminal made this watcher
# sit for its full 18-minute timeout on a post that had gone live in 90 seconds.
TERMINAL = {"PUBLISHED", "ERROR"}
SUCCESS_PREFIXES = ("published",)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("post_id")
    # Metricool's providers[].status lags the ACTUAL publish by several minutes:
    # a post that went live at 13:15 still read PENDING at 13:22 and only flipped
    # by 13:31. A short timeout therefore reports a false failure on a healthy
    # post. Default generously, and say so on timeout rather than implying failure.
    ap.add_argument("--timeout-min", type=int, default=30)
    ap.add_argument("--interval", type=int, default=30)
    a = ap.parse_args(argv)

    creds = mc.load_creds()
    deadline = time.time() + a.timeout_min * 60
    last = None

    while time.time() < deadline:
        st, body = mc._req("GET", f"/v2/scheduler/posts/{a.post_id}", creds)
        if st != 200:
            print(f"lookup {st} - post may have been replaced; list the scheduler")
            return 1
        d = (body or {}).get("data") or body or {}
        for p in (d.get("providers") or []):
            status = (p.get("status") or "").upper()
            detail = p.get("detailedStatus") or ""
            if status != last:
                stamp = datetime.datetime.now().astimezone().strftime("%H:%M:%S")
                print(f"[{stamp}] {p.get('network')}: {status} {detail}".rstrip())
                last = status

            # success is signalled by the DETAIL, not the status
            if detail.strip().lower().startswith(SUCCESS_PREFIXES):
                pid = detail.split(":", 1)[1].strip() if ":" in detail else "?"
                print(f"PUBLISHED. platform id {pid}")
                if p.get("publicUrl"):
                    print(f"  {p['publicUrl']}")
                return 0
            if status == "ERROR":
                print(f"FAILED: {detail!r}")
                return 1
            if status == "PUBLISHED":
                print(f"live at {p.get('publicUrl')}")
                return 0
        time.sleep(a.interval)

    print(f"still not terminal after {a.timeout_min} min (last: {last}).")
    print("NOTE: this is NOT necessarily a failure — Metricool's provider status "
          "lags the real publish by minutes. Check the analytics endpoint or the "
          "account itself before assuming the post did not go out.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
