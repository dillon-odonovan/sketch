"""Turn a TeamData (from any source) into a PokePaste.

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

The rendered shape: gender suffix on the species line, EVs line listing
only non-zero stats, Nature line, four `- Move` lines. No Tera Type,
Level, or IVs — Champions Replica share screens don't surface those,
VRPaste's API doesn't expose them, and the reference paste that
round-trips cleanly through pokepast.es and the in-sheet AppsScript
omits them.
"""

from __future__ import annotations

import logging

import aiohttp

from sketch.pokepaste.validator import canonicalize_pokepaste_url
from sketch.team import STAT_KEYS, PokemonEntry, TeamData

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

# pokepast.es accepts POST to /create with form-encoded fields and responds
# with a 303 redirect whose Location header is the new paste's path
# (e.g. "/abc123def"). We follow the redirect ourselves with
# `allow_redirects=False` so we can capture the URL — letting aiohttp follow
# it would land us on the rendered HTML page and waste bandwidth re-reading
# the paste we just submitted.
_POKEPASTE_HOST = "https://pokepast.es"
_POKEPASTE_CREATE_URL = f"{_POKEPASTE_HOST}/create"

# CRLF line endings throughout the rendered paste. Pokemon Showdown's
# clipboard export uses CRLF (authored for Windows clipboard interchange),
# and pokepast.es's parser splits Pokemon blocks on `\r\n\r\n` specifically
# — emitting plain LF produces a paste that pokepast.es treats as one
# giant single-Pokemon entry instead of six separate ones. The in-sheet
# TEAMDATAFROMPASTE AppsScript fetches the same canonical raw text from
# pokepast.es, so CRLF round-trips through both renderers without surprise.
_LINE_END = "\r\n"


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

    return _LINE_END.join(lines)


def render_showdown(team: TeamData) -> str:
    """Render the full team as one PokePaste-compatible string.

    Each Pokemon block is separated by a blank line. CRLF line endings
    are required by pokepast.es's block-splitter — see `_LINE_END`. No
    trailing newline, matching Showdown's clipboard export shape.
    """
    return (_LINE_END * 2).join(_render_mon(p) for p in team.pokemon)


async def post_to_pokepaste(paste_text: str, title: str) -> str:
    """Upload `paste_text` to pokepast.es and return the canonical URL.

    POSTs to `/create` with the same form fields the website uses
    (paste, title, author, notes). pokepast.es responds with a 303
    redirect whose Location header is the new paste's path; we extract
    the URL from that header rather than follow the redirect.

    Round-trips the URL through `canonicalize_pokepaste_url` so the
    value handed to `SheetsClient.add_row` matches the form
    `find_row_by_url` uses for dedup, and so a malformed Location is
    caught here rather than at row-write time.

    Raises `PokepasteUploadError` on any non-redirect response or
    transport failure. The message is user-facing.
    """
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                _POKEPASTE_CREATE_URL,
                # Match the website's form submission shape exactly: paste +
                # title + empty author + empty notes. Sending only the
                # fields we care about works too, but matching the browser
                # avoids any server-side validation surprise.
                data={
                    "paste": paste_text,
                    "title": title,
                    "author": "",
                    "notes": "",
                },
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
                raise PokepasteUploadError(
                    "Couldn't upload the team to pokepast.es right now — "
                    "please try again in a moment."
                )
            location = resp.headers.get("Location", "").strip()
            if not location:
                logger.warning(
                    "Pokepaste create %s response missing Location header",
                    resp.status,
                )
                raise PokepasteUploadError(
                    "Couldn't upload the team to pokepast.es right now — "
                    "please try again in a moment."
                )
    except aiohttp.ClientError as exc:
        logger.warning("Pokepaste upload transport error: %s", exc)
        raise PokepasteUploadError(
            "Couldn't upload the team to pokepast.es right now — "
            "please try again in a moment."
        ) from exc

    absolute = (
        location if location.startswith("http") else f"{_POKEPASTE_HOST}{location}"
    )
    return canonicalize_pokepaste_url(absolute)
