#!/usr/bin/env python3
"""
score.py — cadence step: pull Metricool metrics, join to ab-database.json by
platform_post_id (falling back to a normalized-caption join), recompute
learnings.json's rollups + front_runners, and append any new conclusion to
decisions_log. Ported from the reference implementation's hermes/src/score.ts
pattern (/tmp/sffs-ai-video-pipeline): pull -> join -> recompute -> log.

Does NOT write a second API client — the actual HTTP call is
post/metricool.py's `_req`, exactly as learn/track.py already uses it. This
file adds the ab-database/learnings split on top; track.py is left in place
as the pre-existing thin tool (see track.py's own docstring) and still works
unmodified.

    python score.py                    # dani (default persona): pull + recompute
    python score.py --persona chloe
    python score.py --no-pull          # recompute learnings.json from whatever
                                        # ab-database.json already has (offline,
                                        # used by tests / a dry run)

FIRST RUN: if <persona>/learn/ab-database.json does not exist yet, it is built
from the legacy <persona>/learn/posts.json (see migrate_from_legacy()) without
losing anything in it. posts.json itself is never modified or deleted.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # ugc-pipeline/learn (CODE root)
CODE_ROOT = os.path.dirname(HERE)
for _p in (os.path.join(CODE_ROOT, "post"), CODE_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import personas  # noqa: E402
from rollup import (  # noqa: E402
    compute_rollups, pick_front_runner, has_metrics, PRIMARY_METRIC, SECONDARY_METRIC, MIN_N,
)
from dimensions import (DEFAULTED_DIMENSIONS, FALLBACK_DEFAULTS, DIMENSIONS,  # noqa: E402  (DIMENSIONS: canonical arm catalog, used to ignore data-quality "unknown" values below)
                        dimensions_for, fallback_defaults_for)
from track import norm as _norm  # noqa: E402  (reuse the ONE caption-normalizer, don't reinvent it)

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def data_paths(persona: dict) -> dict[str, str]:
    learn_dir = os.path.join(persona["root"], "learn")
    return {
        "learn_dir": learn_dir,
        "legacy_posts": os.path.join(learn_dir, "posts.json"),
        "ab_db": os.path.join(learn_dir, "ab-database.json"),
        "learnings": os.path.join(learn_dir, "learnings.json"),
        "defaults": os.path.join(learn_dir, "defaults.json"),
        "proposals": os.path.join(learn_dir, "proposals.json"),
    }


def _read_json(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# one-time migration: posts.json (flat, ad hoc) -> ab-database.json (schema)
# ---------------------------------------------------------------------------
#
# ONE manual correction lives here, not in the algorithmic join below: the
# `value` post whose local `caption` field reads "I used to just throw granola
# bars in there..." (screen text "What lives in my car for the inevitable
# hunger meltdown"; first_comment "the electrolyte packet thing fixed half our
# 5pm meltdowns") was actually PUBLISHED under a completely different, longer
# caption ("Here are my go-to's: Meat sticks... Electrolyte packet: Stuffed in
# with the water... fixes it in 10 minutes...") — verified 2026-07-31 by
# pulling raw Metricool analytics and finding a live TikTok video (videoId
# 7668076679327255822, posted 2026-07-30T00:14:22+0200, 968 views / 3 likes /
# 0 comments / 0 shares) whose caption is NOT a prefix match for the local
# record but whose CONTENT is: the "electrolyte packet ... fixes it" detail
# appears verbatim in both. No normalized-caption-prefix algorithm would ever
# find this join (the two caption strings share no common prefix at all), so
# it is recorded here as a one-time, human-verified bootstrap rather than
# invented logic. Once bootstrapped, `platform_post_id` makes every future
# score.py run join this record by ID (match="id"), same as every other post
# from here on — this is not a permanent special case, just how the first
# join for this one post had to happen.
_GRANOLA_BAR_CAPTION_PREFIX = "i used to just throw granola bars"
_GRANOLA_BAR_BOOTSTRAP_ID = "7668076679327255822"


def _infer_first_comment(comment: str) -> str:
    return "yes" if (comment or "").strip() else "no"


def _infer_density(screen: str) -> str:
    # All 7 real posts' on-screen text is a single short line (see
    # danielle/learn/posts.json); none is the multi-paragraph "wall" density
    # batch.py's copy drafts can produce. Flagging by word count keeps this
    # honest rather than hardcoding "short" for every record.
    words = len((screen or "").split())
    return "wall" if words > 40 else "short"


def migrate_from_legacy(legacy: dict, persona_name: str) -> dict:
    """Build a fresh ab-database.json structure from the old posts.json shape.
    Loses nothing: every field on every record is preserved either as a named
    variant axis or in `notes`."""
    posts_out = []
    for i, rec in enumerate(legacy.get("posts", []), start=1):
        variant = {
            "type": rec.get("type") or "unknown",
            "slot": rec.get("slot") or "unknown",
            "audio": rec.get("audio") or "unknown",
            "setting": rec.get("setting") or "unknown",
            # Not tracked per-post in posts.json — both are single-arm so far
            # (see danielle/persona.json render.text_color_default="light";
            # no `screen` text in the source data is long enough to be
            # "wall"). Inferred, not fabricated: see _infer_density().
            "density": _infer_density(rec.get("screen", "")),
            "first_comment": _infer_first_comment(rec.get("comment", "")),
            "text_color": "light",
        }
        m = rec.get("metrics")
        metrics = None
        if m:
            metrics = {
                "views": m.get("views"), "likes": m.get("likes"),
                "comments": m.get("comments"), "shares": m.get("shares"),
                "engagement_untrusted": None,  # backfilled by score.py's pull
                "source": "api", "as_of": None,
            }
        match = rec.get("match") or "pending"
        platform_post_id = None
        notes = None
        caption_norm = _norm(rec.get("caption", ""))
        if caption_norm.startswith(_GRANOLA_BAR_CAPTION_PREFIX):
            platform_post_id = _GRANOLA_BAR_BOOTSTRAP_ID
            match = "manual"
            notes = (
                "Local caption is stale (drafted before final copy). Joined by "
                "human-verified content match, not caption text — see the "
                "_GRANOLA_BAR_* comment in score.py's migrate_from_legacy(). "
                "platform_post_id is now pinned so every future score.py run "
                "joins this record by id."
            )
        posts_out.append({
            "id": f"p{i}",
            "caption": rec.get("caption", ""),
            "variant": variant,
            "screen": rec.get("screen", ""),
            "comment": rec.get("comment", ""),
            "image": rec.get("image", ""),
            "platform_post_id": platform_post_id,
            "posted_at": rec.get("posted_at"),
            "url": rec.get("url"),
            "match": match,
            "metrics": metrics,
            "notes": notes,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "persona": persona_name,
        "description": (
            "A/B-testing brain (raw layer): one record per post (posted or "
            "drafted), tagged with the 7 content dimensions in dimensions.py, "
            "joined to Metricool performance metrics. learnings.json is ALWAYS "
            "recomputed from this file's posts[] — never hand-edited. Migrated "
            f"from the legacy posts.json on {_now_iso()}; nothing was dropped."
        ),
        "conventions": {
            "primary_metric_by_type": dict(PRIMARY_METRIC),
            "primary_metric_note": (
                "Corrected 2026-07-31 against the first 5 real posts: the "
                "original guess was that `value` posts earn SHARES. Two value "
                "posts in, shares were 0 and 0 — no support — while the "
                "account's best post (a `joke`, 14 likes) was being scored as "
                "a zero because it was typed `value` and judged on shares. "
                "Comments only move on `ask` posts; likes discriminates "
                "everywhere else. See rollup.py's PRIMARY_METRIC for the "
                "live copy of this note."
            ),
            "secondary_metric": SECONDARY_METRIC,
            "aggregation": "median, not mean — small samples with near-zero "
                            "outliers make means meaningless",
            "min_n": MIN_N,
            "engagement_untrusted": (
                "Metricool's own per-post `engagement` rate is stored as a "
                "STRING (metrics.engagement_untrusted) and MUST NEVER be "
                "averaged or fed into a rollup: the account's follower base "
                "is near zero, so any rate metric is dominated by noise. Use "
                "the raw counts (views/likes/comments/shares) instead."
            ),
            "match_confidence_scale": {
                "id": "joined by platform_post_id (TikTok videoId) — the most trustworthy tier, used for every post once its id is known",
                "exact": "70-char normalized-caption match against a live post with no known id yet",
                "prefix": "looser 40-char normalized-caption prefix match",
                "manual": "human-verified content match (NOT a caption match) done once during the posts.json migration; see migrate_from_legacy()",
                "none": "no live post could be matched; metrics remain pending",
            },
        },
        "posts": posts_out,
        "live_captions": legacy.get("live_captions", []),
        "unregistered_live_posts": legacy.get("unregistered_live_posts", []),
    }


def ensure_data_files(paths: dict[str, str], persona_name: str,
                      persona: dict | None = None) -> dict:
    """Create ab-database.json (migrating from posts.json if needed),
    learnings.json, defaults.json, proposals.json — each ONLY if missing.
    Returns the (possibly freshly-migrated) ab-database dict."""
    if not os.path.exists(paths["defaults"]):
        _write_json(paths["defaults"], {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "persona": persona_name,
            "description": (
                "SOURCE OF TRUTH for the current content defaults every post "
                "gets unless it is the one arm under test (see dimensions.py "
                "build_arms()). The ONLY writer is a human via promote.py "
                "--approve; score.py and plan.py only ever READ this file."
            ),
            # Dani's seed ONLY if every value in it is valid in this persona's
            # own universe; {} otherwise. Seeding Ray with type=ask would write a
            # reigning choice nobody made into his directory on first score, and
            # build_arms() then returns no arms for empty defaults rather than
            # testing four content types he has never posted.
            "defaults": fallback_defaults_for(persona),
            "promotion": {
                "metric": "median_likes (median_comments for the `type` "
                          "dimension is intentionally excluded from automatic "
                          "promotion — see promote.py)",
                "min_sample": 5,
                "min_abs_improvement": 2.0,
                "min_rel_improvement": 0.2,
                "notes": "min_sample is deliberately stricter than "
                         "learnings.json's front-runner min_n=3: flipping a "
                         "sticky default is a bigger decision than naming a "
                         "transient front-runner.",
            },
            "history": [],
        })
    if not os.path.exists(paths["proposals"]):
        _write_json(paths["proposals"], {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "persona": persona_name,
            "description": (
                "Durable default-promotion proposal queue. promote.py detect "
                "writes `pending` proposals here when a challenger arm clearly "
                "beats the incumbent default; NEVER auto-applied. A human runs "
                "promote.py approve/reject; every decision is appended to "
                "decisions_log below AND (on approval) to defaults.json history "
                "and learnings.json decisions_log."
            ),
            "proposals": [],
            "decisions_log": [],
        })
    if os.path.exists(paths["ab_db"]):
        return _read_json(paths["ab_db"], {})
    legacy = _read_json(paths["legacy_posts"], {"posts": []})
    db = migrate_from_legacy(legacy, persona_name)
    _write_json(paths["ab_db"], db)
    return db


# ---------------------------------------------------------------------------
# pull + join
# ---------------------------------------------------------------------------
def pull_live(persona: dict) -> list[dict]:
    import metricool as mc
    blog_id = personas.require_blog_id(persona)
    tz = personas.timezone(persona)
    creds = mc.load_creds(blog_id=blog_id)
    d0 = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    status, body = mc._req("GET", "/v2/analytics/posts/tiktok", creds,
                            params={"from": d0, "to": d1, "timezone": tz})
    if status != 200:
        print(f"metricool returned {status}: {str(body)[:200]}", file=sys.stderr)
        return []
    return [x for x in (body if isinstance(body, list) else body.get("data", [])) if isinstance(x, dict)]


def pull_ig_reels(persona: dict) -> list[dict]:
    """
    Instagram reel analytics. Kept separate from pull_live on purpose.

    WHY THIS EXISTS. Every arm in this system was being judged on TikTok, where
    measured 2026-08-13 only 5 of 12 submitted posts ever went live (42%) while
    Instagram ran 18 of 18 (100%). Learning from a surface that drops well over half
    the sample, in an order nobody controls, is learning from noise: an arm can look
    dead because TikTok never shipped it.

    Instagram also returns strictly more of what the operator actually asked about.
    TikTok gives views / likes / comments / shares and nothing else. This endpoint
    returns saved, shares, reach, impressionsTotal, averageWatchTime,
    durationSeconds and reelsSkipRate — so saves are directly measurable and
    completion is derivable, rather than being asked for in the caption.

    The parameter names are `from`/`to`. `start`/`end` returns HTTP 500 and a
    from/to pair without times returns 400; both were confirmed against the live
    API rather than assumed.
    """
    import metricool as mc
    creds = mc.load_creds(blog_id=personas.require_blog_id(persona))
    # The TIME PART IS MANDATORY. A bare yyyy-MM-dd is rejected with
    # "Valid format is: date-time in format yyyy-MM-dd'T'HH:mm:ss", and `start`/`end`
    # instead of `from`/`to` returns a 500. Both confirmed against the live API.
    d0 = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    status, body = mc._req("GET", "/v2/analytics/reels/instagram", creds,
                           params={"from": d0, "to": d1})
    if status != 200:
        print(f"instagram reels returned {status}: {str(body)[:200]}", file=sys.stderr)
        return []
    rows = body if isinstance(body, list) else body.get("data", [])
    return [x for x in rows if isinstance(x, dict)]


def ingest_instagram(db: dict, reels: list[dict], root: str, as_of: str) -> tuple[int, int]:
    """
    Store reel metrics under db["ig_posts"]. Returns (added, refreshed).

    A SEPARATE LIST, NOT MIXED INTO db["posts"]. Two reasons, and the first one is
    the one that would quietly corrupt every number in the system:

    1. THE DENOMINATORS ARE NOT THE SAME UNIT. adjust.py scores comments-per-1000
       views. TikTok "views" and Instagram "reach" are different measurements of
       different things, and a median taken across a pool of both is a number with
       no referent. Appending reels to db["posts"] would have made every existing
       rollup silently wrong while every count went up, which looks like progress.
    2. Nothing else has to change. Every current consumer of db["posts"] keeps
       reading exactly the pool it was written against.

    `views` is set from `reach` — unique accounts, the closest analogue to the
    denominator TikTok gives — and impressionsTotal is kept alongside it rather
    than substituted, so a later decision can pick either without a re-pull.
    """
    existing = {str(r.get("platform_post_id")): r for r in db.get("ig_posts", [])}
    assigned_by_cap = {}
    apath = os.path.join(root, "learn", "assignments.json")
    if os.path.exists(apath):
        try:
            for a in (json.load(open(apath)).get("assignments") or []):
                head = _norm(a.get("caption_head", ""))[:60]
                if head and a.get("variant"):
                    assigned_by_cap.setdefault(head, a)
        except Exception:
            pass

    added = refreshed = 0
    for r in reels:
        rid = str(r.get("reelId") or "")
        if not rid:
            continue
        cap = r.get("content", "") or ""
        watch = r.get("averageWatchTime") or 0
        dur = r.get("durationSeconds") or 0
        metrics = {
            "views": r.get("reach") or 0,          # unique accounts; see docstring
            "impressions": r.get("impressionsTotal") or 0,
            "likes": r.get("likes") or 0,
            "comments": r.get("comments") or 0,
            "shares": r.get("shares") or 0,
            "saves": r.get("saved") or 0,
            "watch_seconds": watch,
            "duration_seconds": dur,
            # completion can exceed 1.0 — a loop replay counts toward watch time.
            # Left uncapped so a >1.0 value stays visible as the replay signal it is.
            "completion": round(watch / dur, 3) if dur else None,
            "skip_rate": r.get("reelsSkipRate"),
            "source": "api", "as_of": as_of,
        }
        if rid in existing:
            existing[rid]["metrics"] = metrics
            refreshed += 1
            continue

        variant, provenance = {}, "inferred"
        a = assigned_by_cap.get(_norm(cap)[:60])
        if a:
            variant = dict(a.get("variant") or {})
            rendered = a.get("rendered") or {}
            for k in ("screen_style", "text_color", "slot"):
                if rendered.get(k) and k not in variant:
                    variant[k] = rendered[k]
            if rendered.get("actual_type") and "type" not in variant:
                variant["type"] = rendered["actual_type"]
            provenance = "assigned" if variant else "inferred"
        if provenance == "inferred":
            variant = {"slot": _infer_slot((r.get("publishedAt") or {}).get("dateTime", "")),
                       "density": _infer_density(cap),
                       "type": _infer_type(cap),
                       "audio": "unknown"}

        db.setdefault("ig_posts", []).append({
            "platform": "instagram",
            "platform_post_id": rid,
            "caption": cap,
            "posted_at": (r.get("publishedAt") or {}).get("dateTime"),
            "url": r.get("url"),
            "match": "id",
            "variant": variant,
            "variant_provenance": provenance,
            "metrics": metrics,
        })
        added += 1
    return added, refreshed


def join_and_update(db: dict, live: list[dict], as_of: str) -> int:
    """Match db['posts'] against `live` (raw Metricool rows). Updates metrics
    IN PLACE. Never overwrites a record that already carries a confident
    manual/id join with contradictory data from a WORSE-confidence path — id
    beats exact beats prefix, and a record already at "manual"/"id" is only
    ever refreshed (same post), never reassigned. Returns count updated."""
    by_id = {str(v.get("videoId")): v for v in live if v.get("videoId")}
    by_norm70 = {_norm(v.get("videoDescription", ""))[:70]: v for v in live}

    updated = 0
    for rec in db.get("posts", []):
        hit, match = None, None
        pid = rec.get("platform_post_id")
        if pid and str(pid) in by_id:
            hit, match = by_id[str(pid)], rec.get("match") if rec.get("match") == "manual" else "id"
        if hit is None:
            key70 = _norm(rec.get("caption", ""))[:70]
            if key70 and key70 in by_norm70:
                hit, match = by_norm70[key70], "exact"
            else:
                key40 = key70[:40]
                for k, v in by_norm70.items():
                    if key40 and k[:40] == key40:
                        hit, match = v, "prefix"
                        break
        if hit is None:
            continue
        rec["match"] = match
        rec["platform_post_id"] = str(hit.get("videoId")) if hit.get("videoId") else pid
        rec["posted_at"] = hit.get("createTime") or rec.get("posted_at")
        rec["url"] = hit.get("shareUrl") or rec.get("url")
        eng = hit.get("engagement")
        rec["metrics"] = {
            "views": hit.get("viewCount"),
            "likes": hit.get("likeCount"),
            "comments": hit.get("commentCount"),
            "shares": hit.get("shareCount"),
            # STRING on purpose — see conventions.engagement_untrusted. Never
            # parse this back to a float for a rollup.
            "engagement_untrusted": f"{eng:.4f}" if isinstance(eng, (int, float)) else None,
            "source": "api",
            "as_of": as_of,
        }
        updated += 1

    known_ids = {str(r.get("platform_post_id")) for r in db.get("posts", []) if r.get("platform_post_id")}
    known_captions = {_norm(r.get("caption", ""))[:70] for r in db.get("posts", [])}
    db["live_captions"] = sorted({_norm(v.get("videoDescription", ""))[:70]
                                  for v in live if v.get("videoDescription")})
    db["unregistered_live_posts"] = [
        {"platform_post_id": str(v.get("videoId")), "caption": v.get("videoDescription", "")[:90],
         "at": v.get("createTime"), "views": v.get("viewCount"), "comments": v.get("commentCount")}
        for v in live
        if str(v.get("videoId")) not in known_ids and _norm(v.get("videoDescription", ""))[:70] not in known_captions
    ]
    return updated


def reconcile_assignments(db: dict, root: str) -> int:
    """
    Upgrade already-stored rows from "inferred" to "assigned" when an assignment
    matches them. Returns the number of rows upgraded.

    THE BUG THIS FIXES, and it is the reason the loop has never learned anything.
    ingest_unregistered() skips any post already in the database (it must — that is
    how it avoids duplicates), so the assignment lookup only ever runs at the moment
    a row is FIRST created. Every row created before assignments carried a
    caption_head was therefore frozen at "inferred" forever, and no amount of
    backfilling assignments.json could reach it. Measured on the box: 47 inferred
    rows, 17 of which had a matching assignment sitting right there unused, and 0
    arms had reached MIN_ARM_N, so adjust.py had never flipped a single default.

    WHY THIS DOES NOT LAUNDER OBSERVATIONS INTO EVIDENCE. The tempting version of
    this function merges the assignment's axis over the top of the inferred slot /
    density / type guesses and calls the whole row "assigned". That would let
    adjust.py flip `slot` on a slot value that was reverse-engineered from a
    timestamp, which is exactly the confusion the two provenance values exist to
    prevent. So a reconciled row keeps ONLY facts recorded at design or render
    time: the assignment's variant, plus the `rendered` block, which experiment.py
    writes from what it actually rendered. The inferred guesses are moved to
    `variant_inferred` — kept, visible, and out of the rollups.

    Only "inferred" and missing provenance are touched. A row already at "assigned"
    or "manual" is left exactly as it is.
    """
    apath = os.path.join(root, "learn", "assignments.json")
    if not os.path.exists(apath):
        return 0
    try:
        records = json.load(open(apath)).get("assignments") or []
    except Exception:
        return 0

    by_cap = {}
    for a in records:
        head = _norm(a.get("caption_head", ""))[:60]
        if head and a.get("variant"):
            by_cap.setdefault(head, a)     # first writer wins, matching ingest order

    # axes experiment.py records from what it really rendered, so they are as
    # authoritative as the axis under test. Anything not in here is a guess.
    RENDERED_OK = ("screen_style", "text_color", "slot")
    upgraded = 0
    for row in db.get("posts", []):
        if (row.get("variant_provenance") or "inferred") not in ("inferred", None, ""):
            continue
        a = by_cap.get(_norm(row.get("caption", ""))[:60])
        if not a:
            continue
        variant = dict(a.get("variant") or {})
        rendered = a.get("rendered") or {}
        for k in RENDERED_OK:
            if rendered.get(k) and k not in variant:
                variant[k] = rendered[k]
        if rendered.get("actual_type") and "type" not in variant:
            variant["type"] = rendered["actual_type"]
        if not variant:
            continue
        prior = row.get("variant") or {}
        if prior:
            row["variant_inferred"] = prior      # kept, but out of the rollups
        row["variant"] = variant
        row["variant_provenance"] = "assigned"
        row["reconciled_from"] = a.get("uid") or a.get("post_id")
        upgraded += 1
    return upgraded


def ingest_unregistered(db: dict, live: list[dict], root: str, as_of: str) -> int:
    """
    Add live posts the database has never seen, so the loop learns from its own work.

    THE BUG THIS FIXES: join_and_update only ever iterated db["posts"] — it refreshed
    records it already had and listed the rest under "unregistered_live_posts" as a
    diagnostic nobody read. The database sat at 7 posts while 16 were live, so every
    cycle recomputed the same seven and the loop's learning was frozen from the day
    it was armed.

    Dimensions come from ONE OF TWO SOURCES, and which one is recorded:
      "assigned"  — learn/assignments.json, written at design time by
                    post/experiment.py. This is a real experiment.
      "inferred"  — derived from the finished post. This is an observation, and
                    rollups should never treat it as evidence about a designed arm.
    Keeping them distinguishable is the whole point; collapsing them would let
    observational noise masquerade as experimental result.
    """
    known_ids = {str(r.get("platform_post_id")) for r in db.get("posts", [])
                 if r.get("platform_post_id")}
    known_caps = {_norm(r.get("caption", ""))[:70] for r in db.get("posts", [])}

    assigned = {}
    apath = os.path.join(root, "learn", "assignments.json")
    if os.path.exists(apath):
        try:
            for a in (json.load(open(apath)).get("assignments") or []):
                if a.get("post_id"):
                    assigned[str(a["post_id"])] = a
        except Exception:
            pass

    added = 0
    for v in live:
        vid = str(v.get("videoId") or "")
        cap = v.get("videoDescription", "") or ""
        if not vid or vid in known_ids or _norm(cap)[:70] in known_caps:
            continue

        created = v.get("createTime") or ""
        variant, provenance = {}, "inferred"
        # assignments are keyed by the METRICOOL post id, not the TikTok video id,
        # so match on the caption the assignment was scheduled with
        for a in assigned.values():
            if _norm(a.get("caption_head", ""))[:60] and \
               _norm(a.get("caption_head", ""))[:60] == _norm(cap)[:60]:
                variant, provenance = dict(a.get("variant") or {}), "assigned"
                break

        if provenance == "inferred":
            variant = {
                "slot": _infer_slot(created),
                "density": _infer_density(cap),
                "type": _infer_type(cap),
                "audio": "unknown",     # the API returns no sound metadata at all
            }

        db.setdefault("posts", []).append({
            "platform_post_id": vid,
            "caption": cap,
            "posted_at": created,
            "url": v.get("shareUrl"),
            "match": "id",
            "variant": variant,
            "variant_provenance": provenance,
            "metrics": {"views": v.get("viewCount"), "likes": v.get("likeCount"),
                        "comments": v.get("commentCount"), "shares": v.get("shareCount"),
                        "source": "api", "as_of": as_of},
        })
        known_ids.add(vid)
        added += 1
    return added


def _infer_slot(created: str) -> str:
    """Nearest named slot to the publish hour. An observation, not an assignment."""
    if len(created) < 16:
        return "unknown"
    try:
        mins = int(created[11:13]) * 60 + int(created[14:16])
    except ValueError:
        return "unknown"
    best, gap = "unknown", 10 ** 9
    for name, (h1, m1, _h2, _m2) in _SLOT_WINDOWS.items():
        d = abs(mins - (h1 * 60 + m1))
        if d < gap:
            best, gap = name, d
    return best if gap <= 120 else "unknown"


def _infer_type(caption: str) -> str:
    c = (caption or "").lower()
    if "unfortunately i do love" in c or "you know you're a mom" in c:
        return "joke"
    if "?" in c or c.startswith(("what", "how", "tell me", "do you", "does", "has any")):
        return "ask"
    if "\u2022" in caption or "\n1. " in caption:
        return "value"
    return "other"


_SLOT_WINDOWS = {
    "morning": (6, 15, 7, 10), "midmorning": (9, 35, 10, 20),
    "lunch": (12, 10, 12, 50), "pickup": (14, 45, 15, 25),
    "dinner": (17, 15, 18, 5), "evening": (20, 5, 20, 50),
    "late-night": (22, 15, 22, 55),
}


# ---------------------------------------------------------------------------
# recompute learnings.json
# ---------------------------------------------------------------------------
def settled_dimensions(decisions_log: list[dict]) -> set[str]:
    return {d.get("dimension") for d in decisions_log
            if d.get("action") == "drop_dimension" and d.get("status") == "applied" and d.get("dimension")}


def recompute_learnings(db: dict, prev: dict, defaults: dict, persona_name: str, pull_note: str,
                         scoring_entry: dict | None,
                         universe: dict[str, tuple[str, ...]] | None = None) -> dict:
    # This persona's arm catalog. Defaults to DIMENSIONS (Dani's) so existing
    # callers are unchanged; pass dimensions_for(persona) to filter rollup cells
    # against the axes that persona actually posts on.
    universe = universe or DIMENSIONS
    posts = db.get("posts", [])
    n_total = len(posts)
    n_with_metrics = sum(1 for p in posts if has_metrics(p))
    rollups = compute_rollups(posts)

    prev_fr = (prev.get("front_runners") or {})
    dropped = settled_dimensions(prev.get("decisions_log", []))
    front_runners: dict[str, str | None] = {}
    single_arm_dimensions: list[str] = []
    decisions_log = list(prev.get("decisions_log", []))

    dim_to_rollup_key = {
        "type": "by_type", "slot": "by_slot", "audio": "by_audio", "setting": "by_setting",
        "density": "by_density", "first_comment": "by_first_comment", "text_color": "by_text_color",
    }
    for dim, key in dim_to_rollup_key.items():
        cells = rollups[key]
        # Some legacy records carry "unknown" for a dimension nobody recorded
        # at post time (see migrate_from_legacy). That is a data-collection
        # gap, not an A/B arm — it must never be named a front-runner or
        # counted toward "this dimension has >1 arm represented", or the loop
        # would end up recommending "unknown" as if it were a real setting.
        canonical_cells = {a: c for a, c in cells.items() if a in universe.get(dim, ())}
        arms_represented = [a for a, c in canonical_cells.items() if c["n_posts"] > 0]
        if len(arms_represented) <= 1:
            single_arm_dimensions.append(dim)
        if dim in dropped:
            front_runners[dim] = None
            continue
        metric = "median_primary" if dim == "type" else "median_likes"
        best, reason = pick_front_runner(canonical_cells, metric=metric)
        front_runners[dim] = best
        if best and best != prev_fr.get(dim):
            decisions_log.append({
                "date": scoring_entry["date"] if scoring_entry else _now_iso()[:10],
                "decision": f"Front-runner {dim} -> {best} ({reason}).",
                "rationale": "Recomputed by score.py from Metricool analytics. "
                             f"NOT actionable below min_n={MIN_N} — see status.",
                "dimension": dim,
                "action": "front_runner",
                "status": "auto",
            })

    status = (
        f"PROVISIONAL — {n_total} posts tracked, {n_with_metrics} with mature "
        f"metrics. min_n={MIN_N} required before ANY dimension names a usable "
        f"front-runner; {len(single_arm_dimensions)}/7 dimensions "
        f"({', '.join(sorted(single_arm_dimensions)) or 'none'}) currently have "
        f"only ONE arm represented in the data at all, so nothing about them "
        f"can be concluded yet, favorable or not. Nothing here is "
        f"statistically meaningful with this sample size."
    )

    learnings = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "persona": persona_name,
        "description": (
            "The decisions brain: rollups + front_runners + an append-only "
            "decisions_log, distilled from ab-database.json. ALWAYS "
            "recomputed from ab-database.json's posts[] — never hand-edited."
        ),
        "status": status,
        "conventions": {
            "primary_metric_by_type": dict(PRIMARY_METRIC),
            "secondary_metric": SECONDARY_METRIC,
            "aggregation": "median, not mean; require n_with_metrics >= min_n before acting",
            "min_n": MIN_N,
        },
        "current_defaults": dict(defaults),
        "rollups": rollups,
        "single_arm_dimensions": sorted(single_arm_dimensions),
        "front_runners": {
            "as_of": (scoring_entry["date"] if scoring_entry else _now_iso()[:10]),
            **front_runners,
            "confidence": "medium" if n_with_metrics >= MIN_N else "low",
            "notes": (
                f"Only dimensions with >= {MIN_N} posts-with-metrics in ANY "
                "single arm can name a front-runner at all; with "
                f"{n_with_metrics} total mature posts across 7 dimensions, "
                "expect most of these to be null. A non-null front-runner "
                "here is a lead worth testing further, not a conclusion."
            ),
        },
        "decisions_log": decisions_log,
        "scoring_log": (prev.get("scoring_log", []) + [scoring_entry])[-60:] if scoring_entry else prev.get("scoring_log", []),
    }
    return learnings


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def print_summary(learnings: dict) -> None:
    print(learnings["status"])
    print()
    for dim, key in (("type", "by_type"), ("slot", "by_slot"), ("audio", "by_audio"),
                      ("setting", "by_setting"), ("density", "by_density"),
                      ("first_comment", "by_first_comment"), ("text_color", "by_text_color")):
        cells = learnings["rollups"][key]
        if not cells:
            print(f"  {dim:<14} (no data)")
            continue
        print(f"  {dim}:")
        for arm, c in sorted(cells.items()):
            flag = "" if c["meaningful"] else "  [n<min_n, not meaningful]"
            extra = f" primary({c.get('primary_metric')})={c.get('median_primary')}" if c.get("primary_metric") else ""
            print(f"    {arm:<14} n={c['n_posts']} (with metrics={c['n_with_metrics']})"
                  f" median_likes={c['median_likes']} median_comments={c['median_comments']}"
                  f"{extra}{flag}")
    print()
    fr = learnings["front_runners"]
    print(f"front_runners (as_of {fr['as_of']}, confidence={fr['confidence']}):")
    for dim in ("type", "slot", "audio", "setting", "density", "first_comment", "text_color"):
        print(f"    {dim:<14} {fr.get(dim)}")
    print(f"\nsingle-arm dimensions (no comparison possible yet): {learnings['single_arm_dimensions']}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def run(persona_name: str, do_pull: bool = True) -> dict:
    persona = personas.resolve(persona_name)
    paths = data_paths(persona)
    db = ensure_data_files(paths, persona["name"], persona)
    defaults_doc = _read_json(paths["defaults"],
                              {"defaults": fallback_defaults_for(persona)})
    prev_learnings = _read_json(paths["learnings"], {})

    scoring_entry = None
    pull_note = "skipped (--no-pull)"
    if do_pull:
        to = datetime.date.today().isoformat()
        frm = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        live = pull_live(persona)
        updated = join_and_update(db, live, as_of=to)
        # ingest anything live we have never tracked, or the loop only ever
        # re-scores the posts it started with (it sat at 7 while 16 were live)
        added = ingest_unregistered(db, live, persona['root'], to)
        if added:
            print(f'  ingested {added} previously untracked live post(s)')
        # rows created before assignments carried a caption_head are stuck at
        # "inferred"; ingest_unregistered can never revisit them. See the docstring.
        fixed = reconcile_assignments(db, persona['root'])
        if fixed:
            print(f'  reconciled {fixed} row(s) inferred -> assigned')
        # Instagram, where delivery is 100% rather than TikTok's measured 42%.
        # Additive and non-fatal: a reels outage must not stop TikTok scoring.
        try:
            ig_added, ig_refreshed = ingest_instagram(
                db, pull_ig_reels(persona), persona['root'], to)
            ig_n = len(db.get('ig_posts', []))
            ig_assigned = sum(1 for r in db.get('ig_posts', [])
                              if r.get('variant_provenance') == 'assigned')
            print(f'  instagram: +{ig_added} new, {ig_refreshed} refreshed, '
                  f'{ig_n} tracked, {ig_assigned} assigned')
        except Exception as exc:
            print(f'  instagram ingest skipped: {type(exc).__name__}: {exc}', file=sys.stderr)
        n_with_metrics = sum(1 for p in db["posts"] if has_metrics(p))
        pull_note = "no matured metrics yet" if updated == 0 else "metrics refreshed"
        scoring_entry = {"date": to, "from": frm, "to": to, "pulled": len(live),
                          "updated": updated, "n_with_metrics": n_with_metrics}
        db["updated_at"] = _now_iso()
        _write_json(paths["ab_db"], db)

    learnings = recompute_learnings(db, prev_learnings, defaults_doc["defaults"], persona["name"],
                                     pull_note, scoring_entry, dimensions_for(persona))
    _write_json(paths["learnings"], learnings)
    return learnings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--no-pull", action="store_true", help="recompute learnings.json offline, no Metricool call")
    args = ap.parse_args(argv)

    learnings = run(args.persona, do_pull=not args.no_pull)
    print_summary(learnings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
