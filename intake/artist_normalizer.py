#!/usr/bin/env python3
"""
Multi-artist albumartist detection for device-library sanity.

Devices that browse by tag (Rockbox, Neutron) treat every distinct albumartist
string as a separate artist, so "Bones & cat soup", "Bones & Eddy Baker", … each
become their own entry. The fix: albumartist holds exactly one canonical primary
artist; the per-track artist tag keeps the full collab credit.

Nothing is ever split automatically. This module only *detects* likely
multi-artist values and proposes a default; the pipeline asks the user once per
distinct raw value and remembers the answer in the decision store's artist_map.

Separator policy (from a scan of the real library):
  feat./ft./featuring  almost always a collab        -> suggest part before "feat"
  ;                    multi-value join (Picard)     -> suggest first part
  " & "                dominant collab separator      -> suggest first part
                       ("Bones & cat soup")
  " vs. "              collab                         -> suggest first part
  " / " or "/"         collab ("Jay-Z / Linkin Park") -> suggest first part, but
                       only tried late: artist names may contain slashes
                       ("Bones & ghost/\\/ghoul" splits on " & " first)
  " x "                collab                         -> suggest first part
  ", "                 NEVER a safe split: classical credits ("Robin Blaze,
                       Masaaki Suzuki, Bach Collegium Japan") and names like
                       "Tyler, the Creator" -> flag, but default = keep as-is
"""

import re
import unicodedata
from dataclasses import dataclass, field

# Unicode look-alikes that make "the same" artist string compare unequal:
# MusicBrainz writes U+2010 hyphens ("Wu‐Tang Clan") while rips use ASCII "-".
_FOLD_MAP = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    " ": " ",
})


def fold(s: str) -> str:
    """Comparison key for artist names: NFC + dash/quote folding + casefold."""
    return unicodedata.normalize("NFC", s).translate(_FOLD_MAP).strip().casefold()

# Ordered: first match wins, so " & " beats "/" inside the same value.
_SEPARATORS: list[tuple[str, re.Pattern, bool]] = [
    # (name, pattern, split_default) — split_default False means default answer
    # is "keep as-is" even though we flag it.
    ("feat", re.compile(r"\s+\(?(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE), True),
    ("semicolon", re.compile(r"\s*;\s*"), True),
    ("ampersand", re.compile(r"\s+&\s+"), True),
    ("vs", re.compile(r"\s+vs\.?\s+", re.IGNORECASE), True),
    ("slash", re.compile(r"\s+/\s+|/"), True),
    ("x", re.compile(r"\s+[xX×]\s+"), True),
    ("with", re.compile(r"\s+with\s+", re.IGNORECASE), True),
    ("comma", re.compile(r",\s+"), False),
]


@dataclass
class CollabSuspicion:
    raw: str
    separator: str
    parts: list[str] = field(default_factory=list)
    suggestion: str = ""          # proposed canonical primary artist
    default_keep: bool = False    # True: default answer should be "keep as-is"


def _clean(part: str) -> str:
    return part.strip(" ()[]").strip()


def detect(raw: str, known_artists: set[str] | None = None,
           confirmed: set[str] | None = None) -> CollabSuspicion | None:
    """Return a CollabSuspicion if `raw` looks like a multi-artist value.

    known_artists: artist names already in the library (folder names). Used
    only to *prefer* a split part as the suggestion — NOT to exempt: a folder
    may itself be an un-normalized collab from an earlier import.
    confirmed: values the user explicitly confirmed as single artists
    (identity mappings in the decision store). Only these exempt `raw`.
    """
    raw = raw.strip()
    if not raw:
        return None
    # folded -> canonical spelling, so suggestions use the library's casing
    # ("BONES & Eddy Baker" suggests "Bones" when that folder already exists)
    known = {fold(a): a.strip() for a in (known_artists or set())}
    if fold(raw) in {fold(a) for a in (confirmed or set())}:
        return None

    for name, pattern, split_default in _SEPARATORS:
        if not pattern.search(raw):
            continue
        parts = [_clean(p) for p in pattern.split(raw) if _clean(p)]
        if len(parts) < 2:
            continue
        suggestion = parts[0]
        # Prefer a part the library already knows (earliest part wins),
        # spelled the way the library spells it.
        for part in parts:
            if fold(part) in known:
                suggestion = known[fold(part)]
                break
        return CollabSuspicion(
            raw=raw,
            separator=name,
            parts=parts,
            suggestion=suggestion,
            default_keep=not split_default,
        )
    return None
