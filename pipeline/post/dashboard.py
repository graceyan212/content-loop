#!/usr/bin/env python3
"""
dashboard.py — one page showing what is actually working.

    python post/dashboard.py --persona dani

Reads live Metricool analytics plus learn/ab-database.json and learn/assignments.json.
Read-only: it never schedules, deletes or edits anything.

THREE THINGS IT REFUSES TO DO, because each one would flatter the numbers:

  1. It never ranks by raw counts. Views are algorithm-allocated and heavy-tailed,
     so a post with 1,094 views and 8 comments is WORSE per-viewer than one with
     469 views and 17. Everything is per-1000-views.

  2. It labels every dimension cell as ASSIGNED or OBSERVED. An assigned arm came
     from post/experiment.py before the post existed — that is an experiment. An
     observed one was inferred from the finished post — that is a description, and
     it carries the scheduler's own selection bias (slots were chosen by which
     slots the library had copy for). Collapsing the two would let that bias read
     as a finding.

  3. It states the sample size needed and shows how far off it is. Detecting a 50%
     difference at this engagement rate needs roughly 20-30 posts per arm. Nothing
     here is significant yet and the page says so at the top rather than in a
     footnote.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import html
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "copy"), os.path.join(ROOT, "learn")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas          # noqa: E402
import metricool as mc   # noqa: E402

OUT = "/tmp/dani-dashboard.html"
NEEDED_PER_ARM = 25       # to detect a 50% lift; see module docstring


def per_k(n, views):
    return (n / views * 1000) if views else 0.0


def fetch(persona) -> tuple[list[dict], dict, dict]:
    creds = mc.load_creds()
    creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(persona))
    d0 = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    st, body = mc._req("GET", "/v2/analytics/posts/tiktok", creds,
                       params={"from": d0, "to": d1, "timezone": "America/Chicago"})
    live = [x for x in (body if isinstance(body, list) else (body or {}).get("data") or [])
            if isinstance(x, dict)]

    root = persona["root"]
    def rd(name, key):
        f = os.path.join(root, "learn", name)
        try:
            return {str(a[key]): a for a in json.load(open(f)).get(name.split(".")[0].replace("ab-database", "posts"), [])}
        except Exception:
            return {}
    db, asn = {}, {}
    try:
        for r in json.load(open(os.path.join(root, "learn", "ab-database.json"))).get("posts", []):
            if r.get("platform_post_id"):
                db[str(r["platform_post_id"])] = r
    except Exception:
        pass
    try:
        for a in json.load(open(os.path.join(root, "learn", "assignments.json"))).get("assignments", []):
            if a.get("caption_head"):
                asn[a["caption_head"][:60]] = a
    except Exception:
        pass
    return live, db, asn


def rows(live, db, asn):
    out = []
    for v in live:
        vid = str(v.get("videoId") or "")
        rec = db.get(vid, {})
        variant = dict(rec.get("variant") or {})
        prov = rec.get("variant_provenance") or ("assigned" if variant else "unknown")
        cap = v.get("videoDescription", "") or ""
        for head, a in asn.items():
            if cap[:60].strip() and cap[:60].strip() == head.strip():
                variant.update(a.get("variant") or {})
                prov = "assigned"
                break
        views = v.get("viewCount") or 0
        out.append({
            "id": vid, "at": v.get("createTime", "")[:16], "caption": cap,
            "views": views, "likes": v.get("likeCount") or 0,
            "comments": v.get("commentCount") or 0, "shares": v.get("shareCount") or 0,
            "cpk": per_k(v.get("commentCount") or 0, views),
            "lpk": per_k(v.get("likeCount") or 0, views),
            "variant": variant, "prov": prov, "url": v.get("shareUrl"),
        })
    out.sort(key=lambda r: r["cpk"], reverse=True)
    return out


def dimension_table(rs, dim):
    by = collections.defaultdict(list)
    for r in rs:
        arm = (r["variant"] or {}).get(dim)
        if arm:
            by[(arm, r["prov"])].append(r)
    cells = []
    for (arm, prov), group in sorted(by.items(), key=lambda kv: -len(kv[1])):
        cells.append({
            "arm": arm, "prov": prov, "n": len(group),
            "cpk": statistics.median(g["cpk"] for g in group),
            "lpk": statistics.median(g["lpk"] for g in group),
            "views": statistics.median(g["views"] for g in group),
            "short": NEEDED_PER_ARM - len(group),
        })
    return cells


CSS = """
:root{--line:#e5e7eb;--mut:#6b7280;--ink:#111}
*{box-sizing:border-box}
body{margin:0;background:#fafafa;color:var(--ink);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
header{padding:22px 30px;background:#fff;border-bottom:1px solid var(--line)}
h1{margin:0 0 4px;font-size:20px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);
 margin:34px 0 12px;font-weight:600}
.warn{background:#fef3c7;border:1px solid #fcd34d;color:#78350f;padding:11px 14px;
 border-radius:9px;font-size:13px;margin-top:12px}
.wrap{max-width:1160px;margin:0 auto;padding:8px 30px 90px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
 border-radius:10px;overflow:hidden;font-size:13.5px}
th{text-align:left;padding:9px 12px;background:#f9fafb;border-bottom:1px solid var(--line);
 font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
td{padding:9px 12px;border-bottom:1px solid #f3f4f6;vertical-align:top}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.big{font-weight:650}
.tag{font-size:10px;padding:1px 6px;border-radius:5px;text-transform:uppercase;letter-spacing:.04em}
.assigned{background:#dcfce7;color:#166534}
.observed{background:#fef3c7;color:#92400e}
.unknown{background:#f3f4f6;color:#6b7280}
.thin{color:#b91c1c;font-size:11px}
.cap{color:#374151;max-width:430px}
.bar{display:inline-block;height:7px;background:#2563eb;border-radius:4px;vertical-align:middle}
a{color:#1d4ed8;text-decoration:none}
"""


def link(url: str | None) -> str:
    """Kept out of the f-string: a backslash-escaped quote inside one is a SyntaxError."""
    return f'<a href="{html.escape(url)}" target="_blank">open</a>' if url else ""


def build(persona_name):
    p = personas.resolve(persona_name)
    live, db, asn = fetch(p)
    rs = rows(live, db, asn)
    tot_v = sum(r["views"] for r in rs)
    tot_c = sum(r["comments"] for r in rs)

    top = rs[:8]
    maxcpk = max((r["cpk"] for r in rs), default=1) or 1
    top_html = "".join(
        f"<tr><td class='num'>{r['at'][5:]}</td>"
        f"<td class='cap'>{html.escape(r['caption'][:96])}</td>"
        f"<td class='num'>{r['views']}</td><td class='num'>{r['comments']}</td>"
        f"<td class='num big'>{r['cpk']:.1f}</td>"
        f"<td><span class='bar' style='width:{r['cpk']/maxcpk*90:.0f}px'></span></td>"
        f"<td>{link(r['url'])}</td></tr>"
        for r in top)

    dims = ""
    for dim in ("type", "list_style", "slot", "density", "audio", "setting"):
        cells = dimension_table(rs, dim)
        if not cells:
            continue
        body = "".join(
            f"<tr><td class='big'>{html.escape(str(c['arm']))}</td>"
            f"<td><span class='tag {c['prov']}'>{c['prov']}</span></td>"
            f"<td class='num'>{c['n']}</td>"
            f"<td class='num big'>{c['cpk']:.1f}</td><td class='num'>{c['lpk']:.1f}</td>"
            f"<td class='num'>{c['views']:.0f}</td>"
            f"<td class='thin'>{'need '+str(c['short'])+' more' if c['short']>0 else 'sufficient'}</td></tr>"
            for c in cells)
        dims += (f"<h2>{dim}</h2><table><tr><th>arm</th><th>provenance</th><th class='num'>n</th>"
                 f"<th class='num'>comments/1k</th><th class='num'>likes/1k</th>"
                 f"<th class='num'>median views</th><th>sample</th></tr>{body}</table>")

    doc = f"""<!doctype html><meta charset="utf-8"><title>Dani — performance</title>
<style>{CSS}</style>
<header>
  <h1>Dani — performance</h1>
  <div style="color:var(--mut);font-size:13px">
    {len(rs)} published posts · {tot_v:,} views · {tot_c} comments ·
    {per_k(tot_c, tot_v):.1f} comments per 1,000 views
  </div>
  <div class="warn"><b>Nothing here is statistically significant.</b>
    Detecting a 50% difference between two arms needs roughly {NEEDED_PER_ARM} posts each;
    no arm is close. Ranking is by <b>comments per 1,000 views</b>, never raw counts —
    views are allocated by the algorithm, so a post with 1,094 views and 8 comments is
    doing worse per viewer than one with 469 and 17.
    <b>assigned</b> cells came from a designed experiment.
    <b>observed</b> cells were inferred after the fact and carry the scheduler's own
    selection bias, so they describe rather than prove.</div>
</header>
<div class="wrap">
  <h2>best posts, by comments per 1,000 views</h2>
  <table><tr><th class='num'>date</th><th>caption</th><th class='num'>views</th>
  <th class='num'>cmts</th><th class='num'>per 1k</th><th></th><th></th></tr>{top_html}</table>
  {dims}
</div>"""
    open(OUT, "w").write(doc)
    return OUT, rs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)
    path, rs = build(a.persona)
    print(f"wrote {path} ({len(rs)} posts)")
    for r in rs[:5]:
        print(f"  {r['cpk']:>5.1f} c/1k  {r['views']:>5}v {r['comments']:>3}c  "
              f"{r['caption'][:56]!r}")
    if not a.no_open:
        subprocess.run(["open", "-a", "Brave Browser", path], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
