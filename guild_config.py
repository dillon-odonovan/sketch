"""Per-guild configuration: which Google Sheet does each Discord guild write to.

Today's store is a static, in-memory `dict[int, GuildConfig]` built from a JSON
env var at startup. The `GuildConfigStore` Protocol exists so a future runtime
store (e.g., SQLite-backed, populated by a `/set-spreadsheet` slash command)
can drop in without touching command handlers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

# Google Sheets IDs are URL-safe base64-ish: letters, digits, underscore, hyphen.
# Reject anything else early so we fail at startup rather than on first write.
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_ALLOWED_GUILD_KEYS = frozenset({"spreadsheet_id", "broadcast_channel_id"})

# Discord snowflake IDs (channels, guilds) are 64-bit unsigned integers, but
# we accept them as numeric strings in JSON to stay consistent with how guild
# IDs are encoded as keys in GUILD_CONFIG_JSON.
_SNOWFLAKE_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class GuildConfig:
    """Configuration for a single guild. Frozen so it can be cached safely."""

    spreadsheet_id: str
    broadcast_channel_id: int | None = None


class GuildConfigStore(Protocol):
    """Lookup of `guild_id -> GuildConfig`. Returns None for unconfigured guilds.

    Sync by design: the in-memory impl is a dict lookup, and a future SQLite
    impl is also microsecond-fast. Making this async would force `await` noise
    into every command handler for no benefit.
    """

    def get(self, guild_id: int) -> GuildConfig | None: ...


class StaticGuildConfigStore:
    """In-memory store built from the parsed env JSON. Immutable after construction."""

    def __init__(self, mapping: dict[int, GuildConfig]) -> None:
        self._mapping = dict(mapping)

    def get(self, guild_id: int) -> GuildConfig | None:
        return self._mapping.get(guild_id)

    def configured_guild_ids(self) -> list[int]:
        """For diagnostic logging at startup."""
        return list(self._mapping.keys())


def parse_guild_config_json(raw: str) -> dict[int, GuildConfig]:
    """Parse the GUILD_CONFIG_JSON env var. Raises ValueError on any malformed input.

    Expected shape:
        {
          "<guild_id>": {
            "spreadsheet_id": "<id>",
            "broadcast_channel_id": "<channel_id>"  # optional
          },
          ...
        }

    Validations:
    - JSON object at the top level
    - Guild keys must be numeric strings (Discord snowflake IDs)
    - Each value must be a JSON object with `spreadsheet_id` (string), and an
      optional `broadcast_channel_id` (numeric snowflake string). Unknown keys
      are rejected.
    - `spreadsheet_id` must match Google's URL-safe charset
    - `broadcast_channel_id`, when present and non-null, must be a non-empty
      numeric string. An explicit JSON `null` is treated the same as the key
      being absent — Terraform's `optional(string)` encodes unset values as
      `null`, so being lenient here keeps the env-var payload portable.
    - Empty `{}` is rejected — a bot with zero configured guilds is almost
      certainly a misconfiguration
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GUILD_CONFIG_JSON is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"GUILD_CONFIG_JSON must be a JSON object, got {type(data).__name__}"
        )

    if not data:
        raise ValueError(
            "GUILD_CONFIG_JSON is empty — configure at least one guild "
            'like {"<guild_id>": {"spreadsheet_id": "<id>"}}'
        )

    result: dict[int, GuildConfig] = {}
    for raw_key, value in data.items():
        if not isinstance(raw_key, str) or not raw_key.isdigit():
            raise ValueError(
                f"GUILD_CONFIG_JSON guild key must be a numeric string, got {raw_key!r}"
            )
        guild_id = int(raw_key)

        if not isinstance(value, dict):
            raise ValueError(
                f"GUILD_CONFIG_JSON value for guild {guild_id} must be an object, "
                f"got {type(value).__name__}"
            )

        unknown = set(value.keys()) - _ALLOWED_GUILD_KEYS
        if unknown:
            raise ValueError(
                f"GUILD_CONFIG_JSON guild {guild_id} has unknown key(s): "
                f"{sorted(unknown)} (allowed: {sorted(_ALLOWED_GUILD_KEYS)})"
            )

        spreadsheet_id = value.get("spreadsheet_id")
        if not isinstance(spreadsheet_id, str) or not spreadsheet_id:
            raise ValueError(
                f"GUILD_CONFIG_JSON guild {guild_id} is missing a non-empty "
                "string `spreadsheet_id`"
            )
        if not _SPREADSHEET_ID_RE.match(spreadsheet_id):
            raise ValueError(
                f"GUILD_CONFIG_JSON guild {guild_id} has spreadsheet_id "
                f"{spreadsheet_id!r} with disallowed characters "
                "(allowed: letters, digits, underscore, hyphen)"
            )

        broadcast_channel_id: int | None = None
        raw_channel = value.get("broadcast_channel_id")
        if raw_channel is not None:
            if not isinstance(raw_channel, str) or not _SNOWFLAKE_RE.match(raw_channel):
                raise ValueError(
                    f"GUILD_CONFIG_JSON guild {guild_id} has broadcast_channel_id "
                    f"{raw_channel!r}; expected a non-empty numeric string "
                    "(Discord channel snowflake ID)"
                )
            broadcast_channel_id = int(raw_channel)

        result[guild_id] = GuildConfig(
            spreadsheet_id=spreadsheet_id,
            broadcast_channel_id=broadcast_channel_id,
        )

    return result
