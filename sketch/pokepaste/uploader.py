"""Upload Showdown export text to pokepast.es and return the canonical URL.

`post_to_pokepaste(text, title)` POSTs already-rendered paste text (see
`sketch.showdown.renderer.render_showdown`) to pokepast.es and returns the
canonical URL of the new paste, ready to hand to `SheetsClient.add_row`.

Kept separate from rendering so the slash command can render once and reuse
the rendered string for both the upload and the user-facing preview, and so
the network call is isolated from the pure-text renderer.
"""

from __future__ import annotations

import logging

import aiohttp

from sketch.pokepaste.validator import canonicalize_pokepaste_url

logger = logging.getLogger(__name__)


class PokepasteUploadError(Exception):
    """Raised when the upload to pokepast.es fails. Message is user-facing."""


# pokepast.es accepts POST to /create with form-encoded fields and responds
# with a 303 redirect whose Location header is the new paste's path
# (e.g. "/abc123def"). We follow the redirect ourselves with
# `allow_redirects=False` so we can capture the URL — letting aiohttp follow
# it would land us on the rendered HTML page and waste bandwidth re-reading
# the paste we just submitted.
_POKEPASTE_HOST = "https://pokepast.es"
_POKEPASTE_CREATE_URL = f"{_POKEPASTE_HOST}/create"


async def post_to_pokepaste(paste_text: str, title: str) -> str:
    """Upload `paste_text` to pokepast.es and return the canonical URL.

    POSTs to `/create` with the same form fields the website uses
    (paste, title, author, notes). pokepast.es responds with a 303
    redirect whose Location header is the new paste's path; we extract
    the URL from that header rather than follow the redirect.

    Round-trips the URL through `canonicalize_pokepaste_url` so the
    value handed to `SheetsClient.add_row` matches the form
    `find_row_by_url` uses for dedup, and so a malformed Location is
    caught here rather than at row-write time.

    Raises `PokepasteUploadError` on any non-redirect response or
    transport failure. The message is user-facing.
    """
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                _POKEPASTE_CREATE_URL,
                # Match the website's form submission shape exactly: paste +
                # title + empty author + empty notes. Sending only the
                # fields we care about works too, but matching the browser
                # avoids any server-side validation surprise.
                data={
                    "paste": paste_text,
                    "title": title,
                    "author": "",
                    "notes": "",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp,
        ):
            if resp.status not in (301, 302, 303, 307, 308):
                body_excerpt = (await resp.text())[:200]
                logger.warning(
                    "Pokepaste create returned non-redirect status %s: %s",
                    resp.status,
                    body_excerpt,
                )
                raise PokepasteUploadError(
                    "Couldn't upload the team to pokepast.es right now — "
                    "please try again in a moment."
                )
            location = resp.headers.get("Location", "").strip()
            if not location:
                logger.warning(
                    "Pokepaste create %s response missing Location header",
                    resp.status,
                )
                raise PokepasteUploadError(
                    "Couldn't upload the team to pokepast.es right now — "
                    "please try again in a moment."
                )
    except aiohttp.ClientError as exc:
        logger.warning("Pokepaste upload transport error: %s", exc)
        raise PokepasteUploadError(
            "Couldn't upload the team to pokepast.es right now — "
            "please try again in a moment."
        ) from exc

    absolute = (
        location if location.startswith("http") else f"{_POKEPASTE_HOST}{location}"
    )
    return canonicalize_pokepaste_url(absolute)
