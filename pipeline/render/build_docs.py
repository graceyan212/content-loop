#!/usr/bin/env python3
"""
build_docs.py — generate the visual design system page from the live tokens.

    python3 render/build_docs.py --persona chloe

The page is generated from design.py and carousel.py rather than hand-written,
so the written spec cannot drift from what the renderer actually does. Every
contrast figure on the page is computed at build time, not typed.

Sections follow the final visual guidelines exactly: fixed hierarchy,
typography, text limits, headline formula, colour hierarchy, cover modes,
icons, overall test.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import design as D           # noqa: E402
from carousel import contrast, MODES, FLOOR_BODY, FLOOR_LARGE, W, H, MARGIN  # noqa: E402

S = D.SCALE

CSS = """
 body{background:#121212;color:#e9e9e9;font:14px/1.65 -apple-system,BlinkMacSystemFont,sans-serif;
      margin:0;padding:38px 46px;max-width:1560px}
 h1{font-size:25px;font-weight:600;margin:0 0 4px}
 .sub{color:#8b8b8b;margin:0 0 8px}
 .gen{color:#5f5f5f;font-size:12px;margin:0 0 30px}
 h2{font-size:12.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#7fb4d8;
    margin:42px 0 12px;border-bottom:1px solid #252525;padding-bottom:7px}
 p,ul,ol{color:#b2b2b2;max-width:94ch} li{margin:3px 0}
 .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
 figure{margin:0} img{display:block;border-radius:7px;background:#FBF6F0}
 figcaption{font-size:10px;color:#6b6b6b;margin-top:5px}
 table{border-collapse:collapse;font-size:13px;margin-top:4px}
 td,th{padding:5px 18px 5px 0;text-align:left;vertical-align:top}
 th{color:#8b8b8b;font-weight:500;border-bottom:1px solid #252525}
 .num{font-variant-numeric:tabular-nums;color:#9ed39e}
 .bad{color:#e8908c}
 .sw{display:inline-block;width:16px;height:16px;border-radius:4px;vertical-align:-3px;
     margin-right:9px;border:1px solid #3b3b3b}
 code{color:#e2cb9d;font-size:12.5px} .warn{color:#e8a87c} .ok{color:#9ed39e}
 .thumbs{display:flex;gap:8px;flex-wrap:wrap} .thumbs img{width:132px;border-radius:4px}
 a{color:#7fb4d8}
"""


def figs(pat, w, root):
    out = ""
    for p in sorted(glob.glob(os.path.join(root, pat))):
        rel = os.path.relpath(p, root)
        out += (f'<figure><a href="{rel}" target=_blank><img src="{rel}" style="width:{w}px">'
                f'</a><figcaption>{os.path.basename(p)}</figcaption></figure>')
    return out


def thumbs(pat, root):
    return "".join(f'<img src="{os.path.relpath(p, root)}">'
                   for p in sorted(glob.glob(os.path.join(root, pat))))


def build(root):
    # 5 · colour, every ratio computed now
    order = [("Cream", "cream", "base ground"), ("Cardinal", "cardinal", "headline on cream"),
             ("Deep red", "deep", "alternate headline on cream"),
             ("Forest", "forest", "accent"), ("Ink", "ink", "body copy on cream"),
             ("Meta brown", "meta", "counter and captions"),
             ("Blush", "blush", "optional pale surface")]
    rows = ""
    for name, key, use in order:
        hexv = D.T[key]
        if key in ("cream", "blush"):
            # These are surfaces, not text colours. Computing a text-contrast
            # ratio for a fill and printing it next to real ones is how a fill
            # gets mistaken for a legible text colour.
            ratio, verdict = "surface, not text", ""
        else:
            r = contrast(hexv, D.T["cream"])
            floor = FLOOR_LARGE if key in ("cardinal", "deep") else FLOOR_BODY
            ratio = f"{r:.2f}:1"
            verdict = ("ok" if r >= floor else "below floor")
        ok, why = D.peach_check(hexv)
        cls = "num" if ok and verdict != "below floor" else "bad"
        if key in ("cream", "blush"):
            cls = ""
        rows += (f'<tr><td><span class=sw style="background:{hexv}"></span>{name}</td>'
                 f'<td><code>{hexv}</code></td><td>{use}</td>'
                 f'<td class={cls}>{ratio}</td><td>{why}</td></tr>')

    # cream-mode levels, measured
    cm = MODES["cream"]
    lvl = ""
    for n, c, floor in (("headline", cm["headline"], FLOOR_LARGE),
                        ("qualifier", cm["qualifier"], FLOOR_BODY),
                        ("series label", cm["label"], FLOOR_BODY),
                        ("slide counter", cm["counter"], FLOOR_BODY)):
        r = contrast(c, cm["ground"])
        lvl += (f'<tr><td>{n}</td><td><code>{c}</code></td>'
                f'<td class="{"num" if r >= floor else "bad"}">{r:.2f}:1</td>'
                f'<td>floor {floor}</td></tr>')

    html = f"""<!doctype html><meta charset=utf-8><title>Chloe — visual design system</title>
<style>{CSS}</style>
<h1>Chloe Chen — visual design system</h1>
<p class=sub>{W}&times;{H} &middot; independent editorial, not an official Stanford post and not a
Canva admissions template</p>
<p class=gen>Generated from <code>design.py</code> and <code>carousel.py</code> by
<code>render/build_docs.py</code>. Every ratio on this page is computed at build time, so the spec
cannot drift from the renderer.</p>

<h2>1 · Fixed hierarchy</h2>
<p>Every cover carries the same four levels, in this order. The two number systems stay separate:
the post number lives in the series label, the slide number lives in the counter.</p>
<ol>
<li><b>Series label</b> — <code>chloe's notes 01</code>, the editorial post number</li>
<li><b>Headline</b> — the dominant visual element</li>
<li><b>Qualifier</b> — optional. Adds information, never repeats the headline</li>
<li><b>Slide counter</b> — <code>01 / 06</code>, position in the carousel</li>
</ol>

<h2>2 · Typography</h2>
<table>
<tr><th>level</th><th>face</th><th>size</th></tr>
<tr><td>Headline</td><td>Playfair Display {D.DISPLAY_WEIGHT['headline']}</td>
    <td class=num>{S['headline_min']}–{S['headline_max']} px, autoscaled</td></tr>
<tr><td>Qualifier</td><td>Figtree 400</td><td class=num>{S['qualifier']} px</td></tr>
<tr><td>Series label</td><td>Figtree 500, letterspaced</td><td class=num>{S['label']} px</td></tr>
<tr><td>Slide counter</td><td>Figtree 400</td><td class=num>{S['counter']} px</td></tr>
<tr><td>Outer margin</td><td>—</td><td class=num>{MARGIN} px</td></tr>
</table>
<p>No handwriting font. Playfair is the only display face; Figtree carries everything else.</p>

<h2>3 · Text limits</h2>
<p>Linted at render time and printed in the build log. Nothing is silently absorbed.</p>
<ul>
<li>Headline {S['headline_words_min']}–{S['headline_words_max']} words, at most
{S['headline_lines_max']} lines</li>
<li>Qualifier one or two short lines. No paragraphs on a cover</li>
<li>One focal message per slide</li>
<li>Interior slides about {S['interior_words_max']} words maximum</li>
</ul>
<p class=warn>If a headline will not fit at the {S['headline_min']}px floor, the renderer reports
<code>COPY TOO LONG: cut the headline</code> rather than shrinking it. Dramatic shrinking means the
copy is wrong, so the tool refuses to hide that.</p>

<h2>4 · Headline formula</h2>
<p><b>familiar topic + unresolved tension.</b> Name the subject and leave the tension open.
Clickbait is useful; vagueness and misleading promises are not.</p>
<ul>
<li>your essay sounds borrowed</li>
<li>common app mistakes</li>
<li>delete these essay phrases</li>
<li>stanford essays: start here</li>
<li>the activities nobody remembers</li>
</ul>

<h2>5 · Colour hierarchy</h2>
<table><tr><th>colour</th><th>hex</th><th>role</th><th>on cream</th><th>peach guard</th></tr>
{rows}</table>
<p>The peach guard is a real check, not a note. Peach and coral carry green well above blue; pink
and true red do not, and it only applies where red is the dominant channel and there is real
chroma. It rejected the original trunk brown <code>#8A6046</code> and correctly rejects poppy,
coral and peach.</p>
<p><b>On photographs:</b> cream type only. Subtle gradient veil for contrast. No red text, and never
an opaque headline panel.<br>
<b>On cream:</b> cardinal or deep-red headline, ink body copy, forest accent, optional pale blush
surface.</p>
<table style="margin-top:14px">
<tr><th>cream-mode level</th><th>colour</th><th>measured</th><th></th></tr>{lvl}</table>

<h2>6 · Cover modes</h2>
<p><b>Photography cover.</b> Full-bleed campus photo, cream Playfair headline, Figtree label and
qualifier, gradient darkening only where the copy sits, no icons.</p>
<div class=row>{figs('render/out/notes-01/01.png', 250, root)}{figs('render/out/notes-01/03.png', 250, root)}{figs('render/out/notes-01/05.png', 250, root)}</div>
<p style="margin-top:20px"><b>Cream editorial cover.</b> Cardinal Playfair headline, ink supporting
copy, forest accent, one optional hand-painted illustration.</p>
<div class=row>{figs('render/out/notes-01/02.png', 250, root)}{figs('render/out/notes-01/04.png', 250, root)}{figs('render/out/notes-01/06.png', 250, root)}</div>
<p class=warn style="margin-top:14px">The veil is solved against every copy element's actual
rectangle, not a fixed band. Solving it against the headline alone left the series label at
3.05:1 while the build still reported a pass.</p>

<h2>7 · Icons</h2>
<p>Flat, filled, hand-painted shapes. Two or three colours, slightly irregular edges, one icon or
small cluster per slide, primarily on cream. Never competing with the headline, and never placed
over a photograph — the renderer drops an icon on a photo slide and logs it.</p>
<div class=row>{figs('brand/icons/*.png', 84, root)}</div>
<div class=row style="margin-top:12px">{figs('brand/trees/*.png', 96, root)}</div>

<h2>8 · Overall test</h2>
<p>At thumbnail size a viewer should immediately see the topic, the curiosity gap, and that it
belongs to Chloe. Here is the deck at 132px, which is roughly a feed thumbnail.</p>
<div class=thumbs>{thumbs('render/out/notes-01/0*.png', root)}</div>

<h2>Campus plates</h2>
<p>Your photography, cropped to {W}&times;{H}.</p>
<div class=row>{figs('brand/plates/*.jpg', 140, root)}</div>
"""
    out = os.path.join(root, "design-system.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    refs = re.findall(r'src="([^"]+)"', html)
    missing = [r for r in refs if not os.path.isfile(os.path.join(root, r))]
    return out, len(refs), missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="chloe")
    a = ap.parse_args()
    root = os.path.abspath(os.path.join(HERE, "..", "..", a.persona))
    out, n, missing = build(root)
    print(f"  wrote {out}")
    print(f"  refs {n}, broken {len(missing)}{'' if not missing else ': ' + str(missing[:4])}")


if __name__ == "__main__":
    main()
