/**
 * Pure formatting: API values in, display strings out. No DOM, no state.
 *
 * The rule vocabulary here mirrors `curbcheck.model.Regulation` (docs/API.md):
 * weekdays are Monday = 0 through Sunday = 6, times are "HH:MM" 24-hour local
 * strings, and money is a decimal string that must never become a float.
 */

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

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
 * Money for display. `null` means unknown, which is not the same as free:
 * showing "$0.00" for an unpriced meter would be a lie about cost.
 */
export function formatMoney(money, priceKnown = true) {
  if (money === null || money === undefined) {
    return "price unknown";
  }
  const text = `$${money}`;
  return priceKnown ? text : `${text} (unconfirmed)`;
}

/**
 * What a result costs, read from the whole result rather than the money field.
 * An unmetered span comes back as "0.00", which is true but reads like a price;
 * say "no meter" instead so "price unknown" keeps its meaning.
 */
export function moneyLabel(result) {
  if (!result.metered && result.money === "0.00") {
    return "no meter";
  }
  return formatMoney(result.money, result.price_known);
}

export function formatWalkMinutes(minutes) {
  if (typeof minutes !== "number" || Number.isNaN(minutes)) {
    return "walk unknown";
  }
  const rounded = minutes < 1 ? Math.round(minutes * 10) / 10 : Math.round(minutes);
  return `${rounded} min walk`;
}

export function formatCapacity(cars) {
  if (cars === null || cars === undefined) {
    return "capacity unknown";
  }
  return cars === 1 ? "about 1 car" : `about ${cars} cars`;
}

export function formatConfidence(confidence) {
  if (typeof confidence !== "number" || Number.isNaN(confidence)) {
    return "confidence unknown";
  }
  return `${Math.round(confidence * 100)}% confidence`;
}

/** "Mon–Fri", "Mon–Wed, Sat", "Every day". */
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

/** Hourly meter rates as "$4.50 first hour, $5.50 after". */
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
 * The status line under the form, built from the server's `counts`.
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
    return "Nothing within that walk radius. Try a longer walk or a different time.";
  }
  const parts = [`${total} stretches within a ${walkMinutes} min walk`];
  if (counts) {
    parts.push(
      `${counts.legal ?? 0} legal for the whole window, ${counts.illegal ?? 0} illegal, ` +
        `${counts.ambiguous ?? 0} ambiguous, ${counts.no_data ?? 0} with no data`,
    );
  }
  if (shown < total) {
    parts.push(`showing the nearest ${shown}`);
  }
  return `${parts.join(" · ")}.`;
}

/** "2026-09-15T09:00" for a datetime-local input, in the browser's local time. */
export function toLocalInputValue(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** The next top of the hour, local time — the default arrival. */
export function nextTopOfHour(now = new Date()) {
  const next = new Date(now.getTime());
  next.setMinutes(0, 0, 0);
  next.setHours(next.getHours() + 1);
  return next;
}
