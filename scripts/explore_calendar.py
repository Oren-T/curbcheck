"""Fetch and summarize the DOT Alternate Side Parking suspension calendar.

nyc.gov refuses requests without a browser User-Agent (403), so the fetch sets
one; everything else is a plain https GET. The ICS carries one VEVENT per
suspension with the reason and the meter status in DESCRIPTION, which is the
only machine-readable statement of "are meters in effect on this date" we
found. No Open Data dataset publishes this calendar; see docs/DATA.md.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "calendar"
OUT = ROOT / "data" / "explore"

ICS_URL = "https://www.nyc.gov/html/dot/downloads/misc/2026-alternate-side.ics"
PDF_URL = "https://www.nyc.gov/html/dot/downloads/pdf/asp-calendar-2026.pdf"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# DOT writes the reason into DESCRIPTION rather than SUMMARY, which is the
# constant string "Alternate Side Parking Suspended" on every event.
REASON = re.compile(r"suspended for (.+?)\.\s", re.I)


def fetch(url: str, destination: Path) -> bytes:
    if not url.startswith("https://www.nyc.gov/"):
        raise ValueError(f"refusing URL outside nyc.gov: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})  # noqa: S310 - host checked
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        payload = response.read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    destination.with_suffix(destination.suffix + ".meta.json").write_text(
        json.dumps(
            {
                "url": url,
                "fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    return payload


def unfold(text: str) -> str:
    """Undo RFC 5545 line folding, where a continuation starts with a space or tab."""
    return re.sub(r"\r?\n[ \t]", "", text)


def events(ics_text: str) -> list[tuple[date, str, bool]]:
    """Return (day, reason, meters_in_effect), one row per suspended day.

    Eight of the 2026 events are two-day holidays expressed as a DTEND one day
    past the last suspended day, so counting VEVENTs undercounts the calendar.
    """
    rows = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfold(ics_text), flags=re.S):
        start = re.search(r"DTSTART[^:]*:(\d{8})", block)
        end = re.search(r"DTEND[^:]*:(\d{8})", block)
        if not start:
            continue
        description = re.search(r"DESCRIPTION:(.*)", block)
        text = description.group(1) if description else ""
        reason = REASON.search(text)
        first = datetime.strptime(start.group(1), "%Y%m%d").date()
        last = datetime.strptime(end.group(1), "%Y%m%d").date() if end else first
        day = first
        while day < max(last, first + timedelta(days=1)):
            rows.append(
                (
                    day,
                    reason.group(1).strip() if reason else "?",
                    "meters will be in effect" in text.lower(),
                )
            )
            day += timedelta(days=1)
    return sorted(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ics = fetch(ICS_URL, RAW / "2026-alternate-side.ics").decode("utf-8", "replace")
    fetch(PDF_URL, RAW / "asp-calendar-2026.pdf")
    rows = events(ics)
    dates = {d for d, _, _ in rows}
    meters_off = [d for d, _, meters in rows if not meters]
    vevents = len(re.findall(r"BEGIN:VEVENT", ics))

    lines = [
        "# NYC DOT Alternate Side Parking 2026 suspension calendar",
        f"source  {ICS_URL}",
        f"VEVENTs  {vevents}",
        f"suspended days after expanding DTSTART..DTEND  {len(rows)}",
        f"distinct suspension dates  {len(dates)}",
        f"dates where the ICS also says meters are NOT in effect  {len(set(meters_off))}",
        "",
        "date        meters_in_effect  reason",
    ]
    lines += [f"{d.isoformat()}  {'yes' if m else 'NO '}               {r}" for d, r, m in rows]
    lines += [
        "",
        "# reasons by frequency",
        "\n".join(
            f"{n:4d}  {r}" for r, n in collections.Counter(r for _, r, _ in rows).most_common()
        ),
    ]
    (OUT / "asp_calendar_2026.txt").write_text("\n".join(lines) + "\n")
    print(f"wrote data/explore/asp_calendar_2026.txt ({len(rows)} days, {len(dates)} dates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
