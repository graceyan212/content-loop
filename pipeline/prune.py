#!/usr/bin/env python3
"""
prune.py — actually delete the posts you X'd out in the dashboard.

WHY THIS IS A SEPARATE STEP: the dashboard's X marks a post in the browser's
localStorage. That hides it from the dashboard and nothing else — batch.py reads
the markdown files, so a "deleted" post would still get rendered and scheduled.
This is what removes it for real.

    # in the dashboard: X the bad ones, then "copy kill list"
    pbpaste | python3 prune.py --paste
    python3 prune.py --uids 'value-01-meals.md::3' 'ask-and-intro.md::ask-m2'
    python3 prune.py --paste --dry-run      # show what would go, change nothing

A uid is `<file>::<### heading>`, exactly as the dashboard emits it.

Every run writes a timestamped backup of each touched file next to it before
editing, because this deletes hand-written copy and there is no undo in a
markdown file.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import personas


def parse_uids(raw: list[str]) -> dict[str, set[str]]:
    """['file.md::3', ...] -> {'file.md': {'3'}}"""
    out: dict[str, set[str]] = {}
    for u in raw:
        u = u.strip().strip('",[]')
        if "::" not in u:
            print(f"  skipping malformed uid {u!r}", file=sys.stderr)
            continue
        f, i = u.split("::", 1)
        out.setdefault(f.strip(), set()).add(i.strip())
    return out


def prune_file(path: str, ids: set[str], dry_run: bool = False) -> tuple[int, list[str]]:
    """
    Drop the `### <id>` blocks named in `ids`. A block runs from its heading to
    the next `### ` or `## ` heading, or EOF. Header comments are untouched.
    Returns (removed_count, ids_not_found).
    """
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines(keepends=True)

    # index every ### block: id -> (start, end)
    blocks: dict[str, tuple[int, int]] = {}
    starts: list[tuple[int, str]] = []
    for n, ln in enumerate(lines):
        m = re.match(r"^###\s+(.+?)\s*$", ln)
        if m:
            starts.append((n, m.group(1)))
    for k, (n, bid) in enumerate(starts):
        end = len(lines)
        for n2, ln2 in enumerate(lines[n + 1:], start=n + 1):
            if re.match(r"^###?\s+", ln2):
                end = n2
                break
        blocks[bid] = (n, end)

    hit = [(bid, blocks[bid]) for bid in ids if bid in blocks]
    missing = sorted(ids - set(blocks))
    if not hit:
        return 0, missing

    if not dry_run:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.bak-{stamp}")
        drop = set()
        for _, (a, b) in hit:
            drop.update(range(a, b))
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(ln for n, ln in enumerate(lines) if n not in drop)

    for bid, (a, b) in sorted(hit, key=lambda x: x[1][0]):
        head = lines[a].strip()
        body = "".join(lines[a:b])
        m = re.search(r"^(?:title|on-screen):\s*(.+)$", body, re.M)
        label = (m.group(1)[:62] if m else "")
        print(f"  {'would remove' if dry_run else 'removed'} {head:14} {label!r}")
    return len(hit), missing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True, help="persona name from personas.json. REQUIRED: this command posts to a brand or writes persona state, and inferring the wrong one is unrecoverable.",)
    ap.add_argument("--uids", nargs="*", default=[])
    ap.add_argument("--paste", action="store_true",
                    help="read the kill list as JSON on stdin")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    raw = list(a.uids)
    if a.paste:
        blob = sys.stdin.read().strip()
        try:
            raw += json.loads(blob)
        except Exception:
            raw += [x for x in re.split(r"[\s,]+", blob) if "::" in x]
    if not raw:
        raise SystemExit("nothing to prune. Give --uids, or pipe the kill list with --paste.")

    p = personas.resolve(a.persona)
    copy_dir = os.path.join(p["root"], "copy")
    targets = parse_uids(raw)

    total, notfound = 0, []
    for fname, ids in sorted(targets.items()):
        path = os.path.join(copy_dir, fname)
        if not os.path.isfile(path):
            print(f"{fname}: not found under {copy_dir}", file=sys.stderr)
            continue
        print(f"{fname}:")
        n, missing = prune_file(path, ids, a.dry_run)
        total += n
        notfound += [f"{fname}::{m}" for m in missing]

    print(f"\n{'would remove' if a.dry_run else 'removed'} {total} post(s) "
          f"from persona {p['name']!r}")
    if notfound:
        print(f"not found ({len(notfound)}): {', '.join(notfound[:8])}"
              f"{' ...' if len(notfound) > 8 else ''}")
    if total and not a.dry_run:
        print("backups written alongside each file as *.bak-<timestamp>")
        print("now rebuild the dashboard:  python3 gallery.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
