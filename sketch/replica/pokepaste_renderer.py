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

The rendered shape matches the sample output from the reference script
(`build_pokepaste.py`) used to validate the OCR approach end-to-end: gender
suffix on the species line, EVs line listing only non-zero stats, Nature
line, four `- Move` lines. No Tera Type, Level, or IVs — the Champions
Replica share screen doesn't surface those, and the reference paste
that round-trips cleanly through pokepast.es and the in-sheet AppsScript
omits them.
"""

from __future__ import annotations

import json
import logging

import aiohttp

from sketch.pokepaste_validator import canonicalize_pokepaste_url
from sketch.replica.extractor import STAT_KEYS, PokemonEntry, TeamData

logger = logging.getLogger(__name__)


class PokepasteUploadError(Exception):
    """Raised when the upload to pokepast.es fails. Message is user-facing."""


# Display capitalization for the EV line. The Showdown / PokePaste parsers
# accept lowercase too, but the canonical form uses these tags and matching
# it keeps the resulting paste readable when an operator opens it manually.
_STAT_DISPLAY = {
    "hp": "HP",
    "atk": "Atk",
    "def": "Def",
    "spa": "SpA",
    "spd": "SpD",
    "spe": "Spe",
}

# pokepast.es exposes a JSON variant of the create endpoint that returns
# {"id": "...", "url": "...", "author": "...", "title": "..."} as the body
# of a 200 response — no redirect to follow. Using this avoids the
# follow-the-Location dance of the HTML endpoint, which is fiddly to do
# in aiohttp without accidentally chasing onto the rendered paste page.
_POKEPASTE_CREATE_URL = "https://pokepast.es/create.json"


def _render_mon(p: PokemonEntry) -> str:
    """Render one Pokemon as a Showdown-export block.

    Field order follows the reference paste's convention exactly (species
    [+gender] [+item], Ability, EVs, Nature, four moves). Order is
    load-bearing because the in-sheet AppsScript `TEAMDATAFROMPASTE`
    pattern-matches against this shape — reorders here would silently
    break parsing for every replica-added team.

    TODO(champions-tera): when Pokemon Champions enables terastallization
    in-game, restore `tera_type` on `PokemonEntry` and emit a `Tera Type:`
    line between `Ability:` and `EVs:` (the canonical Showdown position).
    Level and IVs are intentionally omitted: Champions fixes level to 50
    and all IVs to 31, neither of which the share screen surfaces, so
    emitting them would synthesize data we didn't extract.
    """
    lines: list[str] = []

    gender_suffix = f" ({p.gender})" if p.gender else ""
    item_suffix = f" @ {p.item}" if p.item else ""
    lines.append(f"{p.species}{gender_suffix}{item_suffix}")

    lines.append(f"Ability: {p.ability}")

    ev_parts = [
        f"{p.evs[k]} {_STAT_DISPLAY[k]}" for k in STAT_KEYS if p.evs.get(k, 0) > 0
    ]
    if ev_parts:
        lines.append(f"EVs: {' / '.join(ev_parts)}")

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

    Uses the `/create.json` endpoint which returns a JSON body containing
    the new paste's URL directly — cleaner than the HTML variant that
    redirects via Location header. Round-trips the URL through
    `canonicalize_pokepaste_url` so the value handed to
    `SheetsClient.add_row` matches the form `find_row_by_url` uses for
    dedup, and so a malformed server response is caught here rather than
    at row-write time.

    Raises `PokepasteUploadError` on any non-2xx response or transport failure.
    The message is user-facing.
    """
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                _POKEPASTE_CREATE_URL,
                data={"title": title, "paste": paste_text},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp,
        ):
            if resp.status >= 400:
                body_excerpt = (await resp.text())[:200]
                logger.warning(
                    "Pokepaste create returned error status %s: %s",
                    resp.status,
                    body_excerpt,
                )
                raise PokepasteUploadError(
                    "Couldn't upload the team to pokepast.es right now — "
                    "please try again in a moment."
                )
            body = await resp.text()
    except aiohttp.ClientError as exc:
        logger.warning("Pokepaste upload transport error: %s", exc)
        raise PokepasteUploadError(
            "Couldn't upload the team to pokepast.es right now — "
            "please try again in a moment."
        ) from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.warning("Pokepaste /create.json returned non-JSON body: %s", body[:200])
        raise PokepasteUploadError(
            "Couldn't upload the team to pokepast.es right now — "
            "please try again in a moment."
        ) from exc

    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url:
        logger.warning("Pokepaste /create.json response missing url field: %s", payload)
        raise PokepasteUploadError(
            "Couldn't upload the team to pokepast.es right now — "
            "please try again in a moment."
        )
    return canonicalize_pokepaste_url(url)
