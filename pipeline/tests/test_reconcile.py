"""
reconcile_assignments — the pass that lets the loop connect what it CHOSE to what
HAPPENED. Every assertion here corresponds to a way the loop was silently broken.

Run: python3 tests/test_reconcile.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "learn"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import score  # noqa: E402


def _root(assignments):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "learn"), exist_ok=True)
    with open(os.path.join(d, "learn", "assignments.json"), "w") as fh:
        json.dump({"assignments": assignments}, fh)
    return d


CAP = "Send this to someone who is also holding all of it. She probably has not been told"
ASSIGN = {"post_id": "1", "uid": "f.md::ct-04", "arm": "send",
          "dimension_under_test": "cta", "variant": {"cta": "send"},
          "rendered": {"screen_style": "headline", "text_color": "white",
                       "actual_type": "ask", "slot": "lunch"},
          "caption_head": CAP[:70]}
INFERRED = {"slot": "pickup", "density": "short", "type": "other", "audio": "unknown"}

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        fails.append(name)


print("reconcile_assignments")

# 1. THE CORE BUG. An inferred row with a matching assignment must be upgraded.
db = {"posts": [{"caption": CAP + " yet.", "variant": dict(INFERRED),
                 "variant_provenance": "inferred"}]}
n = score.reconcile_assignments(db, _root([ASSIGN]))
row = db["posts"][0]
check("upgrades a matching inferred row", n == 1 and row["variant_provenance"] == "assigned",
      f"n={n} prov={row.get('variant_provenance')}")
check("carries the axis under test", row["variant"].get("cta") == "send", row["variant"])

# 2. NO LAUNDERING. The inferred slot said pickup; the render said lunch. If the
#    inferred value survived, adjust.py could flip `slot` on a reverse-engineered
#    guess — the exact confusion provenance exists to prevent.
check("render-time slot wins over the inferred guess", row["variant"].get("slot") == "lunch",
      row["variant"].get("slot"))
check("inferred-only axes are NOT promoted",
      "density" not in row["variant"] and "audio" not in row["variant"], row["variant"])
check("inferred guesses are kept, not destroyed",
      row.get("variant_inferred") == INFERRED, row.get("variant_inferred"))

# 3. NEVER DOWNGRADE. A row a human set by hand outranks any automated match.
for prov in ("assigned", "manual"):
    db = {"posts": [{"caption": CAP + " yet.", "variant": {"type": "joke"},
                     "variant_provenance": prov}]}
    n = score.reconcile_assignments(db, _root([ASSIGN]))
    check(f"leaves {prov} rows alone",
          n == 0 and db["posts"][0]["variant"] == {"type": "joke"}, db["posts"][0])

# 4. NO FALSE MATCHES. A different caption must not pick up someone else's arm.
db = {"posts": [{"caption": "Completely unrelated post about a dishwasher.",
                 "variant": dict(INFERRED), "variant_provenance": "inferred"}]}
n = score.reconcile_assignments(db, _root([ASSIGN]))
check("does not match an unrelated caption",
      n == 0 and db["posts"][0]["variant_provenance"] == "inferred", f"n={n}")

# 5. THE ORIGINAL SILENT FAILURE. An assignment with no caption_head must match
#    NOTHING rather than matching everything via an empty-string prefix.
blank = dict(ASSIGN)
blank.pop("caption_head")
db = {"posts": [{"caption": CAP, "variant": dict(INFERRED), "variant_provenance": "inferred"}]}
n = score.reconcile_assignments(db, _root([blank]))
check("an assignment with no caption_head matches nothing", n == 0, f"n={n}")

# 6. Missing file must be a no-op, not a crash — this runs inside the daily cycle.
db = {"posts": [{"caption": CAP, "variant": {}, "variant_provenance": "inferred"}]}
check("missing assignments.json is a no-op",
      score.reconcile_assignments(db, tempfile.mkdtemp()) == 0)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all reconcile checks passed'}")
sys.exit(1 if fails else 0)
