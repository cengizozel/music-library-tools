"""Unit tests for the intake pipeline's pure logic."""

import json
import os
import struct
import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "intake"))

import audio_keys
from artist_normalizer import detect
from decision_store import DecisionStore
from intake import sniff, repair_flac, discover_units, sanitize, DISC_DIR_RE


# ----------------------------------------------------------- artist_normalizer

KNOWN = {"Bones", "Justice", "Linkin Park", "Night Lovell"}


@pytest.mark.parametrize("raw,expected", [
    ("Bones & cat soup", "Bones"),
    ("Bones & Eddy Baker", "Bones"),
    # suggestion uses the library's spelling, not the tag's casing
    ("BONES & Drew The Architect", "Bones"),
    ("Night Lovell feat. $uicideboy$", "Night Lovell"),
    ("Justice vs. Gambit", "Justice"),
    # library-known part wins over first-listed (album lives under Linkin Park)
    ("Jay-Z / Linkin Park", "Linkin Park"),
])
def test_collab_suggestion(raw, expected):
    s = detect(raw, KNOWN)
    assert s is not None, raw
    assert s.suggestion == expected
    assert not s.default_keep


def test_slash_inside_artist_name_splits_on_ampersand_first():
    s = detect("Bones & ghost/\\/ghoul", KNOWN)
    assert s is not None
    assert s.separator == "ampersand"
    assert s.suggestion == "Bones"


def test_plain_known_artist_never_flagged():
    assert detect("Bones", KNOWN) is None
    assert detect("bones", KNOWN) is None  # no separator -> never flagged


def test_confirmed_artist_with_separator_exempt():
    confirmed = {"tyler, the creator"}
    assert detect("Tyler, the Creator", KNOWN, confirmed) is None


def test_library_dir_does_not_self_exempt():
    """A folder created by an un-normalized import must NOT exempt its own
    albumartist from detection (the BONES & Eddy Baker regression)."""
    known = KNOWN | {"BONES & Eddy Baker"}
    s = detect("BONES & Eddy Baker", known, confirmed=set())
    assert s is not None
    assert s.suggestion == "BONES & Eddy Baker" or s.parts[0] == "BONES"


def test_comma_defaults_to_keep():
    s = detect("Robin Blaze, Masaaki Suzuki, Bach Collegium Japan", set())
    assert s is not None
    assert s.separator == "comma"
    assert s.default_keep  # classical credits: never auto-split


def test_known_part_preferred_over_first():
    s = detect("Unknown Guy & Bones", KNOWN)
    assert s.suggestion == "Bones"


def test_plain_artist_not_flagged():
    assert detect("Radiohead", set()) is None
    assert detect("", set()) is None


def test_feat_in_parens():
    s = detect("Artist A (feat. Artist B)", set())
    assert s is not None
    assert s.suggestion == "Artist A"


# ------------------------------------------------------------------ audio keys

def make_fake_mp3(path: Path, payload: bytes = b"\xff\xfb\x90\x00" + b"A" * 1000):
    """Minimal mp3-ish file with ID3v2 + payload + ID3v1."""
    body = payload
    tag_data = b"X" * 100
    id3v2 = b"ID3\x03\x00\x00" + bytes([0, 0, tag_data.__len__() >> 7, len(tag_data) & 0x7F]) + tag_data
    id3v1 = b"TAG" + b"\x00" * 125
    path.write_bytes(id3v2 + body + id3v1)


def test_mp3_key_survives_retag(tmp_path):
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    payload = b"\xff\xfb\x90\x00" + os.urandom(2000)
    make_fake_mp3(a, payload)
    # b: same audio, different/absent tags
    b.write_bytes(payload)
    ka = audio_keys._mp3_stream_sha1(a)
    kb = audio_keys._mp3_stream_sha1(b)
    assert ka == kb


def test_mp3_key_differs_for_different_audio(tmp_path):
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    make_fake_mp3(a, b"\xff\xfb\x90\x00" + b"A" * 500)
    make_fake_mp3(b, b"\xff\xfb\x90\x00" + b"B" * 500)
    assert audio_keys._mp3_stream_sha1(a) != audio_keys._mp3_stream_sha1(b)


def test_album_key_order_independent():
    k1 = audio_keys.album_key(["mp3:b", "flac:a"])
    k2 = audio_keys.album_key(["flac:a", "mp3:b"])
    assert k1 == k2


# -------------------------------------------------------------------- sniffing

