# story-loop — Design

A Hermes-shaped autonomous loop that turns real Reddit posts into original narrated
short-form videos over SFFS gameplay footage, and schedules them to an unbranded
story channel.

## Goal

Produce and publish Reddit-story narration videos — the TikTok/Reels format where a
synthetic voice reads a story over video-game footage with one-word captions — on an
unattended cadence, with a human approval gate for the first few posts only.

The loop is a fourth instance of the Hermes pattern already running in
`GTM/ugc-pipeline` (personas `dani`, `ray`). It keeps that pattern's spine: eight
phases, resumable per-run JSON state, a `guard` module with a kill switch and budget
ledger, and a do-not-touch bracket proving the loop never disturbs pre-existing
scheduled posts.

## Settled decisions

| Decision | Choice | Reason |
|---|---|---|
| Channel identity | **SFFS-adjacent** (revised). No logo or app CTA is *added*, but the gameplay itself carries SFFS marks | Originally specced as unbranded. The supplied footage is a Roblox build of SFFS: the scoreboard shows "Smart Fella" and "smellafella99" and the two-tone brain mascot is on the walls, so the brand is on screen in every frame. Owner reviewed this and chose to keep the footage — the stories are what the viewer is there for. **Consequence carried to Stage 2: the subreddit list must be re-chosen for a brand-adjacent channel; `tifu` and `AmItheAsshole` beside a kids'-brand mascot is the risk this decision accepts.** |
| Story source | Real Reddit posts, LLM-retold into original prose | Authentic story beats without verbatim reuse; dodges copyright gray area and platform "unoriginal content" penalties |
| Autonomy | Fully unattended, behind a self-clearing trust ramp | Owner reviews the first N posts, then the loop graduates itself permanently |
| Code location | Sibling `GTM/story-loop/`, standalone | Zero blast radius on the live `dani`/`ray` loops that post to real brands |
| Opening visual | **Reddit-styled post card** (revised): real subreddit, our retold title, upvote/comment chrome, **no username** | Originally specced as a plain bold-text card on the grounds that Reddit chrome would misrepresent a retelling. Revised on owner instruction: the card is the genre's signature and viewers expect it. The misrepresentation objection is answered by *what the card omits* rather than by dropping the card — the subreddit is factual, the title is our own retelling, and no username appears, so no real person is credited with text they did not write and the originality property protecting monetization is intact. A faithful screenshot (real title + real `u/name`) was declined for exactly that reason. |

## Non-goals

- No SFFS logo, app CTA, or install link is *added* to the frame. (The gameplay footage itself
  carries SFFS marks — see the revised channel-identity decision above.)
- No custom uploader. Scheduling goes through Metricool, which is already paid for and
  already wired. The unofficial TikTok/Instagram uploaders used by comparable
  open-source bots are fragile and their reference project is unmaintained.
- No verbatim narration of anyone's Reddit post.
- No virality prediction. The loop measures what happened; it does not forecast.
- No changes to `GTM/ugc-pipeline`. Not one file.

## Genre conventions (researched, not assumed)

Reddit and web search are both unreachable from the build environment, so the genre was
studied through the configuration defaults of the open-source bots that mass-produce
this format — `elebumm/RedditVideoMakerBot` and its fully-automated fork. Every knob
those projects expose is a choice the format cares about.

| Convention | Value | Adopted |
|---|---|---|
| Canvas | 1080×1920 | yes |
| Background | gameplay; Minecraft is their default | yes — SFFS footage instead |
| Music bed volume | 0.15 under narration | **no** — narration carries it; ducked gameplay audio fills the gaps instead |
| Beat between segments | 0.3 s | yes |
| Overlay opacity | 0.9 | yes, in config |
| Mode | title + body narrated ("storymode") | yes |
| Text reveal | progressive, not one static card | yes |
| Pre-render gates | min comments, NSFW flag, blocked words | yes, extended |
| Captions | one word at a time, CapCut style | yes — the format's primary retention device |
| Watermark | channel name | yes, small and low-opacity |

One number from that research is **rejected**: RedditVideoMakerBot's config claims "200
characters are approximately 50 seconds," which is off by roughly 3× against a normal
150 wpm narration rate. The loop measures its own rate from the first real alignment
response instead. See *Unmeasured* below.

## Architecture

