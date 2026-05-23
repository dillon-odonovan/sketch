from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import google.auth
from googleapiclient.discovery import build

from sketch import config
from sketch.pokepaste_validator import ValidationError, canonicalize_pokepaste_url
from sketch.search.dex import DexIndex
from sketch.search.text_search import DescriptionIndex
from sketch.storage.guild_config import GuildConfigStore

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Transparent retry on transient socket / 5xx errors. Most notably this covers
# `BrokenPipeError` when a pooled httplib2 connection (to Sheets *or* to the
# GCE metadata server during token refresh) has gone stale after an idle
# period. googleapiclient's _retry_request wraps the entire `http.request`
# call including the `before_request` credential-refresh hook, so one knob
# handles both failure points.
_API_RETRIES = 3


@dataclass
class TeamRow:
    row_number: int
    url: str
    description: str
    species: list[str]


@dataclass
class SearchSnapshot:
    """One point-in-time fetch of a sheet plus the index built over it.

    `desc_index`'s row indices are positions into `rows` (NOT
    `TeamRow.row_number`). The snapshot is treated as immutable after
    construction and is safe to share across concurrent /search-teams calls.

    Lives here (rather than in `text_search`) because `rows: list[TeamRow]`
    would otherwise force `text_search` to import sheets-specific types,
    polluting an otherwise pure-text module.
    """

    rows: list[TeamRow]
    desc_index: DescriptionIndex

    def match_rows(self, query: str) -> list[TeamRow]:
        """Return the TeamRows whose description matches the tokenized `query`.

        Convenience wrapper around `desc_index.match`. Result order follows
        the snapshot's `rows` order (i.e., sheet order) so the embed shown to
        the user is stable across queries that return overlapping sets.
        """
        indices = self.desc_index.match(query)
        return [self.rows[i] for i in sorted(indices)]


