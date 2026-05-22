import re

import aiohttp

_POKEPASTE_URL_RE = re.compile(r"^https?://pokepast\.es/[A-Za-z0-9]+/?$")
_REPLICA_RE = re.compile(r"^[0-9A-Fa-f]{10}$")


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
            f"`replica` must be a 10-character hex string. Got `{replica}`."
        )
    return replica.upper()
