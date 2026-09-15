#!/usr/bin/env python3
"""
review.py — show what is actually scheduled, pulled live from Metricool.

Not what the planner intended: what is sitting in the calendar right now, with the
real uploaded image, the real caption, the real time. Renders each frame with
TikTok's UI regions overlaid so occluded text is obvious rather than theoretical.

    python post/review.py                 # text listing
    python post/review.py --html          # review page, opens in the browser
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import subprocess
import sys
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "render")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas          # noqa: E402
import metricool as mc   # noqa: E402
import overlay as ov     # noqa: E402  (for UI_SAFE, so the page and renderer agree)

OUT = "/tmp/dani-scheduled-review.html"


def fetch(persona_name: str | None) -> tuple[list[dict], str]:
    p = personas.resolve(persona_name)
    creds = mc.load_creds()
    blog = personas.blog_id(p)
    if not blog:
        raise SystemExit(f"persona {p['name']!r} has no Metricool blog_id")
    creds["METRICOOL_BLOG_ID"] = str(blog)
    tz = personas.timezone(p)

    d0 = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
    for params in ({"start": d0, "end": d1}, {"from": d0, "to": d1}):
        st, body = mc._req("GET", "/v2/scheduler/posts", creds, params=params)
        if st != 200:
            continue
        items = [x for x in (body if isinstance(body, list) else (body.get("data") or []))
                 if isinstance(x, dict)]
        items.sort(key=lambda x: (x.get("publicationDate") or {}).get("dateTime") or "")
        return items, tz
    raise SystemExit("could not list scheduled posts")


def text_report(items: list[dict], tz: str) -> None:
    print(f"{len(items)} post(s) in the calendar  (times {tz})\n")
    for i, it in enumerate(items, 1):
        pub = (it.get("publicationDate") or {}).get("dateTime") or "?"
        nets = ", ".join(f"{p.get('network')}:{p.get('status')}"
                         for p in (it.get("providers") or []))
        tk = it.get("tiktokData") or {}
        flags = []
        if tk.get("isAigc"):
            flags.append("AI-labelled")
        if tk.get("autoAddMusic"):
            flags.append("autoAddMusic")
        if it.get("firstCommentText"):
            flags.append("HAS FIRST COMMENT")
        text = (it.get("text") or "").strip()
        first = text.split("\n")[0]
        rest = [l for l in text.split("\n")[1:] if l.strip()]
        print(f"{i:>2}. {pub[:16].replace('T', '  ')}   id={it.get('id')}   [{nets}]")
        print(f"    {' · '.join(flags) or 'no flags'}")
        print(f"    caption: {first}")
        for l in rest:
            print(f"             {l}")
        print(f"    media:   {len(it.get('media') or [])} file(s)")
        print()


def html_report(items: list[dict], tz: str) -> str:
    u = ov.UI_SAFE
    cards = []
    for i, it in enumerate(items, 1):
        pub = (it.get("publicationDate") or {}).get("dateTime") or "?"
        media = (it.get("media") or [None])[0]
        tk = it.get("tiktokData") or {}
        text = (it.get("text") or "").strip()
        chips = []
        if tk.get("isAigc"):
            chips.append('<span class="c ok">AI label</span>')
        if tk.get("autoAddMusic"):
            chips.append('<span class="c ok">autoAddMusic</span>')
        if it.get("firstCommentText"):
            chips.append('<span class="c warn">first comment set</span>')
        for pr in (it.get("providers") or []):
            st = (pr.get("status") or "").upper()
            cls = "ok" if st in ("PENDING", "PUBLISHED") else "warn"
            chips.append(f'<span class="c {cls}">{html.escape(pr.get("network") or "")}: {st}</span>')
        img = (f'<div class="ph"><img src="{html.escape(media)}" loading="lazy">'
               f'<i class="ui top"></i><i class="ui bot"></i><i class="ui rail"></i></div>'
               ) if media else '<div class="ph none">no media</div>'
        cards.append(f'''<div class="card">
  {img}
  <div class="meta">
    <div class="when">{html.escape(pub[:16].replace("T", " · "))}</div>
    <div class="id">#{i} · id {it.get("id")}</div>
    <div class="chips">{''.join(chips)}</div>
    <pre class="cap">{html.escape(text)}</pre>
  </div>
</div>''')

    return f'''<!doctype html><meta charset="utf-8">
<title>Dani — scheduled ({len(items)})</title>
<style>
:root{{--line:#e5e7eb;--mut:#6b7280}}
*{{box-sizing:border-box}}
body{{margin:0;background:#fafafa;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#111}}
header{{padding:22px 28px;background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}}
h1{{margin:0 0 4px;font-size:19px}}
.sub{{color:var(--mut);font-size:13px}}
.key{{margin-top:10px;font-size:12px;color:var(--mut)}}
.key b{{display:inline-block;width:11px;height:11px;background:rgba(255,0,0,.28);
  border:1px solid rgba(255,0,0,.5);vertical-align:-1px;margin-right:4px}}
.wrap{{max-width:1500px;margin:0 auto;padding:24px 28px 80px;
  display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:20px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex}}
.ph{{position:relative;width:200px;min-width:200px;background:#111}}
.ph img{{width:100%;display:block}}
.ph.none{{display:flex;align-items:center;justify-content:center;color:#888;height:355px;font-size:12px}}
.ui{{position:absolute;left:0;right:0;background:rgba(255,0,0,.28);pointer-events:none}}
.ui.top{{top:0;height:{u["top"]/1920*100:.2f}%}}
.ui.bot{{bottom:0;height:{(1920-u["bottom"])/1920*100:.2f}%}}
.ui.rail{{top:36%;bottom:22%;left:{u["right"]/1080*100:.2f}%;right:0}}
.meta{{padding:14px 16px;flex:1;min-width:0}}
.when{{font-weight:650;font-size:14px}}
.id{{font-size:11px;color:var(--mut);margin-bottom:8px}}
.chips{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:10px}}
.c{{font-size:10px;padding:2px 7px;border-radius:5px;text-transform:uppercase;letter-spacing:.05em}}
.c.ok{{background:#dcfce7;color:#166534}}
.c.warn{{background:#fee2e2;color:#991b1b}}
.cap{{margin:0;font:13px/1.45 inherit;white-space:pre-wrap;word-break:break-word;color:#374151}}
</style>
<header>
  <h1>Dani — {len(items)} scheduled post(s)</h1>
  <div class="sub">Pulled live from Metricool. Times {html.escape(tz)}. This is what is actually queued, not what the planner intended.</div>
  <div class="key"><b></b> covered by TikTok's own UI (caption row, action rail, top tabs) — on-image text must stay clear of these</div>
</header>
<div class="wrap">{''.join(cards)}</div>'''


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default=None)
    ap.add_argument("--html", action="store_true")
    a = ap.parse_args(argv)
    items, tz = fetch(a.persona)
    if a.html:
        open(OUT, "w").write(html_report(items, tz))
        print(f"wrote {OUT} ({len(items)} posts)")
        subprocess.run(["open", "-a", "Brave Browser", OUT], check=False)
    else:
        text_report(items, tz)
    return 0


if __name__ == "__main__":
    sys.exit(main())