def test_sniff_flac(tmp_path):
    p = tmp_path / "x.flac"
    p.write_bytes(b"fLaC" + b"\x00" * 100)
    assert sniff(p) == "flac"


def test_sniff_mp3_with_id3(tmp_path):
    p = tmp_path / "x.mp3"
    make_fake_mp3(p)
    assert sniff(p) == "mp3"


def test_sniff_mp3_bare_sync(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
    assert sniff(p) == "mp3"


def test_sniff_id3_prefixed_flac(tmp_path):
    """The Metallica Load case: ID3v2 block prepended to real FLAC data."""
    p = tmp_path / "x.flac"
    tag_data = b"X" * 47
    id3 = b"ID3\x03\x00\x00" + bytes([0, 0, 0, len(tag_data)]) + tag_data
    p.write_bytes(id3 + b"fLaC" + b"\x00" * 100)
    assert sniff(p) == "flac_id3"


def test_sniff_garbage(tmp_path):
    p = tmp_path / "x.flac"
    p.write_bytes(b"RIFF" + b"\x00" * 100)
    assert sniff(p) == "unknown"


# ------------------------------------------------------------- decision store

def test_store_roundtrip(tmp_path):
    s = DecisionStore(tmp_path / "d.json")
    s.set_track("mp3:abc", allowed_mp3=True)
    s.set_album("album:xyz", action="mbid", mbid="m-1",
                track_keys=["mp3:abc"], artist="X", album="Y", year="2020")
    s.set_canonical_artist("Bones & cat soup", "Bones")

    s2 = DecisionStore(tmp_path / "d.json")
    assert s2.track("mp3:abc")["allowed_mp3"] is True
    assert s2.album("album:xyz")["mbid"] == "m-1"
    assert s2.canonical_artist("bones & CAT soup".lower()) == "Bones"
    assert s2.canonical_artist("Bones & cat soup") == "Bones"


def test_store_fuzzy_album(tmp_path):
    s = DecisionStore(tmp_path / "d.json")
    keys = [f"flac:{i}" for i in range(10)]
    s.set_album("album:full", action="asis", track_keys=keys, artist="A", album="B")
    # 9 of 10 tracks re-staged
    hit = s.fuzzy_album(keys[:9])
    assert hit is not None
    assert hit[0] == "album:full"
    # only 3 of 10: no match
    assert s.fuzzy_album(keys[:3]) is None


def test_store_atomic_persists_each_mutation(tmp_path):
    path = tmp_path / "d.json"
    s = DecisionStore(path)
    s.set_track("k1", allowed_mp3=True)
    on_disk = json.loads(path.read_text())
    assert "k1" in on_disk["tracks"]


# ------------------------------------------------------------------- grouping

def touch_audio(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fLaC" + b"\x00" * 64)


def test_discover_units_merges_bare_disc_dirs(tmp_path):
    staging = tmp_path / "_Staging"
    touch_audio(staging / "Album X/CD1/01.flac")
    touch_audio(staging / "Album X/CD2/01.flac")
    touch_audio(staging / "Album Y/01.flac")
    units = discover_units(staging)
    dirs = sorted(u.dir.name for u in units)
    assert dirs == ["Album X", "Album Y"]
    x = next(u for u in units if u.dir.name == "Album X")
    assert len(x.files) == 2


def test_discover_units_named_disc_stays_separate(tmp_path):
    staging = tmp_path / "_Staging"
    touch_audio(staging / "Relapse/CD1/01.flac")
    touch_audio(staging / "Relapse/CD2 - Relapse Refill/01.flac")
    units = discover_units(staging)
    names = sorted(u.dir.name for u in units)
    assert names == ["CD2 - Relapse Refill", "Relapse"]


def test_discover_units_vinyl_dirs_merge(tmp_path):
    staging = tmp_path / "_Staging"
    touch_audio(staging / "PHM/12 Vinyl 01/01.flac")
    touch_audio(staging / "PHM/12 Vinyl 02/01.flac")
    units = discover_units(staging)
    assert [u.dir.name for u in units] == ["PHM"]


def test_discover_units_loose_files(tmp_path):
    staging = tmp_path / "_Staging"
    touch_audio(staging / "stray.flac")
    touch_audio(staging / "Album/01.flac")
    units = discover_units(staging)
    loose = [u for u in units if u.loose]
    assert len(loose) == 1
    assert loose[0].dir == staging


def test_discover_units_skips_quarantine(tmp_path):
    staging = tmp_path / "_Staging"
    touch_audio(staging / "_Corrupt/Album/01.flac")
    touch_audio(staging / "Album/01.flac")
    units = discover_units(staging)
    assert [u.dir.name for u in units] == ["Album"]


@pytest.mark.parametrize("name,matches", [
    ("CD1", True), ("CD 01", True), ("cd2", True), ("Disc 3", True),
    ("DISK10", True), ("12 Vinyl 01", True), ("LP 2", True),
    ("CD2 - Relapse Refill", False), ("Bonus CD", False), ("CD", False),
])
def test_disc_dir_regex(name, matches):
    assert bool(DISC_DIR_RE.match(name)) == matches


# ------------------------------------------------------------------- sanitize

@pytest.mark.parametrize("raw,ok", [
    ('AC/DC', "AC_DC"),
    ('What?', "What_"),
    ('trailing. ', "trailing"),
    ('', "_"),
])
def test_sanitize(raw, ok):
    assert sanitize(raw) == ok


# --------------------------------------------------- real-file flac keys (io)

FLAC_BIN = __import__("shutil").which("flac")


@pytest.mark.skipif(not FLAC_BIN, reason="flac CLI not installed")
def test_flac_key_survives_retag_real(tmp_path):
    import wave, math
    wav = tmp_path / "t.wav"
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(struct.pack("<" + "h" * 8000,
                                  *[int(1000 * math.sin(i / 10)) for i in range(8000)]))
    f = tmp_path / "t.flac"
    subprocess.run([FLAC_BIN, "--silent", "-o", str(f), str(wav)], check=True)
    k1 = audio_keys.track_key(f)
    subprocess.run(["metaflac", "--set-tag=TITLE=Completely Different", str(f)], check=True)
    k2 = audio_keys.track_key(f)
    assert k1 == k2
    assert k1.startswith("flac:")


def _make_real_flac(tmp_path: Path) -> bytes:
    import wave, math
    wav = tmp_path / "t.wav"
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(struct.pack("<" + "h" * 4000,
                                  *[int(900 * math.sin(i / 7)) for i in range(4000)]))
    f = tmp_path / "t.flac"
    subprocess.run([FLAC_BIN, "--silent", "-f", "-o", str(f), str(wav)], check=True)
    return f.read_bytes()


@pytest.mark.skipif(not FLAC_BIN, reason="flac CLI not installed")
def test_repair_flac_prepended_id3(tmp_path):
    clean = _make_real_flac(tmp_path)
    f = tmp_path / "t.flac"
    tag = b"Z" * 60
    id3 = b"ID3\x03\x00\x00" + bytes([0, 0, 0, len(tag)]) + tag
    f.write_bytes(id3 + clean)
    assert sniff(f) == "flac_id3"
    assert repair_flac(f)
    assert sniff(f) == "flac"
    assert f.read_bytes() == clean


@pytest.mark.skipif(not FLAC_BIN, reason="flac CLI not installed")
def test_repair_flac_full_wrapping(tmp_path):
    """The Metallica Load case: ID3v2 in front AND ID3v1 trailing."""
    clean = _make_real_flac(tmp_path)
    f = tmp_path / "t.flac"
    tag = b"\x00" * 700000  # big block like embedded art
    sz = len(tag)
    syncsafe = bytes([(sz >> 21) & 0x7F, (sz >> 14) & 0x7F, (sz >> 7) & 0x7F, sz & 0x7F])
    id3v2 = b"ID3\x03\x00\x00" + syncsafe + tag
    id3v1 = b"TAG" + b"\x00" * 125
    f.write_bytes(id3v2 + clean + id3v1)
    assert sniff(f) == "flac_id3"
    assert repair_flac(f)
    assert f.read_bytes() == clean
    assert subprocess.run([FLAC_BIN, "--test", "--silent", str(f)]).returncode == 0


@pytest.mark.skipif(not FLAC_BIN, reason="flac CLI not installed")
def test_repair_flac_trailing_id3v1_only(tmp_path):
    clean = _make_real_flac(tmp_path)
    f = tmp_path / "t.flac"
    f.write_bytes(clean + b"TAG" + b"\x01" * 125)
    assert sniff(f) == "flac"  # head looks fine
    assert repair_flac(f)
    assert f.read_bytes() == clean


@pytest.mark.skipif(not FLAC_BIN, reason="flac CLI not installed")
def test_repair_flac_clean_file_untouched(tmp_path):
    clean = _make_real_flac(tmp_path)
    f = tmp_path / "t.flac"
    f.write_bytes(clean)
    assert repair_flac(f) is False  # nothing to strip
    assert f.read_bytes() == clean


@pytest.mark.skipif(not FLAC_BIN, reason="flac CLI not installed")
def test_repair_flac_truncated_not_fixed(tmp_path):
    clean = _make_real_flac(tmp_path)
    f = tmp_path / "t.flac"
    f.write_bytes(clean[:len(clean) // 2] + b"TAG" + b"\x00" * 125)
    assert repair_flac(f) is False  # repair must not mask real corruption


def test_suggestion_uses_library_spelling():
    s = detect("BONES & Eddy Baker", {"Bones"}, set())
    assert s.suggestion == "Bones"


# ---------------------------------------------------------------- fold/unicode

from artist_normalizer import fold


def test_fold_unicode_hyphen_variants():
    assert fold("Wu‐Tang Clan") == fold("Wu-Tang Clan")  # U+2010 vs ASCII
    assert fold("Guns N’ Roses") == fold("Guns N' Roses")
    assert fold("BONES") == fold("bones")


def test_store_canonical_artist_folds(tmp_path):
    s = DecisionStore(tmp_path / "d.json")
    s.set_canonical_artist("Wu‐Tang Clan & Friend", "Wu‐Tang Clan")
    assert s.canonical_artist("Wu-Tang Clan & Friend") == "Wu‐Tang Clan"


def test_detect_known_matches_across_hyphen_variants():
    s = detect("Wu-Tang Clan & RZA", {"Wu‐Tang Clan"}, set())
    assert s is not None
    assert s.suggestion == "Wu‐Tang Clan"  # library spelling preferred


# ----------------------------------------------------- review-driven fixes

from intake import unique_path


def test_discover_units_disc_dir_at_staging_root(tmp_path):
    """_Staging/CD1 must not make the staging root itself a non-loose unit."""
    staging = tmp_path / "_Staging"
    touch_audio(staging / "CD1/01.flac")
    touch_audio(staging / "CD2/01.flac")
    touch_audio(staging / "Some Album/01.flac")
    units = discover_units(staging)
    assert all(u.dir != staging or u.loose for u in units)
    names = sorted(u.dir.name for u in units if not u.loose)
    assert names == ["CD1", "CD2", "Some Album"]


def test_unique_path(tmp_path):
    p = tmp_path / "x.flac"
    assert unique_path(p) == p
    p.write_bytes(b"a")
    p1 = unique_path(p)
    assert p1.name == "x (1).flac"
    p1.write_bytes(b"b")
    assert unique_path(p).name == "x (2).flac"


@pytest.mark.skipif(not FLAC_BIN, reason="flac CLI not installed")
def test_repair_flac_trailing_ape_with_header(tmp_path):
    """APEv2 tag with header (mp3tag layout): both header+footer must strip."""
    clean = _make_real_flac(tmp_path)
    f = tmp_path / "t.flac"
    def ape_block(is_header: bool, has_header_flag: bool) -> bytes:
        flags = (0x80000000 if has_header_flag else 0) | (0x20000000 if is_header else 0)
        return (b"APETAGEX" + (2000).to_bytes(4, "little") + (32).to_bytes(4, "little")
                + (0).to_bytes(4, "little") + flags.to_bytes(4, "little") + b"\x00" * 8)
    f.write_bytes(clean + ape_block(True, True) + ape_block(False, True))
    assert repair_flac(f)
    assert f.read_bytes() == clean


# ---------------------------------------------------------------- lrc adoption

from intake import Pipeline


def test_adopt_lrc_moves_to_library_copy(tmp_path):
    staged = tmp_path / "staged"; staged.mkdir()
    lib = tmp_path / "lib"; lib.mkdir()
    audio = staged / "01 - Song.flac"; audio.write_bytes(b"fLaC")
    (staged / "01 - Song.lrc").write_text("[00:01.00] hi")
    lib_audio = lib / "01 - Song Title.flac"; lib_audio.write_bytes(b"fLaC")
    Pipeline._adopt_lrc(audio, lib_audio)
    assert (lib / "01 - Song Title.lrc").read_text() == "[00:01.00] hi"
    assert not (staged / "01 - Song.lrc").exists()


def test_adopt_lrc_keeps_existing_library_lyrics(tmp_path):
    staged = tmp_path / "staged"; staged.mkdir()
    lib = tmp_path / "lib"; lib.mkdir()
    audio = staged / "a.flac"; audio.write_bytes(b"fLaC")
    (staged / "a.lrc").write_text("new")
    lib_audio = lib / "b.flac"; lib_audio.write_bytes(b"fLaC")
    (lib / "b.lrc").write_text("existing")
    Pipeline._adopt_lrc(audio, lib_audio)
    assert (lib / "b.lrc").read_text() == "existing"   # never overwritten
    assert (staged / "a.lrc").exists()                 # left for salvage sweep


# --------------------------------------------------------------- classical/fat

def test_sanitize_clamps_long_names_at_utf8_boundary():
    long = "Cantata, BWV 72: I. Coro «Alles nur nach Gottes Willen» " * 8
    out = sanitize(long)
    assert len(out.encode("utf-8")) <= 180
    out.encode("utf-8")  # must not raise (no split surrogates)
    assert sanitize("short") == "short"  # untouched


def test_detect_classical_credit_leads_with_composer():
    s = detect("Johann Sebastian Bach; Bach Collegium Japan, Masaaki Suzuki", set())
    assert s is not None
    assert s.separator == "semicolon"
    assert s.parts[0] == "Johann Sebastian Bach"
    assert not s.default_keep  # semicolon splits are actionable


def test_failed_attempt_memory(tmp_path):
    s = DecisionStore(tmp_path / "d.json")
    assert not s.failed_recently("album:x")
    s.record_failed_attempt("album:x")
    assert s.failed_recently("album:x")
    assert not s.failed_recently("album:x", hours=0)


@pytest.mark.parametrize("raw,expected", [
    ("$uicideboy$ × Getter", "$uicideboy$"),
    ("Metallica with Michael Kamen conducting the San Francisco Symphony", "Metallica"),
])
def test_unicode_x_and_with_separators(raw, expected):
    s = detect(raw, set())
    assert s is not None and s.suggestion == expected


# ---------------------------------------------------------- cover normalizer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cover_normalizer"))
import normalize as cov


def _img(p: Path, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_cover_dedupe_and_junk(tmp_path):
    d = tmp_path / "Album"; d.mkdir()
    (d / "01.flac").write_bytes(b"fLaC")
    _img(d / "cover.jpg", b"JPEGDATA-A")
    _img(d / "folder.jpg", b"JPEGDATA-A")        # exact dup of cover
    _img(d / "AlbumArtSmall.jpg", b"wmpjunk")    # junk
    _img(d / "back.jpg", b"JPEGDATA-B")          # distinct -> kept
    changes = cov.normalize_album_covers(d, apply=True)
    names = sorted(p.name for p in d.iterdir() if p.suffix == ".jpg")
    assert "cover.jpg" in names           # primary kept
    assert "back.jpg" in names            # distinct art kept
    assert "folder.jpg" not in names      # exact dup removed
    assert "AlbumArtSmall.jpg" not in names  # junk removed
    assert changes >= 2


def test_cover_jpeg_extension_renamed(tmp_path):
    d = tmp_path / "Album"; d.mkdir()
    (d / "01.flac").write_bytes(b"fLaC")
    _img(d / "cover.jpeg", b"JPEGDATA")          # .jpeg invisible to Rockbox
    cov.normalize_album_covers(d, apply=True)
    assert (d / "cover.jpg").exists()
    assert not (d / "cover.jpeg").exists()


def test_cover_promotes_primary_to_cover(tmp_path):
    d = tmp_path / "Album"; d.mkdir()
    (d / "01.flac").write_bytes(b"fLaC")
    _img(d / "folder.jpg", b"ART")               # no cover.* present
    cov.normalize_album_covers(d, apply=True)
    assert (d / "cover.jpg").exists()


def test_cover_junk_name_detection():
    assert cov.is_junk("AlbumArtSmall.jpg")
    assert cov.is_junk("AlbumArt_{12345}_Large.jpg")
    assert cov.is_junk("Thumbs.db")
    assert not cov.is_junk("cover.jpg")
    assert not cov.is_junk("booklet-03.jpg")


def test_extract_embedded_cover_skips_when_external_exists(tmp_path):
    from intake import extract_embedded_cover
    d = tmp_path / "Album"; d.mkdir()
    (d / "01.flac").write_bytes(b"fLaC")
    (d / "cover.jpg").write_bytes(b"existing")
    # external cover already present -> no extraction attempted
    assert extract_embedded_cover(d) is False