class SheetsClient:
    """Sheets operations against a single spreadsheet.

    One instance per guild. The Google API service object is built once by the
    registry and shared across instances (it's just an HTTP client); the
    per-spreadsheet caches (`_sheet_id_cache`, `_dex`) are instance-scoped
    because their keys collide across spreadsheets.
    """

    def __init__(
        self,
        service: Any,
        spreadsheet_id: str,
        *,
        search_cache_ttl: float | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id_cache: dict[str, int] = {}
        self._dex: DexIndex | None = None
        # Guards lazy DEX construction so two concurrent first-uses in one
        # guild don't both fetch. Cheap to acquire on the fast path (when
        # _dex is already set, we don't even take it).
        self._dex_lock = asyncio.Lock()
        # `now` and `search_cache_ttl` are injectable so tests can drive the
        # cache deterministically without sleeping or monkeypatching `time`.
        # Production callers should leave both at their defaults.
        self._now = now
        self._search_cache_ttl = (
            search_cache_ttl
            if search_cache_ttl is not None
            else config.SEARCH_CACHE_TTL_SECONDS
        )
        # Keyed by sheet_name. Value is (snapshot, expires_at_monotonic).
        # Each SheetsClient is per-guild, so cross-key collisions across
        # guilds are impossible. Today every guild has exactly one configured
        # sheet, so this dict has at most one entry — but keying by name
        # keeps the door open to per-format snapshots without restructuring.
        self._snapshot_cache: dict[str, tuple[SearchSnapshot, float]] = {}
        # Single lock for snapshot cache mutation. Two concurrent first-uses
        # for the same sheet must NOT both fetch; the second waiter sees the
        # populated cache on recheck. Mirrors the `_dex_lock` pattern.
        self._snapshot_lock = asyncio.Lock()

    async def _run(self, fn, *args, **kwargs):
        # google-api-python-client is synchronous. Offload calls so we don't
        # block discord.py's event loop while a request is in flight.
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def list_tab_names(self) -> list[str]:
        """Return every sheet tab title in the spreadsheet.

        Used by `/register-sheet` to verify the bot's service account has
        access AND that the expected format tabs are present, before
        persisting a new `spreadsheet_id`. Any exception (HttpError 403/404,
        transient socket failure, malformed ID) propagates to the caller so
        the command handler can translate to an actionable refusal.
        """
        meta = await self._run(
            self._service.spreadsheets()
            .get(
                spreadsheetId=self._spreadsheet_id,
                fields="sheets(properties(title))",
            )
            .execute,
            num_retries=_API_RETRIES,
        )
        return [
            s["properties"]["title"]
            for s in meta.get("sheets", [])
            if "properties" in s and "title" in s["properties"]
        ]

    async def _get_sheet_id(self, sheet_name: str) -> int:
        if sheet_name in self._sheet_id_cache:
            return self._sheet_id_cache[sheet_name]
        meta = await self._run(
            self._service.spreadsheets()
            .get(
                spreadsheetId=self._spreadsheet_id,
                fields="sheets(properties(sheetId,title))",
            )
            .execute,
            num_retries=_API_RETRIES,
        )
        for s in meta.get("sheets", []):
            props = s["properties"]
            self._sheet_id_cache[props["title"]] = props["sheetId"]
        if sheet_name not in self._sheet_id_cache:
            raise RuntimeError(f"Sheet tab not found: {sheet_name!r}")
        return self._sheet_id_cache[sheet_name]

    async def get_dex(self) -> DexIndex:
        """Lazy-load the DEX index for this spreadsheet on first call.

        Cached for the bot's lifetime — no TTL. Memory cost is ~250 KB per
        spreadsheet (1000-entry dict with a lowercased mirror), trivial at
        any realistic guild count. Failures are NOT cached so a retry after
        fixing sheet permissions will succeed.
        """
        if self._dex is not None:
            return self._dex
        async with self._dex_lock:
            # Re-check inside the lock; another waiter may have populated it.
            if self._dex is None:
                names = await self._load_dex_names()
                self._dex = DexIndex(names)
        return self._dex

    async def _load_dex_names(self) -> list[str]:
        resp = await self._run(
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=config.DEX_NAME_RANGE,
            )
            .execute,
            num_retries=_API_RETRIES,
        )
        values = resp.get("values", [])
        names = [row[0].strip() for row in values if row and row[0].strip()]
        logger.info(
            "Loaded %d DEX species names from spreadsheet %s",
            len(names),
            self._spreadsheet_id,
        )
        return names

    async def add_row(
        self,
        sheet_name: str,
        url: str,
        description: str,
        replica: str | None,
        paste_type: str,
    ) -> int:
        sheet_id = await self._get_sheet_id(sheet_name)

        col_a = await self._run(
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet_name}!A:A",
            )
            .execute,
            num_retries=_API_RETRIES,
        )
        existing = len(col_a.get("values", []))
        new_row = max(config.FIRST_DATA_ROW, existing + 1)
        template_row = max(config.FIRST_DATA_ROW, new_row - 1)

        # Clone the previous data row's columns B–S into the new row. PASTE_NORMAL
        # carries both formulas (with relative refs adjusted — so column H's
        # TEAMDATAFROMPASTE(A<row>) becomes TEAMDATAFROMPASTE(A<new_row>)) and
        # cell formatting (e.g., the checkbox styling in column B).
        # https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#copypasterequest
        await self._run(
            self._service.spreadsheets()
            .batchUpdate(
                spreadsheetId=self._spreadsheet_id,
                body={
                    "requests": [
                        {
                            "copyPaste": {
                                "source": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": template_row - 1,
                                    "endRowIndex": template_row,
                                    "startColumnIndex": 1,  # B
                                    "endColumnIndex": 19,  # S exclusive
                                },
                                "destination": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": new_row - 1,
                                    "endRowIndex": new_row,
                                    "startColumnIndex": 1,
                                    "endColumnIndex": 19,
                                },
                                "pasteType": "PASTE_NORMAL",
                            }
                        }
                    ]
                },
            )
            .execute,
            num_retries=_API_RETRIES,
        )

        await self._run(
            self._service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet_name}!A{new_row}",
                valueInputOption="USER_ENTERED",
                body={"values": [[url]]},
            )
            .execute,
            num_retries=_API_RETRIES,
        )

        raw_data = [
            {"range": f"{sheet_name}!D{new_row}", "values": [[replica or ""]]},
            {"range": f"{sheet_name}!E{new_row}", "values": [[paste_type]]},
            {"range": f"{sheet_name}!G{new_row}", "values": [[description]]},
        ]
        await self._run(
            self._service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self._spreadsheet_id,
                body={"valueInputOption": "RAW", "data": raw_data},
            )
            .execute,
            num_retries=_API_RETRIES,
        )

        return new_row

    async def poll_species(self, sheet_name: str, row: int) -> list[str] | None:
        # The species in H–M come from TEAMDATAFROMPASTE, a custom AppsScript
        # function that fetches the Pokepaste and parses it. While the function
        # is running (typically 1–10s), cells may be empty, "Loading...", or
        # render "#N/A" if the formula is mid-evaluation. Treat any of those
        # as "not ready yet" and return None so the caller can retry.
        resp = await self._run(
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet_name}!H{row}:M{row}",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute,
            num_retries=_API_RETRIES,
        )
        rows = resp.get("values", [])
        if not rows or not rows[0]:
            return None
        cells = rows[0]
        if any(c is None or c == "" or c == "#N/A" or c == "Loading..." for c in cells):
            return None
        return [str(c).strip() for c in cells]

    async def find_row_by_url(self, sheet_name: str, url: str) -> TeamRow | None:
        """Return the first existing row whose URL canonicalizes to `url`.

        Reads only A:G (URL + description) — species columns are deliberately
        skipped so this is safe to call against rows that were just written and
        whose TEAMDATAFROMPASTE formula is still loading. The returned TeamRow
        therefore always has `species=[]`; callers that need species must use
        `search_rows` or `poll_species` instead.

        `url` does not need to be pre-canonicalized; both sides are normalized.
        Raises ValidationError if `url` isn't a valid Pokepaste URL. Returns
        None when the sheet has no matching row.
        """
        target = canonicalize_pokepaste_url(url)
        resp = await self._run(
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet_name}!A{config.FIRST_DATA_ROW}:G",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute,
            num_retries=_API_RETRIES,
        )
        rows = resp.get("values", [])
        for idx, row in enumerate(rows):
            # Sheets truncates trailing empty cells, so a row with data only
            # through column D comes back as a 4-element list. Pad to length 7
            # so row[6] (description) is always indexable.
            row = row + [""] * (7 - len(row))
            cell_url = (row[0] or "").strip()
            if not cell_url:
                continue
            try:
                if canonicalize_pokepaste_url(cell_url) != target:
                    continue
            except ValidationError:
                # Malformed URL already in the sheet — skip rather than error.
                continue
            description = (row[6] or "").strip()
            return TeamRow(
                row_number=config.FIRST_DATA_ROW + idx,
                url=cell_url,
                description=description,
                species=[],
            )
        return None

    async def get_search_snapshot(self, sheet_name: str) -> SearchSnapshot:
        """Return rows + a tokenized description index.

        Realistic hit profile: /add-team is infrequent and /search-teams
        sessions are typically minutes apart, so most calls are cache
        misses on TTL expiry. The cache is a *burst* amortizer — its
        value shows up when one user iterates several /search-teams
        within a session, or when /add-team is immediately followed by
        /search-teams to verify the new row. Index construction itself
        is ~1–5 ms for 1000 rows; the cache-hit win is skipping the
        ~200–500 ms Sheets API round-trip.

        Freshness model: bot-driven writes invalidate explicitly.
        /add-team (and any future /edit-team) calls `invalidate_snapshot`
        once the species poll settles. The TTL configured in
        `config.SEARCH_CACHE_TTL_SECONDS` bounds staleness for direct
        Sheet edits (Google UI bypassing the bot) and caps memory held
        by long-idle guilds.

        Failures are not cached so a retry after fixing sheet permissions
        still works.

        Concurrency model:
          - Fast path (cache hit, not expired): lockless dict read. Two
            successive search calls reuse the same `SearchSnapshot` object.
          - Slow path (miss or expired): acquire `_snapshot_lock`, recheck
            under the lock, then fetch + build + store. Concurrent first-
            uses for the same sheet collapse to a single fetch.
        """
        cached = self._snapshot_cache.get(sheet_name)
        if cached is not None and cached[1] > self._now():
            return cached[0]
        async with self._snapshot_lock:
            cached = self._snapshot_cache.get(sheet_name)
            if cached is not None and cached[1] > self._now():
                return cached[0]
            rows = await self.search_rows(sheet_name)
            desc_index = DescriptionIndex.from_descriptions(r.description for r in rows)
            snapshot = SearchSnapshot(rows=rows, desc_index=desc_index)
            self._snapshot_cache[sheet_name] = (
                snapshot,
                self._now() + self._search_cache_ttl,
            )
            logger.info(
                "Built search snapshot for %s: %d rows, %d distinct "
                "description tokens (ttl=%.1fs)",
                sheet_name,
                len(rows),
                len(desc_index),
                self._search_cache_ttl,
            )
            return snapshot

    def invalidate_snapshot(self, sheet_name: str) -> None:
        """Drop the cached snapshot for `sheet_name` so the next call refetches.

        Called from bot-driven write paths (/add-team today, /edit-team in
        the future) once the species poll has settled — invalidating before
        species populate would just cause the next snapshot rebuild to skip
        the new row again (since `search_rows` filters out rows whose
        species cells still read "Loading..." or "#N/A").

        Synchronous and lock-free on purpose: `dict.pop` is atomic in
        CPython, and the worst case if a /search-teams is racing us is one
        more stale read before the next call rebuilds. Safe to call when
        there's no cache entry (e.g., the first /add-team after bot start).
        """
        removed = self._snapshot_cache.pop(sheet_name, None)
        if removed is not None:
            logger.info(
                "Invalidated search snapshot for %s (was %d rows)",
                sheet_name,
                len(removed[0].rows),
            )

    async def search_rows(self, sheet_name: str) -> list[TeamRow]:
        resp = await self._run(
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{sheet_name}!A{config.FIRST_DATA_ROW}:M",
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute,
            num_retries=_API_RETRIES,
        )
        rows = resp.get("values", [])
        out: list[TeamRow] = []
        for idx, row in enumerate(rows):
            row = row + [""] * (13 - len(row))
            url = (row[0] or "").strip()
            description = (row[6] or "").strip()
            species = [(c or "").strip() for c in row[7:13]]
            if not url:
                continue
            if any(s == "" or s == "#N/A" or s == "Loading..." for s in species):
                continue
            out.append(
                TeamRow(
                    row_number=config.FIRST_DATA_ROW + idx,
                    url=url,
                    description=description,
                    species=species,
                )
            )
        return out


