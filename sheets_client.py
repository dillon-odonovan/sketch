import asyncio
import logging
from dataclasses import dataclass

import google.auth
from googleapiclient.discovery import build

import config

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


class SheetsClient:
    def __init__(self) -> None:
        # Application Default Credentials: on GCE this resolves to the VM's
        # attached service account via the metadata server; locally it resolves
        # to whatever `gcloud auth application-default login` set up, or the
        # JSON key path in GOOGLE_APPLICATION_CREDENTIALS if that env var is
        # set. No JSON keys need to live on disk in production.
        # https://cloud.google.com/docs/authentication/application-default-credentials
        creds, _ = google.auth.default(scopes=_SCOPES)
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._sheet_id_cache: dict[str, int] = {}

    async def _run(self, fn, *args, **kwargs):
        # google-api-python-client is synchronous. Offload calls so we don't
        # block discord.py's event loop while a request is in flight.
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _get_sheet_id(self, sheet_name: str) -> int:
        if sheet_name in self._sheet_id_cache:
            return self._sheet_id_cache[sheet_name]
        meta = await self._run(
            self._service.spreadsheets().get(
                spreadsheetId=config.SPREADSHEET_ID,
                fields="sheets(properties(sheetId,title))",
            ).execute,
            num_retries=_API_RETRIES,
        )
        for s in meta.get("sheets", []):
            props = s["properties"]
            self._sheet_id_cache[props["title"]] = props["sheetId"]
        if sheet_name not in self._sheet_id_cache:
            raise RuntimeError(f"Sheet tab not found: {sheet_name!r}")
        return self._sheet_id_cache[sheet_name]

    async def load_dex_names(self) -> list[str]:
        resp = await self._run(
            self._service.spreadsheets().values().get(
                spreadsheetId=config.SPREADSHEET_ID,
                range=config.DEX_NAME_RANGE,
            ).execute,
            num_retries=_API_RETRIES,
        )
        values = resp.get("values", [])
        names = [row[0].strip() for row in values if row and row[0].strip()]
        logger.info("Loaded %d DEX species names", len(names))
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
            self._service.spreadsheets().values().get(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{sheet_name}!A:A",
            ).execute,
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
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=config.SPREADSHEET_ID,
                body={
                    "requests": [{
                        "copyPaste": {
                            "source": {
                                "sheetId": sheet_id,
                                "startRowIndex": template_row - 1,
                                "endRowIndex": template_row,
                                "startColumnIndex": 1,   # B
                                "endColumnIndex": 19,    # S exclusive
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
                    }]
                },
            ).execute,
            num_retries=_API_RETRIES,
        )

        await self._run(
            self._service.spreadsheets().values().update(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{sheet_name}!A{new_row}",
                valueInputOption="USER_ENTERED",
                body={"values": [[url]]},
            ).execute,
            num_retries=_API_RETRIES,
        )

        raw_data = [
            {"range": f"{sheet_name}!D{new_row}", "values": [[replica or ""]]},
            {"range": f"{sheet_name}!E{new_row}", "values": [[paste_type]]},
            {"range": f"{sheet_name}!G{new_row}", "values": [[description]]},
        ]
        await self._run(
            self._service.spreadsheets().values().batchUpdate(
                spreadsheetId=config.SPREADSHEET_ID,
                body={"valueInputOption": "RAW", "data": raw_data},
            ).execute,
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
            self._service.spreadsheets().values().get(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{sheet_name}!H{row}:M{row}",
                valueRenderOption="FORMATTED_VALUE",
            ).execute,
            num_retries=_API_RETRIES,
        )
        rows = resp.get("values", [])
        if not rows or not rows[0]:
            return None
        cells = rows[0]
        if any(c is None or c == "" or c == "#N/A" or c == "Loading..." for c in cells):
            return None
        return [str(c).strip() for c in cells]

    async def search_rows(self, sheet_name: str) -> list[TeamRow]:
        resp = await self._run(
            self._service.spreadsheets().values().get(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{sheet_name}!A{config.FIRST_DATA_ROW}:M",
                valueRenderOption="FORMATTED_VALUE",
            ).execute,
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
            out.append(TeamRow(
                row_number=config.FIRST_DATA_ROW + idx,
                url=url,
                description=description,
                species=species,
            ))
        return out
