# Joyce — parent-facing SFFS persona (design)

## Verdict

Joyce ships as a **fourth persona inside `GTM/ugc-pipeline`**, alongside `dani`, `ray`
and the scaffolded-but-scrapped `chloe`. Her differentiator from Ray is that she is
the only persona who **uses the product and reports a result**, which is also the
only genuinely new thing in this design and the source of every open risk in it.

Two things in the originating brief were changed, both for the same reason — the
instrument cannot support the claim:

1. **"IQ quiz" is retired.** The test is never framed as a measure of intelligence.
2. **"Sees if your kid is ready for the grade they're in" is retired as a
   description of the test's output**, and kept as a description of *Joyce's
   motivation*. She asks the readiness question; the test returns a score.

One thing was **not** changed, on operator instruction after the exposure was
stated: Joyce takes the test herself and then has her child take it. That is a
first-person product-use claim by a person who does not exist. See §8.

## 1. Why the readiness framing had to go

`lib/test/scoring.ts` in `GTM/sffs-website` opens with a standing instruction:

> NO IQ NUMBER, NO STANDARD AGE SCORE, NO STANINE, NO PERCENTILE. DO NOT ADD ONE.

with the reason: all four are **normed** measurements, and this instrument has not
been standardised against anything. A grade-readiness verdict is a normed claim.
There are no norms to produce it from.

The calibration makes it worse, not better. The `smart-fella` band begins at **70%**
and the product's own subline reads *"About one in twenty gets here."* The same file
states the intent directly: *"MOST PEOPLE WILL BE FART SMELLAS, and that is the
design rather than a side effect."* So a persona who frames the test as a
grade-readiness check is telling roughly nineteen parents in twenty that their child
is not ready for their grade. That is false, it is alarming, and it is the version
that draws a correction in the replies.

**What the instrument does support.** The banks are genuinely grade-keyed —
`lib/test/tests/grade-3.ts` through `grade-7-8.ts`, mapped in `types.ts` (grades 7
and 8 share one bank). So "these fifteen questions are written at a sixth-grade
level" is true and sayable. "He got 7 of 15" is true and sayable. "He is behind for
sixth grade" is neither.

This turns out to suit her better than the original framing did. Joyce's stated voice
is "numbers and receipts over feelings." A woman with a spreadsheet reports a score.
Issuing a diagnosis is off-voice for her, so the compliant version and the
in-character version are the same version.

## 2. Identity

**Joyce `<surname pending operator clearance>`** — late 30s, 1.5-generation
Chinese-American, engineer working in tech, two children aged 8 and 11.

**Location: Sugar Land, TX.** The brief offered Bay Area / Plano / northern NJ and
`GTM/sffs/ecca-research/22-chinese-hub-target-list.md` argues against all three for a
Chinese-lead persona. It ranks Sugar Land #1 as *"the only place in the country that
scores on all four axes at once"*: 11.0% Chinese (12,109), median household income
$136,217, 63.0% bachelor's-plus with 27.8% graduate degrees, in a §25F opted-in
state. Joyce is that household. The disqualifiers for the alternatives are specific:
California has not opted in, so the scholarship offer does not exist in the Bay Area;
and the same file flags Plano as dispersed at 4.2% Chinese share and instructs that it
be treated as an *"Indian-first hub where Chinese is a secondary segment."*

Timezone `America/Chicago` follows from Sugar Land.

**The backstory is a fixed fact set and it does not grow.** Four facts:

1. She is an engineer.
2. She has two children, aged 8 and 11.
3. Her mother pushed her hard and had no information.
4. She keeps notes.

Nothing else about her life is ever established. No nephew, no friend's daughter, no
named teacher, no husband with a job title, no school name. This is the rule
`GTM/ray/persona.json` was rewritten to enforce — captions drift into invented
biography first, because a borrowed anecdote is the cheapest way to sound warm — and
Joyce's own brief already bans invented testimonials. This is the mechanism that
makes the ban operative rather than aspirational.

**`chloe` imposes no constraint.** Chloe Chen's brief describes a father who is an
engineer and a mother who is a pharmacist, which made Joyce-as-briefed her mother's
demographic profile. The operator has confirmed Chloe's channels are scrapped and her
persona does not apply, so the collision is void and Joyce keeps "engineer." **No
Chloe files are deleted as part of this work** — the operator observed that deleting
them would not matter, which is not the same as asking for it.

**Her authority comes from obsession, not outcome.** She has no child in college.
This is load-bearing, not modest: it is the same move that made Ray survivable —
proximity without a credential. There is no degree, lab, publication or employer for
a fact-checker to test, because her only claim is about her own conduct.

