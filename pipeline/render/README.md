# render/ — text-over-photo vertical renderer

Still photo + hook text → a flattened **1080x1920 PNG + JPEG** (TikTok Photo post,
the default) and/or a **1080x1920, silent, H.264 MP4** with the text burned in and
a slow Ken Burns push on the photo.

Two files:

| file | job |
| --- | --- |
| [`overlay.py`](overlay.py) | text → transparent 1080x1920 RGBA PNG (Pillow). Four styles, word wrap, autofit, safe zone, optional "Dramatization" tag. |
| [`compose.py`](compose.py) | photo + overlay PNG → PNG/JPEG (Pillow) and/or MP4 (ffmpeg). Cover-fit, Ken Burns, composite, encode, self-verify. |

## `--format png` is the default

The account posts **TikTok Photo posts**, and photo-mode carousels measured +81%
engagement / +82% likes vs video on TikTok (−33% shares), so a still is the
default deliverable.

The PNG path is pure Pillow — cover-fit the photo, `alpha_composite` the overlay,
flatten, save — and **never invokes ffmpeg, not even ffprobe**. It is verified to
work with ffmpeg absent from `PATH` entirely. Pillow already knows the exact pixel
dimensions, so there is nothing to probe. Do not add an ffmpeg shell-out to it.

A q92 JPEG sibling (`4:4:4`, progressive) is written next to every PNG because
some upload paths prefer JPEG; `--no-jpeg` skips it. Expect roughly **2.2–2.9 MB
PNG vs 0.4–0.7 MB JPEG** for a photographic still at this size (~5x smaller).

`--format mp4` and `--format both` keep the video path byte-for-byte as it was,
including the colour-range conversion described below.

Fonts live in `fonts/` (Anton + DM Sans, auto-downloaded from Google Fonts on
first run and validated as real TTFs).

---

## Why the text is not drawn by ffmpeg

This machine's ffmpeg is built **without libfreetype**, so the `drawtext` filter
does not exist:

```
$ ffmpeg -filters | grep -c drawtext
0
```

So all type is rasterized by Pillow into a transparent PNG and composited with
ffmpeg's `overlay` filter. Do not try to reintroduce `drawtext`.

---

## Run it

```bash
cd /Users/graceyan/Desktop/alpha/GTM/danielle/render

# TikTok Photo post (default): writes out/post-001.png + out/post-001.jpg
python3 compose.py \
  --still ../character/dani/stills/01-car-selfie.png \
  --text "i sat in the car in the garage for eleven minutes before i went inside on thursday" \
  --out out/post-001.png \
  --safe-zone 72,720,936,840

# video
python3 compose.py \
  --still ../character/dani/stills/01-car-selfie.png \
  --text "I hid in the minivan for 45 minutes. No regrets." \
  --format mp4 \
  --duration 8 \
  --out out/post-001.mp4 \
  --safe-zone 90,1000,900,560 \
  --dramatization
```

Prints a JSON report: the written paths and their byte sizes, the fitted font
size, the wrapped lines, the drawn bbox, whether it fit the safe zone, and — for
the mp4 arms — the exact ffmpeg command and the ffprobe result.

### Flags

| flag | default | notes |
| --- | --- | --- |
| `--still` | required | any size/aspect; cover-fit to 1080x1920 |
| `--text` | — | the hook. Use `--overlay-png` instead to supply your own PNG |
| `--overlay-png` | — | a pre-made transparent 1080x1920 PNG; skips overlay.py |
| `--format` | `png` | `png` \| `mp4` \| `both`. `png` never runs ffmpeg |
| `--no-jpeg` | off | skip the q92 JPEG sibling on the png arm |
| `--style` | `tiktok-native` | `tiktok-native` \| `white-bar` \| `notes-app` \| `neo-brutalist` |
| `--duration` | `8` | seconds (1–30; ship 5–8). mp4 arms only |
| `--out` | required | the **stem** is what matters: the extension is replaced per `--format` (`.png`/`.jpg` and/or `.mp4`) |
| `--safe-zone` | `72,300,936,1180` | `x,y,w,h` — text may only be drawn here |
| `--valign` | per style | `top` \| `center` \| `bottom` inside the safe zone. `white-bar` defaults to `top`, the card styles to `center` |
| `--dramatization` | off | small "Dramatization" tag, style-matched |
| `--dramatization-corner` | `bottom-left` | `bottom-left` \| `bottom-right` \| `top-left` \| `top-right` |
| `--nb-color` | `yellow` | neo-brutalist card fill; any token name (`yellow`, `blue`, `mint`, `coral`, `paper`, `cream`, `ink`) |
| `--notes-meta` | `Today at 9:41 AM` | the gray date line in the notes-app card |
| `--zoom-end` | `1.08` | Ken Burns end scale |
| `--no-keep-overlay` | off | delete the intermediate `<out>.overlay.png` |
| `--print-cmd` | off | print only the ffmpeg command |
| `--make-placeholder PATH` | — | write a gradient 1080x1920 test still and exit |

### Overlay only (no video)

```bash
python3 overlay.py --text "..." --style neo-brutalist --nb-color coral --out ov.png
```

### From a batch driver