```
GTM/story-loop/                    # CODE
  channels.json                    # registry: name -> {root, blog_id, timezone}
  channel.py                       # resolve() — the personas.py pattern
  loop/
    cycle.py                       # the eight phases
    guard.py                       # kill switch, ledger, run lock, snapshot/verify, probation
    llm.py                         # Opus calls with retry
    retell.py                      # source post -> original script
  source/
    reddit.py                      # OAuth fetch
    gates.py                       # safety + quality, all pre-render
  voice/
    tts.py                         # /with-timestamps -> mp3 + alignment, content-hash cached
    words.py                       # character alignment -> word timings
  render/
    footage.py                      # clip library probe + window selection
    captions.py                     # word timings -> ASS subtitle track
    hook.py                         # opening title card
    compose.py                      # ffmpeg assembly
  post/
    queue.py                        # approval queue + trust ramp
    metricool.py                    # scheduler client

GTM/story-loop/channels/<name>/    # DATA, one dir per channel
  channel.json                     # subs, voice_id, cadence, thresholds, layout
  footage/                         # raw gameplay clips + probed manifest.json
  scripts/                         # retold story pool, one JSON per script
  voice/cache/                     # TTS mp3 + alignment, keyed by content hash
  out/pending/  out/published/     # rendered mp4s
  runs/                            # per-run state JSON
  learn/                           # autonomy.json, learnings.json, ledger, killed list
```

Code and per-channel data are separated because `ugc-pipeline` proved the cost of not
doing it. `channels.json` + `channel.py::resolve()` ship on day one despite there being
one channel: the comments in `ugc-pipeline/personas.py` and `loop/cycle.py` record what
retrofitting multi-tenancy cost there — a hardcoded `character/dani` path leaf that hard-
crashed every non-Dani persona in the `make` phase, and a `score.main([])` call that made
a *dry run* of Ray's cycle overwrite Dani's live learning data. The resolver is ~30 lines.

### Phases

Same order and same names as `ugc-pipeline/loop/cycle.py`.

| Phase | Behaviour |
|---|---|
| `preflight` | kill switch; run lock; resolve channel; assert Reddit + ElevenLabs + Metricool creds; assert fonts and ffmpeg present; discover available TTS models; budget check |
| `snapshot` | record every pre-existing scheduled post — **do-not-touch** |
| `pull_and_score` | Metricool analytics -> per-video scores -> `learn/learnings.json` |
| `generate` | fetch candidates -> gate chain -> retell -> script pool; only when pool < low-water |
| `plan` | deterministic, seeded by `run_id`: which scripts, which footage windows, which slots |
| `experiment` | one A/B dimension per run, rotation seeded by `run_id`; dimensions are hook style, `voice_id`, footage source, caption style (one word vs. short phrase), and target duration band |
| `make` | TTS -> word timings -> render -> queue (in probation) or schedule (graduated) |
| `verify` | prove nothing pre-existing moved — **do-not-touch** |

State is one JSON file per run under `channels/<name>/runs/`, written atomically
(tmp + rename) after every phase. A cycle that dies halfway is resumable by re-running
the same `run_id`.

Determinism boundary, inherited deliberately: **the LLM writes and judges copy; it never
decides what to test.** Dimension rotation, footage window choice, and slot assignment
are all seeded by `run_id`.

### Guard changes from the ugc-pipeline original

Three additions, all forced by video being metered and expensive.

**1. A `tts_chars` budget meter.** The existing ledger counts `llm` and `post`.
ElevenLabs bills per character and `make` is now the expensive phase, so synthesis is
gated on a character budget before the first request. Without it, a runaway loop's first
symptom is an invoice.

**2. TTS output cached by content hash.** Key: `sha256(voice_id + model_id + text)`,
storing `<hash>.mp3` and `<hash>.align.json`. Hermes's resumability property was "a run
that dies halfway does not duplicate posts." With a metered API it must also become "a
run that dies after synthesis does not re-pay for it."

**3. Probation state** (`learn/autonomy.json`) — see *Trust ramp*.

## Unit contracts

### `source/reddit.py`

Fetches candidate posts from the subs listed in `channel.json`.

Anonymous Reddit reads are dead: `GET /r/tifu/top.json` with a browser user-agent
returns **HTTP 403** with a ~190 KB block page. The token endpoint
(`POST /api/v1/access_token`) returns **HTTP 401** to empty credentials, meaning it is
reachable and functioning — so authenticated access is the only path.

