"""Validation for Pokemon Champions Replica codes.

Champions Replica codes are 10 characters of alphanumerics. The codes
are case-insensitive at the game UI layer, so we uppercase to a single
canonical form for cache keys and dedup checks.
"""

from __future__ import annotations

import re

from sketch.pokepaste.validator import ValidationError

# Pokemon Champions "Replica" / Team IDs are 10 chars of uppercase letters
# and digits (e.g. "QBXXWXL05U"). We accept any ASCII alphanumeric of
# length 10 and uppercase it for a single canonical form — matches how the
# in-game UI displays the code, and keeps cache keys / dedup case-insensitive.
_REPLICA_RE = re.compile(r"^[A-Za-z0-9]{10}$")


def normalize_replica(replica: str) -> str:
    if not _REPLICA_RE.match(replica):
        raise ValidationError(
            f"`replica` must be a 10-character alphanumeric Champions team ID "
            f"(e.g. `QBXXWXL05U`). Got `{replica}`."
        )
    return replica.upper()
