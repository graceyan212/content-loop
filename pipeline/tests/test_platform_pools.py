"""
Platform pools must never be unioned. TikTok "views" and Instagram "reach" are
different measurements, so a median over both is a number with no referent — and
that failure is invisible, because every count goes UP when you mix them.

Run: python3 tests/test_platform_pools.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "learn"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import adjust  # noqa: E402
import score   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


def mkroot(db, defaults):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "learn"))
    json.dump(db, open(os.path.join(d, "learn", "ab-database.json"), "w"))
    json.dump({"defaults": defaults}, open(os.path.join(d, "learn", "defaults.json"), "w"))
    return d


def tk(arm, comments, views=1000):
    return {"variant": {"type": arm}, "variant_provenance": "assigned",
            "metrics": {"views": views, "comments": comments, "likes": 10}}


def ig(arm, comments, reach=100, saves=None, completion=None):
    m = {"views": reach, "comments": comments, "likes": 10}
    if saves is not None:
        m["saves"] = saves
    if completion is not None:
        m["completion"] = completion
    return {"platform": "instagram", "variant": {"type": arm},
            "variant_provenance": "assigned", "metrics": m}


print("platform pools")

# TikTok decides on comments and says ask leads. Instagram decides on completion and
# says value wins. If the pools leak, one platform's rows shift the other's verdict.
db = {"posts": [tk("ask", 20) for _ in range(6)] + [tk("value", 1) for _ in range(6)],
      "ig_posts": [ig("ask", 1, completion=0.50) for _ in range(6)]
                + [ig("value", 1, completion=0.75) for _ in range(6)]}
root = mkroot(db, {"type": "ask"})

_, applied_tk, _, skipped_tk = adjust.evaluate(root, "tiktok")
check("tiktok pool keeps its own verdict (incumbent ask leads)",
      not applied_tk and any("still leads" in w for _, w in skipped_tk), skipped_tk)

_, ap_ig, pr_ig, _ = adjust.evaluate(root, "instagram")
check("instagram pool reaches the OPPOSITE verdict on the same file",
      len(pr_ig) == 1 and pr_ig[0]["to"] == "value", (ap_ig, pr_ig))
check("instagram n comes only from ig_posts (6, not 12)",
      pr_ig and pr_ig[0]["n_challenger"] == 6,
      pr_ig[0]["n_challenger"] if pr_ig else None)

# A ratio metric may PROPOSE but must never APPLY on its own — completion is not an
# operator-approved decision metric, and auto-applying would let a metric nobody
# signed off on rewrite the content strategy.
check("a completion win is PROPOSED, never APPLIED", not ap_ig and len(pr_ig) == 1,
      (ap_ig, pr_ig))

# The absolute bar, not the 50% relative one. 0.50 -> 0.58 is +16% relative, which
# would clear MIN_LIFT if the wrong bar were applied, but only +0.08 absolute.
near = mkroot({"ig_posts": [ig("ask", 1, completion=0.50) for _ in range(6)]
                         + [ig("value", 1, completion=0.58) for _ in range(6)]},
              {"type": "ask"})
_, ap, pr, sk = adjust.evaluate(near, "instagram")
check("a ratio gain under the absolute floor is held, not proposed",
      not ap and not pr and any("absolute bar" in w for _, w in sk), (ap, pr, sk))

# THE DEAD-METRIC TRAP. Instagram rows with no completion at all must yield no
# verdict rather than silently ranking every arm at zero — that is what made the
# first Instagram run print six confident "incumbent still leads" lines from a pool
# where every arm tied at 0.0.
nocp = mkroot({"ig_posts": [ig("ask", 1) for _ in range(6)]
                         + [ig("value", 30) for _ in range(6)]}, {"type": "ask"})
_, ap, pr, sk = adjust.evaluate(nocp, "instagram")
check("arms with no value for the decision metric yield no verdict",
      not ap and not pr and any("no arm has a completion value" in w for _, w in sk),
      (ap, pr, sk))

# A platform with no rows must report nothing, not silently fall back to the other.
empty = mkroot({"posts": [tk("ask", 20) for _ in range(6)]}, {"type": "ask"})
_, ap, pr, sk = adjust.evaluate(empty, "instagram")
check("a platform with no rows yields no verdict", not ap and not pr, (ap, pr))

# saves / completion: absent on TikTok, and absence must not read as zero.
st = adjust.arm_stats([tk("ask", 20)], "type")
check("tiktok rows carry no saves key", "saves" not in st[("ask", "assigned")], st)
st = adjust.arm_stats([ig("ask", 2, saves=5, completion=0.8)], "type")
row = st[("ask", "assigned")]
check("instagram rows carry saves and completion",
      "saves" in row and row["completion"] == 0.8, row)
check("saves are rated per 1k, not raw", row["saves"] == 50.0, row.get("saves"))

# ingest_instagram must be idempotent — the daily cycle calls it every run.
reels = [{"reelId": "r1", "content": "hello world", "reach": 100, "likes": 3,
          "comments": 1, "saved": 2, "shares": 0, "averageWatchTime": 4,
          "durationSeconds": 8, "publishedAt": {"dateTime": "2026-08-13T20:00:00"}}]
d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "learn"))
db2 = {}
a1 = score.ingest_instagram(db2, reels, d, "2026-08-14")
a2 = score.ingest_instagram(db2, reels, d, "2026-08-15")
check("first ingest adds", a1 == (1, 0), a1)
check("second ingest refreshes, does not duplicate",
      a2 == (0, 1) and len(db2["ig_posts"]) == 1, (a2, len(db2.get("ig_posts", []))))
check("completion derived from watch/duration",
      db2["ig_posts"][0]["metrics"]["completion"] == 0.5,
      db2["ig_posts"][0]["metrics"]["completion"])
check("ig rows are tagged with their platform",
      db2["ig_posts"][0]["platform"] == "instagram")
check("ig rows never land in db['posts']", "posts" not in db2, list(db2))

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all platform-pool checks passed'}")
sys.exit(1 if fails else 0)