Auth strategy, in order:
1. Attempt app-only `client_credentials`. Preferred because it requires no account
   password on disk.
2. If app-only cannot read `/r/<sub>/top`, fall back to the `password` grant with a
   dedicated throwaway account.

Whether app-only reads work for `/r/<sub>/top` is **unverified** — it needs real
credentials. `reddit.py` asserts it explicitly on first run and reports which grant
succeeded rather than failing obscurely.

Returns per candidate: `id`, `subreddit`, `title`, `selftext`, `ups`, `num_comments`,
`over_18`, `permalink`, `created_utc`.

### `source/gates.py`

The gate chain, ordered cheapest-first. **Every gate that can reject runs before
anything that costs money.**

1. **Mechanical** — reject on `over_18`; upvotes below floor; comments below floor;
   `selftext` outside its length band; blocked-word hit; `[removed]`/`[deleted]` body;
   crosspost; non-English.
2. **Dedup** — content hash, plus fuzzy similarity against every previously used story,
   reusing the `gallery.similarity` / `MATCH_THRESHOLD` approach already proven in
   `ugc-pipeline`.
3. **LLM judge** — two questions: is this brand-safe for a channel carrying SFFS
   gameplay, and is it actually a story with a turn rather than a rant? Returns a score;
   reject below threshold.

Only a candidate passing all three reaches `retell.py` (LLM spend), and only a passing
retelling reaches `voice/tts.py` (metered spend).

### `loop/retell.py`

Opus rewrites a gated candidate into original first-person prose: hook in the first
sentence, word count targeted at the configured duration, ending on either a resolution
or a deliberate cliffhanger.

Then a **mechanical originality assertion**: reject the retelling if any 7-word sequence
from the source text survives in it. A prompt asking for originality is a hope; an
n-gram overlap check is a verified property, and it is what stands between the channel
and a monetization strike.

Output: one JSON per script in `channels/<name>/scripts/` — `uid`, `source_id`,
`source_permalink`, `text`, `word_count`, `hook`, `judge_score`, `created`.

### `voice/tts.py`

`POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps`
(verified reachable: `/v1/voices` returns HTTP 200).

Response fields consumed: `audio_base64`, and `normalized_alignment` containing
`characters`, `character_start_times_seconds`, `character_end_times_seconds`.

Model is **discovered at preflight** from `/v1/models`, preferring the narration-quality
tier available to the account. Hardcoding a model name breaks the loop the day the plan
does not include it.

API key resolution copies the pattern already used in
`GTM/sffs-mobile-app/scripts/generate-audio.mjs`: `process.env.ELEVENLABS_API_KEY`,
then `.env.local`, then `.eleven.key`. The key is never written to a rendered artifact
or a run-state file.

Scripts longer than the per-request ceiling are chunked; chunk alignments are stitched
with a cumulative time offset, and each chunk passes the prior chunk's request id via
`previous_request_ids` (the API accepts up to 3) so prosody carries across the seam.

### `voice/words.py`

Pure function. Character alignment -> word timings: group characters on whitespace, take
the first character's start and the last character's end per word.

**Caption from `normalized_alignment`, and time from the same track.** The normalized
track reflects ElevenLabs' own text expansion — `$5` is spoken "five dollars", `Dr.`
becomes "Doctor". Timing from the normalized track while captioning the raw text
desynchronises every word after the first number or abbreviation. Using one track for
both guarantees the word on screen is the word being spoken.

### `render/footage.py`

Probes every clip in `channels/<name>/footage/` with `ffprobe` and writes
`manifest.json` recording **measured** duration, width, height, fps, and audio presence.
The manifest stores what was measured, and the compositor asserts against it rather than
trusting a value it passed in.

Window selection for a story of duration *D*:
- Clip longer than *D*: take a random window **seeded by `run_id`**, so a resumed run
  picks the same footage instead of re-rolling.
- Clip shorter than *D*: concatenate clips until covered.
- Used windows are recorded so two videos do not open on the same footage.

Layout follows from the measured aspect ratio, per clip, with no config change needed:
- Portrait (≥ 9:16): scale and crop to fill 1080×1920.
- Landscape: blurred pillarbox — clip scaled to width over a blurred, blown-up copy of
  itself.

### `render/captions.py`

Word timings -> a transparent caption track, composited with a single `overlay`.

