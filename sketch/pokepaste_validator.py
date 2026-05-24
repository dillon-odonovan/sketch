import re

import aiohttp

_POKEPASTE_URL_RE = re.compile(r"^https?://pokepast\.es/[A-Za-z0-9]+/?$")
# Pokemon Champions "Replica" / Team IDs are 10 chars of uppercase letters and
# digits (e.g. "QBXXWXL05U"). The earlier hex-only assumption was wrong —
# real codes contain letters outside [A-F]. We accept any ASCII alphanumeric
# of length 10 and uppercase it for a single canonical form (matches how the
# in-game UI displays the code, and keeps cache keys / dedup case-insensitive).
_REPLICA_RE = re.compile(r"^[A-Za-z0-9]{10}$")


class ValidationError(Exception):
    """Raised when user-supplied input fails validation."""


async def validate_pokepaste_url(url: str) -> None:
    # Two-layer validation: the regex rejects typos cheaply (wrong host, junk
    # path); the HTTP fetch catches deleted/expired pastes that would otherwise
    # propagate to the sheet as a row whose TEAMDATAFROMPASTE formula
    # permanently returns blank.
    if not _POKEPASTE_URL_RE.match(url):
        raise ValidationError(
            f"`{url}` doesn't look like a Pokepaste URL. "
            "Expected something like `https://pokepast.es/abc123`."
        )
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, allow_redirects=True, timeout=10) as resp,
        ):
            if resp.status != 200:
                raise ValidationError(f"Could not fetch `{url}`: HTTP {resp.status}.")
    except aiohttp.ClientError as exc:
        raise ValidationError(f"Could not fetch `{url}`: {exc}") from exc


def normalize_replica(replica: str) -> str:
    if not _REPLICA_RE.match(replica):
        raise ValidationError(
            f"`replica` must be a 10-character alphanumeric Champions team ID "
            f"(e.g. `QBXXWXL05U`). Got `{replica}`."
        )
    return replica.upper()


def canonicalize_pokepaste_url(url: str) -> str:
    # The paste ID is case-sensitive — `pokepast.es/Abc` and `pokepast.es/abc`
    # are different pastes — so we lowercase only the scheme + host and leave
    # the ID untouched. Forcing https and stripping a trailing slash collapses
    # the four common ways the same paste gets typed into one comparable form.
    stripped = url.strip()
    if not _POKEPASTE_URL_RE.match(stripped):
        raise ValidationError(
            f"`{url}` doesn't look like a Pokepaste URL. "
            "Expected something like `https://pokepast.es/abc123`."
        )
    if stripped.startswith("http://"):
        stripped = "https://" + stripped[len("http://") :]
    if stripped.endswith("/"):
        stripped = stripped[:-1]
    prefix = "https://pokepast.es/"
    if stripped.lower().startswith(prefix):
        stripped = prefix + stripped[len(prefix) :]
    return stripped
