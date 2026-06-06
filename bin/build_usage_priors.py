#!/usr/bin/env python3
"""Build the Showdown usage-prior table used by the EV converter's Tier 2.

Offline build step. Fetches Smogon's monthly "chaos" usage stats for a format
and writes a committed gzipped-JSON artifact that
``sketch/convert/usage_priors.py`` reads at runtime. The deployed bot never
fetches this data — it only reads the committed file — so this script is run by
hand when a regulation or metagame shifts, then the regenerated file is
committed alongside.

The operator only names the *format*; the Smogon format id and the output
filename are both derived from ``USAGE_PRIOR_FORMATS`` in the runtime module, so
the slug written here and the slug read at runtime can never drift (the same
discipline ``build_name_table.py`` follows by importing ``normalize``).

The chaos ``Spreads`` field is keyed ``Nature:hp/atk/def/spa/spd/spe → weight``.
We keep the top-N spreads per species by weight, giving the runtime a compact
``P(spread | species, nature)`` to draw the most-used spread from.

Usage:
    python bin/build_usage_priors.py --month 2026-04
    python bin/build_usage_priors.py --format "Reg M-A" --month 2026-04 --rating 1630
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Import shared format mapping + species normalizer so build-time keys/filenames
# match runtime lookups exactly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
from sketch.convert.ev_model import Format, ev_model_for_format  # noqa: E402
from sketch.convert.usage_priors import (  # noqa: E402
    USAGE_PRIOR_FORMATS,
    artifact_resource,
    normalize_species,
)

_STATS = ("hp", "atk", "def", "spa", "spd", "spe")

# Spreads kept per species. Enough to cover the common natures for a mon while
# keeping the committed artifact small.
_TOP_N = 10

_CHAOS_URL = "https://www.smogon.com/stats/{month}/chaos/{fmt}-{rating}.json.gz"

_DATA_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "sketch", "convert", "data"
)


def _fetch_chaos(month: str, fmt_id: str, rating: int) -> dict:
    """Fetch + decompress one chaos JSON, failing loudly on a missing file."""
    url = _CHAOS_URL.format(month=month, fmt=fmt_id, rating=rating)
    print(f"Fetching {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Fetch failed ({exc.code}) for {url}\n"
            f"  → try a different --rating (e.g. 1500) or --month."
        ) from exc
    return json.loads(gzip.decompress(body).decode("utf-8"))


def _parse_spread_key(key: str) -> tuple[str, dict[str, int]] | None:
    """Parse a ``Nature:hp/atk/def/spa/spd/spe`` chaos key into (nature, evs)."""
    nature, _, ev_str = key.partition(":")
    parts = ev_str.split("/")
    if len(parts) != len(_STATS):
        return None
    try:
        evs = {stat: int(parts[i]) for i, stat in enumerate(_STATS)}
    except ValueError:
        return None
    return nature.strip(), evs


def _build(data: dict, max_per_stat: int) -> tuple[dict[str, list[dict]], int]:
    """Distill per-species top-N spreads; return (spreads, max EV observed)."""
    spreads: dict[str, list[dict]] = {}
    max_ev = 0
    for species, info in data.items():
        raw_spreads = (info or {}).get("Spreads") or {}
        entries: list[dict] = []
        for key, weight in raw_spreads.items():
            parsed = _parse_spread_key(key)
            if parsed is None:
                continue
            nature, evs = parsed
            max_ev = max(max_ev, *evs.values())
            entries.append(
                {"nature": nature, "evs": evs, "weight": round(float(weight), 4)}
            )
        if not entries:
            continue
        entries.sort(key=lambda e: e["weight"], reverse=True)
        spreads[normalize_species(species)] = entries[:_TOP_N]

    # Scale guard: the Champions ladder stores EVs as 0–32 stat points. A max
    # above the regime cap means the data is on a different scale (e.g. 0–252)
    # and would be silently mis-clamped at runtime — fail loudly instead.
    if max_ev > max_per_stat:
        raise SystemExit(
            f"Max EV observed ({max_ev}) exceeds the format cap ({max_per_stat}).\n"
            f"  → the source data is on a different scale; do not commit this."
        )
    return spreads, max_ev


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        default=str(Format.REG_M_A),
        help='Canonical format name (default: "Reg M-A").',
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Smogon stats month, e.g. 2026-04.",
    )
    parser.add_argument(
        "--rating",
        type=int,
        default=1630,
        help="Ladder rating cutoff (default: 1630 — balances sample size and skill).",
    )
    args = parser.parse_args()

    try:
        fmt = Format(args.format)
    except ValueError:
        supported = ", ".join(f'"{f}"' for f in USAGE_PRIOR_FORMATS)
        raise SystemExit(
            f'Unknown format "{args.format}". Supported: {supported}.'
        ) from None
    spec = USAGE_PRIOR_FORMATS.get(fmt)
    if spec is None:
        supported = ", ".join(f'"{f}"' for f in USAGE_PRIOR_FORMATS)
        raise SystemExit(
            f'No usage-prior mapping for "{args.format}". Supported: {supported}.'
        )

    max_per_stat = ev_model_for_format(args.format).max_per_stat
    data = _fetch_chaos(args.month, spec.smogon_id, args.rating).get("data", {})
    spreads, max_ev = _build(data, max_per_stat)
    print(
        f"  {len(spreads)} species, max EV observed {max_ev} (cap {max_per_stat})",
        file=sys.stderr,
    )

    payload = {
        "_meta": {
            "source": "smogon.com chaos stats",
            "smogon_id": spec.smogon_id,
            "month": args.month,
            "rating": args.rating,
            "max_ev_observed": max_ev,
            "species_count": len(spreads),
            "top_n": _TOP_N,
            "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "spreads": dict(sorted(spreads.items())),
    }

    os.makedirs(_DATA_DIR, exist_ok=True)
    out_path = os.path.join(_DATA_DIR, artifact_resource(spec.slug))
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    # mtime=0 keeps the gzip header byte-stable so re-running on the same inputs
    # produces no spurious diff.
    with gzip.GzipFile(out_path, "wb", compresslevel=9, mtime=0) as f:
        f.write(body)
    print(f"Wrote {os.path.normpath(out_path)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
