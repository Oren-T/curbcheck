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
 * The four verdicts and the word that carries each one without colour
 * (SPEC §11). The second, non-colour channel is the line pattern the chip and
 * the map share (`verdicts.js`).
 *
 * One vocabulary, used by the chip, the card, the count pills, the group
 * headings and the legend, so nothing on screen names the same state twice in
 * two words. "Legal / Illegal / Ambiguous" were the engine's words for the
 * engine's states; these are the driver's words for the driver's question, and
 * the question is "can I park here". `legal` still splits on `basis`, but on
 * the chip's *treatment* rather than on a fifth word: see `verdictChip`.
 */
const VERDICT_INFO = {
  legal: { label: "Can park" },
  illegal: { label: "Can't park" },
  ambiguous: { label: "Unclear" },
  no_data: { label: "No data" },
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

const UNNAMED_STRETCH = "Unnamed stretch";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** The verdict's written word. An unknown verdict reads as no_data (docs/API.md). */
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
 * case as "Legal · 100% confidence". It keeps the outlined chip and never gets
 * a confidence figure, but it no longer gets a *fifth verdict word*: "No rule
 * in effect" on the chip, "No posted rule covers this window" as the headline
 * and "none is in effect during your window" as the sentence under it were
 * three ways of saying one thing, stacked. The chip now answers the driver's
 * question and the panel's "why" line — once — says the rule is not in force
 * and that no sign is giving permission.
 */
export function verdictChip(result) {
  const key = verdictKey(result && result.verdict);
  const info = VERDICT_INFO[key];
  if (key === "legal" && result && result.basis === "absence") {
    return { key, label: info.label, className: "verdict-absence" };
  }
  return { key, label: info.label, className: `verdict-${key}` };
}

/**
 * The engine's one-line reason, printed as it arrives.
 *
 * The engine already words `no_data` reasons by `gap_kind`, so the UI never
 * rewrites a sentence the API is responsible for.
 */
export function reasonLine(result) {
  return (result && result.reason) || "";
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
function streetLabelText(streetName) {
  if (typeof streetName !== "string" || streetName === "") {
    return UNNAMED_STRETCH;
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
 * Abbreviations that are not words and must survive title-casing.
 *
 * DOT writes every name in capitals, so the only way to tell "FDR" from "Fdr"
 * is a list. Single letters are here because `E 85 ST` and `7 AVE S` are
 * compass directions, not initials.
 */
const STREET_ACRONYMS = new Set([
  "E",
  "W",
  "N",
  "S",
  "NB",
  "SB",
  "EB",
  "WB",
  "NE",
  "NW",
  "SE",
  "SW",
  "FDR",
  "RFK",
  "JFK",
  "MLK",
  "NYU",
  "II",
  "III",
]);

/** Lowercased inside a name: "Ave of the Americas", "Ped and Bike Path". */
const STREET_SMALL_WORDS = new Set([
  "of",
  "the",
  "and",
  "at",
  "on",
  "to",
  "for",
  "by",
  "in",
  "over",
]);

const SIDE_CLAUSE = /^(north|south|east|west|northeast|northwest|southeast|southwest) side$/i;

/**
 * One DOT street name in sentence case.
 *
 * `capitalizeFirst` is false for the cross-street line, where the leading word
 * is often "at" and "At Park Ave" reads like a typo.
 */
export function titleCaseStreet(text, { capitalizeFirst = true } = {}) {
  if (typeof text !== "string") {
    return "";
  }
  const words = text.split(/\s+/).filter((word) => word !== "");
  return words
    .map((word, index) => {
      const upper = word.toUpperCase();
      if (STREET_ACRONYMS.has(upper)) {
        return upper;
      }
      // A number keeps whatever the data had: "3", "145", "I-95".
      if (/^[\d\W]+$/.test(word)) {
        return word;
      }
      const lower = word.toLowerCase();
      if (STREET_SMALL_WORDS.has(lower) && !(index === 0 && capitalizeFirst)) {
        return lower;
      }
      // Hyphenated and slashed names capitalise on both sides of the mark.
      return lower.replace(
        /(^|[-/'])([a-z])/g,
        (_match, prefix, letter) => prefix + letter.toUpperCase(),
      );
    })
    .join(" ");
}

/**
 * A result's street label split into the two lines a card prints.
 *
 * `street_name` arrives as `E 85 ST, north side, LEXINGTON AVE → 3 AVE`: the
 * stretch, which side of it, and the two corners it runs between. Shouted in
 * capitals on one line it is the loudest thing on a card and the hardest to
 * scan. The stretch and its side are what identify the curb, so they lead; the
 * corners are how you find it once you are on the block, so they follow in
 * muted type.
 *
 * This is display only. The raw value still reaches the "as posted" blocks and
 * the segment facts verbatim (SPEC §10).
 */
export function streetLabelParts(streetName) {
  const cleaned = streetLabelText(streetName);
  if (cleaned === UNNAMED_STRETCH) {
    return { primary: cleaned, secondary: "" };
  }
  const parts = cleaned.split(", ");
  const primary = [];
  const secondary = [];
  for (const [index, part] of parts.entries()) {
    if (index === 0) {
      primary.push(titleCaseStreet(part));
    } else if (SIDE_CLAUSE.test(part)) {
      primary.push(part.toLowerCase());
    } else {
      secondary.push(titleCaseStreet(part, { capitalizeFirst: false }));
    }
  }
  return { primary: primary.join(" · "), secondary: secondary.join(", ") };
}

/**
 * A geocoder label as the UI prints it.
 *
 * CSCL, AddressPoint and CommonPlace all store names in capitals, so
 * `/api/geocode` and `/api/reverse` answer `1519 3 AVE`, `BLEECKER ST`,
 * `1 WORLD TRADE CENTER`. Shouted back in the destination box and in the
 * collapsed search summary — the one line that names the curb every verdict on
 * the page is about — it reads as a database row rather than as a place, and it
 * disagrees with the cards, which have been title-cased since
 * `streetLabelParts` landed.
 *
 * This is display only, and the same rule the cards use: the raw label stays on
 * the candidate and is kept as the field's tooltip, and no sign text, no
 * `street_name` in a facts block and nothing in a request is touched (SPEC §10).
 */
export function placeLabel(label) {
  const text = typeof label === "string" ? label.trim() : "";
  if (text === "") {
    return "";
  }
  // "near" is the API's own word for "this is the closest door, not your pin"
  // and is already lower case; title-casing it would read as a street name.
  const near = /^near\s+(.+)$/i.exec(text);
  return near ? `near ${titleCaseStreet(near[1])}` : titleCaseStreet(text);
}

/**
 * "near <place>" for a dropped pin, said once.
 *
 * `/api/reverse` already names the closest door as `"near 1519 3 AVE"` because
 * it labels the nearest thing, not the building the pin is on (docs/API.md).
 * Prefixing that again produced "near near 1519 3 AVE" in the destination box
 * and in the collapsed search summary. A `street` or `intersection` match
 * arrives without the word, so it still has to be added.
 */
export function nearLabel(label) {
  const text = typeof label === "string" ? label.trim() : "";
  if (text === "") {
    return "";
  }
  return /^near\s/i.test(text) ? text : `near ${text}`;
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

/**
 * "8:30 AM" from "08:30", or "8 AM" when `dropZeroMinutes` is set.
 *
 * Sign hours are almost all on the hour, and "8:00 AM–6:00 PM" reads as a
 * timetable where "8 AM–6 PM" reads as the sign. The user's own window keeps
 * its minutes, because those are a number they typed.
 */
export function formatClock(value, { dropZeroMinutes = false } = {}) {
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
  if (dropZeroMinutes && minutes === "00") {
    return `${hour12} ${suffix}`;
  }
  return `${hour12}:${minutes} ${suffix}`;
}

/** "8:30 AM–7 PM", "all day", or a range flagged as wrapping past midnight. */
export function formatTimeRange(timeFrom, timeTo) {
  if (!timeFrom || !timeTo) {
    return "all day";
  }
  const options = { dropZeroMinutes: true };
  const range = `${formatClock(timeFrom, options)}–${formatClock(timeTo, options)}`;
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

/**
 * The flags as they read *inside a sentence*, rather than in the field table.
 *
 * Two differences, both because `describeRegulation` is read mid-sentence
 * ("… applies for all of it") while the `Flags` row is read as a record of what
 * the parser found:
 *
 *  - `except_sunday` and `including_sunday` are dropped: the parser already
 *    folded them into the day set, so "Mon–Sat, except Sunday" says it twice.
 *  - `street_cleaning` loses its parenthetical, because a nested parenthesis
 *    inside the one the sentence already puts the flags in is unreadable. The
 *    suspension fact keeps its two better homes: the engine's own
 *    "Street cleaning is suspended on this date." caveat when it applies, and
 *    the full label in the field table.
 */
const SENTENCE_FLAG_LABEL = {
  street_cleaning: "street cleaning",
  except_sunday: null,
  including_sunday: null,
};

function sentenceFlags(flags) {
  if (!flags || typeof flags !== "object") {
    return [];
  }
  return Object.entries(flags)
    .filter(([, value]) => value === true)
    .map(([name]) => (name in SENTENCE_FLAG_LABEL ? SENTENCE_FLAG_LABEL[name] : FLAG_LABEL[name]))
    .filter((label) => label !== null && label !== undefined);
}

export function vehicleLabel(vehicleClass, exclusive) {
  const label = VEHICLE_LABEL[vehicleClass] ?? vehicleClass;
  if (!label) {
    return "";
  }
  return exclusive ? `${label} only` : label;
}

/** "2-hour", "90-minute" — a duration as an adjective, or "" when there is none. */
export function durationAdjective(minutes) {
  if (minutes === null || minutes === undefined) {
    return "";
  }
  if (minutes % 60 === 0) {
    return `${minutes / 60}-hour`;
  }
  return `${minutes}-minute`;
}

/** What the rule allows or forbids, with no times in it: "No parking", "2-hour metered parking". */
function ruleSubject(regulation) {
  const vehicle = vehicleLabel(regulation.vehicle_class, regulation.exclusive);
  if (!regulation.permitted) {
    const phrase = ACTION_PROHIBITION[regulation.action] || "Not permitted";
    return vehicle ? `${phrase} for ${vehicle}` : phrase;
  }
  const words = [durationAdjective(regulation.max_duration_min)];
  if (regulation.metered) {
    words.push("metered");
  }
  words.push((ACTION_NOUN[regulation.action] || "Parking").toLowerCase());
  const subject = words.filter((word) => word !== "").join(" ");
  const sentence = vehicle ? `${subject} for ${vehicle}` : subject;
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

/** ", Mon–Fri 8 AM–6 PM" or " at any time" — the clause that says when. */
function ruleWhen(regulation) {
  const days = formatDays(regulation.days);
  if (!regulation.time_from || !regulation.time_to) {
    return days === "every day" ? " at any time" : `, ${days}, any time`;
  }
  return `, ${days} ${formatTimeRange(regulation.time_from, regulation.time_to)}`;
}

/**
 * One parsed rule as the sentence a driver would say: what, then when.
 *
 * "No parking, Mon–Fri 8 AM–6 PM". "2-hour metered parking, Mon–Sat 8 AM–7 PM".
 * This is the only place a `Regulation` becomes English — the panel's "why"
 * line, the reading under each quoted sign, and the rule stack all render from
 * it, so none of the three can describe a different rule from the others
 * (STYLE_GUIDE §2, "one way to do each thing").
 *
 * The old wording led with the engine's own vocabulary ("Parking permitted for
 * passenger cars 8:00 AM–6:00 PM Mon–Fri · metered, max 2 hours"), which put
 * the decisive fact — the hours — in the middle and the limit in a trailing
 * list. The limit is now an adjective on the thing it limits.
 */
export function describeRegulation(regulation) {
  if (!regulation) {
    return "";
  }
  const sentence = `${ruleSubject(regulation)}${ruleWhen(regulation)}`;
  const extras = sentenceFlags(regulation.flags);
  const season = formatSeason(regulation.effective_from, regulation.effective_to);
  if (season) {
    extras.push(season);
  }
  // Parenthesised, not appended after a "·": this sentence is read inside a
  // longer one ("… applies for all of it"), and a trailing dot-separated list
  // left the verb stranded behind a flag — "Mon–Sat 8 AM–7 PM · except Sunday
  // applies for all of it".
  return extras.length > 0 ? `${sentence} (${extras.join(", ")})` : sentence;
}

/**
 * What `/api/segment`'s `in_effect` means for the window the user asked about.
 *
 * `null` is the unreadable sign (D13): its parsed fields are a placeholder, so
 * answering "not in effect" about it would be a claim about text nobody read.
 */
const IN_EFFECT_LABEL = {
  all: "Applies to your window",
  part: "Applies to part of your window",
  none: "Not in effect for your window",
};

export function inEffectLabel(inEffect) {
  return IN_EFFECT_LABEL[inEffect] || null;
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
 *
 * The per-verdict breakdown used to live here too and made this a 44-word
 * paragraph at the top of the rail. It is now the four count pills, which read
 * the same `counts` object.
 */
export function statusLine(counts, shown, walkMinutes) {
  const total = counts && typeof counts.total === "number" ? counts.total : shown;
  if (total === 0) {
    return `Nothing within a ${walkMinutes}-minute walk.`;
  }
  const parts = [`${total.toLocaleString("en-US")} stretches within a ${walkMinutes} min walk`];
  if (shown < total) {
    parts.push(`${shown.toLocaleString("en-US")} drawn, nearest first`);
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
 * The window as a sentence: "Sat Sep 19, 2:00–4:00 PM".
 *
 * The verdict is only ever a claim about these two times, and until now the
 * panel never said them: it printed "your window" four times without once
 * printing the window. AM/PM is stated once when both ends share it.
 */
export function windowSentence(start, end) {
  if (!start || !end) {
    return "";
  }
  const from = toClockText(start);
  const to = toClockText(end);
  const sameDay = toDateInputValue(start) === toDateInputValue(end);
  if (sameDay && from.suffix === to.suffix) {
    return `${dayLabel(start)}, ${from.time}–${to.time} ${to.suffix}`;
  }
  const tail = sameDay ? `${to.time} ${to.suffix}` : `${dayLabel(end)}, ${to.time} ${to.suffix}`;
  return `${dayLabel(start)}, ${from.time} ${from.suffix}–${tail}`;
}

/** How long the window is, as the panel says it: "2 hours", "90 min". */
export function windowMinutes(start, end) {
  if (!start || !end) {
    return null;
  }
  return Math.max(0, Math.round((end.getTime() - start.getTime()) / 60000));
}

function toClockText(date) {
  const hours = date.getHours();
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return { time: `${hour12}:${pad(date.getMinutes())}`, suffix: hours < 12 ? "AM" : "PM" };
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
