"""Tokenized substring matching over team descriptions.

Why this exists
---------------
`/search-teams description:<text>` historically did a single substring check
against the raw description (`needle in haystack.lower()`). That fails the
common natural-recall cases: querying ``shiliang dual weather`` doesn't match
``Shiliang Tang (tsltang0508) Dual Weather`` because the words aren't
contiguous; ``caly zama`` doesn't match ``Calyrex Zamazenta`` because the
query and stored text differ; ``pex`` doesn't match ``Toxapex`` because the
query is a suffix, not a prefix substring of the whole description.

This module replaces that with **order-independent, per-token substring
matching** over a small in-memory inverted index built once per Sheets
fetch.

Matching semantics
------------------
- Tokenizer is the same for stored descriptions and user queries: NFKD-strip
  combining marks → ``casefold()`` → split on ``[^0-9a-z]+``. Hyphens,
  parentheses, slashes, periods, whitespace all delimit. Digits are kept
  (handles gamertags like ``tsltang0508``).
- The index is ``dict[str, set[int]]`` mapping a description token to the
  positional row indices that contain it, plus a list of distinct tokens
  for substring scanning.
- For each tokenized query term:
    - Length ≥ :data:`SUBSTRING_MIN_TOKEN_LENGTH` (3): linear-scan the
      distinct-tokens list with ``query_tok in desc_tok`` (substring/infix).
      Union the postings of every matching description token.
    - Length < 3: require an **exact** token match. Prevents single-letter
      or two-letter tokens from substring-matching nearly every row.
- AND-intersect candidate sets across query tokens.
- Empty / punctuation-only query → empty match set (logged at INFO).

Substring per token subsumes prefix matching (every prefix is a substring),
so ``caly zama`` still matches ``Calyrex Zamazenta`` even though we're not
explicitly prefix-matching anymore.

Performance / memory
--------------------
For a 1000-row sheet with ~10 tokens/description and ~2000 distinct tokens,
the index is ~2 MB resident (mostly Python ``set`` overhead on the posting
lists). Query cost is ~ ``len(query_tokens) * len(distinct_tokens)`` short
``in`` checks — sub-millisecond on CPython. Trie / suffix-array constructions
only start to win above ~10⁴ distinct tokens, which this bot doesn't
approach.

Limitations / future hooks
--------------------------
- **Non-Latin scripts** (CJK, Arabic, etc.) tokenize to nothing because they
  don't survive the ``[^0-9a-z]+`` split after NFKD strip. The Sketch bot's
  descriptions are overwhelmingly English / romanized handles, so this is
  acceptable; revisit if a guild starts logging Japanese descriptions.
- **Aliases / synonyms** (e.g. ``csr`` → ``calyrex shadow rider``): a
  query-side alias map would slot into :func:`tokenize_query` without
  touching the index. Out of scope for v1.
- **Concatenated decomposition** (e.g. ``calyzama`` → ``caly`` + ``zama``):
  when a query token returns zero candidates, a greedy left-to-right split
  over distinct description tokens (with a min-piece-length floor) could
  recover the match. Hook lives in :meth:`DescriptionIndex.match`. Deferred.
- **Typo tolerance** (e.g. ``calyerx`` → ``calyrex``): would wrap the
  per-token scan with ``rapidfuzz`` ratios. Deferred.
- **OR semantics / scoring**: today's AND combinator is one line
  (``set.intersection``); swapping to OR or to a ranked score is a localized
  change in :meth:`DescriptionIndex.match`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Below this length, a query token requires an EXACT match against an indexed
# token (rather than substring). Chosen so ``pex`` and ``csr`` (the shortest
# colloquial nicknames we want to support) work via substring, while ``on``
# and ``a`` don't substring-match into half the corpus. Tune in config later
# if real usage warrants — exposed at module level so tests can override.
SUBSTRING_MIN_TOKEN_LENGTH = 3

# Anything that isn't an ASCII alphanumeric is a delimiter. NFKD-strip-combine
# upstream converts accented chars to their base letter, so "é" reaches the
# split as "e" and survives the regex. Hyphens, parentheses, slashes,
# periods, and whitespace all collapse to delimiters here.
_TOKEN_DELIMITER_RE = re.compile(r"[^0-9a-z]+")


def tokenize(text: str) -> list[str]:
    """Split ``text`` into normalized lower-case alphanumeric tokens.

    Normalization (in order):

    1. ``unicodedata.normalize("NFKD", text)`` to decompose accented and
       composed code points.
    2. Drop ``unicodedata.combining`` code points so ``"é"`` becomes ``"e"``
       and the regex split below treats it as a real letter.
    3. ``.casefold()`` for case insensitivity. Stronger than ``.lower()`` for
       non-ASCII edge cases and free now that we've already touched the str.
    4. Split on any run of non-``[0-9a-z]`` characters.

    Returns an empty list for empty or punctuation-only input.

    Examples:
        >>> tokenize("Shiliang Tang (tsltang0508) Dual Weather")
        ['shiliang', 'tang', 'tsltang0508', 'dual', 'weather']
        >>> tokenize("Pokémon-Café")
        ['pokemon', 'cafe']
        >>> tokenize("()(...)")
        []
    """
    if not text:
        return []
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    folded = stripped.casefold()
    return [t for t in _TOKEN_DELIMITER_RE.split(folded) if t]


def tokenize_query(text: str) -> list[str]:
    """Tokenize a user query string.

    Identical to :func:`tokenize` today. Kept as a separate function so an
    alias / synonyms map (``csr`` → ``calyrex shadow rider`` etc.) can be
    slotted in here later without touching the description-side tokenizer or
    the index.
    """
    return tokenize(text)


@dataclass
class DescriptionIndex:
    """In-memory inverted index over a fixed sequence of descriptions.

    Built via :meth:`from_descriptions`. Row "indices" are positional
    offsets into the iterable passed to ``from_descriptions`` — callers are
    responsible for keeping that list around and mapping back. The index is
    treated as immutable after construction; concurrent readers don't need
    locking.
    """

    # Description token → row positional indices containing it.
    _token_to_rows: dict[str, set[int]] = field(default_factory=dict)
    # All distinct description tokens. Order is insertion order from build,
    # which is fine — we scan linearly. Kept as a list so iteration is cache-
    # friendly and ``len()`` is O(1) for the sizing comment in match().
    _distinct_tokens: list[str] = field(default_factory=list)

    @classmethod
    def from_descriptions(cls, descriptions: Iterable[str]) -> DescriptionIndex:
        """Build an index over ``descriptions``. Row index = position."""
        token_to_rows: dict[str, set[int]] = {}
        for row_idx, desc in enumerate(descriptions):
            for tok in tokenize(desc):
                token_to_rows.setdefault(tok, set()).add(row_idx)
        return cls(
            _token_to_rows=token_to_rows,
            _distinct_tokens=list(token_to_rows.keys()),
        )

    def __len__(self) -> int:
        """Number of distinct indexed tokens. Useful for tests and logging."""
        return len(self._distinct_tokens)

    def match(self, query: str) -> set[int]:
        """Return positional row indices matching every tokenized query term.

        See module docstring for the matching rules. Returns an empty set
        when the query has no usable tokens (empty string or pure
        punctuation) — failing closed is intentional; the caller's UX should
        already require at least one filter parameter, so a zero-result
        response surfaces the malformed query faster than a substring
        fallback would.
        """
        query_tokens = tokenize_query(query)
        if not query_tokens:
            logger.info(
                "DescriptionIndex.match called with empty/punctuation-only query: %r",
                query,
            )
            return set()

        per_token_candidates: list[set[int]] = []
        for q_tok in query_tokens:
            candidates = self._candidates_for_token(q_tok)
            if not candidates:
                # AND short-circuit: any zero-candidate token means no row
                # can satisfy the full query.
                # v2 hook: when this triggers, we could attempt a greedy
                # concatenated-prefix decomposition of `q_tok` against
                # `self._distinct_tokens` (min-piece-length 3) to support
                # queries like "calyzama" → "caly" + "zama". Deferred until
                # the simple path proves insufficient.
                return set()
            per_token_candidates.append(candidates)

        return set.intersection(*per_token_candidates)

    def _candidates_for_token(self, q_tok: str) -> set[int]:
        """Find row indices that contain a description token matching ``q_tok``.

        - Below the substring floor: exact match only. Postings for ``q_tok``
          if it's an indexed token, else empty.
        - At or above the floor: union postings of every distinct description
          token that has ``q_tok`` as a substring.
        """
        if len(q_tok) < SUBSTRING_MIN_TOKEN_LENGTH:
            postings = self._token_to_rows.get(q_tok)
            return set(postings) if postings else set()

        candidates: set[int] = set()
        for d_tok in self._distinct_tokens:
            if q_tok in d_tok:
                candidates |= self._token_to_rows[d_tok]
        return candidates
