"""Street-name normalization shared by the exploration scripts.

The sign dataset spells names out ("EAST   85 STREET", "6 AVENUE") while the
centerline abbreviates them ("E  85 ST", "AVE OF THE AMERICAS"), so any join
between the two has to canonicalize both sides. Measured coverage of this
normalizer is in data/explore/street_names.txt; it is the prototype for the
production normalizer in the ETL.
"""

from __future__ import annotations

import re

# Suffix and directional words, sign spelling -> centerline spelling.
WORD_FORMS: dict[str, str] = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "PLACE": "PL",
    "DRIVE": "DR",
    "DRIVEWAY": "DR",
    "PARKWAY": "PKWY",
    "TERRACE": "TER",
    "SQUARE": "SQ",
    "ROAD": "RD",
    "LANE": "LN",
    "COURT": "CT",
    "ALLEY": "ALY",
    "PLAZA": "PLZ",
    "BRIDGE": "BRG",
    "EXPRESSWAY": "EXPY",
    "HIGHWAY": "HWY",
    "CIRCLE": "CIR",
    "TUNNEL": "TUNL",
    "EAST": "E",
    "WEST": "W",
    "NORTH": "N",
    "SOUTH": "S",
    "FT": "FORT",
    "SAINT": "ST",
    "MT": "MOUNT",
    "FIRST": "1",
    "SECOND": "2",
    "THIRD": "3",
    "FOURTH": "4",
    "FIFTH": "5",
    "SIXTH": "6",
    "SEVENTH": "7",
    "EIGHTH": "8",
    "NINTH": "9",
    "TENTH": "10",
    "ELEVENTH": "11",
    "TWELFTH": "12",
}

# Whole-name aliases the word map cannot reach: honorific renamings and DOT
# shorthands. Keys and values are already in normalized form.
NAME_ALIASES: dict[str, str] = {
    "6 AVE": "AVE OF THE AMERICAS",
    "AVE OF AMERICAS": "AVE OF THE AMERICAS",
    "ADAM C POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "ADAM CLAYTON POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "FRED DOUGLASS BLVD": "FREDERICK DOUGLASS BLVD",
    "FRED DOUGLASS CIR": "FREDERICK DOUGLASS CIR",
    "FDR DR": "FRANKLIN D ROOSEVELT DR",
    "FDR DRIVE": "FRANKLIN D ROOSEVELT DR",
    "F D R DR": "FRANKLIN D ROOSEVELT DR",
    "MALCOLM X BLVD": "LENOX AVE",
    "A C POWELL BLVD": "ADAM CLAYTON POWELL JR BLVD",
    "FRED DOUGLAS BLVD": "FREDERICK DOUGLASS BLVD",
    "FREDERICK DOUGLAS BLVD": "FREDERICK DOUGLASS BLVD",
    "G WASHINGTON BRG": "GEORGE WASHINGTON BRG",
    "BLEEKER ST": "BLEECKER ST",
    "LASALLE ST": "LA SALLE ST",
    "CUMMINGS ST": "CUMMING ST",
    "THEATRE ALY": "THEATER ALY",
    "MARGRET CORBIN DR": "MARGARET CORBIN DR",
    "CORBIN DR": "MARGARET CORBIN DR",
    "W 110 ST": "CATHEDRAL PKWY",
    "MACDOUGAL ST": "MAC DOUGAL ST",
    "MACDOUGAL ALY": "MAC DOUGAL ALY",
    "LAGUARDIA PL": "LA GUARDIA PL",
    "N D PERLMAN PL": "NATHAN D PERLMAN PL",
    "ROBERT F WAGNER PL": "R F WAGNER SR PL",
    "QUEENSBORO BRG": "ED KOCH QUEENSBORO BRG",
    "VAN DAM ST": "VANDAM ST",
    "WILLETT ST": "BIALYSTOKER PL",
    "HARLEM RIVER DR": "HARLEM RIVER DR",
}

# Sign rows use these in from_street/to_street where no cross street exists.
NON_STREET_ENDPOINTS = frozenset({"DEAD END", "END", "CUL DE SAC"})


def normalize_street(name: str) -> str:
    """Canonical uppercase form with collapsed whitespace and abbreviated words.

    Applied to both sides of the sign/centerline join; see module docstring.
    """
    # A few rows qualify the roadway after an asterisk ("PARK AVENUE*WEST RDWY");
    # the qualifier duplicates the on_street_suffix column, so drop it.
    text = re.sub(r"[.,]", "", str(name).split("*")[0]).upper()
    text = re.sub(r"\s+", " ", text).strip()
    text = " ".join(WORD_FORMS.get(token, token) for token in text.split(" "))
    return NAME_ALIASES.get(text, text)