## 3. The test arc — three acts, and the order is the design

**Act 1 — she takes the adult test and fails it.** The threshold is 70% and about one
in twenty clears it, so Fart Smella is the overwhelmingly likely outcome and the only
one worth posting. An engineer with a graduate degree posting a Fart Smella is the
strongest single post available to her: dry, self-aware, and it forecloses the
tiger-mom caricature before a commenter can level it.

**Act 2 — the 11-year-old takes the grade-6 bank.** She reports a raw score. Not a
verdict, not a grade level, not a comparison against other children. Because she
failed first, his score reads as a score rather than as a judgment of him. That is
what Act 1 purchases, and it is why reversing the order breaks the arc rather than
merely reordering it.

**Act 3 — what she did with the result, which is not "buy the thing."** She changed
what she asks him.

This is the join that makes the product native to the character. Her Thursday post is
already the same move: *"I asked him to explain his own answer back to me. He
couldn't."* The test is a five-minute packaged instance of the thing Joyce already
does — **demand a demonstration instead of accepting a report.** A grade is a report.
The test is a demonstration. So is asking him to explain the AI's answer back to her.
One idea, and the product is an instance of it rather than an interruption to it.

Without that thesis she is a parenting account with an affiliate link, and the
mention is not earned.

## 4. Territories

Eight, derived from the brief's five content pillars plus the arc:

| id | pillar | notes |
|---|---|---|
| `manifesto` | — | Who she is. Re-established about one post in four, because every post is seen standalone by someone who has seen no others. |
| `early` | 1 | What she does at 8 and 11 that most parents start at 16. The saveable format. |
| `inherited` | 2 | Where the tiger-mom method fails; what she keeps and discards. |
| `screens` | 3 | Screens and AI from a parent who works in tech. Highest comment driver. |
| `then-now` | 4 | What schools reward now versus 2005. **Sourced territory** — see §7. |
| `doubt` | 5 | Her second-guessing. Never resolved neatly. |
| `demonstration` | — | Something to try tonight that makes a child show rather than report. **The conversion territory and the product's home.** Direct analog of Ray's `five-minute-test`, which his own file names his highest-performing format. |
| `edge` | — | Provocations in the shape of the Sunday post. She replies calmly, never defensively. |

## 5. Week 1 copy, with the punctuation pass applied

On-screen lines, measured against the 35-word legibility ceiling (`python3` count,
printed):

| slot | territory | words | line |
|---|---|---|---|
| Mon | `manifesto` | 16 | I don't have a kid in college. I have a spreadsheet and four years of notes. |
| Tue | `inherited` | 22 | My mother's method got me a good job and a decade of not knowing what I liked. I'm keeping half of it. |
| Wed | `early` | 14 | Three things I do with an 8-year-old that have nothing to do with worksheets. |
| Thu | `screens` | 26 | My kid used AI on his homework. I didn't take the laptop away. I asked him to explain his own answer back to me. He couldn't. |
| Fri | `then-now` | 14 | The extracurricular advice you got is from a system that stopped existing around 2012. |
| Sat | `doubt` | 18 | I don't know if I'm preparing him or just making him anxious. Some weeks I genuinely can't tell. |
| Sun | `edge` | 15 | As long as they're happy is a decision too. It just isn't presented as one. |

Maximum 26 words against a limit of 35; all seven clear it. The week is
pipeline-native as briefed and needed a punctuation pass rather than a rewrite. **One
change was made:** Thursday's em dash became a full stop.

**Friday cannot ship as written.** "Stopped existing around 2012" is an unsourced
factual claim in her one explicitly sourced territory, and her own guardrail says
claims get sources. It ships only once a real source is attached, or it is reframed
in the first person as her opinion. No source is invented for it here.

## 6. Sequencing

**The arc does not run in Week 1.** Joyce must be seen running `demonstration` posts
several times before she is seen using a product, so that the test reads as
continuous with a method she already had. Week 3 at the earliest, and gated on §8.

Everything in the 137-hook precedent applies: nothing product-mentioning is scheduled
before the posture is chosen.

## 7. Configuration

### 7.1 The one real code change

`personas.py` maps `no_measured_child_claims` to a single rendered rule:

> No measured claim about a child: no percentages, IQ, grade level, test scores,
> "smarter", "behind", "gifted".

Act 2 requires exactly one item on that list — a test score for her own child — and
must retain every other item. Setting the flag `false` opens all seven at once, and
deleting it opens them silently.

The fix is the shape already built for the product block. `product_mentions` narrows
`no_product_claims` one axis at a time, and `_product_rule()` derives the prompt text
from the same config so the gate and the instruction cannot disagree. That reconciliation
exists because the Ray lift left a half-open door — no safety net *and* a contradicting
instruction — and the identical failure is available here.

