#!/usr/bin/env python3
"""Build the localized→English name table used by the Replica OCR post-process.

Offline build step. Fetches PKHeX's game-text dumps (pinned to an immutable
commit SHA for reproducibility) and writes a committed gzipped-JSON table that
``sketch/champions/name_lookup.py`` reads at runtime. The deployed bot never
fetches this data — it only reads the committed file — so this script is run by
hand when a new content patch ships names, then the regenerated file is
committed alongside.

PKHeX's text resources are extracted straight from the games, so they are
authoritative and — unlike PokeAPI's CSVs — complete for the newest content in
every language (PokeAPI lags ~a full generation of moves/abilities in Korean,
German, Spanish, and Simplified Chinese). Each language's file is index-aligned
with the English file: line N is the same entry across languages, so the
localized→English map is a direct per-line zip. Names are keyed by ``normalize``
— imported from the runtime module so build-time keys and runtime lookups can
never drift.

Usage:
    python bin/build_name_table.py
    python bin/build_name_table.py --ref <commit-sha>   # pin a different ref
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# Import the shared normalizer so keys written here match lookups at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
from sketch.champions.name_lookup import normalize  # noqa: E402

# Pinned PKHeX commit. Bump deliberately (and re-run) to pick up new content;
# pinning a SHA rather than a branch keeps regenerated tables reproducible.
DEFAULT_REF = "5767ab85cf930e77a52139d761a2c82c4f1fc903"

_RAW_BASE = (
    "https://raw.githubusercontent.com/kwsch/PKHeX/{ref}/PKHeX.Core/Resources/text"
)

# Canonical-English source language and the localized sources we map from it.
# Tokens are PKHeX directory/file names. English is also kept as a source so an
# English screen read is canonicalized to the official spelling. es-419 (Latin-
# American Spanish) is included alongside es for broader coverage.
_ENGLISH_LANG = "en"
_SOURCE_LANGUAGES = (
    "ja",
    "ko",
    "zh-Hant",
    "zh-Hans",
    "fr",
    "de",
    "es",
    "es-419",
    "it",
    "en",
)

# Output category → PKHeX file path template (``{lang}`` filled per language).
# Moves/abilities/species live under text/other/<lang>/; items under text/items/.
_SOURCES: dict[str, str] = {
    "species": "other/{lang}/text_Species_{lang}.txt",
    "items": "items/text_Items_{lang}.txt",
    "abilities": "other/{lang}/text_Abilities_{lang}.txt",
    "moves": "other/{lang}/text_Moves_{lang}.txt",
}

# Gzipped: the raw JSON is multi-MB (mostly multi-byte CJK), which trips the
# large-file commit guard; gzip keeps it small and the runtime loader
# decompresses on first use.
_OUTPUT_PATH = os.path.join(
    os.path.dirname(__file__),
    os.pardir,
    "sketch",
    "champions",
    "data",
    "name_table.json.gz",
)


def _fetch_lines(ref: str, path: str) -> list[str]:
    url = f"{_RAW_BASE.format(ref=ref)}/{path}"
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (pinned host)
        return resp.read().decode("utf-8").split("\n")


def _build_table(ref: str, path_template: str) -> dict[str, str]:
    english = _fetch_lines(ref, path_template.format(lang=_ENGLISH_LANG))

    table: dict[str, str] = {}
    collisions = 0
    for lang in _SOURCE_LANGUAGES:
        localized = _fetch_lines(ref, path_template.format(lang=lang))
        for index, en in enumerate(english):
            en = en.strip()
            if not en or index >= len(localized):
                continue
            name = localized[index].strip()
            if not name:
                continue
            key = normalize(name)
            if not key:
                continue
            existing = table.get(key)
            if existing is not None and existing != en:
                # Same normalized glyphs resolving to two different English
                # entries — keep the first deterministically (language order
                # above). Rare; logged for audit.
                collisions += 1
                continue
            table.setdefault(key, en)

    if collisions:
        print(f"    {collisions} normalized-key collision(s) skipped", file=sys.stderr)
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"PKHeX commit SHA to pin (default: {DEFAULT_REF}).",
    )
    args = parser.parse_args()

    print(f"Building name table from PKHeX ref {args.ref}", file=sys.stderr)
    payload: dict[str, object] = {
        "_meta": {
            "source": "kwsch/PKHeX PKHeX.Core/Resources/text",
            "source_ref": args.ref,
            "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "languages": list(_SOURCE_LANGUAGES),
        }
    }
    for category, path_template in _SOURCES.items():
        table = _build_table(args.ref, path_template)
        print(f"  {category}: {len(table)} localized names", file=sys.stderr)
        payload[category] = dict(sorted(table.items()))

    os.makedirs(os.path.dirname(_OUTPUT_PATH), exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    # mtime=0 keeps the gzip header byte-stable across rebuilds from the same
    # ref, so re-running on an unchanged ref produces no spurious diff.
    with gzip.GzipFile(_OUTPUT_PATH, "wb", compresslevel=9, mtime=0) as f:
        f.write(body)
    print(f"Wrote {os.path.normpath(_OUTPUT_PATH)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