**Not ASS, and not `drawtext`.** The installed ffmpeg (homebrew 8.1.2) is built without
libass *and* without libfreetype: checked against all 481 available filters, `ass`,
`subtitles`, and `drawtext` are all absent. `overlay`, `gblur`, `concat`, `amix`, and
`loudnorm` are present. Rebuilding ffmpeg is a system-level yak shave with an uncertain
outcome; rendering text in Python is not.

Mechanism, using capability `ugc-pipeline` already relies on:

1. Pillow (12.1.1, installed) renders one RGBA PNG per word.
2. A `concat` demuxer manifest gives each PNG the duration of its word.
3. That encodes once to a lossless alpha track (`qtrle`, confirmed available; `prores_ks`
   and `libvpx-vp9` are fallbacks).
4. One `overlay` composites the track onto the gameplay.

One PNG per word rather than one per frame, and one overlay rather than one per word —
a 200-word script stays 200 small images and a single filter.

Font: `TikTokSans-Variable.ttf`, confirmed present in
`GTM/ugc-pipeline/render/fonts/` and copied into `story-loop/assets/fonts/` so the loop
is self-contained. Its variable Weight axis runs 300–900, so captions set weight 900
without needing a second font file. Positioned at 55–60% frame height, inside a safe box
clearing TikTok's bottom caption strip and right-hand action rail.

Rendering captions in Python rather than in ffmpeg also makes them golden-file testable
as images, which a burned-in subtitle filter never is.

### `render/hook.py`

Superseded by `render/post_card.py` as the opening visual, but retained: it still renders
the plain wrapped text card, which `post_card.py` reuses for the title inside the card and
which remains the fallback when a story has no source subreddit.

`Anton-Regular.ttf` (confirmed present, copied into `assets/fonts/`), rendered through the
same Pillow path as captions and carried on the same alpha track, so there is one
compositing mechanism rather than two.

### `render/post_card.py`

The opening visual: a Reddit-styled post card rendered with Pillow onto the same alpha
track. Contents, per the revised decision:

- the real subreddit (`r/tifu`) — factual provenance
- the **retold** title, wrapped, in `Anton-Regular.ttf`
- upvote and comment-count chrome for genre recognition
- **no username, and no avatar identifying a real account**

Reddit's own wordmark and logo are not reproduced; the card is *styled* like a post, not a
copy of Reddit's interface.

### Narration structure (revised)

The story is narrated in **two segments**, which is why the genre's tooling exposes a
segment-gap setting at all:

1. **Title** — narrated while the post card holds the frame. The card's duration is the
   title audio's measured duration, so they end together by construction.
2. `segment_beat_s` (0.3 s) of silence.
3. **Body** — narrated with one-word captions.

Two TTS calls, not one. Each is cached under its own content hash, so re-rendering a story
whose body changed does not re-pay for its unchanged title.

### `render/compose.py`

ffmpeg assembly (`ffmpeg` and `ffprobe` confirmed at `/opt/homebrew/bin/`).

- Video: footage window -> layout filter -> `overlay` of the alpha caption track ->
  watermark.
- Audio: narration at full level over ducked gameplay audio, then `loudnorm` across the
  mix. Default target **-14 LUFS** — a streaming-standard figure, *not* a measured TikTok
  specification, which is why it lives in config.

**There is no music bed.** The genre convention is a lofi track at 0.15, and it is
deliberately dropped: the narration carries the video, and a bed is an asset dependency
the loop would fail on when it is missing. The gameplay audio serves the one function the
bed actually performed — without it *and* without a bed, the 0.3 s beat between segments
is pure digital silence, which reads as a broken file rather than a pause. So gameplay
audio stays in the mix, ducked to a level set in config.

Clips whose probed manifest reports no audio track are mixed against generated silence,
so a silent clip is a quieter video rather than an ffmpeg failure.
- Output: H.264, `yuv420p`, 1080×1920, `+faststart`, AAC, CRF from config.

### `post/queue.py`

`--list`, `--approve <id>`, `--reject <id>`. Approving schedules the video through
Metricool and increments the probation counter. Rejecting adds the script uid to the
killed list so it is never retried.

## Trust ramp

`channels/<name>/learn/autonomy.json`: `{"approved": 0, "threshold": 3,
"graduated": false}`.

