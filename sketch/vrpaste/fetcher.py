"""Fetch a VRPaste's team data and convert it to a `TeamData`.

VRPastes' public page is a Next.js shell — the team itself is loaded
client-side from a separate backend endpoint at
`https://vrpaste-backend.vercel.app/api/paste/<id>?lang=english`. We
hit that endpoint directly and convert the JSON into the same
`TeamData` / `PokemonEntry` shape every other team producer in this
bot returns, so the downstream Pokepaste renderer + sheet writer don't
need to know the team came from VRPaste.

Refuses password-protected / encrypted pastes with a user-facing
message rather than trying to decrypt — the backend doesn't return
team data for those.

Sidesteps the line-ending caveat in issue #30 (VRPaste's clipboard
export uses `\\n\\n`, which pokepast.es treats as a single Pokemon
block): we never touch the exported text, we build a `TeamData`
directly and let `sketch.pokepaste.renderer.render_showdown` emit
canonical CRLF separators when it serializes for upload.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from sketch.team import STAT_KEYS, PokemonEntry, TeamData
from sketch.vrpaste.validator import extract_vrpaste_id

logger = logging.getLogger(__name__)


class VRPasteFetchError(Exception):
    """Raised when we couldn't turn a VRPaste URL into a usable TeamData.

    Message is user-facing; the slash-command handler forwards it
    verbatim. Diagnostic detail (URLs, HTTP statuses, JSON snippets)
    goes to WARNING-level logs, not into this message.
    """


# Production backend that the VRPaste web client itself queries. Not
# under the user-visible vrpastes.com host — point at the Vercel-hosted
# API project directly. The `lang` query param selects the translation
# bundle returned alongside each field; we ignore the translated copies
# and only read the canonical English values.
_VRPASTE_API_BASE = "https://vrpaste-backend.vercel.app/api/paste"


async def fetch_vrpaste(url: str) -> TeamData:
    """Resolve a VRPaste URL into a `TeamData` ready for `render_showdown`.

    Raises `VRPasteFetchError` on any failure path (password-protected
    paste, HTTP error, malformed payload, zero pokemon). The id is
    extracted via `sketch.vrpaste.validator.extract_vrpaste_id` so URL
    spelling is validated as a side effect.
    """
    vrpaste_id = extract_vrpaste_id(url)
    api_url = f"{_VRPASTE_API_BASE}/{vrpaste_id}?lang=english"

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp,
        ):
            if resp.status != 200:
                logger.warning(
                    "VRPaste backend returned HTTP %s for id=%s",
                    resp.status,
                    vrpaste_id,
                )
                raise VRPasteFetchError(
                    "Couldn't fetch that VRPaste right now — please try "
                    "again in a moment."
                )
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as exc:
                logger.warning(
                    "VRPaste backend returned non-JSON for id=%s: %s",
                    vrpaste_id,
                    exc,
                )
                raise VRPasteFetchError(
                    "Couldn't read that VRPaste — the backend returned an "
                    "unexpected response. Please try again in a moment."
                ) from exc
    except aiohttp.ClientError as exc:
        logger.warning("VRPaste fetch transport error for id=%s: %s", vrpaste_id, exc)
        raise VRPasteFetchError(
            "Couldn't fetch that VRPaste right now — please try again in a moment."
        ) from exc

    if not isinstance(payload, dict):
        logger.warning(
            "VRPaste backend returned non-object payload for id=%s: %r",
            vrpaste_id,
            payload,
        )
        raise VRPasteFetchError(
            "Couldn't read that VRPaste — unexpected response shape."
        )

    if payload.get("is_encrypted") or payload.get("hasPassword"):
        raise VRPasteFetchError(
            "This VRPaste is password-protected and can't be added — please "
            "share the unlocked version."
        )

    raw_pokemon = payload.get("teams")
    if not isinstance(raw_pokemon, list) or not raw_pokemon:
        logger.warning(
            "VRPaste id=%s payload missing or empty 'teams' field",
            vrpaste_id,
        )
        raise VRPasteFetchError(
            "Couldn't read that VRPaste — no pokemon found in the team."
        )

    try:
        pokemon = [_parse_pokemon(p) for p in raw_pokemon]
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "VRPaste id=%s payload failed local parse: %s; payload=%r",
            vrpaste_id,
            exc,
            payload,
        )
        raise VRPasteFetchError(
            "Couldn't read that VRPaste — unexpected pokemon shape."
        ) from exc

    return TeamData(pokemon=pokemon)


def _parse_pokemon(raw: Any) -> PokemonEntry:
    """Convert one entry in the API's `teams` list into a `PokemonEntry`.

    The VRPaste backend uses "teams" as the list of Pokemon (slight
    misnomer in their schema). Per-Pokemon fields the renderer needs:

      species (string, required)  → species
      item    (string, optional)  → item  (None when missing/empty)
      ability (string, required)  → ability
      moves   (list[string])      → moves (first 4 only — Showdown caps at 4)
      nature  (string, required)  → nature
      gender  (string, optional)  → gender ("M"/"F" or None)
      evs     (dict, optional)    → evs   (defaults to all-zero when absent)

    The sample paste the feature was developed against doesn't include
    EVs in the response; we default to an all-zero dict so the renderer
    omits the EV line entirely (matching the existing behavior for
    zero-investment teams). If/when VRPaste starts surfacing EVs we
    pick them up automatically with no code change here, since we
    already iterate STAT_KEYS to populate the dict.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"pokemon entry must be a dict, got {type(raw).__name__}")

    species = raw.get("species")
    if not isinstance(species, str) or not species:
        raise ValueError("pokemon entry missing 'species'")
    ability = raw.get("ability")
    if not isinstance(ability, str) or not ability:
        raise ValueError(f"pokemon {species!r} missing 'ability'")
    nature = raw.get("nature")
    if not isinstance(nature, str) or not nature:
        raise ValueError(f"pokemon {species!r} missing 'nature'")
    raw_moves = raw.get("moves")
    if not isinstance(raw_moves, list) or not raw_moves:
        raise ValueError(f"pokemon {species!r} missing 'moves'")

    item_raw = raw.get("item")
    item = item_raw if isinstance(item_raw, str) and item_raw else None
    gender_raw = raw.get("gender")
    gender = gender_raw if gender_raw in ("M", "F") else None

    evs_raw = raw.get("evs") or {}
    evs: dict[str, int] = {}
    for key in STAT_KEYS:
        value = evs_raw.get(key, 0) if isinstance(evs_raw, dict) else 0
        try:
            evs[key] = max(0, int(value))
        except (TypeError, ValueError):
            evs[key] = 0

    moves = [str(m) for m in raw_moves[:4]]

    return PokemonEntry(
        species=species,
        gender=gender,
        item=item,
        ability=ability,
        nature=nature,
        evs=evs,
        moves=moves,
    )
