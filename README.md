# content-loop

An autonomous social-content pipeline: it writes posts, gates them, renders them,
schedules them, reads how they did, and changes its own defaults when the evidence
earns it. ~22,000 lines of Python across two layers, 19 test files.

The interesting part isn't the generation. It's the closed loop — and the fact
that the loop is **not** allowed to promote its own defaults without a human.

```
generate ──> gate ──> render ──> schedule ──> score ──> roll up ──> adjust
   ^                                                                  │
   └──────────────── defaults (human-approved) ◄──────────────────────┘
```

## The layers

| Module | Lines | What it does |
|---|---|---|
| `pipeline/loop/` | 1,447 | One autonomous cycle: read the numbers, decide, write, schedule |
| `pipeline/copy/` | 2,614 | Copy atoms — hooks, captions, timing — and a deterministic compliance gate |
| `pipeline/render/` | 2,784 | Compose the image: overlays, carousels, type |
| `pipeline/post/` | 4,060 | Approval queue, upload, schedule, crosspost, dashboards |
| `pipeline/learn/` | 2,506 | Score, join, A/B rollup math, propose default flips |
| `render-layer/` | 5,363 | Channel + voice abstraction over the render step |

## Three design decisions worth pointing at

**The gate has no model in it.** `copy/gates.py` is deterministic — no model calls,
no network. A compliance rule enforced by asking a model is a rule that fails
differently every run. Everything that must *always* hold is a pure function with
unit tests (`copy/test_gates.py`).

**The rollup math is pure.** `learn/rollup.py` does the A/B statistics with no I/O,
no network and no persona resolution, so the part that decides what "worked" can be
tested in isolation (`learn/test_rollup.py`). Every impure concern lives one layer
out, in `learn/score.py` and `learn/track.py`.

**Defaults are human-gated.** `learn/adjust.py` lets the loop change its own
parameters, but `learn/promote.py` only *proposes* a default flip — a person
approves it. An unattended loop that can rewrite its own success criteria will
eventually optimise into nonsense; `loop/guard.py` holds the rest of the rails for
an unattended cycle.

## Running it

```bash
cp pipeline/personas.example.json pipeline/personas.json   # then fill in real ids
python -m pipeline.loop.cycle --persona persona-a --dry-run
pytest                                                      # 19 test files
```

Scheduling goes through [Metricool](https://metricool.com); `post/metricool.py` is
the only place that talks to it. Model calls are funnelled through `loop/llm.py` —
one place, so the model is swappable.

## Note on content

This drove synthetic personas — accounts with a consistent voice and posting
cadence, not real individuals. Persona content, imagery and real account
identifiers are **not** in this repo; `personas.example.json` carries placeholders,
and `personas.json` is gitignored. What's here is the engine.
