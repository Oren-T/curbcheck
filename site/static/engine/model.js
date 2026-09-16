/**
 * Shared data contract for parsed parking regulations, and the read half of `db.py`.
 *
 * Port of `curbcheck/model.py` plus `db.regulation_from_row`, `db.days_from_mask`
 * and `db.json_string_list`. Every verdict, every API response and the pack's
 * `regulations` table all speak this schema; change it here and nowhere else.
 * See docs/SPEC.md §6 and §8.
 */

/** Monday is 0, Sunday is 6, matching `datetime.weekday()`. */
export const ALL_DAYS = Object.freeze([0, 1, 2, 3, 4, 5, 6]);

/** The three curb actions NYC signs regulate, least to most restrictive when prohibited. */
export const Action = Object.freeze({
  PARK: "park",
  STAND: "stand",
  STOP: "stop",
});

/** The vehicle class a sign names. ALL means the sign does not single one out. */
export const VehicleClass = Object.freeze({
  ALL: "all",
  PASSENGER: "passenger",
  COMMERCIAL: "commercial",
  TRUCK: "truck",
  BUS: "bus",
  TAXI: "taxi",
  AUTHORIZED: "authorized", // AVO, police, diplomat, government plates, etc.
  HANDICAP: "handicap",
  OTHER: "other",
});

/**
 * Which way the rule extends from the sign post along the curb.
 *
 * FORWARD means in the direction of increasing distance_from_intersection.
 * NONE means the sign has no arrow; by 34 RCNY 4-08 it then governs the whole
 * blockface.
 */
export const Arrow = Object.freeze({
  FORWARD: "forward",
  BACKWARD: "backward",
  BOTH: "both",
  NONE: "none",
});

export const ParseMethod = Object.freeze({
  GRAMMAR: "grammar",
  LLM: "llm", // reserved; not used in v1, see docs/DECISIONS.md D2
  UNPARSED: "unparsed",
});

/**
 * Conditions that modify when or whether a rule is in force, as
 * `[<API name>, <field name>]` in `model.Flags` declaration order: bit i of the
 * pack's `flags` column is entry i here.
 */
const FLAG_FIELDS = Object.freeze([
  ["street_cleaning", "streetCleaning"], // broom symbol; suspended on ASP days
  ["school_days", "schoolDays"], // only in force on DOE school days
  ["except_sunday", "exceptSunday"], // sign says EXCEPT SUNDAY
  ["including_sunday", "includingSunday"], // overrides the meter Sunday exemption
  ["snow_emergency", "snowEmergency"], // only in force during a declared snow emergency
  ["holiday_exempt", "holidayExempt"], // sign says EXCEPT HOLIDAYS or similar
  ["temporary", "temporary"], // sign text marks itself temporary
  ["meta", "meta"], // sign modifies a sibling rule rather than standing alone (§8.4 ex. 7)
]);

const ACTIONS = new Set(Object.values(Action));
const VEHICLE_CLASSES = new Set(Object.values(VehicleClass));
const ARROWS = new Set(Object.values(Arrow));
const PARSE_METHODS = new Set(Object.values(ParseMethod));

// Monday is bit 0 through Sunday is bit 6, matching model.Weekday.
const DAY_BITS = ALL_DAYS.map((day) => 1 << day);

export function daysFromMask(mask) {
  return ALL_DAYS.filter((day) => mask & DAY_BITS[day]);
}

/**
 * Read a JSON list column as strings, treating anything unreadable as empty.
 *
 * `derived_from`, `street_names` and `hour_rates` are all JSON lists, and
 * everything in the pack came from `data/`, which is untrusted at read time
 * (CLAUDE.md). A cell that is not a JSON list means "no ids", "no names" or
 * "no known rate", which every caller already handles; throwing would turn a
 * half-built snapshot into a failed search.
 */
export function jsonStringList(value) {
  if (value === null || value === undefined) {
    return [];
  }
  let parsed;
  try {
    parsed = JSON.parse(String(value));
  } catch {
    return [];
  }
  return Array.isArray(parsed) ? parsed.map((item) => String(item)) : [];
}

/**
 * Rebuild a regulation from row `row` of `pack.regulations`.
 *
 * Rows are untrusted at read time (CLAUDE.md), so every field is checked here
 * the way `db.regulation_from_row` puts them back through Pydantic. Times are
 * "HH:MM" 24-hour strings and null for both means all day; a `timeTo` earlier
 * than `timeFrom` wraps past midnight.
 */
