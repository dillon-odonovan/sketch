"""Load candidate teams from the guild's team bank.

To fill an OTS's missing EVs we mine the spreads of teams already in the
bank. The sheet stores only each team's URL + its six species (populated
by the in-sheet `TEAMDATAFROMPASTE` formula), not the full sets — so for
every bank team that shares at least one species with the OTS we fetch
its Pokepaste's raw Showdown text and parse it into a `TeamData`. Keeping
the whole parsed team (not just isolated mons) lets the matcher weigh how
much of a candidate team's composition overlaps the OTS.

Fetching is best-effort and bounded: only teams sharing a species are
fetched, fetches run concurrently under a small semaphore, and any team
whose paste can't be fetched or doesn't parse under the format's EV model
is skipped rather than failing the whole conversion.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sketch.convert.ev_model import EvModel
from sketch.pokepaste.fetcher import fetch_pokepaste_raw
from sketch.showdown.parser import ShowdownParseError, parse_showdown
from sketch.storage.sheets_client import SheetsClient
from sketch.team import TeamData, norm_species

logger = logging.getLogger(__name__)

# Cap on concurrent pokepast.es fetches. Bank teams that share a species
# with the OTS are typically a handful; this keeps a large bank from
# opening dozens of simultaneous connections while still parallelizing.
_FETCH_CONCURRENCY = 8


@dataclass(frozen=True)
class BankTeam:
    """A parsed team from the bank, with the URL it came from."""

    url: str
    team: TeamData


async def load_bank_teams(
    sheets: SheetsClient,
    sheet_name: str,
    ots_species: set[str],
    ev_model: EvModel,
) -> list[BankTeam]:
    """Fetch + parse bank teams that share a species with the OTS.

    `ots_species` is the set of normalized (casefolded) species in the
    OTS. Returns the successfully-parsed `BankTeam`s; teams that fail to
    fetch or parse (e.g. a non-conforming paste under `ev_model`'s cap)
    are logged and skipped. Returns an empty list on a snapshot read
    failure so the caller can fall back to LLM guessing for every mon.
    """
    try:
        snapshot = await sheets.get_search_snapshot(sheet_name)
    except Exception:
        logger.warning(
            "Bank snapshot read failed for %s; converting with no bank teams",
            sheet_name,
            exc_info=True,
        )
        return []

    # Distinct URLs of rows sharing >=1 species with the OTS. De-duping
    # here means a team appearing on multiple rows is fetched once.
    urls: list[str] = []
    seen: set[str] = set()
    for row in snapshot.rows:
        if row.url in seen:
            continue
        if ots_species & {norm_species(s) for s in row.species}:
            seen.add(row.url)
            urls.append(row.url)

    if not urls:
        return []

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _load(url: str) -> BankTeam | None:
        async with semaphore:
            try:
                text = await fetch_pokepaste_raw(url)
            except Exception:
                # Fetching is best-effort — any error (network, non-200,
                # transport) skips this team rather than failing the conversion.
                logger.warning(
                    "Skipping bank team (fetch failed): %s", url, exc_info=True
                )
                return None
        try:
            team = parse_showdown(text, max_ev_per_stat=ev_model.max_per_stat)
        except ShowdownParseError as exc:
            logger.info("Skipping bank team (parse failed): %s (%s)", url, exc)
            return None
        return BankTeam(url=url, team=team)

    results = await asyncio.gather(*(_load(u) for u in urls))
    return [r for r in results if r is not None]