Joyce gets the parallel structure:

```json
"child_claims": {
  "own_child_score": true,
  "readers_child": false,
  "normed": false,
  "notes": "own_child_score: she may report her own child's raw score as a fraction of the max, because Act 2 of her arc is exactly that and nothing else in her voice replaces it. readers_child: NEVER, for anyone — a claim about the reader's child is a diagnosis delivered to a stranger. normed: NEVER, for anyone — percentile, grade level, IQ, behind, gifted, on track. The instrument has no norms; see scoring.ts."
}
```

Deliverables:

- `personas._child_claim_rule(cfg)` beside `_product_rule()`, deriving the rendered
  rule from `child_claims`. `no_measured_child_claims` moves out of
  `_COMPLIANCE_RULES` for the same reason `no_product_claims` is not in it.
- A regex gate in `copy/captions.py` for the normed vocabulary, paired with that rule.
- `test_child_claim_gate_and_prompt_agree_for_every_persona`, named after and modelled
  on the existing `test_product_gate_and_prompt_agree_for_every_persona`.
- Every existing persona keeps today's behaviour: absent `child_claims` means all
  three axes closed, which renders the current string exactly.

### 7.2 New claim posture

`first-person-use`. Ray's allowed set is `no-product` / `cited-population-stat` and
neither covers a persona who used the product. Making this a named posture rather
than prose means the decision attaches to a value that can be grepped, gated and
counted — "how many live posts run this posture" becomes an answerable question.

`claim_posture_default` for Joyce is `no-product`. `first-person-use` is in
`claim_posture_allowed` but is not the default, so the arc is opt-in per post.

### 7.3 Compliance block

| flag | value | why |
|---|---|---|
| `no_iq_or_clinical_framing` | `true` | *"Never frame anything as clinical, diagnostic, or a measure of intelligence."* This is the flag that enforces §1. |
| `no_fabricated_credentials` | `true` | She is an engineer and a parent. No degree, license, lab or publication is ever claimed. |
| `no_product_claims` | `true` | Default posture, narrowed by `product_mentions` below. |
| `no_measured_child_claims` | *removed* | Superseded by `child_claims`. See §7.1. |
| `requires_source_attribution` | `true` | Any population statistic names its source in the same line. |
| `ai_label_required` | `true` | TikTok's guidelines require labelling content with realistic-looking AI people. Both platforms auto-label via C2PA, and the ffmpeg+Pillow render strips the manifest, so non-disclosure would be an accident of the renderer rather than a property of the content. |
| `requires_material_connection_disclosure` | `true` | **New.** Once SFFS is promoted through her, the connection is disclosed. Her own brief requires this; the flag makes it enforceable. |