export function regulationFromPack(pack, row) {
  const table = pack.regulations;
  const regulation = {
    action: oneOf(ACTIONS, table.action[row], "action"),
    permitted: Boolean(table.permitted[row]),
    vehicleClass: oneOf(VEHICLE_CLASSES, table.vehicleClass[row], "vehicle_class"),
    exclusive: Boolean(table.exclusive[row]),
    days: daysFromMask(Number(table.daysMask[row])),
    timeFrom: optionalText(table.timeFrom[row]),
    timeTo: optionalText(table.timeTo[row]),
    metered: Boolean(table.metered[row]),
    maxDurationMin: optionalInt(table.maxDurationMin[row]),
    flags: flagsFromMask(Number(table.flags[row])),
    effectiveFrom: optionalText(table.effectiveFrom[row]),
    effectiveTo: optionalText(table.effectiveTo[row]),
    arrow: oneOf(ARROWS, table.arrow[row], "arrow"),
  };
  checkTimes(regulation);
  return regulation;
}

/** Exactly what pydantic's `Regulation.model_dump(mode="json")` produces for the API. */
export function regulationToJson(reg) {
  const flags = {};
  for (const [name, field] of FLAG_FIELDS) {
    flags[name] = reg.flags[field];
  }
  return {
    action: reg.action,
    permitted: reg.permitted,
    vehicle_class: reg.vehicleClass,
    exclusive: reg.exclusive,
    days: [...reg.days],
    time_from: reg.timeFrom,
    time_to: reg.timeTo,
    metered: reg.metered,
    max_duration_min: reg.maxDurationMin,
    flags,
    effective_from: reg.effectiveFrom,
    effective_to: reg.effectiveTo,
    arrow: reg.arrow,
  };
}

/**
 * Passenger-car reading of this rule: true permitted, false prohibited, null
 * not applicable.
 */
export function appliesToPassenger(reg) {
  if (reg.vehicleClass === VehicleClass.ALL || reg.vehicleClass === VehicleClass.PASSENGER) {
    return reg.permitted;
  }
  if (reg.exclusive) {
    return false;
  }
  return null;
}

/**
 * One parsed rule plus the provenance the verdict has to account for.
 *
 * An `unparsed` entry carries no usable rule: its `regulation` is a placeholder
 * the ETL writes so the sign is visible in the stack, and the engine only ever
 * reads its metadata.
 */
export function regulationWithMeta(pack, row) {
  const table = pack.regulations;
  return {
    regulation: regulationFromPack(pack, row),
    parseMethod: oneOf(PARSE_METHODS, table.parseMethod[row], "parse_method"),
    parseConfidence: Number(table.parseConfidence[row]),
    // `regulation` carries the span row; `reg_seg_id` lives on `spans`, where
    // the SQL read it straight off the joined `regulation` row.
    regSegId: String(pack.spans.regSegId[table.span[row]]),
    rawSignDescription: String(table.rawSignDescription[row]),
  };
}

function flagsFromMask(mask) {
  const flags = {};
  FLAG_FIELDS.forEach(([, field], bit) => {
    flags[field] = Boolean(mask & (1 << bit));
  });
  return flags;
}

function checkTimes(reg) {
  if ((reg.timeFrom === null) !== (reg.timeTo === null)) {
    throw new Error("time_from and time_to must both be set or both be None");
  }
  for (const value of [reg.timeFrom, reg.timeTo]) {
    if (value !== null && !isHhmm(value)) {
      throw new Error(`time must be HH:MM, got ${JSON.stringify(value)}`);
    }
  }
  for (const value of [reg.effectiveFrom, reg.effectiveTo]) {
    if (value !== null && !isMmdd(value)) {
      throw new Error(`seasonal date must be MM-DD, got ${JSON.stringify(value)}`);
    }
  }
}

function isHhmm(value) {
  if (value.length !== 5 || value[2] !== ":") {
    return false;
  }
  const hours = value.slice(0, 2);
  const minutes = value.slice(3);
  return isDigits(hours) && isDigits(minutes) && Number(hours) < 24 && Number(minutes) < 60;
}

function isMmdd(value) {
  if (value.length !== 5 || value[2] !== "-") {
    return false;
  }
  const month = value.slice(0, 2);
  const day = value.slice(3);
  if (!isDigits(month) || !isDigits(day)) {
    return false;
  }
  return Number(month) >= 1 && Number(month) <= 12 && Number(day) >= 1 && Number(day) <= 31;
}

function isDigits(value) {
  return /^\d+$/.test(value);
}

function oneOf(allowed, value, field) {
  const text = String(value);
  if (!allowed.has(text)) {
    throw new Error(`unknown ${field}: ${JSON.stringify(text)}`);
  }
  return text;
}

function optionalText(value) {
  return value === null || value === undefined ? null : String(value);
}

function optionalInt(value) {
  return value === null || value === undefined ? null : Math.trunc(Number(value));
}
