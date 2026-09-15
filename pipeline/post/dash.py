#!/usr/bin/env python3
"""
dash.py — Dani's loop dashboard, in the shape of Hermes's.

    python post/dash.py --persona dani              # write + open locally
    python post/dash.py --persona dani --serve      # serve on :8790

Same skeleton as hermes/src/dashboard.ts: KPI tiles, then pinned cards —
RUN / A/B / DATA / LEARN / LOG / SCHEDULED. Styling is the SFFS neo-brutalist
system from GTM/brand-assets/tokens.css (thick ink borders, zero-blur hard
shadows, cream ground, the four brand accents), so this reads as the same family
of tool rather than a second unrelated page.

Read-only. It reads live Metricool analytics plus learn/*.json and never writes,
schedules or deletes anything.

WHAT IT DOES DIFFERENTLY FROM HERMES, deliberately:

  - Ranks by comments per 1,000 views, never raw counts. Views are algorithm-
    allocated: a post with 1,094 views and 8 comments is doing worse per viewer
    than one with 469 and 17. Hermes's "top arms by views" table would rank those
    the other way round.

  - Labels every arm ASSIGNED or OBSERVED. Assigned came from a designed
    experiment; observed was inferred after the fact and carries the scheduler's
    own selection bias. Hermes has a `match_confidence` field for this that no
    code path ever sets to anything but "high", so the distinction is decorative
    there. Here it changes what the loop is allowed to act on.

  - States the sample size needed and how far short each arm is, in the header
    rather than a footnote. Hermes's own dashboard showed a hook experiment at
    10/15 reels and a 7-day mandate at 0.95% of target; the numbers were there but
    the framing did not lead with "this is not conclusive".
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
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "copy"), os.path.join(ROOT, "learn")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas          # noqa: E402
import metricool as mc   # noqa: E402

OUT = "/tmp/dani-dash.html"
NEEDED_PER_ARM = 25

# tokens.css, verbatim
CSS = """
:root{--ink:#000;--paper:#fff;--cream:#f6f4ee;--blue:#839aff;--mint:#c6fcd0;
 --coral:#fd7962;--yellow:#fce552;--green:#63c088;--mut:#555}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--cream);color:var(--ink);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
header{background:var(--blue);border-bottom:5px solid var(--ink);padding:18px 22px;
 display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between}
header h1{margin:0;font:800 24px/1 "Segoe UI",sans-serif;letter-spacing:.5px}
.tag{background:var(--ink);color:var(--yellow);padding:4px 10px;border-radius:6px;
 font-weight:800;font-size:12px;letter-spacing:1px}
.tag.ro{background:#0d0d0d;color:var(--mint)}
.wrap{max-width:1120px;margin:0 auto;padding:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.kpi{background:var(--paper);border:4px solid var(--ink);border-radius:16px;
 box-shadow:8px 8px 0 0 var(--ink);padding:16px}
.kpi .v{font:800 34px/1 "Segoe UI",sans-serif}
.kpi .k{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#444;margin-top:6px}
.statgroup{margin-bottom:20px}
.statlabel{font:800 12px/1 "Segoe UI",sans-serif;text-transform:uppercase;
 letter-spacing:1.5px;background:var(--yellow);border:3px solid var(--ink);
 border-radius:9px;display:inline-block;padding:6px 11px;margin-bottom:12px;
 box-shadow:3px 3px 0 0 var(--ink)}
.card{background:var(--paper);border:4px solid var(--ink);border-radius:18px;
 box-shadow:8px 8px 0 0 var(--ink);padding:20px;margin-bottom:22px}
.card h2{margin:0 0 14px;font-size:20px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.card h2 .pin{background:var(--coral);border:3px solid var(--ink);border-radius:8px;
 padding:2px 8px;font-size:12px;font-weight:800}
.card h2 .pin.pr{background:var(--yellow)}
.card h2 .pin.ok{background:var(--mint)}
.note{background:var(--yellow);border:3px solid var(--ink);border-radius:10px;
 padding:11px 13px;font-size:13px;font-weight:600;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;padding:7px 9px;border-bottom:3px solid var(--ink);
 font-size:11px;text-transform:uppercase;letter-spacing:.06em}
td{padding:7px 9px;border-bottom:1px solid #e8e6de;vertical-align:top}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.big{font-weight:800}
.chip{font-size:10px;padding:2px 7px;border-radius:6px;border:2px solid var(--ink);
 font-weight:800;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.assigned{background:var(--mint)}
.observed{background:var(--yellow)}
.unknown{background:#eee;color:#666}
.thin{color:#a11;font-size:11px;font-weight:700}
.cap{max-width:420px;color:#333}
.bar{display:inline-block;height:12px;background:var(--coral);
 border:2px solid var(--ink);border-radius:3px;vertical-align:middle}
a{color:#1b3bd1}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
"""


def per_k(n, v):
    return (n / v * 1000) if v else 0.0


def gather(persona):
    root = persona["root"]
    creds = mc.load_creds()
    creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(persona))
    d0 = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")

    _, an = mc._req("GET", "/v2/analytics/posts/tiktok", creds,
                    params={"from": d0, "to": d1, "timezone": "America/Chicago"})
    live = [x for x in (an if isinstance(an, list) else (an or {}).get("data") or [])
            if isinstance(x, dict)]
    _, sc = mc._req("GET", "/v2/scheduler/posts", creds, params={"start": d0, "end": d1})
    sched = [x for x in (sc if isinstance(sc, list) else (sc or {}).get("data") or [])
             if isinstance(x, dict)]

    def rj(name, default=None):
        try:
            return json.load(open(os.path.join(root, "learn", name)))
        except Exception:
            return default if default is not None else {}

    db = rj("ab-database.json", {"posts": []})
    return {
        "live": live, "sched": sched, "db": db,
        "learn": rj("learnings.json"), "defaults": rj("defaults.json"),
        "assign": rj("assignments.json", {"assignments": []}),
        "props": rj("proposals.json", {"proposals": [], "decisions_log": []}),
        "runs_dir": os.path.join(root, "learn", "runs"),
    }


def rows(d):
    byid = {str(r.get("platform_post_id")): r for r in d["db"].get("posts", [])
            if r.get("platform_post_id")}
    heads = {(a.get("caption_head") or "")[:60].strip(): a
             for a in d["assign"].get("assignments", []) if a.get("caption_head")}
    out = []
    for v in d["live"]:
        rec = byid.get(str(v.get("videoId")), {})
        variant = dict(rec.get("variant") or {})
        prov = rec.get("variant_provenance") or ("assigned" if variant else "unknown")
        cap = v.get("videoDescription") or ""
        a = heads.get(cap[:60].strip())
        if a:
            variant.update(a.get("variant") or {})
            prov = "assigned"
        views = v.get("viewCount") or 0
        out.append({"at": (v.get("createTime") or "")[:16], "cap": cap, "views": views,
                    "likes": v.get("likeCount") or 0, "cmts": v.get("commentCount") or 0,
                    "shares": v.get("shareCount") or 0, "url": v.get("shareUrl"),
                    "cpk": per_k(v.get("commentCount") or 0, views),
                    "lpk": per_k(v.get("likeCount") or 0, views),
                    "spk": per_k(v.get("shareCount") or 0, views),
                    "ppl": (v.get("commentCount") or 0) / max(v.get("likeCount") or 0, 1),
                    "variant": variant, "prov": prov})
    out.sort(key=lambda r: r["cpk"], reverse=True)
    return out


def kpi(v, k):
    return f'<div class="kpi"><div class="v">{v}</div><div class="k">{html.escape(k)}</div></div>'


def card(pin, title, body, cls=""):
    return (f'<div class="card"><h2><span class="pin {cls}">{pin}</span> {title}</h2>'
            f'{body}</div>')


def build(persona_name: str) -> str:
    p = personas.resolve(persona_name)
    d = gather(p)
    rs = rows(d)
    tv = sum(r["views"] for r in rs)
    tc = sum(r["cmts"] for r in rs)
    pending = [x for x in d["sched"]
               if any((q.get("status") or "").upper() == "PENDING"
                      for q in (x.get("providers") or []))]

    # ---- RUN ----------------------------------------------------------------
    runs = []
    try:
        runs = sorted(f for f in os.listdir(d["runs_dir"]) if f.endswith(".json"))
    except Exception:
        pass
    last = {}
    if runs:
        try:
            last = json.load(open(os.path.join(d["runs_dir"], runs[-1])))
        except Exception:
            pass
    ph = last.get("phases") or {}
    run_body = (
        f'<div class="grid statgroup">'
        f'{kpi(len(runs), "cycles on record")}'
        f'{kpi(last.get("run_id", "—"), "latest run")}'
        f'{kpi(sum(1 for v in ph.values() if v.get("ok")), "phases ok")}'
        f'{kpi(sum(1 for v in ph.values() if not v.get("ok")), "phases failed")}'
        f'</div><table><tr><th>phase</th><th>at</th><th>ok</th><th>detail</th></tr>' +
        "".join(f'<tr><td class="big">{html.escape(k)}</td>'
                f'<td class="mono">{html.escape(str(v.get("at",""))[11:19])}</td>'
                f'<td>{"✓" if v.get("ok") else "✗"}</td>'
                f'<td class="mono">{html.escape(str({x: y for x, y in v.items() if x not in ("at","ok")})[:110])}</td></tr>'
                for k, v in ph.items()) + "</table>")

    # ---- A/B ---------------------------------------------------------------
    asn = d["assign"].get("assignments", [])
    ab_body = (f'<div class="note">Every row here was assigned an arm BEFORE the post '
               f'existed. That is what makes it an experiment rather than a description.</div>'
               f'<table><tr><th>dimension</th><th>arm</th><th>scheduled</th><th>post</th></tr>' +
               ("".join(f'<tr><td class="big">{html.escape(a.get("dimension_under_test",""))}</td>'
                        f'<td><span class="chip assigned">{html.escape(str(a.get("arm")))}</span></td>'
                        f'<td class="mono">{html.escape(str(a.get("scheduled_for",""))[:16])}</td>'
                        f'<td class="mono">{html.escape(str(a.get("uid","")))}</td></tr>'
                        for a in sorted(asn, key=lambda x: x.get("scheduled_for") or ""))
                or '<tr><td colspan="4">no assignments yet</td></tr>') + "</table>")

    # ---- DATA --------------------------------------------------------------
    mx = max((r["cpk"] for r in rs), default=1) or 1
    data_body = ('<table><tr><th>when</th><th>caption</th><th class="num">views</th>'
                 '<th class="num">cmts</th><th class="num">c/1k</th><th class="num">sh/1k</th><th></th><th></th></tr>' +
                 "".join(f'<tr><td class="mono">{html.escape(r["at"][5:])}</td>'
                         f'<td class="cap">{html.escape(r["cap"][:88])}</td>'
                         f'<td class="num">{r["views"]}</td><td class="num">{r["cmts"]}</td>'
                         f'<td class="num big">{r["cpk"]:.1f}</td>'
                         f'<td class="num">{r["spk"]:.2f}</td>'
                         f'<td><span class="bar" style="width:{r["cpk"]/mx*80:.0f}px"></span></td>'
                         f'<td>{link(r["url"])}</td></tr>' for r in rs) + "</table>")

    # ---- LEARN -------------------------------------------------------------
    learn_body = ""
    for dim in ("type", "cta", "register", "framing", "community_move", "list_style", "screen_style", "slot",
                "density", "audio", "text_color", "setting"):
        by = collections.defaultdict(list)
        for r in rs:
            arm = (r["variant"] or {}).get(dim)
            if arm and arm != "unknown":
                by[(arm, r["prov"])].append(r)
        if not by:
            continue
        learn_body += f'<div class="statlabel">{dim}</div><table>' \
                      '<tr><th>arm</th><th>provenance</th><th class="num">n</th>' \
                      '<th class="num">c/1k</th><th class="num">l/1k</th><th>sample</th></tr>'
        for (arm, prov), g in sorted(by.items(), key=lambda kv: -len(kv[1])):
            short = NEEDED_PER_ARM - len(g)
            learn_body += (
                f'<tr><td class="big">{html.escape(str(arm))}</td>'
                f'<td><span class="chip {prov}">{prov}</span></td>'
                f'<td class="num">{len(g)}</td>'
                f'<td class="num big">{statistics.median(x["cpk"] for x in g):.1f}</td>'
                f'<td class="num">{statistics.median(x["lpk"] for x in g):.1f}</td>'
                f'<td class="num">{statistics.median(x["spk"] for x in g):.2f}</td>'
                f'<td class="num big">{statistics.median(x["ppl"] for x in g):.2f}</td>'
                f'<td class="thin">{"need "+str(short)+" more" if short > 0 else "sufficient"}</td></tr>')
        learn_body += "</table>"

    # ---- LOG ---------------------------------------------------------------
    log = (d["props"].get("decisions_log") or []) + (d["learn"].get("decisions_log") or [])
    log_body = ('<table><tr><th>date</th><th>what</th></tr>' +
                ("".join(f'<tr><td class="mono">{html.escape(str(e.get("date","")))}</td>'
                         f'<td>{html.escape(str(e.get("decision") or e.get("rationale") or e)[:190])}</td></tr>'
                         for e in log[-14:]) or '<tr><td colspan="2">empty</td></tr>') + "</table>")

    # ---- SCHEDULED ---------------------------------------------------------
    per_day = collections.Counter((x.get("publicationDate") or {}).get("dateTime", "")[:10]
                                  for x in pending)
    sched_body = ('<table><tr><th>when</th><th>caption</th><th>status</th></tr>' +
                  "".join(f'<tr><td class="mono">{html.escape(((x.get("publicationDate") or {}).get("dateTime") or "")[:16].replace("T"," "))}</td>'
                          f'<td class="cap">{html.escape((x.get("text") or "").split(chr(10))[0][:82])}</td>'
                          f'<td><span class="chip observed">pending</span></td></tr>'
                          for x in sorted(pending, key=lambda y: (y.get("publicationDate") or {}).get("dateTime") or "")) +
                  "</table>")

    defaults = (d["defaults"] or {}).get("defaults", {})
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dani — Self-Improving Loop</title><style>{CSS}</style></head><body>
<header>
  <h1>Dani — Self-Improving Loop</h1>
  <div><span class="tag">t4g.small · daily 08:24 CT</span>
       <span class="tag ro">READ ONLY</span></div>
</header>
<div class="wrap">
  <div class="note">
    <b>Nothing here is statistically significant.</b> Detecting a 50% difference
    between two arms needs about {NEEDED_PER_ARM} posts each and no arm is close.
    Ranked by <b>comments per 1,000 views</b>, never raw counts — views are
    algorithm-allocated, so 1,094 views with 8 comments is worse per viewer than
    469 with 17. <b>assigned</b> arms came from a designed experiment;
    <b>observed</b> arms were inferred afterwards and carry the scheduler's own
    selection bias, so the loop may suggest from them but never flip a default on them.
  </div>

  <div class="statgroup"><div class="statlabel">live totals</div>
  <div class="grid">
    {kpi(len(rs), "published")}
    {kpi(f"{tv:,}", "views")}
    {kpi(tc, "comments")}
    {kpi(f"{per_k(tc, tv):.1f}", "comments / 1k views")}
    {kpi(len(pending), "scheduled ahead")}
  </div></div>

  <div class="statgroup"><div class="statlabel">current defaults
    <span style="font-weight:600;letter-spacing:.3px;text-transform:none">
      · what the loop posts unless an experiment says otherwise</span></div>
  <div class="grid">
    {"".join(kpi(html.escape(str(v)), k) for k, v in defaults.items())}
  </div></div>

  {card("RUN", "Latest cycle", run_body, "ok")}
  {card("A/B", "Designed arms &amp; assignments", ab_body)}
  {card("DATA", "Analytics per post", data_body)}
  {card("LEARN", "Rollups by dimension", learn_body, "pr")}
  {card("LOG", "Decisions", log_body, "pr")}
  {card("PLAN", f"Scheduled ahead — {len(pending)} post(s), {len(per_day)} day(s)", sched_body)}
</div></body></html>"""


def link(url):
    return f'<a href="{html.escape(url)}" target="_blank">open</a>' if url else ""


class Handler(BaseHTTPRequestHandler):
    persona = "dani"

    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            page = build(self.persona).encode()
            code = 200
        except Exception as e:
            page = f"<pre>dashboard error: {html.escape(str(e))}</pre>".encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)

    if a.serve:
        Handler.persona = a.persona
        srv = None
        for port in range(a.port, a.port + 12):
            try:
                srv = HTTPServer(("0.0.0.0", port), Handler)
                break
            except OSError:
                continue
        if srv is None:
            raise SystemExit("no free port")
        print(f"serving on :{port} (rebuilt live on every request)")
        srv.serve_forever()
        return 0

    open(OUT, "w").write(build(a.persona))
    print(f"wrote {OUT}")
    if not a.no_open:
        subprocess.run(["open", "-a", "Brave Browser", OUT], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
