#!/usr/bin/env python3
"""
guard.py — the safety rails for an unattended cycle.

Four independent things, adapted from Hermes's `guardrails.ts` and `cost_governor.py`:

  DO-NOT-TOUCH BRACKET  snapshot every pre-existing scheduled post before the cycle
                        runs, verify afterwards that not one of them moved. This is
                        the single best idea in Hermes: it turns "the run didn't
                        break anything" from a claim into a per-run proof. It matters
                        here specifically because this pipeline's own tooling deletes
                        and recreates posts (Metricool has no in-place edit), so a
                        buggy cycle could quietly eat approved work.

  KILL SWITCH           an env var or a stop-file halts the cycle before it acts.
                        A file, because you can create one over SSM without editing
                        a unit and restarting anything.

  RUN LOCK              flock on a pidfile. Hermes relies on systemd `Type=oneshot`
                        to prevent overlap and has no lock of its own; that protects
                        against the timer double-firing but not against a human
                        running the cycle by hand while the timer's run is live.

  BUDGET CEILING        a per-day cap on posts created and LLM calls, tracked in an
                        append-only ledger. Prevents a loop bug from posting fifty
                        times overnight.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "post")):
    if p not in sys.path:
        sys.path.insert(0, p)

import metricool as mc  # noqa: E402

# ---------------------------------------------------------------------------
# kill switch and lock paths
#
# These were single globals named after Dani: /etc/dani/STOP, /tmp/dani-loop.lock
# and DANI_LOOP_KILL. With one persona that was fine. With three it means stopping
# Ray also stops Dani, and one persona's cycle blocks another's on a lock they have
# no reason to share.
#
# The fix is ADDITIVE, deliberately. ops/DEPLOY.md documents `sudo touch
# /etc/dani/STOP` as the live kill switch on a deployed timer, and the unit runs as
# a `dani` user out of /opt/dani. Renaming a working kill switch to tidy a name is a
# bad trade, so the old paths keep working and keep meaning "stop everything".
# What is new is a per-persona layer underneath.
# ---------------------------------------------------------------------------

# Global halts. Any one of these stops every persona. /etc/dani/STOP is the
# documented one and stays.
GLOBAL_STOP_FILES = ("/etc/dani/STOP", "/etc/sffs/STOP")
GLOBAL_KILL_ENVS = ("DANI_LOOP_KILL", "SFFS_LOOP_KILL")

# Back-compat aliases. Existing callers and docs refer to these names.
STOP_FILE = GLOBAL_STOP_FILES[0]


def stop_file(persona_name: str) -> str:
    """Per-persona stop file. `touch /etc/sffs/ray/STOP` halts Ray only."""
    return os.path.join("/etc/sffs", persona_name, "STOP")


def kill_env(persona_name: str) -> str:
    """Per-persona kill env var, e.g. RAY_LOOP_KILL."""
    return f"{persona_name.upper()}_LOOP_KILL"


def lock_file(persona_name: str | None = None, root: str | None = None) -> str:
    """
    One lock per persona, so two personas' cycles do not block each other.

    PREFER THE ROOT-BASED PATH. `ops/dani-loop.service` sets `PrivateTmp=true`,
    which gives the unit its own /tmp namespace — so `/tmp/dani-loop.lock` inside
    the service and `/tmp/dani-loop.lock` seen by a human running the cycle by
    hand are two DIFFERENT files. The flock has therefore never actually provided
    the protection its docstring claims ("a human running the cycle by hand while
    the timer's run is live"). A lock under the persona root is a real shared path
    on both sides, and the unit already grants ReadWritePaths=/opt/dani.

    The /tmp paths remain as the no-root fallback so existing zero-argument
    callers keep working.
    """
    if root:
        return os.path.join(root, "learn", "loop.lock")
    if persona_name in (None, "", "dani"):
        return "/tmp/dani-loop.lock"
    return f"/tmp/sffs-loop-{persona_name}.lock"


LOCK_FILE = lock_file("dani")

# Per-day ceilings. Deliberately low: this account posts 3/day by design, so
# anything past ~8 means a loop bug, not a good day.
# Raised from 8 when Instagram + Facebook mirroring landed. Each TikTok post now
# creates a second scheduler entry for the Reel mirror, so a 4/day cadence spends 8
# entries a day and would sit exactly on the old ceiling with zero headroom for a
# retry or a catch-up run.
MAX_POSTS_PER_DAY = 14
MAX_LLM_CALLS_PER_DAY = 120


class Halt(Exception):
    """Raised to stop a cycle cleanly. Never caught inside the cycle."""


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------
def _env_set(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0", "false")


def check_kill_switch(persona_name: str | None = None) -> None:
    """Halt if any global switch is set, or this persona's own switch.

    persona_name is optional so the old zero-argument call still works and still
    checks every global. Pass it and the persona-specific switch is checked too.
    """
    for env in GLOBAL_KILL_ENVS:
        if _env_set(env):
            raise Halt(f"{env} is set (global)")
    if persona_name and _env_set(kill_env(persona_name)):
        raise Halt(f"{kill_env(persona_name)} is set")

    paths = list(GLOBAL_STOP_FILES)
    if persona_name:
        paths.append(stop_file(persona_name))
    for path in paths:
        if os.path.exists(path):
            try:
                why = open(path).read().strip()
            except Exception:
                why = ""
            raise Halt(f"stop-file present at {path}" + (f": {why}" if why else ""))


# ---------------------------------------------------------------------------
# run lock
# ---------------------------------------------------------------------------
class RunLock:
    """flock, so a manual run and a timer run cannot interleave."""

    def __init__(self, path: str | None = None, persona_name: str | None = None,
                 root: str | None = None):
        path = path or lock_file(persona_name, root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Halt(f"another cycle already holds {self.path}")
        self.fh.write(f"{os.getpid()}\n{datetime.datetime.now().astimezone().isoformat()}\n")
        self.fh.flush()
        return self

    def __exit__(self, *a):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


# ---------------------------------------------------------------------------
# budget ledger
# ---------------------------------------------------------------------------
def ledger_path(root: str) -> str:
    return os.path.join(root, "learn", "loop-ledger.jsonl")


def ledger_append(root: str, kind: str, n: int = 1, **extra) -> None:
    rec = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "kind": kind, "n": n, **extra}
    os.makedirs(os.path.dirname(ledger_path(root)), exist_ok=True)
    with open(ledger_path(root), "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def spent_today(root: str, kind: str) -> int:
    f = ledger_path(root)
    if not os.path.exists(f):
        return 0
    today = datetime.date.today().isoformat()
    total = 0
    for line in open(f):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == kind and (r.get("at") or "").startswith(today):
            total += int(r.get("n") or 0)
    return total


def check_budget(root: str, kind: str, want: int = 1) -> None:
    cap = {"post": MAX_POSTS_PER_DAY, "llm": MAX_LLM_CALLS_PER_DAY}.get(kind)
    if cap is None:
        return
    used = spent_today(root, kind)
    if used + want > cap:
        raise Halt(f"daily {kind} ceiling reached ({used}/{cap}); refusing to continue")


# ---------------------------------------------------------------------------
# do-not-touch bracket
# ---------------------------------------------------------------------------
def snapshot(creds: dict) -> dict:
    """
    Every scheduled post that exists BEFORE the cycle touches anything.

    Records id -> (scheduled time, first 60 chars of text) so the verify step can
    tell "still there" from "still there but rewritten".
    """
    d0 = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
    d1 = (datetime.date.today() + datetime.timedelta(days=45)).strftime("%Y-%m-%dT00:00:00")
    st, body = mc._req("GET", "/v2/scheduler/posts", creds, params={"start": d0, "end": d1})
    if st != 200:
        raise Halt(f"cannot snapshot before running: scheduler list returned {st}")
    out = {}
    for x in (body if isinstance(body, list) else (body or {}).get("data") or []):
        if not isinstance(x, dict) or not x.get("id"):
            continue
        out[str(x["id"])] = {
            "when": (x.get("publicationDate") or {}).get("dateTime"),
            "head": (x.get("text") or "")[:60],
        }
    return out


def verify(creds: dict, before: dict, expected_new: set[str] | None = None) -> dict:
    """
    Confirm nothing that existed before was deleted or rewritten.

    Returns a report. Raises Halt on violation — loudly, because a silent pass here
    is exactly the failure this whole mechanism exists to catch.
    """
    after = snapshot(creds)
    expected_new = expected_new or set()

    vanished = [pid for pid in before if pid not in after]
    changed = [pid for pid in before
               if pid in after and after[pid] != before[pid]]
    added = [pid for pid in after if pid not in before]
    unexpected = [pid for pid in added if pid not in expected_new]

    report = {"before": len(before), "after": len(after),
              "vanished": vanished, "changed": changed,
              "added": added, "unexpected_added": unexpected}

    if vanished or changed:
        raise Halt(
            "DO-NOT-TOUCH VIOLATION — the cycle altered posts it did not create. "
            f"vanished={vanished} changed={changed}. "
            "Every pre-existing scheduled post must survive a cycle untouched."
        )
    return report
