"""Format-driven EV regime.

Different competitive formats train Pokemon under different EV rules.
Pokemon Champions formats (Regulation M-A, M-B, …) cap EVs at 32 per
stat and ~66 total ("Stat Points"); mainline VGC formats (Regulation A/B,
VGC 20xx, …) cap at 252 per stat within a 510 total ("Legacy EVs"). The
converter is only wired for Champions today, but every place that depends
on the cap reads it from an `EvModel` rather than a literal so adding a
legacy format later is a registry entry, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sketch.pokepaste.validator import ValidationError
from sketch.team import CHAMPIONS_EV_MAX_PER_STAT

# Champions approximate total budget. The game doesn't publish the exact
# internal formula, but competitive observation puts the usable total at
# roughly 66 Stat Points (two stats fully invested at 32 each with a few
# left over for a third). Surfaced in the LLM prompt so the model knows
# how sparse spreads should be.
CHAMPIONS_EV_MAX_TOTAL = 66

# Mainline VGC (Gen 8/9): 252 per stat, 508 usable total.
LEGACY_EV_MAX_PER_STAT = 252
LEGACY_EV_MAX_TOTAL = 508


class Format(StrEnum):
    """Canonical format identifiers.

    Using a `StrEnum` means each member IS a `str` (e.g.
    ``Format.REG_M_A == "Reg M-A"`` is ``True``), so existing code that
    passes plain string format names to `ev_model_for_format` continues to
    work without changes — dict lookup uses the inherited `str` hash.
    """

    REG_M_A = "Reg M-A"
    REG_M_B = "Reg M-B"


@dataclass(frozen=True)
class EvModel:
    """The EV rules for a family of formats.

    `label` is a short human name for the regime (surfaced in the LLM
    prompt). `max_per_stat` is the per-stat cap used to validate mined
    spreads and clamp guessed ones. `max_total` is the total-EV budget
    across all stats; informational (used in the LLM prompt) — it is NOT
    enforced when selecting bank spreads, since those come from real teams
    that the game itself constrains.
    """

    label: str
    max_per_stat: int
    max_total: int | None = None


# Champions: sparse spreads, 32 per stat, ~66 total. The per-stat cap lives
# in `team.py` so the strict Showdown parser and this model share one source.
CHAMPIONS = EvModel(
    label="Stat Points",
    max_per_stat=CHAMPIONS_EV_MAX_PER_STAT,
    max_total=CHAMPIONS_EV_MAX_TOTAL,
)

# Mainline VGC. Defined for extensibility — no format maps to it yet, so the
# converter never selects it, but it documents the intended shape and makes
# "support a legacy format" a one-line `FORMAT_EV_MODELS` addition.
LEGACY = EvModel(
    label="Legacy EVs",
    max_per_stat=LEGACY_EV_MAX_PER_STAT,
    max_total=LEGACY_EV_MAX_TOTAL,
)


# Maps a Format to its EV regime. Add an entry here whenever a new Format
# member is added — `ev_model_for_format` raises loudly if a format is
# missing, preventing silent conversion under the wrong cap.
FORMAT_EV_MODELS: dict[Format, EvModel] = {
    Format.REG_M_A: CHAMPIONS,
    Format.REG_M_B: CHAMPIONS,
}


class UnsupportedFormatError(ValidationError):
    """Raised when a format has no EV model wired up.

    Subclasses `ValidationError` so command handlers that already catch
    that type for user-facing input problems surface this the same way.
    The message is user-facing.
    """


def ev_model_for_format(fmt_name: str) -> EvModel:
    """Return the `EvModel` for `fmt_name`, or raise `UnsupportedFormatError`.

    Accepts a plain ``str`` or a ``Format`` member. ``Format(fmt_name)``
    performs the ``StrEnum`` coercion so the dict lookup is fully typed;
    a ``ValueError`` from an unrecognised string is caught and re-raised
    as ``UnsupportedFormatError`` — a fail-loud guard against silently
    converting under the wrong cap when a new format is added to
    `config.FORMAT_SHEETS`.
    """
    try:
        key = Format(fmt_name)
    except ValueError:
        raise UnsupportedFormatError(
            f"`{fmt_name}` isn't supported by /convert-ots yet."
        ) from None
    model = FORMAT_EV_MODELS.get(key)
    if model is None:
        # Format member exists but has no EvModel wired up — registry gap.
        raise UnsupportedFormatError(
            f"`{fmt_name}` isn't supported by /convert-ots yet."
        )
    return model