While `graduated` is false, `make` renders into `out/pending/` with a review manifest
and schedules **nothing**. When `approved` reaches `threshold`, the loop sets
`graduated: true`, logs the transition loudly, and every later run schedules directly —
fully unattended. `--force-probation` reverts to the gated state.

The kill switch and the do-not-touch snapshot/verify bracket apply in both states.

## Configuration

`channels.json` — registry only:

```json
{"<name>": {"root": "./channels/<name>", "blog_id": "<id-or-null>", "timezone": "<tz>"}}
```

`channels/<name>/channel.json` — everything else: subreddit list, `voice_id`, posts per
day, horizon days, pool low-water mark, gate thresholds (min ups, min comments, length
band, blocked words, judge score floor), target duration, gameplay-audio duck level,
overlay opacity, loudness target, CRF, watermark text, probation threshold.

`blog_id` is required before any scheduling call, following
`personas.require_blog_id()`: a channel with no `blog_id` must refuse to post rather
than fall through to another account.

Gate thresholds start from the genre's published defaults — minimum 20 comments,
NSFW disallowed — and are tuned once `pull_and_score` has real data. Two values must be
supplied at setup with no sensible default: the channel's `blog_id` and the watermark
text (the channel's display name).

## Build order

Four stages, each independently verifiable, ordered so the riskiest unknown is proven
first and nothing external is required until it is actually needed.

1. **One video, end to end, from a hand-written script.** `footage.py` -> `tts.py` ->
   `words.py` -> `captions.py` -> `hook.py` -> `compose.py`. No Reddit, no loop, no
   scheduling. Proves the hard part — that character alignment yields correctly
   synchronised one-word captions over real footage — and measures the TTS rate that
   every later word-count target depends on.
2. **Sourcing.** `reddit.py` -> `gates.py` -> `retell.py`. Resolves the auth-grant
   question and fills the script pool. Verifiable without rendering anything.
3. **The loop.** `channel.py`, `guard.py`, `cycle.py` — all eight phases, run state,
   budget meters, TTS cache, `--dry-run`.
4. **Publishing.** `metricool.py`, `post/queue.py`, the probation state machine, and the
   do-not-touch bracket under live conditions.

Stage 1 is the one that can invalidate the design. If alignment-derived captions drift,
everything downstream changes, so it comes first and it comes with real footage and a
real key.

## Testing

Pure logic, unit-tested with `pytest` (the convention in `ugc-pipeline`):

- `words.py` grouping, from a fixture alignment JSON — including a `$5` / `Dr.` case
  proving the normalized-track decision.
- Every `gates.py` decision, including boundary values.
- The 7-gram originality check, with a deliberately plagiarised retelling.
- Footage window determinism: same `run_id` -> same window.
- Caption safe-box math.
- Probation state machine: gated -> approve ×N -> graduated, and `--force-probation`.

Golden-file image tests for caption and hook PNG rendering — pixel-comparable, which is
the direct payoff of rendering text in Pillow instead of inside a video filter. ffmpeg
integration test against a synthetic `lavfi testsrc` clip — no network, no API key, no
footage required. `--dry-run` exercises the whole chain with a stubbed alignment and
spends nothing.

`preflight` asserts the required ffmpeg filters (`overlay`, `gblur`, `scale`, `crop`,
`concat`, `amix`, `loudnorm`, `anullsrc`, `trim`, `atrim`) and the `qtrle` encoder are
present, and fails loudly naming the missing one. This design was invalidated once
already by assuming a filter existed; the loop should not rediscover that at render time.

## Unmeasured

Carried as first-run assertions, not as invented numbers:

- **TTS characters-per-second.** Sets the retell word-count target. Measured from the
  first real alignment response and written to config.
- **Whether app-only Reddit auth can read `/r/<sub>/top`.** Asserted on first run, with
  password-grant fallback.
- **ElevenLabs plan limits** — which models the key covers, per-request text ceiling,
  monthly character allowance. Discovered at preflight.
- **Per-video cost.** Computable only after one real run.

`pull_and_score` is genuinely inert until published posts have accumulated analytics,
roughly a week or two after launch. The loop measures outcomes; it does not predict them.

## Open item

The gameplay footage location. `render/footage.py` probes and adapts to whatever it
finds — portrait or landscape, long or short — so this does not block implementation.
Drop clips in `channels/<name>/footage/` and run the probe.
