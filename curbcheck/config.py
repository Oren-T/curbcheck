"""Constants: paths, the network allowlist, and the physical numbers the engine uses.

Values here are deliberately not runtime-configurable. The allowlist in
particular is a security control (SPEC §3.2), not a user preference.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent

# The container mounts a large drive at <repo>/data; CURBCHECK_DATA_DIR lets a
# different machine point elsewhere without editing code.
DATA_DIR = Path(os.environ.get("CURBCHECK_DATA_DIR") or REPO_ROOT / "data").resolve()
DB_PATH = DATA_DIR / "curbcheck.sqlite"
RAW_DIR = DATA_DIR / "raw"

# SPEC §3.4 (threat T4): never 0.0.0.0. This is a review blocker, not a default.
BIND_HOST = "127.0.0.1"
PORT = 8765

# SPEC §3.2. Every outbound request is checked against this in curbcheck.net.
ALLOWED_HOSTS = frozenset(
    {
        "data.cityofnewyork.us",  # Socrata: signs, meters, centerline
        "api.us.socrata.com",  # Socrata discovery/metadata API
        "www.nyc.gov",  # DOT ASP calendar, meter-rate reference pages
        "s-media.nyc.gov",  # nyc.gov static asset host for the above
        "build.protomaps.com",  # one-time basemap PMTiles download (SPEC §16)
        "api-portal.nyc.gov",  # NYC 311 public API, ASP suspensions (SPEC §5.6)
    }
)

# 34 RCNY 4-08(e)(1) forbids parking within 15 ft of a hydrant.
HYDRANT_SETBACK_FT = 15

# Curb length one parked passenger car occupies, including the gap to the car
# behind. DOT uses 22 ft when converting blockface length to space counts.
CAR_LENGTH_FT = 22

# Mean adult walking speed, Bohannon & Williams Andrews 2011 meta-analysis
# (1.34 m/s across age groups). Used to turn walk distance into walk minutes.
WALK_SPEED_M_PER_S = 1.34

# Manhattan's grid means the walked path is longer than the straight line.
# 1.3 is the standard circuity factor for dense gridded street networks.
WALK_DETOUR_FACTOR = 1.3

# Every parking rule is stated in local time, including DST transitions.
NYC_TZ = ZoneInfo("America/New_York")
