"""What a geocode answer is, and what each rung of the ladder is worth.

`suggest` and `reverse_geocode` both hand back one of these two records, and
every rung that can produce one scores it against the confidence constants
here, so the ladder is readable in one place rather than spread over the
modules that implement its rungs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from curbcheck.engine.coverage import COVERAGE_AREA

# Eight is the longest list a person scans without reading it as a search
# result page rather than as a disambiguation (docs/ux/UX_AUDIT.md P1-5 asks
# for candidates to always be offered, which only works if the list stays
# glanceable).
MAX_CANDIDATES = 8

# Everything in the database is in one borough, so the second line of a
# candidate says so when it has nothing more specific to say.
DEFAULT_SECONDARY = COVERAGE_AREA

CONFIDENCE_ADDRESS_POINT = 0.98
CONFIDENCE_INTERSECTION = 0.95
CONFIDENCE_PLACE = 0.85
CONFIDENCE_ADDRESS_INTERPOLATED = 0.75
CONFIDENCE_NEAR_ADDRESS = 0.60
CONFIDENCE_ADDRESS_RANGE = 0.50
# A street whose spelling the query matches exactly ("5 ave", "broadway") is
# what the user is naming, and has to outrank a place whose name merely
# contains those words: at 0.45 the query "5 ave" answered 5 AVE SYNAGOGUE
# (0.85 x 2 of its 3 words) before Fifth Avenue. A street reached by prefix
# ("broadwa") is still a guess about what is being typed, and one offered
# because half of an unmatched intersection hit it is a consolation prize that
# ranks below a partial place-name match.
CONFIDENCE_STREET_EXACT = 0.70
CONFIDENCE_STREET_WHOLE_QUERY = 0.45
CONFIDENCE_STREET_HALF_QUERY = 0.30
CONFIDENCE_ZIP = 0.25

# A place name matching only some of its own words is a weak hit ("86 ST"
# matches "CTL PK W DR OV 86 ST TRNVS RD"), so the score scales with how much
# of the place's name the query accounted for, and weak hits are dropped.
MIN_PLACE_COVERAGE = 0.5

# A fuzzy street match is a guess about what the user meant, so it costs a
# fifth of the candidate's confidence rather than being silently as good.
FUZZY_CONFIDENCE_PENALTY = 0.8


class GeocodeKind(StrEnum):
    """What a candidate *is*, which is what decides the icon and the second line.

    PIN is the one value the geocoder never returns: it is what the frontend
    labels a crosshair the user has not dropped yet. Everything else here is
    produced by `suggest` or `reverse_geocode`.
    """

    ADDRESS = "address"
    INTERSECTION = "intersection"
    STREET = "street"
    ZIP = "zip"
    PLACE = "place"
    PIN = "pin"


# A street's pin is a vertex of one of its own centerline segments and a corner
# is a segment endpoint (`etl.addresses._street_points`, `_write_intersections`),
# so both are at distance zero from the centerline by construction and asking
# `within_coverage` costs a scan to learn nothing. The kinds read off another
# dataset -- a surveyed door, a place, a ZIP centroid -- are still checked. On


@dataclass(frozen=True)
class GeocodeCandidate:
    """One place the query might mean. `confidence` is 0-1, higher is better.

    `label` is the line the user reads and `secondary` the muted line under it:
    the ZIP, how the point was arrived at, or the borough when there is nothing
    narrower to say. Both are the city's own capitals and are untrusted text.
    """

    label: str
    lat: float
    lon: float
    kind: GeocodeKind
    confidence: float
    secondary: str | None = DEFAULT_SECONDARY


@dataclass(frozen=True)
class ReverseMatch:
    """What a dropped pin is nearest to. `distance_m` is to the returned point."""

    label: str
    secondary: str | None
    kind: GeocodeKind
    lat: float
    lon: float
    distance_m: float


def zip_secondary(zipcode: object) -> str:
    text = str(zipcode or "").strip()
    return f"{DEFAULT_SECONDARY} {text}" if text else DEFAULT_SECONDARY
