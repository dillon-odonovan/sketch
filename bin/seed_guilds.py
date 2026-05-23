#!/usr/bin/env python3
"""Write or overwrite a guild config document in Firestore.

This is the human-facing path for adding/editing guilds today. The future
/register-guild slash command will write to the same collection at runtime;
both paths share `sketch.storage.guild_config.GUILD_CONFIGS_COLLECTION` so
they cannot drift.

Auth uses Application Default Credentials. Run under
`gcloud auth application-default login` locally, or attach a service
account in CI. The target project comes from $GCP_PROJECT, falling back to
ADC's quota project — pass --project to override.

Validation runs at write time (the bot's read path is permissive on purpose,
so a bad value never crashes the whole bot). The patterns here mirror what
the env-var parser used to enforce.

Usage:
    python bin/seed_guilds.py 1506464289777647747 \\
        --spreadsheet-id 10OCm5yOqVzhe7pUR5Cdc8gAybcfpCt6jjBgoCkXxK0Q \\
        --broadcast-channel-id 1507428032170823770

    python bin/seed_guilds.py 669224212497694720 \\
        --spreadsheet-id 1L2zAod_MOba7EdSekIcVmMTzRvQwhqG_o1qVQASKSBg \\
        --clear-broadcast
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from google.cloud import firestore

# Importing the shared collection name keeps the seed script in sync with
# the bot's read path. If we ever rename the collection, both update together.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
from sketch.storage.guild_config import GUILD_CONFIGS_COLLECTION  # noqa: E402

# Same validation the env-var parser used to enforce. Google Sheets IDs are
# URL-safe-ish; Discord snowflakes are decimal integers serialized as strings
# (HCL number precision is 53 bits but JSON / Firestore preserve full 64-bit
# values when they're stringified).
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SNOWFLAKE_RE = re.compile(r"^[0-9]+$")


def _snowflake(value: str) -> str:
    if not _SNOWFLAKE_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a Discord snowflake (expected a non-empty "
            "decimal-digit string)"
        )
    return value


def _spreadsheet_id(value: str) -> str:
    if not _SPREADSHEET_ID_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} contains characters outside [A-Za-z0-9_-] — not a "
            "valid Google Sheets ID"
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed or update a guild config document in Firestore.",
        epilog="Run under `gcloud auth application-default login` credentials.",
    )
    parser.add_argument(
        "guild_id",
        type=_snowflake,
        help="Discord guild ID (numeric snowflake string).",
    )
    parser.add_argument(
        "--spreadsheet-id",
        type=_spreadsheet_id,
        required=True,
        help="Google Sheets ID this guild writes to.",
    )
    bcast = parser.add_mutually_exclusive_group()
    bcast.add_argument(
        "--broadcast-channel-id",
        type=_snowflake,
        default=None,
        help=(
            "Discord channel snowflake to broadcast new teams to. Omit (or "
            "use --clear-broadcast) for guilds that shouldn't broadcast."
        ),
    )
    bcast.add_argument(
        "--clear-broadcast",
        action="store_true",
        help="Remove any existing broadcast_channel_id for this guild.",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT"),
        help="GCP project ID (default: $GCP_PROJECT or ADC's quota project).",
    )
    args = parser.parse_args(argv)

    payload: dict[str, str | None] = {"spreadsheet_id": args.spreadsheet_id}
    if args.broadcast_channel_id is not None:
        payload["broadcast_channel_id"] = args.broadcast_channel_id
    elif args.clear_broadcast:
        # Explicit None tells Firestore to remove the field on merge.
        # Without --clear-broadcast we leave any existing value untouched.
        payload["broadcast_channel_id"] = firestore.DELETE_FIELD

    client = firestore.Client(project=args.project)
    doc_ref = client.collection(GUILD_CONFIGS_COLLECTION).document(args.guild_id)
    # merge=True so callers can update one field at a time without having to
    # restate the whole document. The bot reads the union of fields each
    # boot anyway.
    doc_ref.set(payload, merge=True)

    written = doc_ref.get().to_dict() or {}
    print(f"Wrote guild_configs/{args.guild_id}:")
    for k, v in sorted(written.items()):
        print(f"  {k} = {v!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
