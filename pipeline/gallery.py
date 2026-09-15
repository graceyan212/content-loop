#!/usr/bin/env python3
"""
gallery.py — build a single self-contained HTML page to review the whole library.

Renders every post as a card: a live CSS preview of the on-screen text sitting on
one of the real stills (same look as the tiktok-native renderer: centred white
bold, soft dark shadow, no bands), next to the caption, hashtags, and Dani's
self-comment. Plus a contact sheet of the images at the top.

The point is to review copy and image together without rendering 100 PNGs.

    python gallery.py                     # Dani (default persona), writes gallery.html, opens it
    python gallery.py --no-open
    python gallery.py --persona chloe --no-open
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import subprocess
import sys
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "copy"))
import settings as cset   # ONE setting implementation, shared with batch.py

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import personas  # noqa: E402  (personas.py — persona registry + resolution)


def rel(p: str, root: str) -> str:
    return os.path.relpath(p, root)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
# Files in copy/ that are NOT post libraries. Everything else is loaded.
#
# THIS IS AN EXCLUDE LIST ON PURPOSE. It used to be an include list — two separate
# hardcoded globs for "value-*.md" and "ask-*.md", one in schedule_batch.load_library
# and a second copy in respin.library_by_uid. Adding events-01.md and confess-01.md
# made them invisible to the ENTIRE scheduler with no error anywhere: the posts
# parsed, gated and rendered fine in isolation and simply never got picked. An
# include list fails silently when content is added, which is the common case; an
# exclude list fails loudly when a non-post file is added, which is the rare one.
# `noschedule-` is the ONLY prefix that neither loader will schedule.
#
# `drafts-` is NOT safe for this: gal.library_files() skips it, but
# schedule_batch.load_drafts_library() deliberately globs drafts-*.md as a second
# library format (the "hook" format batch.py uses). Two files written to hold
# unfinished content — an Instagram-only intro, and reply-to-comment posts containing
# literal <<PASTE>> placeholders — were only saved from going live by an unrelated
# accident: the hook parser demands a `### short|wall` heading and theirs did not
# match, so it warned and skipped them. Adding one heading would have posted
# "<<PASTE the comment>>" to TikTok.
# `igonly-` is Instagram/Facebook-only content: post/backfill_ig.py reads it by name
# and schedules it as Reels. It must never reach TikTok, so it lives behind the same
# two exclusions as `noschedule-`.
NOT_A_LIBRARY = ("drafts-", "noschedule-", "igonly-")
NEVER_SCHEDULE = ("noschedule-", "igonly-")
IG_ONLY_PREFIX = "igonly-"
NOT_A_LIBRARY_EXACT = ("bank.json", "killed.json")


def library_files(root: str) -> list[str]:
    """Every markdown post library for a persona, sorted deterministically."""
    import glob as _glob
    out = []
    for f in sorted(_glob.glob(os.path.join(root, "copy", "*.md"))):
        base = os.path.basename(f)
        if base.startswith(NOT_A_LIBRARY) or base in NOT_A_LIBRARY_EXACT:
            continue
        out.append(f)
    return out


def parse_posts(path: str) -> list[dict]:
    """
    Both library formats share a `key: value` shape under a `### id` heading.
    `items:` is a following block of `- ` bullets. Header HTML comments are
    skipped so the correction notes don't show up as posts.
    """
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)          # strip header comments
    posts, cur, in_items = [], None, False

    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            if cur:
                posts.append(cur)
            cur = {"id": m.group(1), "items": [], "src": os.path.basename(path)}
            in_items = False
            continue
        if cur is None:
            continue
        if in_items and line.startswith("- "):
            cur["items"].append(line[2:].strip())
            continue
        # NOTE the underscore: the original class was [a-z-]* and silently dropped every
        # key with an underscore. `count_title:` was written to 63 posts and none of
        # them parsed — the field existed on disk and nothing could see it.
        km = re.match(r"^([a-z][a-z_-]*):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            in_items = (key == "items")
            if key != "items":
                cur[key] = val
            continue
        if line.strip() == "---" or (line.startswith("#") and not line.startswith("### ")):
            # A separator or a `#`/`## ` heading CLOSES the current post: section prose
            # after it is prose, not a dropped field, and the orphan check below must
            # not fire on it.
            #
            # FLUSH, do not discard. A pending post is appended by the NEXT `### `
            # (line 84), so setting cur = None here deleted the last post of every
            # section — 160 posts became 150 and the only symptom was a smaller number.
            # Closing a post and losing a post are one character apart.
            if cur:
                posts.append(cur)
            cur, in_items = None, False
            continue
        # An indent-free, key-free, bullet-free line inside a post is a continuation
        # someone expected to be read. It never was: values are single-line, and the
        # convention for a line break is " / " (see screen_text). Multi-line
        # `on-screen:` blocks were silently truncated to their first line and
        # rendered as a third of the post. Warn rather than drop.
        if cur is not None and line.strip() and not line.startswith(("#", "<!--", "-->")):
            print(f"  WARNING {os.path.basename(path)}::{cur.get('id')}: orphan line "
                  f"ignored (use ' / ' for a line break): {line.strip()[:56]!r}",
                  file=sys.stderr)
    if cur:
        posts.append(cur)

    # a real post has something to put on screen
    return [p for p in posts if p.get("on-screen") or p.get("title")]


# Setting inference now lives in copy/settings.py so batch.py pairs posts to
# photos with EXACTLY the same rules this dashboard displays. Two copies of
# this logic drifted apart once already.
def setting_of(p: dict) -> str:
    return cset.setting_of(p)


def screen_text(p: dict) -> str:
    """The line that gets burned into the photo."""
    t = p.get("on-screen") or p.get("title") or ""
    return t.replace(" / ", "\n")   # intro posts use " / " as a line break


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------
CSS = """
:root{--ink:#111;--mut:#6b7280;--line:#e5e7eb;--bg:#fafafa;--card:#fff;--accent:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
header{padding:28px 32px 18px;border-bottom:1px solid var(--line);background:var(--card);
       position:sticky;top:0;z-index:20}
h1{margin:0 0 4px;font-size:20px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px}
.wrap{max-width:1500px;margin:0 auto;padding:0 32px 80px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);
   margin:40px 0 14px;font-weight:600}
