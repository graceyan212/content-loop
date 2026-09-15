#!/usr/bin/env python3
"""
approve.py — an approval queue for what is actually scheduled.

    python post/approve.py            # serves on :8770 and opens the browser

Shows only posts that still need a decision: PENDING in Metricool, and not already
approved or rejected. Published posts never appear — the decision is moot.

  APPROVE  keeps it scheduled, records the decision, removes it from the queue.
           You will not be asked about it again.
  DELETE   deletes it from Metricool for real, and records the rejection.

WHY A SERVER AND NOT A STATIC PAGE: deleting has to reach the Metricool API, and a
file:// page has no credentials and no way to make that call. This runs locally,
holds the creds in the process, and never exposes them to the page.

Every decision appends to learn/decisions.json, which is the point of the exercise:
rejections become kills the scheduler honours, and the approve/reject rate per
type, setting and slot tells the next batch what to make more of. A decision here
is training data, not just a click.
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE, os.path.join(ROOT, "render"), os.path.join(ROOT, "copy")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas          # noqa: E402
import metricool as mc   # noqa: E402
import overlay as ov     # noqa: E402
import review            # noqa: E402
import respin            # noqa: E402  (library_by_uid + caption matching, one impl)
import gallery as gal    # noqa: E402

STATE = {}


def decisions_path(root: str) -> str:
    return os.path.join(root, "learn", "decisions.json")


def load_decisions(root: str) -> dict:
    f = decisions_path(root)
    if os.path.exists(f):
        try:
            return json.load(open(f))
        except Exception:
            pass
    return {"_note": "Operator approve/reject decisions from post/approve.py. "
                     "Rejected uids are treated as kills by the scheduler; the "
                     "approve rate per type/setting/slot guides future batches.",
            "decisions": []}


def record(root: str, entry: dict) -> None:
    db = load_decisions(root)
    db["decisions"] = [d for d in db["decisions"] if d.get("post_id") != entry["post_id"]]
    db["decisions"].append(entry)
    os.makedirs(os.path.dirname(decisions_path(root)), exist_ok=True)
    json.dump(db, open(decisions_path(root), "w"), indent=2)


def decided_ids(root: str) -> set:
    return {str(d.get("post_id")) for d in load_decisions(root)["decisions"]}


def uid_for(live: dict, lib: dict) -> tuple[str | None, float]:
    cap = (live.get("text") or "").strip()
    best, score = None, 0.0
    for uid, lp in lib.items():
        sc = gal.similarity(respin.caption_text(lp), cap)
        if sc > score:
            best, score = uid, sc
    return best, round(score, 2)


def queue(persona_name: str | None) -> list[dict]:
    p = personas.resolve(persona_name)
    root = p["root"]
    items, _ = review.fetch(persona_name)
    lib = respin.library_by_uid(root)
    done = decided_ids(root)
    out = []
    for it in items:
        statuses = [(pr.get("status") or "").upper() for pr in (it.get("providers") or [])]
        if not statuses or "PENDING" not in statuses:
            continue                      # published or errored: nothing to decide
        if str(it.get("id")) in done:
            continue                      # already approved or rejected
        uid, score = uid_for(it, lib)
        out.append({"live": it, "uid": uid, "match": score})
    out.sort(key=lambda x: (x["live"].get("publicationDate") or {}).get("dateTime") or "")
    return out


PAGE = """<!doctype html><meta charset="utf-8"><title>Approve — {n} waiting</title>
<style>
:root{{--line:#e5e7eb;--mut:#6b7280}}
*{{box-sizing:border-box}}
body{{margin:0;background:#fafafa;color:#111;
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}
header{{padding:20px 28px;background:#fff;border-bottom:1px solid var(--line);
 position:sticky;top:0;z-index:9}}
h1{{margin:0 0 3px;font-size:19px}}
.sub{{color:var(--mut);font-size:13px}}
.key b{{display:inline-block;width:11px;height:11px;background:rgba(255,0,0,.28);
 border:1px solid rgba(255,0,0,.5);vertical-align:-1px;margin-right:4px}}
.key{{font-size:12px;color:var(--mut);margin-top:8px}}
.wrap{{max-width:1180px;margin:0 auto;padding:24px 28px 100px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;
 display:flex;overflow:hidden;margin-bottom:16px;transition:opacity .18s}}
.card.gone{{opacity:0;pointer-events:none}}
.ph{{position:relative;width:186px;min-width:186px;background:#111}}
.ph img{{width:100%;display:block}}
.ui{{position:absolute;left:0;right:0;background:rgba(255,0,0,.26);pointer-events:none}}
.ui.top{{top:0;height:12.5%}} .ui.bot{{bottom:0;height:26%}}
.ui.rail{{top:36%;bottom:22%;left:83.3%;right:0}}
.body{{padding:15px 18px;flex:1;min-width:0}}
.when{{font-weight:650}}
.meta{{font-size:11px;color:var(--mut);margin-bottom:9px}}
pre{{margin:0 0 12px;font:13px/1.45 inherit;white-space:pre-wrap;word-break:break-word;color:#374151}}
.btns{{display:flex;gap:8px}}
button{{font:600 13px inherit;padding:8px 16px;border-radius:8px;cursor:pointer;border:1px solid}}
.ok{{background:#16a34a;border-color:#16a34a;color:#fff}}
.ok:hover{{background:#15803d}}
.no{{background:#fff;border-color:var(--line);color:#b91c1c}}
.no:hover{{border-color:#dc2626;background:#fef2f2}}
.empty{{text-align:center;padding:70px 20px;color:var(--mut)}}
#toast{{position:fixed;left:50%;transform:translateX(-50%);bottom:26px;background:#111;
 color:#fff;padding:10px 18px;border-radius:9px;font-size:13px;opacity:0;transition:opacity .2s}}
#toast.on{{opacity:1}}
</style>
<header>
  <h1>Approve queue</h1>
  <div class="sub"><span id="left">{n}</span> post(s) waiting. Approved posts stay scheduled and disappear from here. Deleted posts are removed from Metricool for real.</div>
  <div class="key"><b></b> covered by TikTok's UI — on-image text must stay clear</div>
</header>
<div class="wrap" id="wrap">{cards}</div>
<div id="toast"></div>
<script>
function toast(m){{const t=document.getElementById('toast');t.textContent=m;t.classList.add('on');
  setTimeout(()=>t.classList.remove('on'),1600);}}
async function act(id, action, btn){{
  const card=btn.closest('.card');
  card.style.opacity=.4;
  const r=await fetch('/'+action,{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{post_id:id}})}});
  const j=await r.json();
  if(!j.ok){{card.style.opacity=1;toast('failed: '+(j.error||'?'));return;}}
  card.classList.add('gone');
  setTimeout(()=>{{card.remove();
    const n=document.querySelectorAll('.card').length;
    document.getElementById('left').textContent=n;
    if(!n)document.getElementById('wrap').innerHTML='<div class="empty">Nothing left to review.</div>';
  }},200);
  toast(action==='approve'?'approved, staying scheduled':'deleted from Metricool');
}}
</script>"""


def render_page(q: list[dict]) -> str:
    cards = []
    for it in q:
        live = it["live"]
        pub = (live.get("publicationDate") or {}).get("dateTime") or "?"
        media = (live.get("media") or [None])[0]
        pid = live.get("id")
        img = (f'<div class="ph"><img src="{html.escape(media)}" loading="lazy">'
               f'<i class="ui top"></i><i class="ui bot"></i><i class="ui rail"></i></div>'
               if media else '<div class="ph"></div>')
        cards.append(f'''<div class="card" data-id="{pid}">{img}
<div class="body">
  <div class="when">{html.escape(pub[:16].replace("T", " · "))}</div>
  <div class="meta">{html.escape(it["uid"] or "?")} · id {pid}</div>
  <pre>{html.escape((live.get("text") or "").strip())}</pre>
  <div class="btns">
    <button class="ok" onclick="act({pid},'approve',this)">Approve</button>
    <button class="no" onclick="act({pid},'delete',this)">Delete</button>
  </div>
</div></div>''')
    body = "".join(cards) or '<div class="empty">Nothing left to review.</div>'
    return PAGE.format(n=len(q), cards=body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        page = render_page(queue(STATE["persona"])).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or "{}")
        pid = str(payload.get("post_id"))
        action = self.path.strip("/")
        root = STATE["root"]
        entry = {"post_id": pid, "action": action,
                 "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
        # attach the uid + dimensions so this is usable as training signal later
        for it in STATE.get("last_queue", []):
            if str(it["live"].get("id")) == pid:
                entry["uid"] = it["uid"]
                entry["scheduled_for"] = (it["live"].get("publicationDate") or {}).get("dateTime")
                break
        if action == "delete":
            st, _ = mc._req("DELETE", f"/v2/scheduler/posts/{pid}", STATE["creds"])
            if st not in (200, 204):
                return self._json({"ok": False, "error": f"metricool {st}"}, 500)
        elif action != "approve":
            return self._json({"ok": False, "error": "unknown action"}, 400)
        record(root, entry)
        return self._json({"ok": True})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--port", type=int, default=8770)
    a = ap.parse_args(argv)

    p = personas.resolve(a.persona)
    creds = mc.load_creds(); creds["METRICOOL_BLOG_ID"] = str(personas.require_blog_id(p))
    STATE.update(persona=a.persona, root=p["root"], creds=creds)
    q = queue(a.persona)
    STATE["last_queue"] = q
    print(f"{len(q)} post(s) awaiting a decision")

    # walk up if the port is taken rather than dying — a stale server from an
    # earlier run should not block a review session
    srv = None
    for port in range(a.port, a.port + 12):
        try:
            srv = HTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit(f"no free port in {a.port}-{a.port + 11}")
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.4, lambda: subprocess.run(
        ["open", "-a", "Brave Browser", url], check=False)).start()
    print(f"serving {url}   (ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
