"""Deterministic grammar for NYC `sign_description` strings (docs/DECISIONS.md D2).

`parse_description(raw)` is the whole public surface. It is pure and cached:
74,590 active Manhattan sign rows use only 1,790 distinct strings, so the cache
turns the ETL's parse step into 1,790 real parses.

## Representation choices

**Vehicle-class exclusives.** Every sign that reserves the curb for a named
class — `COMMERCIAL VEHICLES ONLY`, `TRUCK LOADING ONLY`, `AVO`, `TAXI STAND`,
`BUS STOP`, `FIRE ZONE`, `NYP LICENSE PLATES ONLY` — becomes one regulation with
`permitted=True`, `vehicle_class` set to that class and `exclusive=True`. That is
the single representation, used even when the sign spells the prohibition out
(`BUS STOP SIGN (BUS & HANDICAP SYMBOLS) NO STANDING`), so that
`Regulation.applies_to_passenger()` returns False for all of them by one path.

**`EXCEPT <class>` riders are not exclusives.** `NO STANDING EXCEPT TRUCKS
LOADING & UNLOADING` and `NO PARKING EXCEPT AUTHORIZED VEHICLES` stay
prohibitions with `vehicle_class=ALL`: the exception does not help a passenger
car, and writing it as an exclusive would claim the sign reserves the space.

**Single arrows.** The glyph says only how many arrows a sign has, never which
way one points (docs/DECISIONS.md D3 refined). A single arrow is emitted as
`Arrow.FORWARD` as a *placeholder*; the snapping step replaces it with the
bearing from the sign row's `arrow_direction` column. `Arrow.BOTH` and
`Arrow.NONE` are final.

## Confidence scale

| value | meaning |
|---|---|
| 1.0 | every token consumed by the grammar |
| 0.7 | the rule is established but a rider or token was skipped, or the sign says something no single `Regulation` can express |
| 0.0 | `ParseMethod.UNPARSED`, empty regulation list |

0.7 is deliberately below `engine.resolve.AMBIGUITY_THRESHOLD`, so a partial
parse reaches the user as "ambiguous" rather than as a verdict. A partial parse
of a *prohibitive* sign still emits the prohibition, which is the conservative
direction; a partial parse of anything permissive for a passenger car goes
`UNPARSED` instead, so a skipped rider can never turn into a false "legal".
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import ValidationError

from curbcheck.etl.parse.grammar import parse_rules
from curbcheck.etl.parse.panels import advisory_class, panel_class
from curbcheck.etl.parse.tokens import MAX_INPUT_CHARS, lex, normalize
from curbcheck.model import ParsedSign, ParseMethod

__all__ = ["panel_class", "parse_description"]

FULL_CONFIDENCE = 1.0
PARTIAL_CONFIDENCE = 0.7

# One per distinct description, with headroom for the other boroughs' vocabulary.
_CACHE_SIZE = 8192


@lru_cache(maxsize=_CACHE_SIZE)
def parse_description(raw: str) -> ParsedSign:
    """Read one `sign_description` into structured regulations.

    Never raises and never guesses: a string it cannot establish a rule from
    comes back as `ParseMethod.UNPARSED` with an empty regulation list.
    """
    if len(raw) > MAX_INPUT_CHARS:
        return _unparsed(raw, f"longer than {MAX_INPUT_CHARS} characters")

    panel = panel_class(raw)
    if panel is not None:
        return ParsedSign(
            raw=raw, regulations=[], parse_method=ParseMethod.GRAMMAR, confidence=1.0, notes=panel
        )

    lexed = lex(raw)
    try:
        result = parse_rules(lexed)
    except ValidationError as error:
        # The grammar built a field combination `model.Regulation` rejects. That
        # is a parser bug, but on untrusted input the safe response is the same
        # as for any unreadable sign (SPEC §11), not a traceback out of the ETL.
        return _unparsed(raw, f"rejected by the regulation schema: {error.error_count()} errors")
    if not result.regulations:
        advisory = advisory_class(normalize(raw))
        if advisory is not None:
            return ParsedSign(
                raw=raw,
                regulations=[],
                parse_method=ParseMethod.GRAMMAR,
                confidence=1.0,
                notes=advisory,
            )
        return _unparsed(raw, "no rule head recognized")

    skipped = [*result.degraded, *(f"'{token}'" for token in result.leftovers)]
    if not skipped:
        return ParsedSign(
            raw=raw,
            regulations=result.regulations,
            parse_method=ParseMethod.GRAMMAR,
            confidence=FULL_CONFIDENCE,
            notes="",
        )

    note = "skipped: " + "; ".join(skipped)
    if any(rule.applies_to_passenger() is True for rule in result.regulations):
        return _unparsed(raw, f"partial match on a permissive sign; {note}")
    return ParsedSign(
        raw=raw,
        regulations=result.regulations,
        parse_method=ParseMethod.GRAMMAR,
        confidence=PARTIAL_CONFIDENCE,
        notes=note,
    )


def _unparsed(raw: str, reason: str) -> ParsedSign:
    return ParsedSign(
        raw=raw,
        regulations=[],
        parse_method=ParseMethod.UNPARSED,
        confidence=0.0,
        notes=reason,
    )
