"""VRPaste URL validation and canonicalization.

VRPastes is Victory Road's Pokemon team sharer. URLs look like
`https://www.vrpastes.com/<id>` where `<id>` is a short alphanumeric
slug (the sample paste used during development was `gxmfscC1`).

`canonicalize_vrpaste_url` collapses spelling variants (with/without
`www.`, http vs https, trailing slash) so the same paste compares
equal regardless of how the user typed it. The id portion is treated
as case-sensitive: `gxmfscC1` and `gxmfscc1` resolve to different
pastes on VRPaste, mirroring how `pokepast.es` treats its paste ids.

No standalone HTTP probe: `sketch.vrpaste.fetcher.fetch_vrpaste` is
the only call path that needs a backend round-trip, and it already
fails cleanly when the paste doesn't exist. A separate `validate`
function would either duplicate that fetch or probe the user-facing
HTML page (which is a Next.js shell that 200s for any path the route
matches — useless as an existence check).
"""

from __future__ import annotations

import re

from sketch.pokepaste.validator import ValidationError

# Accept both `https://www.vrpastes.com/<id>` and `https://vrpastes.com/<id>`.
# VRPaste itself canonicalizes to the www-prefixed host (the live site
# redirects), and `canonicalize_vrpaste_url` does the same so dedup
# checks compare on one form.
_VRPASTE_URL_RE = re.compile(r"^https?://(?:www\.)?vrpastes\.com/([A-Za-z0-9]+)/?$")

_CANONICAL_HOST_PREFIX = "https://www.vrpastes.com/"


def _match_or_raise(url: str) -> re.Match[str]:
    stripped = url.strip()
    match = _VRPASTE_URL_RE.match(stripped)
    if not match:
        raise ValidationError(
            f"`{url}` doesn't look like a VRPaste URL. "
            "Expected something like `https://www.vrpastes.com/abc123`."
        )
    return match


def is_vrpaste_url(url: str) -> bool:
    """Cheap dispatch check: does `url` look like a VRPaste URL?

    Used by callers that need to route a user-supplied URL to the
    right source-specific resolver (Pokepaste vs VRPaste) without
    raising. Returns False for None-shaped, malformed, or non-VRPaste
    URLs.
    """
    if not url:
        return False
    return _VRPASTE_URL_RE.match(url.strip()) is not None


def extract_vrpaste_id(url: str) -> str:
    """Return the id portion of a VRPaste URL (the part after the last `/`).

    Used as the cache key and as the title slug when minting the
    derived Pokepaste, so callers can refer back to the source paste
    without rebuilding the URL.
    """
    return _match_or_raise(url).group(1)


def canonicalize_vrpaste_url(url: str) -> str:
    match = _match_or_raise(url)
    return f"{_CANONICAL_HOST_PREFIX}{match.group(1)}"
