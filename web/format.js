/**
 * Pure formatting: API values in, display strings out. No DOM, no state.
 *
 * The rule vocabulary here mirrors `curbcheck.model.Regulation` (docs/API.md):
 * weekdays are Monday = 0 through Sunday = 6, times are "HH:MM" 24-hour local
 * strings, and money is a decimal string that must never become a float.
 */

import { CONFIDENCE_LABEL } from "./copy.js";

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const WEEKDAY_FROM_SUNDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/**
 * The four verdicts, each with the word and the symbol that carry it without
 * colour (SPEC §11). `legal` splits on `basis`: see `verdictChip`.
 */
const VERDICT_INFO = {
  legal: { label: "Legal", symbol: "✓" },
  illegal: { label: "Illegal", symbol: "✕" },
  ambiguous: { label: "Ambiguous", symbol: "?" },
  no_data: { label: "No data", symbol: "–" },
};

const ACTION_NOUN = { park: "Parking", stand: "Standing", stop: "Stopping" };
const ACTION_PROHIBITION = { park: "No parking", stand: "No standing", stop: "No stopping" };

const VEHICLE_LABEL = {
  all: "",
  passenger: "passenger cars",
  commercial: "commercial vehicles",
  truck: "trucks",
  bus: "buses",
  taxi: "taxis",
  authorized: "authorized vehicles",
  handicap: "permit holders",
  other: "the class named on the sign",
};

