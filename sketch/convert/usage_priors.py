"""Format-native external usage prior (Tier 2 of the EV backoff).

When the guild's known-teams bank has no match for an OTS Pokemon, this tier
supplies an *empirical* spread before the LLM fallback: the most-used spread
for that species + nature on the Showdown ladder, taken from Smogon's monthly
"chaos" usage stats.

The chaos JSON is multi-MB and only exposes *marginal* distributions (the
`Spreads` field is keyed `Nature:hp/atk/def/spa/spd/spe → weight`, separate
from `Items`/`Abilities`), so the realistic conditional prior is
``P(spread | species, nature)`` — nature is known from the OTS, item is not
jointly available. The distribution is distilled offline by
``bin/build_usage_priors.py`` into a small committed
``data/usage_priors_<slug>.json.gz`` that this module reads at runtime; nothing
here touches the network. ``normalize_species`` and ``USAGE_PRIOR_FORMATS`` are
shared with the build script so the keys/filenames written at build time and
read at runtime can never drift.
"""

from __future__ import annotations

import gzip
import importlib.resources
import json
import logging
from dataclasses import dataclass
from functools import lru_cache

from sketch.convert.bank import _norm_species
from sketch.convert.ev_matcher import EvChoice, clamp_evs
from sketch.convert.ev_model import EvModel, Format
from sketch.team import STAT_KEYS, PokemonEntry

logger = logging.getLogger(__name__)

_DATA_PACKAGE = "sketch.convert"
_DATA_DIR = "data"

# Weight-share thresholds (chosen spread's share of its species+nature usage)
# that map to a coarse confidence band surfaced in the conversion summary.
_HIGH_CONFIDENCE = 0.40
_MEDIUM_CONFIDENCE = 0.20


@dataclass(frozen=True)
class UsageFormatSpec:
    """How one Format maps onto Smogon usage stats.

    `smogon_id` is the chaos-stats format id (e.g. ``gen9championsvgc2026regma``)
    the build script fetches; `slug` names the committed artifact
    (``usage_priors_<slug>.json.gz``) and is what the runtime loader resolves.
    """

    smogon_id: str
    slug: str


# Single source of truth for build *and* runtime. Add an entry when a format
# gains usage-stat support; the build script imports this so the slug it writes
# and the slug the loader reads are always the same.
USAGE_PRIOR_FORMATS: dict[Format, UsageFormatSpec] = {
    Format.REG_M_A: UsageFormatSpec(
        smogon_id="gen9championsvgc2026regma", slug="reg-m-a"
    ),
}


@dataclass(frozen=True)
class SpreadEntry:
    """One usage spread for a species: a nature + EVs + its usage weight."""

    nature: str
    evs: dict[str, int]
    weight: float


@dataclass(frozen=True)
class UsagePriors:
    """Per-species usage spread distributions for one format."""

    # Normalized species name → spreads, descending by weight.
    spreads: dict[str, list[SpreadEntry]]


def normalize_species(name: str) -> str:
    """Casefold a species name for artifact keys and lookups.

    Delegates to ``bank._norm_species`` (rather than re-deriving the rule) so
    the keys written at build time and the OTS species looked up at runtime
    can never drift from how the bank matcher compares species.
    """
    return _norm_species(name)


def artifact_resource(slug: str) -> str:
    """The artifact filename for a format slug (shared build/runtime name)."""
    return f"usage_priors_{slug}.json.gz"


@lru_cache(maxsize=4)
def _load_artifact(slug: str) -> UsagePriors | None:
    """Load and cache the usage-prior artifact for `slug`.

    Returns None (and logs) if the artifact is missing or malformed, so a
    packaging slip degrades to "no usage prior" — the converter falls through
    to the LLM — rather than failing the conversion outright.
    """
    try:
        resource = importlib.resources.files(_DATA_PACKAGE).joinpath(
            _DATA_DIR, artifact_resource(slug)
        )
        raw = json.loads(gzip.decompress(resource.read_bytes()).decode("utf-8"))
    except (FileNotFoundError, ModuleNotFoundError, OSError, ValueError) as exc:
        logger.warning(
            "Could not load usage priors '%s'; usage tier disabled: %s", slug, exc
        )
        return None

    spreads: dict[str, list[SpreadEntry]] = {}
    for species, entries in (raw.get("spreads") or {}).items():
        parsed: list[SpreadEntry] = []
        for e in entries:
            evs = {k: int(e.get("evs", {}).get(k, 0)) for k in STAT_KEYS}
            parsed.append(
                SpreadEntry(
                    nature=str(e.get("nature", "")),
                    evs=evs,
                    weight=float(e.get("weight", 0.0)),
                )
            )
        spreads[species] = parsed
    return UsagePriors(spreads=spreads)


def load_usage_priors(fmt_name: str) -> UsagePriors | None:
    """Return the usage priors for `fmt_name`, or None if unsupported/missing.

    None means "no usage tier for this format" — an unmapped format or a
    missing artifact — and the caller simply skips the tier.
    """
    try:
        key = Format(fmt_name)
    except ValueError:
        return None
    spec = USAGE_PRIOR_FORMATS.get(key)
    if spec is None:
        return None
    return _load_artifact(spec.slug)


def _confidence_band(share: float) -> str:
    if share >= _HIGH_CONFIDENCE:
        return "high"
    if share >= _MEDIUM_CONFIDENCE:
        return "medium"
    return "low"


def choose_usage_spread(
    target: PokemonEntry,
    priors: UsagePriors,
    ev_model: EvModel,
    pins: dict[str, int] | None = None,
) -> EvChoice | None:
    """Pick the most-used spread for `target`'s species + nature.

    Applies the same hard constraints as every tier: EVs are clamped to
    `ev_model.max_per_stat`, and when `pins` are present only spreads whose
    clamped values honor *every* pin are eligible (mirrors the bank pin gate).

    Returns None — so the caller falls through to the LLM — when the species is
    absent from the priors, no spread matches the target's nature, or no spread
    survives the pin gate.
    """
    entries = priors.spreads.get(normalize_species(target.species))
    if not entries:
        return None

    nature = (target.nature or "").strip().lower()
    nature_matches = [e for e in entries if e.nature.strip().lower() == nature]
    if not nature_matches:
        return None

    # Pin gate: keep only spreads consistent with every confirmed stat.
    eligible = nature_matches
    if pins:
        eligible = [
            e
            for e in nature_matches
            if all(clamp_evs(e.evs, ev_model).get(k) == v for k, v in pins.items())
        ]
        if not eligible:
            return None

    # MAP estimate: the highest-weight eligible spread.
    chosen = max(eligible, key=lambda e: e.weight)
    evs = clamp_evs(chosen.evs, ev_model)

    # Confidence from the chosen spread's share of this species+nature usage.
    nature_total = sum(e.weight for e in nature_matches) or 1.0
    share = chosen.weight / nature_total
    band = _confidence_band(share)

    detail = (
        f"usage nature={chosen.nature} share={share:.2f} band={band} "
        f"(from {len(nature_matches)} nature-matching spread(s), "
        f"{len(eligible)} pin-consistent)"
    )
    return EvChoice(
        evs=evs,
        source="usage",
        detail=detail,
        source_url=None,
        confidence=band,
    )