```python
import sys; sys.path.insert(0, "/Users/graceyan/Desktop/alpha/GTM/danielle/render")
from compose import compose

r = compose(
    still="../character/dani/stills/01-car-selfie.png",
    text="i sat in the car in the garage for eleven minutes before i went inside on thursday",
    out="out/post-001.png",
    fmt="png",                    # "mp4" | "both"
    style="tiktok-native",        # "white-bar" | "notes-app" | "neo-brutalist"
    safe_zone={"x": 72, "y": 720, "w": 936, "h": 840},
)
print(r.png, r.png_bytes, r.jpg, r.jpg_bytes)
print(r.overlay["fits_safe_zone"], r.overlay["truncated"])

r = compose(..., out="out/post-001.mp4", fmt="mp4", duration=8, dramatization=True)
print(r.probe)          # {'width': 1080, 'height': 1920, 'duration': 8.0, 'audio_streams': 0, ...}
```

On the mp4 arms `compose()` raises `RuntimeError` if the output is not
1080x1920 / yuv420p / silent / the requested duration; on the png arm it raises
if the composite is not exactly 1080x1920. Either way a batch driver fails loudly
rather than shipping a bad file. `overlay.py` can also be imported on its own
(`render_overlay(...)`).

`ComposeResult` fields: `out` (the primary file), `fmt`, `png`, `jpg`,
`png_bytes`, `jpg_bytes`, `mp4`, `overlay_png`, `ffmpeg_cmd` (empty on the png
arm), `overlay`, `probe` (empty on the png arm).

---

## The exact ffmpeg invocation produced

For `--duration 6` (so `N = 6 * 30 = 180` frames, `on/(N-1)` = `on/179`):

```
ffmpeg -y -loglevel error \
  -loop 1 -framerate 30 -t 6.000 -i STILL.png \
  -loop 1 -framerate 30 -t 6.000 -i OUT.overlay.png \
  -filter_complex "[0:v]scale=2160:3840:force_original_aspect_ratio=increase:flags=lanczos,crop=2160:3840,setsar=1,format=rgb24,zoompan=z='1+0.080000*on/179':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30[bg];[1:v]format=rgba[ov];[bg][ov]overlay=0:0:format=rgb:eof_action=repeat,scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p[vout]" \
  -map "[vout]" -an \
  -c:v libx264 -profile:v high -pix_fmt yuv420p -preset medium -crf 18 \
  -r 30 -fps_mode cfr -movflags +faststart \
  -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -t 6.000 OUT.mp4
```

Filter chain, left to right:

1. `scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840` —
   cover-fit at **2x** supersample. The 2x plate is what keeps the Ken Burns push
   from stair-stepping: `zoompan`'s crop origin is an integer, so on a 2x plate
   the quantization is half an output pixel.
2. `zoompan=z='1+0.08*on/(N-1)':x=…:y=…:d=1:s=1080x1920:fps=30` — linear centered
   push from 1.00 to 1.08 across the clip, one output frame per input frame.
3. `overlay=0:0:format=rgb` — composite the RGBA type **in RGB**, so glyph edges
   never get chroma-subsampled.
4. `scale=in_range=full:out_range=tv:out_color_matrix=bt709,format=yuv420p` —
   one conversion to limited-range bt709. **Without `out_range=tv` libx264 tags
   the file `yuvj420p` (full range)** and players wash the image out.
5. `-an` — no audio track, ever.

---

## Styles

**`tiktok-native` (default).** What the live posts actually look like: DM Sans
Bold, white, **centred, no bands**, soft blurred dark drop shadow (9px gaussian,
alpha 168, offset 0/3) on its own layer so the blur cannot eat the glyph edges,
1.14 leading. Autofits 96px → 42px. Sentence case and punctuation are preserved
verbatim — this style must never transform the copy.

**`white-bar`.** DM Sans Bold, white, on per-line semi-opaque black
(alpha 158) rounded bands, left-aligned, 14px gaps. Autofits 64px → 30px. Rides
the top of the safe zone so a talking-head's face stays clear. This was the old
default; `tiktok-native` replaced it because it matches the live posts.

**`notes-app`.** Off-white rounded card (radius 40) with a soft blurred shadow, a
gray "Today at 9:41 AM" meta line, and near-black Helvetica Neue body at
1.34 leading. Autofits 52px → 26px.

**`neo-brutalist`.** The SFFS system, values read from
[`../../brand-assets/tokens.css`](../../brand-assets/tokens.css): flat bright
fill (`--yellow #FCE552` default), `--ink #000000` 4px border, zero-blur 11px
hard shadow (`--shadow-card 16px` scaled by the same 4/6 factor the tokens use
for 1080p video), radius 40 (`--radius 44`). Uppercase Anton, centered, autofits
112px → 40px. No colors are invented — the palette is exactly the seven tokens.

The knobs are module constants at the top of `overlay.py`: `NB_BORDER`,
`NB_SHADOW`, `NB_RADIUS`, `TOKENS`, `DEFAULT_SAFE_ZONE`, `DEFAULT_VALIGN`.

## Safe zone and overflow

`--safe-zone x,y,w,h` is a hard constraint: nothing (band, card, border, shadow)
is drawn outside it. The default `72,300,936,1180` clears TikTok's top chrome and
leaves the bottom 480px free for the caption and the right-hand button rail.
The "Dramatization" tag sits outside the safe zone by design, in a frame corner.

Autofit shrinks the type until the wrapped block fits. If even the minimum size
overflows (absurd text vs. a tiny box), the block is **truncated with an
ellipsis** and a warning goes to stderr with `truncated: true` in the JSON —
it never spills out of the box.

## Test renders

`test/` holds the verification renders: `{white-bar,notes-app,neo-brutalist}-{long,short}.mp4`
plus `frame-*.png` stills extracted from them, and `placeholder-still.png`.
Regenerate/extend by editing the case list in that same pattern.
