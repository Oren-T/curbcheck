"""The sign grammar: token stream in, `Regulation` list out.

The parser is a flat left-to-right scan that classifies each token as a rule
head, a day expression, a clock range, a season, a known rider, or an unknown
word, and then assembles the classified items into clauses. A sign carries more
than one clause when it stacks rules (`2 HMP 7AM-6PM EXCEPT SUNDAY 6 HMP
6PM-MIDNIGHT EXCEPT SUNDAY`) or when a second head qualifies the first
(`3 HMP COMMERCIAL VEHICLES ONLY`).

Two deliberate asymmetries, both in the direction of never inventing a
permission:

- Unknown words inside a clause whose head is a vehicle-class exclusive are
  absorbed silently. Those heads name the authorized party (`AVO DEPT OF
  EDUCATION`, `AUTHORIZED VEHICLES ONLY WORKERS' COMPENSATION BOARD`) and there
  are hundreds of such names; none of them can make the sign permissive for a
  passenger car, so reading them is unnecessary.
- Unknown words inside any other clause are leftovers and lower the confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from curbcheck.etl.parse import times
from curbcheck.etl.parse.days import DaySpec, day_number, scan_days
from curbcheck.etl.parse.tokens import Lexed, tokenize
from curbcheck.etl.parse.vocabulary import (
    CLAUSE_BREAKS,
    DURATION_RIDER_UNITS,
    DURATION_UNITS,
    EXCEPT_RIDER,
    EXCEPT_TYPOS,
    GLUED_VERBS,
    HEAD_PHRASES,
    LONGEST_HEAD,
    LONGEST_RIDER,
    RIDER_PHRASES,
    Head,
    Rider,
    is_noise,
)
from curbcheck.model import ALL_DAYS, Action, Flags, Regulation, VehicleClass, Weekday


@dataclass(frozen=True)
class ParseResult:
    """The rules a description states, plus what the scan did with the rest of it.

    `leftovers` lower the confidence, `absorbed` do not (they are the name of an
    authorized party), and `degraded` names a thing the sign says that one
    `Regulation` cannot hold.
    """

    regulations: list[Regulation]
    leftovers: list[str]
    absorbed: list[str]
    degraded: list[str]


@dataclass
class _Group:
    """One day set with the clock ranges that go with it."""

    spec: DaySpec | None = None
    ranges: list[tuple[str, str]] = field(default_factory=list)
    all_times: bool = False


@dataclass
class _Clause:
    """One rule: a head, the schedule that qualifies it, and what went unread."""

    head: Head
    other_times: bool = False
    season: tuple[str, str] | None = None
    groups: list[_Group] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    temporary: bool = False

    def current_group(self) -> _Group:
        if not self.groups:
            self.groups.append(_Group())
        return self.groups[-1]


def parse_rules(lexed: Lexed) -> ParseResult:
    """Read every rule the description states. Leftover tokens are reported, never guessed."""
    tokens = tokenize(times.mark_atoms(lexed.text))
    tokens = _rejoin_split_days(_split_glued_verbs(tokens))
    clauses, leftovers = _build_clauses(tokens)

    regulations: list[Regulation] = []
    absorbed: list[str] = []
    degraded: list[str] = []
    for clause in clauses:
        if clause.head.exclusive:
            absorbed.extend(clause.unknown)
        else:
            leftovers.extend(clause.unknown)
        regulations.extend(_emit(clause, lexed, degraded))
    return ParseResult(
        regulations=regulations, leftovers=leftovers, absorbed=absorbed, degraded=degraded
    )


def _split_glued_verbs(tokens: list[str]) -> list[str]:
    split: list[str] = []
    for token in tokens:
        for verb in GLUED_VERBS:
            if token.startswith(verb) and len(token) > len(verb) and token[len(verb)].isalpha():
                split.extend((verb, token[len(verb) :]))
                break
        else:
            split.append(token)
    return split


def _rejoin_split_days(tokens: list[str]) -> list[str]:
    """`MONDAY TUESDAY THU RSDAY FRIDAY`: DOT sometimes breaks a day name in two."""
    joined: list[str] = []
    index = 0
    while index < len(tokens):
        pair = "".join(tokens[index : index + 2])
        halves_are_days = day_number(tokens[index]) is not None and (
            index + 1 < len(tokens) and day_number(tokens[index + 1]) is not None
        )
        if index + 1 < len(tokens) and day_number(pair) is not None and not halves_are_days:
            joined.append(pair)
            index += 2
            continue
        joined.append(tokens[index])
        index += 1
    return joined


def _build_clauses(tokens: list[str]) -> tuple[list[_Clause], list[str]]:
    clauses: list[_Clause] = []
    leftovers: list[str] = []
    current: _Clause | None = None
    pending: list[str] = []
    next_is_other_times = False
    index = 0

    while index < len(tokens):
        word = tokens[index]

        broke = _match_break(tokens, index)
        if broke:
            current = None
            next_is_other_times = tokens[index] != "OTHERS"
            index += broke
            continue

        head, length = _match_head(tokens, index)
        if head is not None:
            if current is not None and _merge_head(current, head):
                index += length
                continue
            # Words DOT puts in front of the head name the party a reservation is
            # for (`NYP LICENSE PLATES ONLY`, `MTA BUS LAYOVER ONLY`), so they
            # belong to the clause the head opens and are judged with it.
            current = _Clause(head=head, other_times=next_is_other_times, unknown=pending)
            pending = []
            next_is_other_times = False
            clauses.append(current)
            index += length
            continue

        if times.is_season_token(word):
            if current is not None:
                current.season = times.read_season_token(word)
            index += 1
            continue

        if times.is_time_token(word):
            if current is None:
                pending.append(word)
            else:
                current.current_group().ranges.append(times.read_time_token(word))
            index += 1
            continue

        if word == "ANYTIME" or (word == "ALL" and _peek(tokens, index + 1) == "DAY"):
            if current is not None:
                group = current.current_group()
                group.all_times = True
                if word == "ANYTIME":
                    group.spec = group.spec or DaySpec(days=ALL_DAYS, length=0)
            index += 1 if word == "ANYTIME" else 2
            continue

        if word in EXCEPT_TYPOS:
            word = tokens[index] = EXCEPT_RIDER
        if word == EXCEPT_RIDER and _peek(tokens, index + 1) != "SUNDAY":
            index = _skip_except_rider(tokens, index)
            continue

        spec = scan_days(tokens, index)
        if spec is not None:
            if current is not None:
                _add_days(current, spec)
            index += spec.length
            continue

        rider, length = _match_rider(tokens, index)
        if rider is not None:
            if current is not None:
                current.temporary = current.temporary or rider.temporary
            index += length
            continue

        duration = _match_duration_rider(tokens, index)
        if duration is not None:
            minutes, length = duration
            if current is not None:
                current.head = _with_duration(current.head, minutes)
            index += length
            continue

        if word == "-" or is_noise(word):
            index += 1
            continue

        if current is None:
            pending.append(word)
        else:
            current.unknown.append(word)
        index += 1

    leftovers.extend(pending)
    return clauses, leftovers


def _skip_except_rider(tokens: list[str], index: int) -> int:
    """Consume `EXCEPT <class phrase>`; the prohibition it qualifies stands unchanged.

    The class phrase may itself be a reservation head (`NO STANDING 5PM-MIDNIGHT
    MON-FRI EXCEPT TLC LICENSED VEHICLES PRE-ARRANGED SERVICE ONLY`). It is
    swallowed with the rest of the rider: the sign said `EXCEPT`, so the
    reservation is the exception to this prohibition, not a second rule covering
    the hours this one leaves open.
    """
    index += 1
    while index < len(tokens):
        if times.is_time_token(tokens[index]) or times.is_season_token(tokens[index]):
            break
        if scan_days(tokens, index) is not None:
            break
        head, length = _match_head(tokens, index)
        if head is not None and not head.exclusive:
            break
        index += length if head is not None else 1
    return index


def _add_days(clause: _Clause, spec: DaySpec) -> None:
    group = clause.current_group()
    if group.spec is None:
        group.spec = spec
        return
    clause.groups.append(_Group(spec=spec))


def _merge_head(clause: _Clause, head: Head) -> bool:
    """Fold a second head into the current clause when the two describe one rule.

    `3 HMP COMMERCIAL VEHICLES ONLY` and `BUS STOP SIGN NO STANDING` are each one
    rule written as two phrases. DOT puts the class either side of the schedule
    (`3 HOUR PARKING 9AM-6PM MON-FRI COMMERCIAL VEHICLES ONLY`), so naming a class
    folds into a *permission* wherever it appears; a second *rule* head, and a
    class named after a prohibition's hours, only fold in before the clause has a
    schedule of its own.
    """
    if head.meta or clause.head.meta:
        return False
    if head.exclusive and clause.head.exclusive and not clause.groups:
        # Two names for one reservation: `FOR-HIRE VEHICLES ONLY PICK-UP/DROP-OFF
        # ONLY`, `MICROHUB ZONE VEHICLES WITH PERMIT ONLY`. The first names the
        # party the curb is held for, so it is the one that is kept.
        return True
    if (
        head.exclusive
        and not clause.head.exclusive
        and clause.head.vehicle_class is VehicleClass.ALL
        # `NO STANDING 8AM-6PM EXCEPT SUNDAY W/ ACCESS-A-RIDE BUS STOP`: folding
        # the class into a prohibition that already carries hours would confine
        # the reservation to those hours and leave the rest of the week unposted,
        # which is the one direction a parse must never move in.
        and not (clause.groups and not clause.head.permitted)
    ):
        clause.head = Head(
            action=head.action if clause.head.permitted else clause.head.action,
            permitted=True,
            vehicle_class=head.vehicle_class,
            exclusive=True,
            metered=clause.head.metered,
            max_duration_min=clause.head.max_duration_min,
        )
        return True
    if not head.permitted and clause.head.exclusive and not clause.groups:
        clause.head = Head(
            action=head.action,
            permitted=True,
            vehicle_class=clause.head.vehicle_class,
            exclusive=True,
            metered=clause.head.metered,
            max_duration_min=clause.head.max_duration_min,
        )
        return True
    return False


def _with_duration(head: Head, minutes: int) -> Head:
    return Head(
        action=head.action,
        permitted=head.permitted,
        vehicle_class=head.vehicle_class,
        exclusive=head.exclusive,
        metered=head.metered,
        max_duration_min=minutes,
    )


def _peek(tokens: list[str], index: int) -> str | None:
    return tokens[index] if 0 <= index < len(tokens) else None


def _match_break(tokens: list[str], index: int) -> int:
    for phrase in CLAUSE_BREAKS:
        if tuple(tokens[index : index + len(phrase)]) == phrase:
            return len(phrase)
    return 0


def _match_head(tokens: list[str], index: int) -> tuple[Head | None, int]:
    duration = _match_duration_head(tokens, index)
    if duration is not None:
        return duration
    for length in range(min(LONGEST_HEAD, len(tokens) - index), 0, -1):
        head = HEAD_PHRASES.get(tuple(tokens[index : index + length]))
        if head is not None:
            return head, length
    return None, 0


def _match_duration_head(tokens: list[str], index: int) -> tuple[Head, int] | None:
    """`2 HMP`, `1 HOUR METERED PARKING`, `30 MMP`, `15 MINUTE PARKING`."""
    if not tokens[index].isdigit():
        return None
    count = int(tokens[index])
    for unit, (metered, minutes_each) in DURATION_UNITS.items():
        if tuple(tokens[index + 1 : index + 1 + len(unit)]) == unit:
            head = Head(
                action=Action.PARK,
                permitted=True,
                metered=metered,
                max_duration_min=count * minutes_each,
            )
            return head, 1 + len(unit)
    return None


def _match_duration_rider(tokens: list[str], index: int) -> tuple[int, int] | None:
    """`1 HOUR LIMIT` on a taxi relief stand: a duration without a head of its own."""
    if not tokens[index].isdigit():
        return None
    count = int(tokens[index])
    for unit, minutes_each in DURATION_RIDER_UNITS.items():
        if tuple(tokens[index + 1 : index + 1 + len(unit)]) == unit:
            return count * minutes_each, 1 + len(unit)
    return None


def _match_rider(tokens: list[str], index: int) -> tuple[Rider | None, int]:
    for length in range(min(LONGEST_RIDER, len(tokens) - index), 0, -1):
        rider = RIDER_PHRASES.get(tuple(tokens[index : index + length]))
        if rider is not None:
            return rider, length
    return None, 0


def _emit(clause: _Clause, lexed: Lexed, degraded: list[str]) -> list[Regulation]:
    groups = [group for group in clause.groups if _group_is_meaningful(group)] or [_Group()]
    if len(groups) > 1 and any(group.spec is not None and not group.ranges for group in groups):
        degraded.append("day list without its own hours")
    if clause.other_times:
        degraded.append(
            "'other times' covers the complement of another rule, which one "
            "Regulation cannot express"
        )

    regulations: list[Regulation] = []
    for group in groups:
        spans: list[tuple[str, str] | None] = list(group.ranges) or [None]
        for span in spans:
            regulations.append(_regulation(clause, group, span, lexed))
    return regulations


def _group_is_meaningful(group: _Group) -> bool:
    return group.spec is not None or bool(group.ranges) or group.all_times


def _regulation(
    clause: _Clause, group: _Group, span: tuple[str, str] | None, lexed: Lexed
) -> Regulation:
    spec = group.spec
    days: list[Weekday] = list(spec.days) if spec is not None else list(ALL_DAYS)
    head = clause.head
    return Regulation(
        action=head.action,
        permitted=head.permitted,
        vehicle_class=head.vehicle_class,
        exclusive=head.exclusive,
        days=days,
        time_from=span[0] if span else None,
        time_to=span[1] if span else None,
        metered=head.metered,
        max_duration_min=head.max_duration_min,
        flags=Flags(
            street_cleaning=lexed.street_cleaning,
            snow_emergency=lexed.snow_emergency,
            school_days=spec.school_days if spec else False,
            except_sunday=spec.except_sunday if spec else False,
            including_sunday=spec.including_sunday if spec else False,
            temporary=clause.temporary,
            meta=head.meta,
        ),
        effective_from=clause.season[0] if clause.season else None,
        effective_to=clause.season[1] if clause.season else None,
        arrow=lexed.arrow,
    )
