#!/usr/bin/env python3
"""
export.py — turn a rendered batch into a Metricool bulk-import CSV + a queue map.

    python post/export.py --run-id 2026-07-27

Emits:
  post/metricool-<runId>.csv     (or -1.csv / -2.csv ... at >50 rows per file)
  post/queue-<runId>.json        (account, scheduled_datetime) -> variantId

The queue file is the join key for pulling analytics back later
(learn/pull.py in the design spec).

CSV FORMAT PROVENANCE
---------------------
Column names are transcribed from Metricool's own help centre, "Annex:
Available Fields in the Template":
  https://help.metricool.com/en/article/how-to-schedule-posts-in-batch-with-a-csv-file-in-metricool-3wihqx/
plus the troubleshooting article:
  https://help.metricool.com/en/article/common-troubleshooting-when-importing-csv-into-metricool-16c2syb/

What that documentation establishes:
  * Date and Time are two SEPARATE columns. There is no ISO8601 datetime column
    and no timezone column. The date/time FORMAT is chosen by the operator in a
    dropdown at import time, so it must match what this file writes.
  * Networks are BOOLEAN columns per network (TikTok / Instagram / ...), TRUE or
    FALSE. There is no single "network" column and no lowercase slug values.
  * A video goes in `Picture Url 1` — there is no separate video URL column. It
    must be a public URL resolving directly to the .mp4.
  * 50 rows/file is Metricool's recommendation, not a hard limit.
  * There is NO AI-disclosure column. See post/CSV-FORMAT-UNVERIFIED.md.

What is NOT verified is written up in post/CSV-FORMAT-UNVERIFIED.md. Read it
before your first real import.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COPY_DIR = os.path.join(ROOT, "copy")

DEFAULT_MEDIA_BASE = "https://REPLACE-ME/"

# Networks Metricool exposes as boolean columns, in the documented order.
NETWORKS = ["Facebook", "Twitter/X", "LinkedIn", "GBP", "Instagram", "Pinterest",
            "TikTok", "YouTube", "Threads", "Bluesky"]

# The documented date/time formats. Metricool makes the operator declare these
# in a dropdown at import; these two are the examples the article itself gives.
DATE_FMT = "%Y-%m-%d"          # YYYY-MM-DD
TIME_FMT = "%H:%M:%S"          # 00:00:00

# Time buckets from the design spec's test matrix, as [hour_lo, hour_hi) local.
TIME_BUCKETS = {
    "morning": (7, 9),
    "midday": (11, 14),
    "evening": (17, 20),
    "late-night": (21, 23),
}
BUCKET_ORDER = ["morning", "midday", "evening", "late-night"]

ROWS_PER_FILE = 50


def full_columns() -> list[str]:
    """
    The complete documented field list, in the Annex's order.

    The Annex writes repeated fields as "Picture Url 1 (up to 10)". Those
    parentheticals are editorial, so they are expanded to the real indexed
    names here. The TikTok "(personal accounts only)" suffixes are NOT
    expanded/stripped because they read as part of the documented field name
    and we could not verify either way — see CSV-FORMAT-UNVERIFIED.md.
    """
    cols = ["Text", "Date", "Time", "Draft", *NETWORKS]
    cols += [f"Picture Url {i}" for i in range(1, 11)]
    cols += [f"Alt text picture {i}" for i in range(1, 11)]
    cols += ["Document title", "Shortener", "Video Thumbnail Url", "Video Cover Frame",
             "Twitter/X Can reply", "Twitter/X Type", "Twitter/X Poll Duration minutes"]
    cols += [f"Twitter/X Poll Option {i}" for i in range(1, 5)]
    cols += ["Pinterest Board", "Pinterest Pin Title", "Pinterest Pin Link",
             "Pinterest Pin New Format", "Instagram Post Type",
             "Instagram Show Reel On Feed", "YouTube Video Title", "YouTube Video Type",
             "YouTube Video Privacy", "YouTube video for kids", "YouTube Video Category",
             "YouTube Video Tags", "YouTube Playlist", "GBP Post Type",
             "Facebook Post Type", "Facebook Title", "First Comment Text",
             "TikTok Title", "TikTok disable comments", "TikTok disable duet",
             "TikTok disable stitch",
             "TikTok Post Privacy (personal accounts only)",
             "TikTok Branded Content (personal accounts only)",
             "TikTok Your Brand (personal accounts only)",
             "TikTok Auto Add Music (personal accounts only)",
             "TikTok Photo Cover Index", "TikTok musicId", "TikTok music title",
             "TikTok music author", "TikTok music previewUrl",
             "TikTok music thumbnailUrl", "TikTok music soundVolume",
             "TikTok music originalVolume", "TikTok music startMillis",
             "TikTok music endMillis", "LinkedIn Type", "LinkedIn Poll Question"]
    cols += [f"LinkedIn Poll Option {i}" for i in range(1, 5)]
    cols += ["LinkedIn Poll Duration", "LinkedIn Show link preview",
             "LinkedIn Images as Carousel", "Threads Reply Control",
             "Threads Is Spoiler", "Threads Post Type", "Brand name"]
    return cols


def minimal_columns() -> list[str]:
    """Only the load-bearing columns for a video post."""
    return ["Text", "Date", "Time", "Draft", *NETWORKS, "Picture Url 1", "Brand name"]


# ---------------------------------------------------------------------------
def schedule(n: int, start: date, per_day: int, seed: str) -> list[dict]:
    """
    Spread n posts across consecutive days, rotating the four time buckets and
    jittering the minutes so nothing fires on an exact clock mark.

    Deterministic for a given (n, start, per_day, seed) so re-running an export
    does not reshuffle a schedule you already imported.
    """
    rng = random.Random(f"{seed}|{start.isoformat()}|{per_day}|{n}")
    slots = []
    for i in range(n):
        day = start + timedelta(days=i // per_day)
        bucket = BUCKET_ORDER[i % len(BUCKET_ORDER)] if per_day >= len(BUCKET_ORDER) \
            else BUCKET_ORDER[(i) % len(BUCKET_ORDER)]
        lo, hi = TIME_BUCKETS[bucket]
        hour = rng.randint(lo, hi - 1) if hi - 1 > lo else lo
        minute = rng.randint(0, 59)
        if minute % 15 == 0:                 # dodge :00 :15 :30 :45
            minute = (minute + rng.randint(1, 7)) % 60
        second = 0
        slots.append({
            "datetime": datetime(day.year, day.month, day.day, hour, minute, second),
            "time_bucket": bucket,
        })
    slots.sort(key=lambda s: s["datetime"])
    return slots


def load_batch(run_id: str) -> dict:
    path = os.path.join(COPY_DIR, f"batch-{run_id}.json")
    if not os.path.isfile(path):
        raise SystemExit(f"FATAL: {path} not found. Run batch.py --run-id {run_id} first.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def media_rel_path(v: dict) -> str:
    """
    The variant's primary media file, relative to the project root.

    batch.py writes `media_path` (png for a TikTok Photo post, mp4 for a video)
    and only writes `mp4_path` when an MP4 actually exists. Older batch JSON has
    `mp4_path` only, hence the fallback.
    """
    return v.get("media_path") or v["mp4_path"]


def build_rows(variants, slots, accounts, media_base, network, columns, brand_default):
    rows, queue = [], []
    for i, (v, slot) in enumerate(zip(variants, slots)):
        account = accounts[i % len(accounts)]
        rel = media_rel_path(v)
        mp4 = os.path.basename(rel)
        url = media_base.rstrip("/") + "/" + mp4
        dt = slot["datetime"]
        row = {c: "" for c in columns}
        row["Text"] = v["hook_text"]
        row["Date"] = dt.strftime(DATE_FMT)
        row["Time"] = dt.strftime(TIME_FMT)
        row["Draft"] = "FALSE"
        for net in NETWORKS:
            if net in row:
                row[net] = "TRUE" if net == network else "FALSE"
        row["Picture Url 1"] = url
        if "Brand name" in row:
            row["Brand name"] = brand_default or account
        rows.append(row)
        queue.append({
            "account": account,
            "scheduled_datetime": dt.isoformat(timespec="seconds"),
            "variantId": v["variantId"],
            "network": network,
            "time_bucket": slot["time_bucket"],
            "media_url": url,
            "media": rel,
            "mp4": v.get("mp4_path"),
            "territory": v["territory"],
            "text_density": v["text_density"],
            "text_style": v["text_style"],
            "claim_posture": v["claim_posture"],
            "duration": v["duration"],
            "still": v["still"],
        })
    return rows, queue


def write_csvs(rows, columns, run_id, out_dir) -> list[str]:
    chunks = [rows[i:i + ROWS_PER_FILE] for i in range(0, len(rows), ROWS_PER_FILE)] or [[]]
    paths = []
    for idx, chunk in enumerate(chunks, 1):
        name = (f"metricool-{run_id}.csv" if len(chunks) == 1
                else f"metricool-{run_id}-{idx}.csv")
        path = os.path.join(out_dir, name)
        # newline="" per csv docs; utf-8-sig so Excel/Sheets read the BOM and
        # do not mangle the em-dashes and curly quotes a hook may carry.
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, lineterminator="\r\n")
            w.writeheader()
            w.writerows(chunk)
        paths.append(path)
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Export a rendered batch to a Metricool bulk-import CSV + queue map.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--start-date", default=None,
                    help="first scheduled day, YYYY-MM-DD (default: tomorrow)")
    ap.add_argument("--per-day", type=int, default=4,
                    help="posts per day; 4 covers all four time buckets (default 4)")
    ap.add_argument("--accounts", default="danielle_main",
                    help="comma-separated account handles to round-robin across")
    ap.add_argument("--network", default="TikTok", choices=NETWORKS)
    ap.add_argument("--brand-name", default=None,
                    help="value for the Metricool 'Brand name' column "
                         "(default: the account handle)")
    ap.add_argument("--columns", default="full", choices=["full", "minimal"],
                    help="'full' writes every documented template column (Metricool's "
                         "troubleshooting doc says not to delete columns); 'minimal' "
                         "writes only the load-bearing ones")
    ap.add_argument("--out", default=None, help="output dir (default post/)")
    a = ap.parse_args(argv)

    out_dir = os.path.abspath(a.out or HERE)
    os.makedirs(out_dir, exist_ok=True)

    batch = load_batch(a.run_id)
    variants = [v for v in batch["variants"] if v.get("rendered")]
    if not variants:
        raise SystemExit(f"FATAL: no rendered variants in batch-{a.run_id}.json")

    missing = [v["variantId"] for v in variants
               if not os.path.isfile(os.path.join(ROOT, media_rel_path(v)))]
    if missing:
        print(f"WARN: {len(missing)} variant(s) have no media file on disk: {missing}",
              file=sys.stderr)

    if a.start_date:
        start = datetime.strptime(a.start_date, "%Y-%m-%d").date()
    else:
        start = date.today() + timedelta(days=1)

    per_day = max(1, a.per_day)
    accounts = [s.strip() for s in a.accounts.split(",") if s.strip()] or ["danielle_main"]
    media_base = os.environ.get("MEDIA_BASE_URL", DEFAULT_MEDIA_BASE)
    if "REPLACE-ME" in media_base:
        print("WARN: MEDIA_BASE_URL is unset, so media URLs use the "
              f"{DEFAULT_MEDIA_BASE!r} placeholder. Metricool attaches media BY URL and "
              "will reject these until the MP4s are hosted somewhere public.",
              file=sys.stderr)

    columns = full_columns() if a.columns == "full" else minimal_columns()
    slots = schedule(len(variants), start, per_day, seed=a.run_id)
    rows, queue = build_rows(variants, slots, accounts, media_base,
                             a.network, columns, a.brand_name)

    csv_paths = write_csvs(rows, columns, a.run_id, out_dir)

    queue_path = os.path.join(out_dir, f"queue-{a.run_id}.json")
    # The brief's join key is (account, scheduled_datetime) -> variantId. Keep
    # that exact mapping addressable, and carry the detail rows alongside.
    mapping = {f"{q['account']}|{q['scheduled_datetime']}": q["variantId"] for q in queue}
    if len(mapping) != len(queue):
        print("WARN: two posts share an (account, scheduled_datetime) key; "
              "the analytics join will be ambiguous. Raise --per-day spread.",
              file=sys.stderr)
    with open(queue_path, "w", encoding="utf-8") as fh:
        json.dump({
            "runId": a.run_id,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "network": a.network,
            "media_base_url": media_base,
            "csv_files": [os.path.relpath(p, ROOT) for p in csv_paths],
            "join_key_format": "<account>|<scheduled_datetime ISO8601 local>",
            "mapping": mapping,
            "entries": queue,
        }, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    for p in csv_paths:
        print(f"wrote {p} ({sum(1 for _ in open(p, encoding='utf-8-sig')) - 1} rows)")
    print(f"wrote {queue_path} ({len(mapping)} scheduled posts)")
    print(f"date format {DATE_FMT} / time format {TIME_FMT} — select these in "
          f"Metricool's import dropdowns or the rows will land on the wrong day.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
