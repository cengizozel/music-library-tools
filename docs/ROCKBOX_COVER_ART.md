# Rockbox / PictureFlow cover-art compatibility

Hard-won notes on making album art display correctly in Rockbox PictureFlow on
old iPods (tested on iPod Classic 6G, `ipod6g`, Rockbox 4.0). The `flac_mp3_sync`
mirror step now produces compliant covers automatically; this records *why*.

The FLAC library keeps its original/full-resolution covers untouched — every
rule below applies only to the derived **device mirror** (`flac_mp3_sync`).

## The requirements (all enforced by sync.py `_cover_to_device_image`)

1. **External `cover.jpg` per album folder** — PictureFlow does NOT read embedded
   art. (Intake's `extract_embedded_cover` writes one when an album has only
   embedded art.)
2. **Baseline, not progressive** — Rockbox can't decode progressive JPEGs (shows
   "bad art"). jpegtran/cjpeg produce baseline.
3. **JPEG or BMP, not PNG** — PictureFlow's art lookup is JPEG/BMP only; large
   PNGs show blank.
4. **Reasonably small** — ~300–600px; large images are slow or fail to decode.
   We cap at 500px.
5. **`.jpg` extension** (not `.jpeg`) — harmless either way in modern builds, but
   `.jpg` is universally found.
6. **TWO quantization tables (THE color fix)** — this is the subtle one.

## The two-quantization-table bug (why covers rendered grayscale)

Symptom: covers were full color on a PC but **grayscale** in PictureFlow, even on
a color iPod with a color build. Verified the cached `.pfraw` tiles were RGB565
(color format) but desaturated (R≈G≈B).

Cause: Rockbox's JPEG decoder (`apps/recorder/jpeg_load.c`) dequantizes chroma
with a hard-coded `quanttable[!!ci]` lookup — component 0 → table 0 (luma),
components 1/2 → table **1** (chroma). But **ffmpeg's mjpeg encoder writes only
ONE quantization table** (all components point at table 0). So Rockbox reads an
empty table 1 for chroma, the chroma planes collapse, and YUV→RGB yields gray.
The JPEG is valid (PCs use the component selectors), so it looks fine off-device.

A correct color JPEG (e.g. the original rips) has **2 DQT tables**, selectors
`[0,1,1]`. To diagnose, count `0xFFDB` (DQT) marker segments in the file:
`==2` is color-safe, `==1` renders grayscale on Rockbox.

### The fix

Re-encode with `cjpeg` (libjpeg-turbo), which writes both tables:

```bash
ffmpeg -v error -i SRC -vf "scale='min(500,iw)':'min(500,ih)':force_original_aspect_ratio=decrease" \
  -pix_fmt rgb24 -f image2pipe -vcodec ppm - \
| cjpeg -quality 90 -baseline -sample 2x2,1x1,1x1 -qslots 0,1,1 > cover.jpg
```

- `-qslots 0,1,1` = component 0 uses quant table 0, components 1,2 use table 1
  (→ two DQT tables, matching Rockbox's expectation).
- `-sample 2x2,1x1,1x1` = conventional 4:2:0.
- `-baseline` = no progressive.

What did NOT work: `ffmpeg -force_duplicated_matrix 1` still emitted one table.
**BMP is NOT required** — a correctly-encoded JPEG renders in color. (BMP works
too, as raw RGB, but is larger and unnecessary.)

## After changing covers: rebuild the PictureFlow cache

PictureFlow caches decoded/resized tiles as `.pfraw`; changing the cover files
does nothing until the cache is rebuilt. The reliable reset:

```bash
rm -rf <iPod>/.rockbox/rocks/demos/pictureflow/
```

Then on the device: PictureFlow → menu → Settings → **Rebuild cache** (NOT
"Update cache" — Update only adds new albums and keeps stale tiles).

## Device transfer notes

- iPods are FAT32: rsync with `-rt --modify-window=2` (FAT has 2s mtime
  granularity; without it everything re-copies every run).
- Old iPods drop off USB under sustained writes / idle-poweroff. The resilient
  per-artist push with `--bwlimit` and auto-remount handles disconnects.
- Always target the device's `Music/` folder, never its root (`.rockbox` lives
  next to it and `--delete` would eat the firmware).
