# intake — single-command music library pipeline

## Goal

One command processes everything in `_Staging/`:

```
venv/bin/python intake/intake.py            # interactive (default)
venv/bin/python intake/intake.py --auto     # non-interactive: safe defaults, defer questions
venv/bin/python intake/intake.py --dry-run  # report only
```

Phases, in order:

1. **Strip** — delete junk from staging. Keep: audio (`.flac .mp3 .m4a .wav .aiff .ogg .opus .wma .ape .wv`),
   lyrics (`.lrc`), images (`.jpg .jpeg .png`), `.keep`, and identification artifacts
   (`.pdf` booklets, `.cue`) — those stay with the unit and are cleaned up only after the album
   successfully lands; unresolved albums keep them for manual identification. Delete everything else
   (`.nfo .log .txt .m3u .m3u8 .accurip .sfv .db .srr .nzb`, video, etc.). Remove empty dirs.
   Note: non-flac/mp3 *audio* is intentionally kept here — phase 2 decides its fate (a `.wav` may be
   losslessly converted; deleting it in strip would be data loss).
2. **Validate / classify** — sniff magic bytes of every audio file:
   - `.flac` + fLaC magic → OK
   - `.mp3` + fLaC magic → auto-rename to `.flac` (safe, logged)
   - `.flac` + mp3 data → QUESTION: rename to .mp3 (+allow) / delete / skip
   - real `.mp3` → consult store `allowed_mp3`; unknown → QUESTION: allow / delete / skip
   - lossless other (wav/aiff) → QUESTION: convert to FLAC (default) / keep+allow / delete / skip
   - lossy other (m4a/ogg/...) → QUESTION: allow as-is / convert to mp3 / delete / skip
   - zero-byte / unreadable → QUESTION: delete / skip
   All answers remembered by content key where possible.
3. **Corruption check** — `flac --test` for FLAC, `ffmpeg -v error` decode for everything else,
   thread pool. Corrupt → QUESTION: delete / quarantine to `_Staging/_Corrupt/` / ignore(+remember).
   Known-corrupt keys from store auto-flag without re-decode.
4. **Group into album units** — a unit is a dir directly containing audio; bare `CD*/Disc*/12 Vinyl*`
   subfolders merge into their parent (one unit). NAMED disc folders (`CD2 - Relapse Refill`) are
   their own unit (usually their own MusicBrainz release) and get physically relocated to the
   staging root ("un-nested") so beets' multi-disc heuristics can't re-merge them into the parent's
   import. Loose audio directly at staging root becomes one loose unit handled in the manual phase
   (never offered to beets — it would sweep up the other units).
5. **Replay remembered decisions** — exact album-key hit in store: `mbid` → forced beets import
   (`--search-id`), `asis` → manual placement with stored target. No questions. Fuzzy hit
   (≥80% of track keys belong to one stored album) → used as the default answer in phase 7.
6. **Beets quiet import** — per unit: `beet import -q <unit>` with `quiet_fallback: skip`
   (unmatched units stay in place and OUT of the beets db). Matched units move into the
   library. EVERY album beets created from the unit (interactive sessions can yield several
   via "Group albums") is recorded separately: its track keys are recomputed from the files
   it actually received (content keys are path-independent), so each album gets its own
   correct decision entry.
7. **Interactive leftovers** — per remaining unit, menu:
   - `[i]` interactive beets import (full beets TUI for this unit; outcome captured + remembered)
   - `[m]` manual placement: artist/album/year/type prompts (defaults from tags/dirname),
     files renamed `NN - Title.ext` from tags (no number prefix if no track tag; filename stem
     as title fallback), moved to `Artist/[Year] Album/`. The typed answers are also written
     into the files' tags (only filling EMPTY fields, never overwriting) so players and the
     MP3 mirror see them. Remembered as `asis` decision.
   - `[s]` skip (stays in staging), `[d]` delete unit (confirm).
8. **Albumartist normalization** — for every album imported this run: if albumartist looks
   multi-artist (`;` `/` `feat` `ft.` ` & ` ` x ` `, ` …) and isn't an exact existing library
   artist or known band name: consult `artist_map`; unknown → QUESTION: primary artist?
   (default = first segment). Apply via `beet modify -y -a albumartist=...` (beets re-paths
   automatically). Track-level `artist` keeps the full collab credit. Mapping remembered forever
   ("keep as-is" stored as identity mapping).
9. **Companion files** — `.lrc` mapped to audio by basename before import, re-attached at the
   new path (renamed to the new track basename) after import. Cover images: beets `fetchart`
   with `filesystem` as first source picks up `cover.jpg` from the source dir; `embedart` embeds.
