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

from dataclasses import dataclass, field

from sketch.pokepaste.validator import ValidationError
from sketch.team import CHAMPIONS_EV_MAX_PER_STAT

# Champions approximate total budget. The game doesn't publish the exact
# internal formula, but competitive observation puts the usable total at
# roughly 66 Stat Points (two stats fully invested at 32 each with a few
# left over for a third). Used as a soft filter in `ev_matcher` to
# de-prioritize bank spreads that are suspiciously over budget.
CHAMPIONS_EV_MAX_TOTAL = 66

# Mainline VGC (Gen 8/9): 508 total EVs, 252 per stat, 4 everywhere else.
LEGACY_EV_MAX_TOTAL = 508


@dataclass(frozen=True)
class EvModel:
    """The EV rules for a family of formats.

    `label` is a short human name for the regime (surfaced in the LLM
    prompt). `max_per_stat` is the per-stat cap used to validate mined
    spreads and clamp guessed ones. `max_total` is the total-EV budget
    across all stats; used as a soft sanity filter when mining bank spreads
    (spreads over budget are de-prioritised, not rejected outright, since
    the bank's strict Showdown parser doesn't enforce the total).
    """

    label: str
    max_per_stat: int
    max_total: int | None = field(default=None)


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
    max_per_stat=252,
    max_total=LEGACY_EV_MAX_TOTAL,
)


# Maps a format name (the keys of `config.FORMAT_SHEETS`) to its EV regime.
# Add an entry here whenever a new format is added to `config.FORMAT_SHEETS` —
# the `ev_model_for_format` guard will raise loudly if a format is missing.
FORMAT_EV_MODELS: dict[str, EvModel] = {
    "Reg M-A": CHAMPIONS,
}


class UnsupportedFormatError(ValidationError):
    """Raised when a format has no EV model wired up.

    Subclasses `ValidationError` so command handlers that already catch
    that type for user-facing input problems surface this the same way.
    The message is user-facing.
    """


def ev_model_for_format(fmt_name: str) -> EvModel:
    """Return the `EvModel` for `fmt_name`, or raise `UnsupportedFormatError`.

    Every format in `config.FORMAT_SHEETS` should have a corresponding entry
    in `FORMAT_EV_MODELS`. This raises loudly if a new format is added to the
    choices before its EV regime is registered — prevents silently converting
    under the wrong cap.
    """
    model = FORMAT_EV_MODELS.get(fmt_name)
    if model is None:
        raise UnsupportedFormatError(
            f"`{fmt_name}` isn't supported by /convert-ots yet."
        )
    return model
