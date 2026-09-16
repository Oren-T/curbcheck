"""Finding a name by the words in it, and deciding how well each one was named.

The street and place rungs both answer the same question — which of these
names did the user mean? — so they ask it the same way: every typed word has to
prefix-match a word of some spelling of the name, in any order, and where the
words landed is what the answer is worth. `etl.addresses` writes the word index
both read (`docs/ux/AUTOCOMPLETE_RESEARCH.md` §6, `docs/DECISIONS.md` D31).

The search is two passes over one index and never a join. The first pass pulls
the candidates of the typed word that matches fewest rows; the second checks
the remaining words against the spelling that came back with each candidate,
which is why a common word like "school" can never truncate the answer a rare
one like "fashion" already found.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

from curbcheck.geocode.query import prefix_bound

# What one typed word may pull out of the token index. Bounded because a single
# letter is a real query: "C" leads 2,722 of the 24,000 place-token rows, and
# the index is ordered so that the rows this stops at are the ones where the
# word is the whole name or starts it.
MAX_TOKEN_ROWS = 200

# A candidate set this small is not worth scanning a second word to beat, and
# most real queries reach it on the first: "fashion" matches 2 rows.
ENOUGH_TOKEN_ROWS = 40

# How many of the typed words may be looked up before one of them has to be the
# driver. A 120-character query can hold twenty words and each one is a range
# scan; every word is still *checked*, in Python, against every candidate.
MAX_TOKEN_SCANS = 3


class NameMatch(IntEnum):
    """Where the typed words landed in the name. Ordered: higher is a better match.

    The order is the one a person scans a suggestion list in — the name they
    typed whole, then the names that begin with what they typed, then the names
    that begin with one of their words, then the names that merely contain
    them. Google Maps ranks this way and the UX audit's testers expected it.
    """

    INNER = 0
    LEADING = 1
    PREFIX = 2
    WHOLE = 3


@dataclass(frozen=True)
class NameHit:
    """One name the query could mean: the row's key, the spelling it matched, how well."""

    name_id: str
    search_name: str
    match: NameMatch


def match_name(tokens: Sequence[str], words: Sequence[str]) -> NameMatch | None:
    """How `tokens` match one spelling's `words`, or None when they do not.

    Every token must prefix-match some word, which is what makes the match
    order-independent: "fashion high" and "high fashion" both find HIGH SCHOOL
    OF FASHION INDUSTRIES. Stopwords are already gone from both sides
    (`etl.addresses.name_words`), so "of" can neither be required nor get in
    the way.
    """
    if not tokens or not words:
        return None
    if not all(any(word.startswith(token) for word in words) for token in tokens):
        return None
    if len(tokens) == len(words) and all(
        token == word for token, word in zip(tokens, words, strict=True)
    ):
        return NameMatch.WHOLE
    if len(tokens) <= len(words) and all(
        word.startswith(token) for token, word in zip(tokens, words[: len(tokens)], strict=True)
    ):
        return NameMatch.PREFIX
    if any(words[0].startswith(token) for token in tokens):
        return NameMatch.LEADING
    return NameMatch.INNER


def search_names(
    conn: sqlite3.Connection, token_sql: str, tokens: Sequence[str], limit: int
) -> list[NameHit]:
    """The `limit` names best matching every one of `tokens`, best first.

    `token_sql` selects (name key, search name) for a word prefix and takes the
    three bound parameters `prefix`, `prefix_bound`, `limit`; it is a constant
    of the calling module, never built from anything typed.

    Ties are broken by the shorter name and then alphabetically, so BRYANT PARK
    comes before FIVE BRYANT PARK and the order never depends on which row
    SQLite reached first.
    """
    best: dict[str, NameHit] = {}
    for name_id, spelling in _driver_rows(conn, token_sql, tokens):
        match = match_name(tokens, spelling.split(" "))
        if match is None:
            continue
        previous = best.get(name_id)
        if previous is None or match > previous.match:
            best[name_id] = NameHit(name_id=name_id, search_name=spelling, match=match)
    ordered = sorted(
        best.values(), key=lambda hit: (-hit.match, len(hit.search_name), hit.search_name)
    )
    return ordered[:limit]


def _driver_rows(
    conn: sqlite3.Connection, token_sql: str, tokens: Sequence[str]
) -> list[tuple[str, str]]:
    """Candidates from the typed word that matched fewest rows, longest word first.

    Longest first because a longer prefix is usually the rarer one, and the
    scan stops as soon as a word has come back with few enough rows that
    another word could not narrow the work that follows.

    Sorted on the way out: `search_names` keeps the first spelling it sees at
    an equal match rank, and iterating a set here made the fourth suggestion
    for "p" depend on the process's hash seed. The browser engine
    (docs/STATIC_SITE.md) is checked against this function's output, so the
    reference has to be the same answer every time.
    """
    driver: set[tuple[str, str]] = set()
    for index, token in enumerate(sorted(tokens, key=len, reverse=True)[:MAX_TOKEN_SCANS]):
        rows = {
            (str(row[0]), str(row[1]))
            for row in conn.execute(token_sql, (token, prefix_bound(token), MAX_TOKEN_ROWS))
        }
        if index == 0 or len(rows) < len(driver):
            driver = rows
        if len(driver) <= ENOUGH_TOKEN_ROWS:
            break
    return sorted(driver)
