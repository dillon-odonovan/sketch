#!/usr/bin/env python3
"""Build the localized→English name table used by the Replica OCR post-process.

Offline build step. Fetches PokeAPI's name CSVs (pinned to an immutable commit
SHA for reproducibility) and writes a committed gzipped-JSON table that
``sketch/champions/name_lookup.py`` reads at runtime. The deployed bot never
fetches this data — it only reads the committed file — so this script is run by
hand when PokeAPI ships names for a new content patch, then the regenerated
file is committed alongside.

The GitHub raw host is used because it serves the pinned ref immutably and is
reachable from restricted networks; the same data also lives at ``pokeapi.co``.

For each of species / items / abilities / moves we map every localized name (in
the supported game languages) to that entry's canonical English name, keyed by
``normalize`` — imported from the runtime module so build-time keys and runtime
lookups can never drift.

Usage:
    python bin/build_pokeapi_names.py
    python bin/build_pokeapi_names.py --ref <commit-sha>   # pin a different ref
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# Import the shared normalizer so keys written here match lookups at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
from sketch.champions.name_lookup import normalize  # noqa: E402

# Pinned PokeAPI commit. Bump deliberately (and re-run) to pick up new content;
# pinning a SHA rather than a branch keeps regenerated tables reproducible.
DEFAULT_REF = "329253f4b502293f5cf0eaee5a8ba672c7ca7828"

_RAW_BASE = "https://raw.githubusercontent.com/PokeAPI/pokeapi/{ref}/data/v2/csv/{name}"

_ENGLISH_LANG_ID = 9

# PokeAPI local_language_id → game language. English (9) is included as a source
# so English screen text is canonicalized to PokeAPI's spelling too. IDs verified
# against languages.csv; ja-hrkt/ja are kana/kanji, zh-hant/zh-hans are
# Traditional/Simplified Chinese.
_SOURCE_LANGUAGES: dict[int, str] = {
    1: "ja-Hrkt",
    11: "ja",
    3: "ko",
    4: "zh-Hant",
    12: "zh-Hans",
    5: "fr",
    6: "de",
    7: "es",
    8: "it",
    9: "en",
}

# Output category → (CSV file, id column).
_SOURCES: dict[str, tuple[str, str]] = {
    "species": ("pokemon_species_names.csv", "pokemon_species_id"),
    "items": ("item_names.csv", "item_id"),
    "abilities": ("ability_names.csv", "ability_id"),
    "moves": ("move_names.csv", "move_id"),
}

# Hand-curated localized→English fills for names PokeAPI's CSVs are missing.
# PokeAPI's localized data lags for the newest content: the Gen 9 / DLC moves
# below have no Korean (and often no Chinese / German / Spanish / Italian) rows
# at any ref, so a foreign-language read of one falls through to the model's
# guess and can land on a look-alike (e.g. Korean Matcha Gotcha → "Strength
# Sap"). Each value is the official in-game name (sourced from Bulbapedia's
# "In other languages") keyed by the same language tags as _SOURCE_LANGUAGES;
# only the languages PokeAPI omits are listed. Revisit when bumping the ref —
# entries PokeAPI has since filled in become redundant (harmless, but prunable).
_OVERRIDES: dict[str, dict[str, dict[str, str]]] = {
    "moves": {
        "Matcha Gotcha": {
            "ko": "휘적휘적포",
            "zh-Hant": "刷刷茶炮",
            "zh-Hans": "刷刷茶炮",
            "de": "Quirlschuss",
            "es": "Cañón Batidor",
            "it": "Spruzzatè",
        },
        "Blood Moon": {
            "ko": "블러드문",
            "zh-Hant": "血月",
            "zh-Hans": "血月",
            "de": "Blutmond",
            "es": "Luna Roja",
            "it": "Luna Rossa",
        },
        "Syrup Bomb": {
            "ko": "시럽봄",
            "zh-Hant": "糖漿炸彈",
            "zh-Hans": "糖浆炸弹",
            "de": "Sirupbombe",
            "es": "Bomba Caramelo",
            "it": "Bomba Sciroppata",
        },
        "Ivy Cudgel": {
            "ko": "덩굴방망이",
            "zh-Hant": "棘藤棒",
            "zh-Hans": "棘藤棒",
            "de": "Rankenkeule",
            "es": "Garrote Liana",
            "it": "Clava di Liane",
        },
        "Blazing Torque": {
            "ko": "번액셀",
            "zh-Hans": "灼热暴冲",
            "de": "Hitzeturbo",
            "es": "Pirochoque",
        },
        "Wicked Torque": {
            "ko": "다크액셀",
            "zh-Hans": "黑暗暴冲",
            "de": "Finsterturbo",
            "es": "Ominochoque",
        },
        "Noxious Torque": {
            "ko": "포이즌액셀",
            "zh-Hans": "剧毒暴冲",
            "de": "Toxiturbo",
            "es": "Ponzochoque",
        },
        "Combat Torque": {
            "ko": "파이트액셀",
            "zh-Hans": "格斗暴冲",
            "de": "Raufturbo",
            "es": "Pugnachoque",
        },
        "Magical Torque": {
            "ko": "매지컬액셀",
            "zh-Hans": "魔法暴冲",
            "de": "Zauberturbo",
            "es": "Feerichoque",
        },
    },
}

# Gzipped: the raw JSON is ~850 KB (mostly multi-byte CJK), which trips the
# large-file commit guard; gzip lands it near ~270 KB and the runtime loader
# decompresses on first use.
_OUTPUT_PATH = os.path.join(
    os.path.dirname(__file__),
    os.pardir,
    "sketch",
    "champions",
    "data",
    "pokeapi_names.json.gz",
)


def _fetch_csv(ref: str, name: str) -> str:
    url = _RAW_BASE.format(ref=ref, name=name)
    print(f"  fetching {name} …", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (pinned host)
        return resp.read().decode("utf-8")


def _build_table(csv_text: str, id_col: str) -> dict[str, str]:
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    # entity_id → canonical English name.
    english: dict[str, str] = {
        r[id_col]: r["name"]
        for r in rows
        if int(r["local_language_id"]) == _ENGLISH_LANG_ID
    }

    table: dict[str, str] = {}
    collisions = 0
    for r in rows:
        if int(r["local_language_id"]) not in _SOURCE_LANGUAGES:
            continue
        en = english.get(r[id_col])
        if not en:
            continue
        key = normalize(r["name"])
        if not key:
            continue
        existing = table.get(key)
        if existing is not None and existing != en:
            # Same normalized glyphs across two entries — keep the first
            # deterministically (sorted CSV order). Rare; logged for audit.
            collisions += 1
            continue
        table[key] = en

    if collisions:
        print(f"    {collisions} normalized-key collision(s) skipped", file=sys.stderr)
    return table


def _apply_overrides(
    table: dict[str, str], overrides: dict[str, dict[str, str]]
) -> int:
    """Merge hand-curated localized→English fills into a built table in place.

    `overrides` maps English name → {language tag: localized name}; each
    localized name is normalized to a key (same as the CSV path) pointing at the
    English name. Returns the number of keys newly added (already-present keys,
    e.g. a language PokeAPI has since filled, are left untouched).
    """
    added = 0
    for english, by_language in overrides.items():
        for localized in by_language.values():
            key = normalize(localized)
            if not key or key in table:
                continue
            table[key] = english
            added += 1
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"PokeAPI commit SHA to pin (default: {DEFAULT_REF}).",
    )
    args = parser.parse_args()

    print(f"Building PokeAPI name table from ref {args.ref}", file=sys.stderr)
    payload: dict[str, object] = {
        "_meta": {
            "source": "PokeAPI/pokeapi data/v2/csv",
            "source_ref": args.ref,
            "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "languages": sorted(set(_SOURCE_LANGUAGES.values())),
        }
    }
    for category, (filename, id_col) in _SOURCES.items():
        table = _build_table(_fetch_csv(args.ref, filename), id_col)
        added = _apply_overrides(table, _OVERRIDES.get(category, {}))
        suffix = f" (+{added} override fills)" if added else ""
        print(f"    {category}: {len(table)} localized names{suffix}", file=sys.stderr)
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
