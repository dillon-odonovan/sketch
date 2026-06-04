"""Format-driven EV regime.

Different competitive formats train Pokemon under different EV rules.
Pokemon Champions formats (Regulation M-A, M-B, …) cap EVs at 32 per
stat ("Stat Points"); mainline VGC formats (Regulation A/B, VGC 20xx, …)
cap at 252 per stat within a 510 total ("Legacy EVs"). The converter is
only wired for Champions today, but every place that depends on the cap
reads it from an `EvModel` rather than a literal so adding a legacy
format later is a registry entry, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass

from sketch import config
from sketch.pokepaste.validator import ValidationError
from sketch.team import CHAMPIONS_EV_MAX_PER_STAT


@dataclass(frozen=True)
class EvModel:
    """The EV rules for a family of formats.

    `label` is a short human name for the regime (surfaced in the LLM
    prompt). `max_per_stat` is the per-stat cap used to validate mined
    spreads and clamp guessed ones.
    """

    label: str
    max_per_stat: int


# Champions: sparse spreads, 32 per stat. The cap lives in `team.py` so the
# strict Showdown parser and this model share one source of truth.
CHAMPIONS = EvModel(label="Stat Points", max_per_stat=CHAMPIONS_EV_MAX_PER_STAT)

# Mainline VGC. Defined for extensibility — no format maps to it yet, so the
# converter never selects it, but it documents the intended shape and makes
# "support a legacy format" a one-line `FORMAT_EV_MODELS` addition.
LEGACY = EvModel(label="Legacy EVs", max_per_stat=252)


# Maps a format name (the keys of `config.FORMAT_SHEETS`) to its EV regime.
# Only Champions formats are present today; a legacy format would map to
# `LEGACY` here and otherwise flow through the same converter unchanged.
FORMAT_EV_MODELS: dict[str, EvModel] = {
    name: CHAMPIONS for name in config.FORMAT_SHEETS
}


class UnsupportedFormatError(ValidationError):
    """Raised when a format has no EV model wired up.

    Subclasses `ValidationError` so command handlers that already catch
    that type for user-facing input problems surface this the same way.
    The message is user-facing.
    """


def ev_model_for_format(fmt_name: str) -> EvModel:
    """Return the `EvModel` for `fmt_name`, or raise `UnsupportedFormatError`.

    Every format in `config.FORMAT_SHEETS` is currently a Champions format,
    so this only raises if a new format is added to the choices before its
    EV regime is registered here — a fail-loud guard against silently
    converting a team under the wrong cap.
    """
    model = FORMAT_EV_MODELS.get(fmt_name)
    if model is None:
        raise UnsupportedFormatError(
            f"`{fmt_name}` isn't supported by /convert-ots yet."
        )
    return model