10. **Post-check** — magic-byte validation of imported files; non-FLAC audio in library reported
    unless allowed in store or in an exempt dir.
11. **MP3 mirror sync** — optional (`--sync` or config `auto_sync`); subprocess of
    `flac_mp3_sync/sync.py`.
12. **Summary** — per phase counts, questions answered (now remembered), deferred items.

## Files

```
intake/
  intake.py             # CLI + pipeline orchestration + interactive prompts
  audio_keys.py         # content-based keys: flac STREAMINFO md5; mp3 raw-stream sha1 (ID3-stripped);
                        # fallback ffmpeg decoded-pcm md5; album key = sha1(sorted track keys)
  decision_store.py     # JSON store, atomic writes, schema_version; sections:
                        #   tracks{key: {allowed_mp3, corrupt, wrong_ext, format_decision, ...}}
                        #   albums{key: {action: mbid|asis, mbid, artist, album, year, type, decided_at}}
                        #   artist_map{raw_lower: canonical}
  artist_normalizer.py  # multi-artist detection + split suggestion; never auto-splits without
                        #   a map hit; seeds "known artists" from library dir names
  beets_runner.py       # wraps beet subprocess: effective temp config (base yaml + overrides),
                        #   quiet/interactive/forced-id imports, db queries (added-since, album mbid,
                        #   item paths for an album), beet modify
  config.example.toml   # library, staging, mp3_target, bitrate, beets_config, jobs, auto_sync
  DESIGN.md
tests/                  # pytest unit tests (audio_keys, decision_store, artist_normalizer, grouping)
```

Store lives at `<library>/.music-tools/decisions.json` — inside the library so it rides along
with backups. Lockfile `<library>/.music-tools/intake.lock` prevents concurrent runs. Store is
saved after every single decision (Ctrl-C safe).

## Content keys (path-independent, retag-proof)

- FLAC: `flac:<STREAMINFO MD5>` via `metaflac --show-md5sum` (set by encoder, survives any retag).
  If unset (all zeros), fall back to decode md5.
- MP3: `mp3:<sha1 of raw frames>` — file bytes minus ID3v2 header (size from syncsafe int),
  minus ID3v1 (`TAG` 128-byte tail) and APEv2 tail. Pure python, no decode. Survives retagging.
- Other: `pcm:<md5>` via `ffmpeg -map 0:a -f md5` (decoded), or `file:<sha1>` if ffmpeg fails.
- Album: `album:<sha1 of newline-joined sorted track keys>`.

## beets config changes (beets/config.yaml)

- `quiet_fallback: skip` (was `asis` — asis silently imported junk-tagged albums and polluted the db)
- drop the `mb_albumid::^$: _Staging/...` path rule (the orchestrator owns unmatched handling now)
- multi-disc: `per_disc_numbering: yes`, inline plugin `multidisc: 1 if disctotal > 1 else 0`,
  paths `.../%if{$multidisc,$disc}$track - $title` → `101 - ...`, `201 - ...` (README convention,
  previously NOT implemented — multi-disc imports collided as duplicate `01 - ...`)
- `match.strong_rec_thresh: 0.15` (was 0.40 — auto-accepting ~60% similarity in quiet mode is a
  mistagging factory; weaker matches now go to the interactive phase instead)
- `original_date: yes` (reissues file under original year)
- `fetchart.sources: filesystem coverart itunes amazon` (use the rip's own cover first)
- `directory`/`fpcalc` injected by the orchestrator at runtime (temp effective config); yaml keeps
  Linux defaults so standalone `beet` use still works.

## Interactivity contract

Prompts read stdin via `input()` — testable by piping answers (tests/drive_intake.py is an
expect-style driver that also answers beets' TUI prompts). Every question shows a default;
empty input takes it. `--auto` answers nothing: it applies only stored decisions + safe automatic
actions, defers the rest, and lists what's pending. Exit code 0 = staging fully processed,
2 = staging still holds audio (deferred/skipped/held). The summary's "remembered N decision(s)"
counts actual store writes, not answered prompts (skips are deliberately not persisted).

## Artist-name matching

All artist comparisons go through `fold()` (NFC + dash/quote folding + casefold) so
"Wu‐Tang Clan" (U+2010, MusicBrainz style) and "Wu-Tang Clan" (ASCII) are the same artist,
and suggestions always use the library's existing spelling. Detection exemption comes ONLY
from user-confirmed identity mappings — never from a folder merely existing (an un-normalized
import creates its own folder and must not self-exempt).
