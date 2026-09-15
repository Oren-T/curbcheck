"""The phrase tables the grammar matches against.

Every entry was found in `data/explore/descriptions.tsv` (1,790 distinct active
Manhattan strings) or in `descriptions_all_boroughs.tsv`; nothing here is a
guess at wording DOT might use. Keeping the vocabulary separate from the scan
means extending the parser to a new sign family is usually a new row here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from curbcheck.model import Action, VehicleClass


@dataclass(frozen=True)
class Head:
    """What a rule head establishes before any schedule is read."""

    action: Action
    permitted: bool
    vehicle_class: VehicleClass = VehicleClass.ALL
    exclusive: bool = False
    metered: bool = False
    max_duration_min: int | None = None
    meta: bool = False


@dataclass(frozen=True)
class Rider:
    """A phrase that qualifies a rule without changing who may do what."""

    temporary: bool = False


_PROHIBITIONS: dict[str, Head] = {
    "NO PARKING": Head(Action.PARK, permitted=False),
    "NO STANDING": Head(Action.STAND, permitted=False),
    "NO STOPPING": Head(Action.STOP, permitted=False),
}

# Signs that reserve the curb for a named class. Representation choice, applied
# everywhere: `permitted=True` for that class with `exclusive=True`, which is
# what makes `Regulation.applies_to_passenger()` return False. See the package
# docstring.
_EXCLUSIVES: dict[str, Head] = {
    "AVO": Head(Action.PARK, True, VehicleClass.AUTHORIZED, exclusive=True),
    "AUTHORIZED VEHICLES ONLY": Head(Action.PARK, True, VehicleClass.AUTHORIZED, exclusive=True),
    "AUTHORIZED VEHICLE ONLY": Head(Action.PARK, True, VehicleClass.AUTHORIZED, exclusive=True),
    "LICENSE PLATES ONLY": Head(Action.PARK, True, VehicleClass.AUTHORIZED, exclusive=True),
    "PERMITS ONLY": Head(Action.PARK, True, VehicleClass.AUTHORIZED, exclusive=True),
    "VEHICLES WITH PERMIT ONLY": Head(Action.PARK, True, VehicleClass.AUTHORIZED, exclusive=True),
    "FIRE ZONE": Head(Action.STAND, True, VehicleClass.AUTHORIZED, exclusive=True),
    "AMBULANCE ONLY": Head(Action.STAND, True, VehicleClass.AUTHORIZED, exclusive=True),
    "AMBULETTE ONLY": Head(Action.STAND, True, VehicleClass.AUTHORIZED, exclusive=True),
    "AMBULANCE AMBULETTE ONLY": Head(Action.STAND, True, VehicleClass.AUTHORIZED, exclusive=True),
    "COMMERCIAL VEHICLES ONLY": Head(Action.PARK, True, VehicleClass.COMMERCIAL, exclusive=True),
    "LOADING ONLY": Head(Action.STAND, True, VehicleClass.COMMERCIAL, exclusive=True),
    "LOADING ZONE": Head(Action.STAND, True, VehicleClass.COMMERCIAL, exclusive=True),
    "TRUCK LOADING ONLY": Head(Action.STAND, True, VehicleClass.TRUCK, exclusive=True),
    "TRUCKS LOADING ONLY": Head(Action.STAND, True, VehicleClass.TRUCK, exclusive=True),
    "TRUC LOADING ONLY": Head(Action.STAND, True, VehicleClass.TRUCK, exclusive=True),
    "TRUCK WAITING LINE": Head(Action.STAND, True, VehicleClass.TRUCK, exclusive=True),
    "WAITING LINE": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "BUS STOP": Head(Action.STAND, True, VehicleClass.BUS, exclusive=True),
    "BUS STOP SIGN": Head(Action.STAND, True, VehicleClass.BUS, exclusive=True),
    "BUS STAND": Head(Action.STAND, True, VehicleClass.BUS, exclusive=True),
    "BUS LAYOVER ONLY": Head(Action.STAND, True, VehicleClass.BUS, exclusive=True),
    "BUSES ONLY": Head(Action.STAND, True, VehicleClass.BUS, exclusive=True),
    "BUSES WITH PERMIT ONLY": Head(Action.STAND, True, VehicleClass.BUS, exclusive=True),
    "TAXI STAND": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "TAXI RELIEF STAND": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "TAXI FHV STAND": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "TAXI/FHV STAND": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "TAXI/FHV RELIEF STAND": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "FOR-HIRE VEHICLES ONLY": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "FOR HIRE VEHICLES ONLY": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "PICK-UP/DROP-OFF ONLY": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "PICK-UP DROP-OFF ONLY": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "TAXI/FHV PICK-UP/DROP-OFF ONLY": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "PRE-ARRANGED SERVICE ONLY": Head(Action.STAND, True, VehicleClass.TAXI, exclusive=True),
    "HOTEL LOADING ZONE": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "FARMERS MARKET ONLY": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "FLEA MARKET LOADING ONLY": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "CARSHARE PARKING ONLY": Head(Action.PARK, True, VehicleClass.OTHER, exclusive=True),
    "MOTORCYCLE PARKING ONLY": Head(Action.PARK, True, VehicleClass.OTHER, exclusive=True),
    "HORSE DRAWN CABS ONLY": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "ELECTRIC VEHICLE CHARGING ONLY": Head(Action.PARK, True, VehicleClass.OTHER, exclusive=True),
    "ELECTRIC VEHICLES CHARGING ONLY": Head(Action.PARK, True, VehicleClass.OTHER, exclusive=True),
    "COMMUTER VAN STOP": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "AUTHORIZED COMMUTER VANS ONLY": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "MICROHUB ZONE": Head(Action.STAND, True, VehicleClass.OTHER, exclusive=True),
    "SECURITY CHECKPOINT": Head(Action.STAND, True, VehicleClass.AUTHORIZED, exclusive=True),
    "CHECKPOINT": Head(Action.STAND, True, VehicleClass.AUTHORIZED, exclusive=True),
}

# SPEC §8.4 example 7: these modify a sibling rule instead of standing alone.
# The regulation they yield exists only to carry `flags.meta`, which
# `engine.resolve.ambiguity_reason` turns into an AMBIGUOUS verdict.
_META: dict[str, Head] = {
    "METERS ARE NOT IN EFFECT ABOVE TIMES": Head(Action.PARK, permitted=True, meta=True),
    "LIMITED PARKING IS NOT IN EFFECT ABOVE TIMES": Head(Action.PARK, permitted=True, meta=True),
}

_RIDERS: dict[str, Rider] = {
    "TEMPORARY CONSTRUCTION REGULATION": Rider(temporary=True),
    "CONSTRUCTION": Rider(temporary=True),
    "NIGHT REGULATION": Rider(),
    "SPECIAL NIGHT REGULATION": Rider(),
    "IN TUNNEL": Rider(),
    "NO ENGINE IDLING": Rider(),
    "MAX FINE": Rider(),
    "LICENSE PLATES": Rider(),
    "DECALS ONLY": Rider(),
    "DELIVERY DECAL REQUIRED": Rider(),
    "ROUTES": Rider(),
    "REVISED": Rider(),
    "REVISION": Rider(),
    "DATED": Rider(),
}

# `EXCEPT <class>` never helps a passenger car, so the prohibition stands as
# written with vehicle_class ALL (SPEC §8.5, "vehicle-class exclusions").
EXCEPT_RIDER = "EXCEPT"
EXCEPT_TYPOS = frozenset({"EXEPT", "EXCPET", "EXECPT"})

# Drafting marks DOT leaves in the description: sign codes, revision dates, sheet
# sizes, fine amounts. They say nothing about the curb.
NOISE_WORDS = frozenset(
    {
        "DATED",
        "ES",
        "FOR",
        "IF",
        "NECESSARY",
        "REV",
        "RIDER",
        "ROUTE",
        "ROUTES",
        "SEE",
        "SIZE",
        "SUPERSEDE",
        "SUPERSEDED",
        "SUPERSEDES",
        "SUPERSED",
    }
)
_NOISE_TOKEN = re.compile(
    r"^W/\d+$"
    r"|^#\d+$"
    r"|^\$[\dX]+$"
    r"|^REV\.?#?\d*$"
    r"|^-?\d{1,4}[-/.]\d{1,4}(?:[-/.]\d{2,4})?$"
    r"|^-\d+$"
    r"|^[A-Z]{1,6}\d*-[A-Z0-9.#-]+$"
    r"|^\d+[\"']X?$"
)

CLAUSE_BREAKS: tuple[tuple[str, ...], ...] = (("OTHER", "TIMES"), ("OTHERS",))

DURATION_UNITS: dict[tuple[str, ...], tuple[bool, int]] = {
    ("HMP",): (True, 60),
    ("HOUR", "METERED", "PARKING"): (True, 60),
    ("HR", "METERED", "PARKING"): (True, 60),
    ("HOUR", "PARKING"): (False, 60),
    ("HR", "PARKING"): (False, 60),
    ("MMP",): (True, 1),
    ("MINUTE", "PARKING"): (False, 1),
    ("MIN", "PARKING"): (False, 1),
}
DURATION_RIDER_UNITS: dict[tuple[str, ...], int] = {("HOUR", "LIMIT"): 60, ("HR", "LIMIT"): 60}

HEAD_PHRASES: dict[tuple[str, ...], Head] = {
    tuple(phrase.split()): head
    for table in (_PROHIBITIONS, _EXCLUSIVES, _META)
    for phrase, head in table.items()
}
RIDER_PHRASES: dict[tuple[str, ...], Rider] = {
    tuple(phrase.split()): rider for phrase, rider in _RIDERS.items()
}
LONGEST_HEAD = max(len(phrase) for phrase in HEAD_PHRASES)
LONGEST_RIDER = max(len(phrase) for phrase in RIDER_PHRASES)

# A prohibition glued to the next word occurs in the live data
# (`NO STANDINGHANDICAP`); splitting it back out costs nothing and is safe
# because no legitimate token starts with one of these verbs.
GLUED_VERBS = ("STANDING", "PARKING", "STOPPING")


def is_noise(token: str) -> bool:
    """True for a drafting mark: a sign code, a revision date, a sheet size, a fine."""
    return token in NOISE_WORDS or bool(_NOISE_TOKEN.match(token))