.sheet{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px}
.sheet figure{margin:0;width:150px}
.sheet img{width:150px;height:267px;object-fit:cover;border-radius:8px;
           border:1px solid var(--line);display:block;cursor:pointer}
.sheet figcaption{font-size:11px;color:var(--mut);margin-top:5px;word-break:break-all}
.controls{display:flex;gap:10px;align-items:center;margin:14px 0 0;flex-wrap:wrap}
input[type=search],select{padding:7px 10px;border:1px solid var(--line);border-radius:7px;
                          font-size:13px;background:var(--card)}
input[type=search]{width:260px}
.count{color:var(--mut);font-size:12px}
.grid{display:flex;flex-wrap:wrap;gap:18px}
.grid > .card{flex:1 1 430px;max-width:640px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
      display:flex;flex-direction:column}
.card .top{display:flex;gap:0}
.prev{position:relative;width:186px;min-width:186px;height:331px;background:#222;overflow:hidden}
.prev img{width:100%;height:100%;object-fit:cover;display:block}
.prev .ov{position:absolute;left:8%;right:8%;bottom:12%;text-align:center;color:#fff;
          font-weight:700;font-size:15px;line-height:1.18;white-space:pre-wrap;
          text-shadow:0 2px 9px rgba(0,0,0,.72),0 0 3px rgba(0,0,0,.5)}
.body{padding:14px 16px;flex:1;min-width:0}
.idline{font-size:11px;color:var(--mut);margin-bottom:8px;display:flex;
        align-items:center;gap:6px;flex-wrap:wrap}
.idline .spacer{flex:1 1 auto;min-width:0}
.tag{background:#f3f4f6;border-radius:5px;padding:1px 6px;font-size:10px;white-space:nowrap}
.set-car{background:#dbeafe;color:#1e40af}
.set-kitchen{background:#fef3c7;color:#92400e}
.set-outdoor{background:#dcfce7;color:#166534}
.set-home{background:#f3e8ff;color:#6b21a8}
.set-any{background:#f3f4f6;color:#6b7280}
.nomatch{background:#fee2e2;color:#991b1b}
.matched{background:#dcfce7;color:#166534;cursor:help}
.kill{margin-left:6px;border:1px solid var(--line);background:var(--card);color:#9ca3af;
      border-radius:6px;width:22px;height:22px;line-height:1;font-size:11px;cursor:pointer;
      padding:0;flex:0 0 auto}
.kill:hover{border-color:#dc2626;color:#dc2626;background:#fef2f2}
.card.is-killed{opacity:.32}
.card.is-killed .prev{filter:grayscale(1) blur(1px)}
.card.is-killed .scr,.card.is-killed .cap{text-decoration:line-through}
.killcount{color:#991b1b;font-size:12px}
.lbl{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:11px 0 3px}
.scr{font-weight:650;font-size:14px;white-space:pre-wrap}
.cap{font-size:13px}
.items{margin:3px 0 0;padding-left:17px;font-size:12.5px;color:#374151}
.items li{margin-bottom:3px}
.hash{font-size:11.5px;color:var(--accent)}
.cmt{font-size:13px;background:#f9fafb;border-left:2px solid var(--line);padding:6px 9px;
     border-radius:0 5px 5px 0;font-style:italic}
button.copy{margin-top:11px;font-size:11px;padding:4px 9px;border:1px solid var(--line);
            border-radius:6px;background:var(--card);cursor:pointer;color:var(--mut)}
button.copy:hover{border-color:var(--accent);color:var(--accent)}
.hidden{display:none}
.note{width:100%;font:13px/1.45 inherit;padding:6px 8px;border:1px solid var(--line);
      border-radius:6px;background:#fffdf6;resize:vertical;min-height:38px;color:var(--ink)}
.note:focus{outline:none;border-color:var(--accent);background:#fff}
.note.has{background:#fef9c3;border-color:#eab308}
.notecount{color:#92400e;font-size:12px}
.done{font-size:10px;color:var(--mut);display:flex;align-items:center;
      gap:4px;cursor:pointer;user-select:none;text-transform:uppercase;letter-spacing:.06em}
.done input{cursor:pointer;margin:0}
.card.is-done{opacity:.5}
.card.is-done .prev{filter:grayscale(1)}
.card.is-done .body{text-decoration:none}
.bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden;width:190px}
.bar span{display:block;height:100%;background:#16a34a;transition:width .2s}
.reset{font-size:11px;color:var(--mut);border:1px solid var(--line);background:var(--card);
       border-radius:6px;padding:5px 9px;cursor:pointer}
.reset:hover{border-color:#dc2626;color:#dc2626}
"""

JS = """
const KEY='__POSTED_KEY__';
const store=JSON.parse(localStorage.getItem(KEY)||'{}');
const q=document.getElementById('q'), sel=document.getElementById('src'),
      setSel=document.getElementById('setting'), stSel=document.getElementById('status'),
      cards=[...document.querySelectorAll('.card')], cnt=document.getElementById('cnt'),
      imgsel=document.getElementById('img');
function isKilled(c){ return !!killed[c.dataset.uid]; }
function isDone(c){
  // an explicit tick wins; otherwise fall back to what learn/posts.json knew
  const v=store[c.dataset.uid];
  return v===undefined ? c.dataset.seeded==='1' : v;
}
function paint(){
  let done=0, nkill=0;
  cards.forEach(c=>{
    const d=isDone(c), k=isKilled(c);
    c.classList.toggle('is-done',d && !k);
    c.classList.toggle('is-killed',k);
    c.querySelector('.tick').checked=d;
    c.querySelector('.kill').textContent = k ? '\u21ba' : '\u2715';
    c.querySelector('.kill').title = k ? 'undo' : 'mark for deletion';
    if(d)done++; if(k)nkill++;
  });
  const kc=document.getElementById('killcount');
  kc.textContent = nkill ? nkill+' marked for deletion' : '';
  const pct=cards.length?Math.round(done/cards.length*100):0;
  document.getElementById('prog').style.width=pct+'%';
  document.getElementById('progtxt').textContent=done+' / '+cards.length+' posted';
}
function reorder(){
  // posted and killed cards sink; the grid is a flex/grid container so `order`
  // does the work without moving nodes and losing scroll position
  cards.forEach(c=>{
    c.style.order = isKilled(c) ? 2 : (isDone(c) ? 1 : 0);
  });
}
function apply(){
  const t=q.value.toLowerCase(), s=sel.value, g=setSel.value, st=stSel.value; let n=0;
  cards.forEach(c=>{
    const d=isDone(c);
    const k=isKilled(c);
    let stOk;
    if(st==='killed')      stOk = k;
    else if(st==='all')    stOk = true;
    else if(st==='done')   stOk = d && !k;
    else                   stOk = !d && !k;          // 'todo' hides killed
    const ok=(s==='all'||c.dataset.src===s) && (g==='all'||c.dataset.setting===g)
             && stOk && c.innerText.toLowerCase().includes(t);
    c.classList.toggle('hidden',!ok); if(ok)n++;
  });
  cnt.textContent=n+' shown';
  reorder();
}
q.addEventListener('input',apply); sel.addEventListener('change',apply);
setSel.addEventListener('change',apply); stSel.addEventListener('change',apply);
document.querySelectorAll('.tick').forEach(t=>t.addEventListener('change',e=>{
  const c=e.target.closest('.card');
  store[c.dataset.uid]=e.target.checked;
  localStorage.setItem(KEY,JSON.stringify(store));
  paint(); apply();
}));
function paintNotes(){
  let n=0;
  cards.forEach(c=>{
    const t=c.querySelector('.note'), v=notes[c.dataset.uid]||'';
    if(t.value!==v) t.value=v;
    t.classList.toggle('has',!!v.trim());
    if(v.trim())n++;
  });
  document.getElementById('notecount').textContent = n? n+' with notes' : '';
}
let noteTimer=null;
document.querySelectorAll('.note').forEach(t=>t.addEventListener('input',e=>{
  const c=e.target.closest('.card');
  notes[c.dataset.uid]=e.target.value;
  clearTimeout(noteTimer);
  noteTimer=setTimeout(()=>{
    localStorage.setItem(NKEY,JSON.stringify(notes));
    paintNotes();
  },400);
}));
document.getElementById('exportnotes').addEventListener('click',()=>{
  const out={};
  Object.keys(notes).forEach(k=>{ if((notes[k]||'').trim()) out[k]=notes[k].trim(); });
  const n=Object.keys(out).length;
  if(!n){ alert('No notes written yet.'); return; }
  navigator.clipboard.writeText(JSON.stringify(out,null,2));
  alert(n+' note(s) copied as JSON. Notes live in this browser only, so paste them somewhere durable if they matter.');
});
document.querySelectorAll('.kill').forEach(b=>b.addEventListener('click',e=>{
  const c=e.target.closest('.card'), u=c.dataset.uid;
  if(killed[u]) delete killed[u]; else killed[u]=true;
  localStorage.setItem(KKEY,JSON.stringify(killed));
  paint(); apply();
}));
document.getElementById('exportkill').addEventListener('click',()=>{
  const ids=Object.keys(killed).filter(k=>killed[k]);
  if(!ids.length){ alert('Nothing marked for deletion yet.'); return; }
  navigator.clipboard.writeText(JSON.stringify(ids,null,2));
  alert(ids.length+' id(s) copied. Marking here only hides them; to remove them from the copy files run:  pbpaste | python3 prune.py --paste');
});
document.getElementById('reset').addEventListener('click',()=>{
  if(!confirm('Clear every tick? Posts already in learn/posts.json stay marked.'))return;
  localStorage.removeItem(KEY);
  Object.keys(store).forEach(k=>delete store[k]);
  paint(); apply();
});
imgsel.addEventListener('change',()=>{
  document.querySelectorAll('.prev img').forEach(i=>i.src=imgsel.value);
});
document.querySelectorAll('.sheet img').forEach(i=>i.addEventListener('click',()=>{
  imgsel.value=i.getAttribute('src'); imgsel.dispatchEvent(new Event('change'));
}));
document.querySelectorAll('button.copy').forEach(b=>b.addEventListener('click',()=>{
  navigator.clipboard.writeText(JSON.parse(b.dataset.payload));
  const o=b.textContent; b.textContent='copied'; setTimeout(()=>b.textContent=o,900);
}));
paint(); paintNotes(); apply();
"""


_STOP = set("a an the and or but so of to in on at for with from is it its was were "
            "i im ive my me we our you your he she they that this then than there here "
            "have has had do does did just about like get got not no yes if when what "
            "one out up all been being am are be by as into over".split())


def _toks(t: str) -> set:
    """Content words only. Hashtags and punctuation carry no matching signal."""
    t = re.sub(r"#\w+", " ", (t or "").lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return {w for w in t.split() if len(w) > 2 and w not in _STOP}


MIN_DISCRIMINATING = 2


def similarity(a: str, b: str) -> float:
    """
    Token overlap as the max of the two directional coverages.

    Directional on purpose: a library caption may be a long list while the version
    actually posted is a rewritten paragraph, so symmetric Jaccard scores them as
    unrelated even when one clearly derives from the other. Coverage catches that.
    """
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    # Require an absolute floor of shared words, not just a good ratio. A short
    # caption ("what are your tiktok secrets" -> {tiktok, secrets}) hits 0.5
    # coverage against anything that mentions TikTok, so ratio alone flagged
    # unrelated posts as already-published.
    # Scale the floor to the SHORTER string. A flat floor of 4 was too strict for
    # a genuinely short live caption ("what are your tiktok secrets" -> 2 content
    # words) and dropped a real match; requiring both of its 2 words instead keeps
    # precision high without penalising brevity.
    # NEVER let the floor fall to 1, and refuse to match against a string that has
    # fewer than 2 content words at all.
    #
    # MEASURED BUG THIS FIXES: a scheduled caption reading literally "What do you
    # think." reduces to the single token {think}. Directional coverage then scored
    # 1.00 against EVERY library post containing the word "think" (1/1 covered), which
    # cleared the 0.42 threshold and marked them all as already-posted. Nine posts were
    # silently unschedulable, six of them on that one word alone, with no error
    # anywhere — they simply never got picked, forever.
    #
    # The scaling floor was added so a genuinely short live caption ("what are your
    # tiktok secrets" -> {tiktok, secrets}) could still match on both of its two words.
    # That case is preserved: the floor scales down to 2, not to 1. Two shared
    # distinctive words is evidence; one shared common word is a coincidence.
    if min(len(ta), len(tb)) < MIN_DISCRIMINATING:
        return 0.0
    need = max(MIN_DISCRIMINATING, min(MIN_OVERLAP, len(ta), len(tb)))
    if inter < need:
        return 0.0
    return max(inter / len(ta), inter / len(tb))


# Tuned against the real library vs the 6 live captions: rewritten versions of a
# library post land ~0.5-0.8, unrelated posts sit under ~0.3. Deliberately loose —
# a false positive is a greyed-out row you can untick, a false negative means
# posting the same thing twice.
MATCH_THRESHOLD = 0.42
MIN_OVERLAP = 4          # shared content words required before a ratio counts at all


def posted_keys(root: str) -> set:
    """
    Captions we have already shipped, from learn/posts.json, so the dashboard
    opens with those rows already ticked. Matching on a normalized caption prefix
    because hand-posted content carries no id we control.
    """
    f = os.path.join(root, "learn", "posts.json")
    if not os.path.exists(f):
        return set()
    try:
        db = json.load(open(f))
    except Exception:
        return set()
    out = set(db.get("live_captions") or [])       # every post actually live on TikTok
    for r in db.get("posts", []):
        c = (r.get("caption") or "").lower()
        c = re.sub(r"[^a-z0-9 ]", " ", c)
        c = re.sub(r"\s+", " ", c).strip()
        if c:
            out.add(c[:48])
    return out


def build(open_after: bool = True, persona_name: str = personas.DEFAULT_PERSONA) -> str:
    persona = personas.resolve(persona_name)
    root = persona["root"]
    cfg = persona["config"]
    display_name = cfg.get("display_name") or persona_name.capitalize()

    copy_dir = os.path.join(root, "copy")
    stills_dir = os.path.join(root, "character", persona_name, "stills")
    round3_dir = os.path.join(root, "character", persona_name, "round3", "degraded")
    out_path = os.path.join(root, "gallery.html")

    # jpg as well as png: degraded frames land as .jpg and a *.png-only glob
    # silently dropped one of only three images in rotation.
    imgs = sorted(glob.glob(os.path.join(stills_dir, "*.png")) +
                  glob.glob(os.path.join(stills_dir, "*.jpg")))
    extra = sorted(glob.glob(os.path.join(round3_dir, "*.jpg")) +
                   glob.glob(os.path.join(round3_dir, "*.png")))
    default_img = rel(imgs[0], root) if imgs else ""

    # Map each still to its setting tag, straight from the manifest, so a card
    # previews on the photo batch.py would actually pair it with. Without this the
    # dashboard showed every post on one globally-chosen image and told you nothing
    # about the pairing.
    man_path = os.path.join(os.path.dirname(stills_dir), "manifest.json")
    by_setting: dict[str, list[str]] = {}
    try:
        for e in json.load(open(man_path)):
            tag = (e.get("setting_tag") or "any").lower()
            f = os.path.join(root, e["file"]) if not os.path.isabs(e["file"]) else e["file"]
            f = os.path.join(os.path.dirname(stills_dir), e["file"]) \
                if not os.path.exists(f) else f
            if os.path.exists(f):
                by_setting.setdefault(tag, []).append(rel(f, root))
    except Exception:
        pass

    def matched_img(setting: str, i: int) -> tuple[str, bool]:
        """(image, was_matched). Round-robins within a setting so a 35-post
        category does not show the identical thumbnail 35 times."""
        pool = by_setting.get(setting)
        if pool:
            return pool[i % len(pool)], True
        flat = [f for v in by_setting.values() for f in v]
        if setting == "any" and flat:
            return flat[i % len(flat)], True
        return (flat[i % len(flat)] if flat else default_img), False

    # Glob by PATTERN, not by hard-coded filenames. `ask-and-intro.md` was named
    # explicitly, so a new ask-*.md file would have been silently absent from the
    # dashboard with no error anywhere.
    files = sorted(glob.glob(os.path.join(copy_dir, "value-*.md"))) + \
            sorted(glob.glob(os.path.join(copy_dir, "ask-*.md")))
    posts = []
    for f in files:
        posts += parse_posts(f)
    live = sorted(posted_keys(root))
    for p in posts:
        p["setting"] = setting_of(p)
        p["uid"] = f"{p['src']}::{p['id']}"          # stable across rebuilds
        # Fuzzy, deliberately. Captions get rewritten before posting, so an exact
        # prefix match caught 1 of 5 live posts. Compare BOTH the caption and the
        # on-screen line against every live caption — the on-screen text is burned
        # into the image and cannot be edited afterwards, so it survives rewrites.
        mine = [p.get("caption") or "", p.get("title") or "",
                p.get("on-screen") or "", p.get("comment") or ""]
        best, bestlive = 0.0, ""
        for lv in live:
            for m in mine:
                sc = similarity(m, lv)
                if sc > best:
                    best, bestlive = sc, lv
        p["posted"] = best >= MATCH_THRESHOLD
        p["_match"] = round(best, 2)
        p["_matched_live"] = bestlive[:48]

    def sheet(paths, note):
        if not paths:
            return ""
        figs = "".join(
            f'<figure><img src="{html.escape(rel(p, root))}" title="click to preview text on this image">'
            f'<figcaption>{html.escape(os.path.basename(p))}</figcaption></figure>'
            for p in paths)
        return f'<h2>{html.escape(note)}</h2><div class="sheet">{figs}</div>'

    opts = "".join(f'<option value="{html.escape(rel(p, root))}">{html.escape(os.path.basename(p))}</option>'
                   for p in imgs + extra)
    setting_counts = {}
    for p in posts:
        setting_counts[p["setting"]] = setting_counts.get(p["setting"], 0) + 1
    set_opts = '<option value="all">all settings</option>' + "".join(
        f'<option value="{k}">{k} ({v})</option>'
        for k, v in sorted(setting_counts.items(), key=lambda kv: -kv[1]))
    srcs = sorted({p["src"] for p in posts})
    src_opts = '<option value="all">all files</option>' + "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in srcs)

    # group by setting so scrolling for "something for the car photo" works
    order = {"car": 0, "outdoor": 1, "kitchen": 2, "home": 3, "any": 4}
    posts.sort(key=lambda x: (order.get(x["setting"], 9), x["src"], x["id"]))
    cards = []
    _seen: dict[str, int] = {}
    for p in posts:
        _n = _seen.get(p["setting"], 0); _seen[p["setting"]] = _n + 1
        p["_img"], p["_matched"] = matched_img(p["setting"], _n)
        st = screen_text(p)
        items = "".join(f"<li>{html.escape(i)}</li>" for i in p.get("items", []))
        payload = json.dumps("\n".join(filter(None, [
            "ON SCREEN:\n" + st,
            ("\nITEMS:\n" + "\n".join("- " + i for i in p["items"])) if p.get("items") else "",
            "\nCAPTION:\n" + p.get("caption", ""),
            "\nHASHTAGS:\n" + p.get("hashtags", ""),
            "\nCOMMENT:\n" + p.get("comment", ""),
        ])))
        cards.append(f"""
<div class="card" data-src="{html.escape(p['src'])}" data-setting="{p['setting']}" data-uid="{html.escape(p['uid'])}" data-seeded="{'1' if p['posted'] else '0'}">
  <div class="top">
    <div class="prev"><img src="{html.escape(p['_img'])}" alt="">
      <div class="ov">{html.escape(st)}</div></div>
    <div class="body">
      <div class="idline"><span class="tag">{html.escape(p['src'].replace('.md',''))} · {html.escape(p['id'])}</span><span class="tag set-{p['setting']}">{p['setting']}</span>
      {'' if p['_matched'] else '<span class="tag nomatch">no photo</span>'}
      {f'<span class="tag matched" title="matched a live post at {p["_match"]} confidence: {html.escape(p["_matched_live"])}">live {p["_match"]}</span>' if p['posted'] else ''}
      <span class="spacer"></span>
      <label class="done"><input type="checkbox" class="tick"> posted</label>
      <button class="kill" title="mark for deletion">&#10005;</button></div>
      <div class="lbl">on screen</div><div class="scr">{html.escape(st)}</div>
      {f'<div class="lbl">items</div><ul class="items">{items}</ul>' if items else ''}
      <div class="lbl">caption</div><div class="cap">{html.escape(p.get('caption',''))}</div>
      <div class="lbl">hashtags</div><div class="hash">{html.escape(p.get('hashtags',''))}</div>
      <div class="lbl">first comment</div><div class="cmt">{html.escape(p.get('comment',''))}</div>
      <div class="lbl">notes</div>
      <textarea class="note" rows="2" placeholder="your notes on this idea..."></textarea>
      <button class="copy" data-payload='{html.escape(payload)}'>copy all</button>
    </div>
  </div>
</div>""")

    js = JS.replace("__POSTED_KEY__", f"{persona_name}-posted-v1")

    doc = f"""<!doctype html><meta charset="utf-8">
<title>{html.escape(display_name)} — content library</title><style>{CSS}</style>
<header>
  <h1>{html.escape(display_name)} — content library</h1>
  <div class="sub">{len(posts)} posts · {len(imgs)} stills in rotation · community phase, no product</div>
  <div class="controls">
    <input type="search" id="q" placeholder="search copy...">
    <select id="status">
      <option value="todo">to post</option>
      <option value="all">all</option>
      <option value="done">posted</option>
      <option value="killed">killed</option>
    </select>
    <select id="setting">{set_opts}</select>
    <select id="src">{src_opts}</select>
    <label class="count">preview on:</label>
    <select id="img">{opts}</select>
    <span class="count" id="cnt"></span>
    <div class="bar"><span id="prog"></span></div>
    <span class="count" id="progtxt"></span>
    <span class="count" id="src-note"></span>
    <button class="reset" id="reset">reset ticks</button>
    <span class="killcount" id="killcount"></span>
    <button class="reset" id="exportkill">copy kill list</button>
    <span class="notecount" id="notecount"></span>
    <button class="reset" id="exportnotes">export notes</button>
  </div>
</header>
<div class="wrap">
  {sheet(imgs, f"images in rotation ({len(imgs)})")}
  {sheet(extra, f"round-3 selfies, not in the stills folder ({len(extra)})")}
  <h2>posts</h2>
  <div class="grid">{''.join(cards)}</div>
</div>
<script>{js}</script>"""

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"wrote {out_path}  ({len(posts)} posts, {len(imgs)+len(extra)} images)")
    if open_after:
        subprocess.run(["open", "-a", "Brave Browser", out_path], check=False)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--persona", default=personas.DEFAULT_PERSONA,
                    help="persona name from personas.json (default: dani)")
    a = ap.parse_args()
    build(open_after=not a.no_open, persona_name=a.persona)