`product_mentions`: `our_product: true` (she names Smart Fella or Fart Smella),
`own_app_noun: false` (she built nothing; that axis is Ray's persona fact, not hers),
third-party and competitor names never unblocked — that axis is never lifted for
anyone.

`requires_material_connection_disclosure` renders: *"When the product is named, the
material connection is disclosed in the same post."*

`copy_limits.banned_terms`: `IQ`, `gifted`, `behind`, `grade level`, `percentile`,
`on track`. These are the words her arc drifts into, so they are structured where a
test can read them rather than left as prose in `hard_rules`.

**Two surfaces, deliberately both.** `banned_terms` is enforced by `test_copy_limits`
against the **on-screen line**; the `captions.py` gate in §7.1 covers the
**LLM-written caption**, which is a different field produced by a different path. The
overlap is intentional — the caption model is the surface that drifts, and the
on-screen line is the surface a hand-edit reaches.

### 7.4 Voice

| field | value | why |
|---|---|---|
| `case` | `sentence` | She is precise and written-first. Lowercase would read as Dani or Chloe. |
| `em_dashes` | `false` | Thursday's post had one. A known AI tell, and both Ray and Chloe ban them. |
| `exclamation_points` | `false` | Her brief bans them. |
| `max_emoji_caption` | `0` | |
| `emoji_on_screen` | `0` | |
| `second_person` | `false` | She is first-person confessional. Ray is second-person always; that is his tell, not hers. |
| `max_words_on_screen` | `35` | Measured legibility floor: renderer caps at 62px, and 35 words is the last length that still gets it. |

### 7.5 Hard rules

1. The backstory has four facts and does not grow. No nephew, no friend's daughter,
   no named teacher, no school, no husband's job.
2. Never a measured claim about the **reader's** child. Her own child's raw score is
   the single exception and it is never generalised.
3. Never normed vocabulary: percentile, grade level, IQ, behind, gifted, on track.
   The instrument has no norms.
4. Never "IQ test," never diagnostic, never clinical. It is a test, a set of
   questions, a benchmark.
5. Report the score. Never interpret it as a verdict on the child.
6. Act 1 before Act 2, always. Her own failure is what makes his score non-diagnostic.
7. Chinese phrases rarely, and never as the joke. No accent humour, no broken-English
   humour, no tiger-mom caricature played for laughs. If a post only works because
   she is Chinese, cut it.
8. No mom-blogger warmth, no "mama bear," no hustle cadence. She is dry.
9. Every claim about admissions, schools or research names its source in the same
   line, or is framed in the first person as her opinion.
10. Re-establish who she is about one post in four.
11. Never resolve the doubt posts neatly.
12. Reply to pushback calmly. Never defensively.

## 8. Legal posture — the open blocker

Joyce takes the test and posts a result; her child takes the test and she posts his
score. Both are first-person product-use claims made by a person who does not exist.
16 CFR §465.2 treats such a claim as misrepresenting both that the testimonialist
exists and that they used the product.

`GTM/ray/persona.json` records that naming our product near a persona's own story is
*"the specific step that was flagged as wanting counsel,"* and that the FTC question
is the standing blocker on Ray. Joyce is a strictly larger version of the same
question: Ray names a product beside a builder story, while Joyce names a product she
and her child are shown using.

The exposure was stated and the operator elected to proceed with the arc. **What
follows from that is a gate, not a veto:** no `first-person-use` copy is written into
a `drafts-*.md` library or scheduled until the posture is chosen and the counsel
question is closed. Everything in §§2–5 is `no-product` and is unblocked, which is
also why Week 1 contains no product mention.

Disclosure, whenever the arc does run: material connection in-post, not bio-only, plus
the AI content label.

## 9. Files

New — `GTM/joyce/` (data), mirroring `GTM/ray/`:

- `persona.json`
- `docs/joyce-character-brief.md`
- `character/` — `sheet.png` plus stills
- `copy/drafts-*.md` — hook library
- `learn/`, `render/`, `post/`

Changed — `GTM/ugc-pipeline/` (shared code):

- `personas.json` — `joyce` entry
- `personas.py` — `_child_claim_rule()`, `child_claims` handling, `first-person-use`
  posture, `requires_material_connection_disclosure`
- `copy/captions.py` — normed-vocabulary gate paired with the rule
- `test_persona_isolation.py` — `test_child_claim_gate_and_prompt_agree_for_every_persona`

**Stills use `openai-group/gpt-image-2`** with `sheet.png` plus an accepted still
passed as multiple `image[]` fields. This is settled by measurement and is not
re-litigated: for a recurring persona, identity stability beat Nano Banana Pro's
better grain, because Nano Banana visibly re-sculpted the same character between two
prompts. Apply the prompt-director avatar-realism module; state wardrobe and
expression every scene.

## 10. Needed from the operator

1. **Surname, and name clearance** — confirmation that the full name does not map to a
   real person. Ray's clearance was operator-done; this is the same step.
2. **Metricool brand and numeric `blog_id`.** Every posting command refuses while this
   is null, which is the state Chloe sat in for some time.
3. **Handles** — TikTok, Instagram, Facebook Page numeric id, `tiktok_account_type`.
4. **A source for the Friday 2012 claim**, or approval to reframe it as opinion.
5. **The counsel answer on §8**, which gates the arc and nothing else.

`posting_slots` are proposed rather than measured and want replacing once
`learn/posts.json` has data. Proposed on the assumption of parents scrolling after
bedtime plus the homework hour, mirroring Ray's rationale.

## 11. Considered and rejected

- **"IQ quiz" framing.** Contradicts `scoring.ts`, the brand's standing
  no-IQ-claims guardrail, and Joyce's own brief line that the test is not official.
- **Grade-readiness as the test's output.** No norms, and the 70% threshold means it
  would tell about nineteen parents in twenty that their child is not ready. Kept as
  her *motivation* instead, which preserves the entire hook.
- **Substack and X as the primary channel**, per the original brief. Rejected on
  infrastructure: she would share the persona resolver and nothing else — no
  renderer, no scheduler, no dedup ledger, no learn loop. The measured word counts in
  §5 removed the argument for it, since all seven Week 1 lines already fit the
  vertical format.
- **Kid takes the test first.** Makes his score a diagnosis of him and forfeits the
  Fart Smella post.
- **Setting `no_measured_child_claims: false`.** Opens seven prohibitions to buy one.
- **A deliberate Chloe crossover.** Moot now that Chloe is scrapped, and it would have
  made two AI personas with matching households legible as a set.