const FLAG_LABEL = {
  street_cleaning: "street cleaning (suspended on ASP suspension days)",
  school_days: "school days only",
  except_sunday: "except Sunday",
  including_sunday: "including Sunday",
  snow_emergency: "snow emergencies only",
  holiday_exempt: "except holidays",
  temporary: "sign marks itself temporary",
  meta: "modifies another sign on this stretch",
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Verdict label and symbol. An unknown verdict reads as no_data (docs/API.md). */
export function verdictInfo(verdict) {
  return VERDICT_INFO[verdict] || VERDICT_INFO.no_data;
}

export function verdictKey(verdict) {
  return VERDICT_INFO[verdict] ? verdict : "no_data";
}

/**
 * What the verdict chip says for one result.
 *
 * A `legal` verdict with `basis: "absence"` is legality by *absence of a rule*,
 * not by permission, and UX_AUDIT P0-1 found the old UI announcing exactly that
 * case as "Legal · 100% confidence". It gets its own word, its own outlined
 * chip in the same colour family, and never a confidence figure.
 */
export function verdictChip(result) {
  const key = verdictKey(result && result.verdict);
  if (key === "legal" && result && result.basis === "absence") {
    return { key, label: "Nothing posted", symbol: "✓", className: "verdict-absence" };
  }
  const info = VERDICT_INFO[key];
  return { key, label: info.label, symbol: info.symbol, className: `verdict-${key}` };
}

/** True when this result may print a confidence figure at all. */
export function confidenceShown(result) {
  return Boolean(result && result.confidence_shown && typeof result.confidence === "number");
}

/** "Data confidence 94%", or null when the number would mean nothing. */
export function confidenceText(result) {
  if (!confidenceShown(result)) {
    return null;
  }
  return `${CONFIDENCE_LABEL} ${Math.round(result.confidence * 100)}%`;
}

/**
 * Money for display. `null` means unknown, which is not the same as free:
 * showing "$0.00" for an unpriced meter would be a lie about cost.
 */
export function formatMoney(money, priceKnown = true) {
  if (money === null || money === undefined) {
    return "Price unknown";
  }
  const text = `$${money}`;
  return priceKnown ? text : `${text} (unconfirmed)`;
}

/**
 * What a result costs, read from the whole result.
 *
 * `null` means print nothing at all. UX_AUDIT P0-5: a grey span asserted
 * "Money — no meter" about curb the app knows nothing about, so `no_data` never
 * prints a price, a "$0", or a "no meter" — not even an unknown one. "Free" is
 * reserved for a span where a sign was actually read and says so.
 */
export function priceLabel(result) {
  const key = verdictKey(result.verdict);
  if (key === "no_data") {
    return null;
  }
  // Legality by absence read no rule at all, so it has no price to report:
  // the engine's "0.00" there means "nothing charges in this window", and
  // printing that as a price would be the app's most confident voice on the
  // thing it knows least about (UX_AUDIT P0-1, P0-5).
  if (result.basis === "absence") {
    return null;
  }
  const money = result.money;
  if (money === null || money === undefined) {
    return key === "illegal" ? null : "Price unknown";
  }
  if (result.price_known === false) {
    return `$${money} (unconfirmed)`;
  }
  if (!result.metered && money === "0.00") {
    if (key === "illegal") {
      return null;
    }
    return result.basis === "posted" ? "Free · no meter" : "Price unknown";
  }
  return `$${money}`;
}

/**
 * The span's street label, with a degenerate cross-street pair dropped.
 *
 * The server builds `street_name` from the centerline's own end nodes, and a
 * segment that begins and ends on the same avenue produces "E 86 ST, south
 * side, PARK AVE → PARK AVE" (UX_AUDIT P2-7). Saying it once is true; saying
 * it twice reads like a bug and tells the driver nothing.
 */
export function streetLabelText(streetName) {
  if (typeof streetName !== "string" || streetName === "") {
    return "Unnamed stretch";
  }
  const parts = streetName.split(", ");
  const last = parts[parts.length - 1];
  const arrow = last.split(" → ");
  if (arrow.length === 2 && arrow[0] === arrow[1]) {
    return `${parts.slice(0, -1).join(", ")}, at ${arrow[0]}`;
  }
  return streetName;
}

/**
 * Walk time in whole minutes, floored at one.
 *
 * UX_AUDIT P2-3: "0.2 min walk" is false precision, and ranking on tenths of a
 * minute ranks on noise.
 */
export function walkText(minutes) {
  if (typeof minutes !== "number" || Number.isNaN(minutes)) {
    return "—";
  }
  return `${Math.max(1, Math.round(minutes))} min`;
}

/**
 * Capacity, phrased so it cannot be read as availability (UX_AUDIT P2-4).
 * Only parkable verdicts get one; `null` means print nothing.
 */
export function capacityLabel(result) {
  const key = verdictKey(result.verdict);
  if (key === "illegal" || key === "no_data") {
    return null;
  }
  const cars = result.capacity_cars;
  if (cars === null || cars === undefined) {
    return null;
  }
  return cars === 1 ? "room for ~1 car" : `room for ~${cars} cars`;
}

export function formatCapacity(cars) {
  if (cars === null || cars === undefined) {
    return null;
  }
  return cars === 1 ? "room for ~1 car when empty" : `room for ~${cars} cars when empty`;
}

export function formatConfidence(confidence) {
  if (typeof confidence !== "number" || Number.isNaN(confidence)) {
    return null;
  }
  return `${Math.round(confidence * 100)}%`;
}

/** "Mon–Fri", "Mon–Wed, Sat", "every day". */
export function formatDays(days) {
  if (!Array.isArray(days) || days.length === 0) {
    return "no days";
  }
  const sorted = [...new Set(days)].filter((day) => day >= 0 && day <= 6).sort((a, b) => a - b);
  if (sorted.length === 7) {
    return "every day";
  }
  const runs = [];
  for (const day of sorted) {
    const last = runs[runs.length - 1];
    if (last && day === last[1] + 1) {
      last[1] = day;
    } else {
      runs.push([day, day]);
    }
  }
  return runs
    .map(([from, to]) => {
      if (from === to) {
        return DAY_NAMES[from];
      }
      if (to === from + 1) {
        return `${DAY_NAMES[from]}, ${DAY_NAMES[to]}`;
      }
      return `${DAY_NAMES[from]}–${DAY_NAMES[to]}`;
    })
    .join(", ");
}

/** "8:30 AM" from "08:30". */
export function formatClock(value) {
  if (typeof value !== "string" || value.length !== 5) {
    return String(value ?? "");
  }
  const hours = Number(value.slice(0, 2));
  const minutes = value.slice(3);
  if (Number.isNaN(hours)) {
    return value;
  }
  const suffix = hours < 12 ? "AM" : "PM";
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return `${hour12}:${minutes} ${suffix}`;
}

/** "8:30 AM–7:00 PM", "all day", or a range flagged as wrapping past midnight. */
export function formatTimeRange(timeFrom, timeTo) {
  if (!timeFrom || !timeTo) {
    return "all day";
  }
  const range = `${formatClock(timeFrom)}–${formatClock(timeTo)}`;
  return timeTo <= timeFrom ? `${range} (past midnight)` : range;
}

export function formatDuration(minutes) {
  if (minutes === null || minutes === undefined) {
    return null;
  }
  if (minutes % 60 === 0) {
    const hours = minutes / 60;
    return hours === 1 ? "1 hour" : `${hours} hours`;
  }
  return `${minutes} min`;
}

/** "Mar 1 – Nov 30" for a seasonal rule, or null when it runs all year. */
export function formatSeason(effectiveFrom, effectiveTo) {
  if (!effectiveFrom || !effectiveTo) {
    return null;
  }
  return `${formatMonthDay(effectiveFrom)} – ${formatMonthDay(effectiveTo)}`;
}

function formatMonthDay(value) {
  const month = Number(value.slice(0, 2));
  const day = Number(value.slice(3));
  if (Number.isNaN(month) || Number.isNaN(day) || month < 1 || month > 12) {
    return value;
  }
  return `${MONTHS[month - 1]} ${day}`;
}

export function flagLabels(flags) {
  if (!flags || typeof flags !== "object") {
    return [];
  }
  return Object.entries(flags)
    .filter(([, value]) => value === true)
    .map(([name]) => FLAG_LABEL[name] || name);
}

export function vehicleLabel(vehicleClass, exclusive) {
  const label = VEHICLE_LABEL[vehicleClass] ?? vehicleClass;
  if (!label) {
    return "";
  }
  return exclusive ? `${label} only` : label;
}

export function actionLabel(action, permitted) {
  if (permitted) {
    return `${ACTION_NOUN[action] || "Parking"} permitted`;
  }
  return ACTION_PROHIBITION[action] || "Not permitted";
}

/** One plain-English sentence for a parsed rule. */
export function describeRegulation(regulation) {
  if (!regulation) {
    return "";
  }
  const parts = [actionLabel(regulation.action, regulation.permitted)];
  const vehicle = vehicleLabel(regulation.vehicle_class, regulation.exclusive);
  if (vehicle) {
    parts.push(`for ${vehicle}`);
  }
  parts.push(formatTimeRange(regulation.time_from, regulation.time_to));
  parts.push(formatDays(regulation.days));

  const extras = [];
  if (regulation.metered) {
    extras.push("metered");
  }
  const limit = formatDuration(regulation.max_duration_min);
  if (limit) {
    extras.push(`max ${limit}`);
  }
  extras.push(...flagLabels(regulation.flags));
  const season = formatSeason(regulation.effective_from, regulation.effective_to);
  if (season) {
    extras.push(season);
  }

  const sentence = parts.join(" ");
  return extras.length > 0 ? `${sentence} · ${extras.join(", ")}` : sentence;
}

/** Hourly meter rates as "$4.50 first hour, then $5.50". */
export function formatHourRates(hourRates) {
  if (!Array.isArray(hourRates) || hourRates.length === 0) {
    return "no published rate";
  }
  if (hourRates.length === 1) {
    return `$${hourRates[0]} per hour`;
  }
  const [first, ...rest] = hourRates;
  return `$${first} first hour, then ${rest.map((rate) => `$${rate}`).join(", ")}`;
}

/**
 * The count line above the results, built from the server's `counts`.
 *
 * `counts` is measured over everything in radius, before either cap, and the
 * response can hold fewer rows than that (docs/API.md). Counting the rows we
 * were sent would state as fact something the query never established, which
 * is the failure docs/VALIDATION.md U1 recorded — so the numbers come from the
 * server and the line says plainly when it is showing a subset.
 */
export function statusLine(counts, shown, walkMinutes) {
  const total = counts && typeof counts.total === "number" ? counts.total : shown;
  if (total === 0) {
    return `Nothing within a ${walkMinutes}-minute walk.`;
  }
  const parts = [`${total} stretches within a ${walkMinutes} min walk`];
  if (counts) {
    parts.push(
      `${counts.legal ?? 0} legal · ${counts.ambiguous ?? 0} ambiguous · ` +
        `${counts.illegal ?? 0} illegal · ${counts.no_data ?? 0} no data`,
    );
  }
  if (shown < total) {
    parts.push(`${shown} drawn, nearest first`);
  }
  return parts.join(" · ");
}

/* ---- The search window ------------------------------------------------- */

const pad = (value) => String(value).padStart(2, "0");

/** "2026-09-16" for a `<input type="date">`, in the browser's local time. */
export function toDateInputValue(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** "10:00" for a `<input type="time">`, in the browser's local time. */
export function toTimeInputValue(date) {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * A naive local ISO string for the API.
 *
 * docs/API.md: a value without an offset is read in America/New_York, which is
 * what every parking sign states its hours in. Sending the browser's UTC offset
 * instead would silently shift the window for anyone not in New York.
 */
export function toApiDateTime(dateValue, timeValue) {
  return `${dateValue}T${timeValue}`;
}

/** A local `Date` from the two input values, or null when either is empty. */
export function parseLocal(dateValue, timeValue) {
  if (!dateValue || !timeValue) {
    return null;
  }
  const [year, month, day] = dateValue.split("-").map(Number);
  const [hour, minute] = timeValue.split(":").map(Number);
  if ([year, month, day, hour, minute].some((part) => Number.isNaN(part))) {
    return null;
  }
  return new Date(year, month - 1, day, hour, minute, 0, 0);
}

export function addMinutes(date, minutes) {
  return new Date(date.getTime() + minutes * 60 * 1000);
}

/** The next quarter hour, local time — the default arrival. */
export function nextQuarterHour(now = new Date()) {
  const next = new Date(now.getTime());
  next.setSeconds(0, 0);
  next.setMinutes(Math.ceil((next.getMinutes() + 1) / 15) * 15);
  return next;
}

function dayLabel(date) {
  return `${WEEKDAY_FROM_SUNDAY[date.getDay()]} ${MONTHS[date.getMonth()]} ${date.getDate()}`;
}

/**
 * "Wed Sep 16, 10:00 → 12:00", or with the second date when the window crosses
 * midnight. UX_AUDIT P0-7: a `datetime-local` widget clipped its own value, so
 * the window — which drives every verdict — has to be rendered as text.
 */
export function windowSummary(start, end) {
  if (!start || !end) {
    return "";
  }
  const from = `${dayLabel(start)}, ${toTimeInputValue(start)}`;
  const sameDay = toDateInputValue(start) === toDateInputValue(end);
  const to = sameDay ? toTimeInputValue(end) : `${dayLabel(end)}, ${toTimeInputValue(end)}`;
  return `${from} → ${to}`;
}
