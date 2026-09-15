# Dani loop — deployment

**Instance** `i-03edf0fbf2e4d601a` · t4g.small · private subnet, no public IP, zero
inbound rules · `aws ssm start-session --target i-03edf0fbf2e4d601a`

## 1. Credentials (the one manual step)

Secrets are never in user-data (readable from instance metadata forever) and never
in the unit file. They go in one root-owned file the `dani` user can read.

```bash
sudo tee /etc/dani/dani.env >/dev/null <<'ENV'
METRICOOL_TOKEN=...
METRICOOL_USER_ID=5094279
METRICOOL_BLOG_ID=1000001
ANTHROPIC_BASE_URL=...
ANTHROPIC_AUTH_TOKEN=...
ENV
sudo chown root:dani /etc/dani/dani.env
sudo chmod 640 /etc/dani/dani.env
```

The two ANTHROPIC vars are the TrueFoundry gateway. Without them the cycle still
runs and still posts — it just cannot generate new copy, and logs the generate
phase as failed. That degradation is deliberate: a gateway outage should not stop
the calendar being filled from the existing library.

## 2. One real cycle before arming anything

```bash
cd /opt/dani/ugc-pipeline
sudo -u dani env $(cat /etc/dani/dani.env | xargs) \
  /usr/local/bin/python3 loop/cycle.py --persona dani --dry-run
```

Expect: preflight ok, snapshot N, pull_and_score, generate (skipped while the pool
is healthy), plan, verify with `vanished: []` and `changed: []`.

## 3. Arm it

```bash
sudo systemctl enable --now dani-loop.timer
systemctl list-timers dani-loop.timer
```

Nothing auto-starts before this. Installing the units and arming them are separate
on purpose.

## 3b. A second persona (Ray)

`ray-loop.service` / `ray-loop.timer` are the same shape, `--persona ray`, fired at
14:05 UTC instead of 13:20 so the two do not contend on one Metricool account.

```bash
sudo -u dani env $(cat /etc/dani/dani.env | xargs) \
  /usr/local/bin/python3 loop/cycle.py --persona ray --dry-run
sudo systemctl enable --now ray-loop.timer
```

**`--persona` is mandatory on `cycle.py`.** It used to default to `dani`. On a box
running two timers, a unit that omits the flag posts to the wrong brand, so the
flag is now required and both units pass it explicitly.

**What is shared and what is not.** Both units read the same
`/etc/dani/dani.env`, because the Metricool `TOKEN` and `USER_ID` are account-wide.
The **brand is not**: `cycle.py` takes `blog_id` from that persona's `persona.json`
via `personas.require_blog_id()`, which refuses to run rather than fall back to the
env file's `METRICOOL_BLOG_ID`. Dani is `1000001`, Ray is `1000002`.

**Stopping one without the other.**

| | |
|---|---|
| stop everything | `sudo touch /etc/dani/STOP`, or `DANI_LOOP_KILL=1` |
| stop Ray only | `sudo touch /etc/sffs/ray/STOP`, or `RAY_LOOP_KILL=1` |
| stop Dani only | `sudo touch /etc/sffs/dani/STOP` |

**The run lock moved out of `/tmp`.** It is now `<persona root>/learn/loop.lock`.
`PrivateTmp=true` gives each unit its own `/tmp`, so the old `/tmp/dani-loop.lock`
was a *different file* from the one a human running the cycle by hand would take —
meaning the lock never actually prevented the overlap its docstring describes. The
root-based path is real on both sides and is covered by
`ReadWritePaths=/opt/dani`. Paths are per-persona, so the two cycles can now run
concurrently; the timers are staggered anyway.

Ray's log is `/var/log/dani/ray-cycle.log`, separate from Dani's `cycle.log`.

## 4. Stop it

```bash
sudo touch /etc/dani/STOP                      # optionally echo a reason into it
```

The next cycle halts at preflight and says why. No unit edit, no restart. Remove
the file to resume. `DANI_LOOP_KILL=1` in the env file does the same thing.

## Operating

```bash
sudo tail -f /var/log/dani/cycle.log           # what it did
systemctl status dani-loop.service             # last run
sudo -u dani cat /opt/dani/danielle/learn/runs/$(date +%F).json   # per-phase record
sudo -u dani tail /opt/dani/danielle/learn/loop-ledger.jsonl      # posts + $ spent
```

## What it does each fire (13:20 UTC / 08:20 Central)

1. **preflight** — kill switch, flock, credentials, budget ceiling
2. **snapshot** — record every pre-existing scheduled post
3. **pull_and_score** — Metricool analytics into ab-database and learnings
4. **generate** — Opus 5 writes new posts, but only when the unposted pool drops
   below 25. Steered by `learn/decisions.json`, the operator's kept/cut record.
5. **plan** — what the calendar is missing over a 3-day horizon
6. **make** — render, upload, schedule, 3/day
7. **verify** — prove not one pre-existing post was altered

## Ceilings

`MAX_POSTS_PER_DAY = 8`, `MAX_LLM_CALLS_PER_DAY = 120` in `loop/guard.py`. The
account posts 3/day by design, so 8 means a loop bug rather than a good day. Cost
per generation call is real, not estimated — the gateway returns `usage.costInUSD`
and it goes in the ledger. Measured: ~$0.09 for 8 posts.

## Known gaps

- The instance reuses `hermes-sffs-s3-profile` because `iam:CreateRole` is denied by
  the `InternSandboxBoundary`. It therefore carries RW on `s3://hermes-sffs-media`
  that it does not need. Tightening requires an admin to mint an SSM-only role.
- Opus 5 bills reasoning tokens invisibly. A too-small `max_tokens` returns EMPTY
  content rather than truncated content, and it is intermittent. `llm.MIN_TOKENS`
  floors every call at 512 and an empty reply raises rather than shipping silence.
- The loop AUTO-SCHEDULES live posts. The do-not-touch bracket means it cannot alter
  a post it did not create, but it can create posts nobody has reviewed. The
  approval queue is a veto, not a gate.