class SheetsClientRegistry:
    """Maps guild_id → SheetsClient, lazily constructed on first use.

    Owns the shared Google Sheets service object (built once via ADC). Returns
    None for guilds not present in the configured GuildConfigStore — callers
    use that to send a "this server isn't configured" refusal.
    """

    def __init__(self, store: GuildConfigStore) -> None:
        self._store = store
        # Application Default Credentials: on GCE this resolves to the VM's
        # attached service account via the metadata server; locally it resolves
        # to whatever `gcloud auth application-default login` set up, or the
        # JSON key path in GOOGLE_APPLICATION_CREDENTIALS if that env var is
        # set. No JSON keys need to live on disk in production.
        # https://cloud.google.com/docs/authentication/application-default-credentials
        creds, _ = google.auth.default(scopes=_SCOPES)
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._clients: dict[int, SheetsClient] = {}

    def get(self, guild_id: int) -> SheetsClient | None:
        cfg = self._store.get(guild_id)
        if cfg is None:
            return None
        client = self._clients.get(guild_id)
        if client is None:
            client = SheetsClient(self._service, cfg.spreadsheet_id)
            self._clients[guild_id] = client
        return client

    def invalidate(self, guild_id: int) -> None:
        """Evict the cached SheetsClient for `guild_id`.

        Call this after the guild's `spreadsheet_id` changes — the cached
        client is bound to the old ID (and its `_sheet_id_cache`, `_dex`,
        and snapshot caches are keyed against it), so reusing it would
        silently route writes to the wrong spreadsheet. The next
        `get(guild_id)` lazily rebuilds the client against the fresh config.
        """
        self._clients.pop(guild_id, None)

    def build_probe_client(self, spreadsheet_id: str) -> SheetsClient:
        """Build a one-off SheetsClient pointed at `spreadsheet_id`, without
        consulting the store.

        Used by `/register-sheet` to verify the bot's service account has
        access *before* the new ID is persisted, so a bad write doesn't
        leave the guild in a broken state. The returned client is not
        cached — callers should drop the reference once the probe completes.
        """
        return SheetsClient(self._service, spreadsheet_id)
