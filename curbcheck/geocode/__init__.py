"""Address suggestions and reverse lookup, served entirely from the local index.

No network, by design: sending a destination to a third-party geocoder would
leak exactly the thing this app exists to keep local, and an autocomplete that
fires per keystroke would leak the typing rather than just the answer (SPEC
§3.1 threat T6, §10; `docs/ux/AUTOCOMPLETE_RESEARCH.md` §3 surveys the online
options and rejects all of them).

The index `etl.addresses` builds is a *vocabulary* index, so this package
parses first and looks up second. The only hard part of a Manhattan query is the
street name — the house number is an integer and the grammar is tiny — so the
ETL pre-expands every street into the spellings a person might type and a
typed street resolves in one prefix range-scan. Tolerance falls out of that:
`1519 third avenue`, `1519 3rd ave` and `1519 3rd av` fold to one key, `86 & 3`
and `lex & 86` reach the same node, and a typo costs an edit-distance scan that
only runs when the exact and prefix passes came back empty.

A name is also reachable by any word in it, in any order, because a person
types the word they remember: `fashion` and `hs fashion` both find HIGH SCHOOL
OF FASHION INDUSTRIES. That rung reads a second index, of words rather than
whole spellings, and `names.py` is where a match is scored (D31).

The answer ladder, best first, with what each rung is worth:

| rung | confidence | what it is |
|---|---|---|
| address point | 0.98 | a surveyed door from OTI AddressPoint |
| intersection | 0.95 | a centerline node both streets meet at |
| place | 0.85 named whole, 0.66-0.64 by one of its words | a CommonPlace feature name |
| interpolated | 0.75 | between two surveyed same-parity neighbours |
| near a door | 0.60 | the closest surveyed number on that street |
| address range | 0.50 | CSCL's own range, for a street with no doors |
| street | 0.70 / 0.69-0.67 / 0.45 / 0.30 | named exactly, by a word of its name, reached by prefix, or half of a failed corner |

Where each of those lives:

```
candidates.py    the two answer records, the kinds, and the confidence ladder
query.py         cleaning and classifying what was typed
names.py         matching a typed word against the words of a name, and its rank
street.py        which street a spelling means, and the street as an answer
address.py       the house-number rungs, including the neighbour interpolation
intersection.py  the corner rung        places.py   place names and ZIP centres
suggest.py       which rungs run, in what order, and what survives coverage
reverse.py       the inverse: what a dropped pin is nearest to
```

The package is the seam between the ETL's street-name knowledge and the API:
it imports the ETL's `fold` (one direction only, never the reverse) so a name
typed by the user is folded exactly the way the ETL folded the data. What is
re-exported here is what `api/routes.py` and `engine/` may use; everything else
is an implementation detail of one rung.
"""

from curbcheck.geocode.candidates import (
    MAX_CANDIDATES,
    GeocodeCandidate,
    GeocodeKind,
    ReverseMatch,
)
from curbcheck.geocode.query import (
    MAX_QUERY_CHARS,
    AddressQuery,
    IntersectionQuery,
    ParsedQuery,
    StreetQuery,
    ZipQuery,
    clean_query,
    parse_query,
)
from curbcheck.geocode.reverse import REVERSE_ADDRESS_MAX_M, REVERSE_SEARCH_M, reverse_geocode
from curbcheck.geocode.suggest import geocode, suggest

__all__ = [
    "MAX_CANDIDATES",
    "MAX_QUERY_CHARS",
    "REVERSE_ADDRESS_MAX_M",
    "REVERSE_SEARCH_M",
    "AddressQuery",
    "GeocodeCandidate",
    "GeocodeKind",
    "IntersectionQuery",
    "ParsedQuery",
    "ReverseMatch",
    "StreetQuery",
    "ZipQuery",
    "clean_query",
    "geocode",
    "parse_query",
    "reverse_geocode",
    "suggest",
]
