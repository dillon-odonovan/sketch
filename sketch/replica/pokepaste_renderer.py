"""Turn an extracted TeamData into a PokePaste.

Two steps the caller composes:
  1. `render_showdown(team)` — produce the Showdown / PokePaste export text,
     the same line-oriented format the in-sheet TEAMDATAFROMPASTE AppsScript
     already knows how to parse.
  2. `post_to_pokepaste(text, title)` — POST that text to pokepast.es and
     return the canonical URL of the new paste, ready to hand to
     `SheetsClient.add_row`.

The two steps are separate so tests can golden-file the text without
touching the network, and so the slash command can render once and then
reuse the rendered string for both the upload and the user-facing preview.
"""

from __future__ import annotations

import logging

import aiohttp

from sketch.pokepaste_validator import canonicalize_pokepaste_url
from sketch.replica.extractor import STAT_KEYS, PokemonEntry, TeamData

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised when the upload to pokepast.es fails. Message is user-facing."""


# Display capitalization for the EV / IV lines. The Showdown / PokePaste
# parsers accept lowercase too, but the canonical form uses these tags and
# matching it keeps the resulting paste readable when an operator opens it
# manually.
_STAT_DISPLAY = {
    "hp": "HP",
    "atk": "Atk",
    "def": "Def",
    "spa": "SpA",
    "spd": "SpD",
    "spe": "Spe",
}

_POKEPASTE_CREATE_URL = "https://pokepast.es/create"
_POKEPASTE_HOST = "https://pokepast.es"


def _render_mon(p: PokemonEntry) -> str:
    """Render one Pokemon as a Showdown-export block.

    Field order matches the de facto convention (species/item, Ability,
    Level, Tera Type, EVs, IVs, Nature, moves) so the result is visually
    indistinguishable from a hand-edited Showdown export.
    """
    lines: list[str] = []

    item_suffix = f" @ {p.item}" if p.item else ""
    lines.append(f"{p.species}{item_suffix}")
    lines.append(f"Ability: {p.ability}")

    # Always emit Level. Both Showdown and PokePaste default to 100 when
    # the line is absent — VGC mons are level 50, so an absent line would
    # silently misrepresent the team.
    lines.append(f"Level: {p.level}")

    lines.append(f"Tera Type: {p.tera_type}")

    ev_parts = [
        f"{p.evs[k]} {_STAT_DISPLAY[k]}" for k in STAT_KEYS if p.evs.get(k, 0) > 0
    ]
    if ev_parts:
        lines.append(f"EVs: {' / '.join(ev_parts)}")

    if p.ivs is not None:
        iv_parts = [
            f"{p.ivs[k]} {_STAT_DISPLAY[k]}"
            for k in STAT_KEYS
            if p.ivs.get(k, 31) != 31
        ]
        if iv_parts:
            lines.append(f"IVs: {' / '.join(iv_parts)}")

    lines.append(f"{p.nature} Nature")

    for move in p.moves:
        lines.append(f"- {move}")

    return "\n".join(lines)


def render_showdown(team: TeamData) -> str:
    """Render the full team as one PokePaste-compatible string.

    Each Pokemon block is separated by a blank line. No trailing newline —
    matching how Showdown's clipboard export and pokepast.es's display
    treat the format.
    """
    return "\n\n".join(_render_mon(p) for p in team.pokemon)


async def post_to_pokepaste(paste_text: str, title: str) -> str:
    """Upload `paste_text` to pokepast.es and return the canonical URL.

    pokepast.es responds to a successful create with a 302/303 redirect
    whose Location header is the new paste's path (typically relative,
    e.g. "/abc123def"). We follow that redirect ourselves so we can capture
    the URL — letting aiohttp follow it would land us on the rendered HTML
    page with no easy way to read back the URL we ended up at.

    Raises `RenderError` on any non-redirect response or transport failure.
    The message is user-facing.
    """
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                _POKEPASTE_CREATE_URL,
                data={"title": title, "paste": paste_text},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp,
        ):
            if resp.status not in (301, 302, 303, 307, 308):
                body_excerpt = (await resp.text())[:200]
                logger.warning(
                    "Pokepaste create returned non-redirect status %s: %s",
                    resp.status,
                    body_excerpt,
                )
                raise RenderError(
                    "Couldn't upload the team to pokepast.es right now — "
                    "please try again in a moment."
                )
            location = resp.headers.get("Location", "").strip()
            if not location:
                raise RenderError(
                    "Couldn't upload the team to pokepast.es right now — "
                    "please try again in a moment."
                )
    except aiohttp.ClientError as exc:
        logger.warning("Pokepaste upload transport error: %s", exc)
        raise RenderError(
            "Couldn't upload the team to pokepast.es right now — "
            "please try again in a moment."
        ) from exc

    absolute = (
        location if location.startswith("http") else f"{_POKEPASTE_HOST}{location}"
    )
    # Round-trip through the canonicalizer so the URL handed to
    # SheetsClient.add_row matches the form `find_row_by_url` will compare
    # against on dedup, and so a malformed Location is caught here rather
    # than at row-write time.
    return canonicalize_pokepaste_url(absolute)
