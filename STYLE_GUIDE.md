# CurbCheck Coding Style Guide

This guide applies to every file in the repository, whether written by a person
or by an LLM. It is short on purpose. When in doubt, optimize for a reviewer who
is reading the code for the first time, whether that reviewer is a human or a
model.

## 1. Comments: explain why, not what

Write comments the way a careful colleague would. The code already says *what*
it does; a comment earns its place only by saying something the code cannot.

**Do comment:**

- The reason a non-obvious choice was made ("Socrata omits null fields from JSON
  rows, so a missing key means null, not an error").
- A constraint that comes from outside the code: a regulation, a dataset quirk,
  a security requirement, a measured number.
- A known limitation or a deliberate shortcut, with what would fix it.
- A link to the spec section, ticket, or source that justifies a rule.

**Do not comment:**

- What a line does when the line already says it (`# increment counter`).
- Restatements of the function name or signature.
- Narration of the control flow ("first we loop, then we check").
- Change history, author names, or dates. Git records those.

**Form:**

- Inline comments are one or two lines. If a comment is growing past that, it
  probably belongs in a docstring, a module header, or a design doc.
- Docstrings state the contract: inputs, outputs, and the edge cases that
  matter. One paragraph is usually enough. Do not repeat type hints in prose.
- A short module header is welcome when the module's role in the pipeline is
  not obvious from its name.
- Prefer a well-named function to a comment. If you want to write "this block
  strips the arrow glyph from the description," extract `strip_arrow()`.
- No commented-out code. Delete it; git has it.
- Never leave a placeholder comment (`# TODO: handle this`) without saying what
  "this" is and why it is deferred.

Good:

```python
# DOT measures distance_from_intersection from the from_street end, but the
# centerline's digitization direction is arbitrary, so reverse when needed.
if segment.starts_at(to_node):
    offset_ft = segment.length_ft - offset_ft
```

Bad:

```python
# Check if segment starts at to_node and if so subtract offset from length
if segment.starts_at(to_node):
    offset_ft = segment.length_ft - offset_ft
```

## 2. Readability for humans and models

- **Small units.** A function does one thing and fits on a screen. Files stay
  under a few hundred lines; split by responsibility, not by size limit.
- **Names carry meaning.** `snap_confidence`, not `sc`. `distance_ft`, not
  `distance`. Include units in names for physical quantities.
- **Explicit over clever.** No metaprogramming, no operator overloading, no
  implicit coercion. Flat is better than nested; return early.
- **Straight-line data flow.** Prefer pure functions that take data in and
  return data out. Keep I/O (network, disk, database) at the edges of the
  program so the logic in the middle is testable without fixtures.
- **Typed boundaries.** Every public Python function has type hints. Every
  external input (HTTP body, downloaded row, parser output) passes through a
  Pydantic model before the rest of the code touches it.
- **One way to do each thing.** Reuse the existing helper. If a second version
  of something is needed, change the first rather than adding a near-duplicate.
- **Constants have a home.** Magic numbers (22 ft per car, 15 ft hydrant
  setback, 1.34 m/s walking speed) live in a named constant with a comment
  giving the source.

## 3. Python

- Python 3.12. Format with `ruff format`, lint with `ruff check`, type-check
  with `mypy --strict` on the `curbcheck` package. CI fails on any of these.
- Standard library first. Add a dependency only when it is pinned, hashed, and
  listed in `docs/DEPENDENCIES.md` with a reason.
- `pathlib.Path` for paths, `datetime` with explicit `ZoneInfo("America/New_York")`
  for anything that touches a calendar, `decimal.Decimal` for money.
- Raise specific exceptions. Never `except Exception: pass`. If a failure is
  expected and recoverable, return a result type or `None` and document it.
- Logging via the `logging` module with structured, greppable messages. No
  `print()` outside CLI entry points.
- Tests with `pytest`. Test names describe the behavior:
  `test_midnight_wrapping_range_splits_into_two_intervals`. Every parser
  template and every regulatory edge case in the spec gets a test.

## 4. JavaScript (frontend)

- Plain ES modules, no framework, no bundler, no transpilation. The browser
  loads exactly the files that are in the repository.
- Format with Prettier, lint with ESLint. Same CI gate as Python.
- Never assign untrusted text to `innerHTML`. Use `textContent` or build DOM
  nodes. Sign text is untrusted.
- Keep map rendering, API calls, and UI state in separate modules.

## 5. Security rules that are also style rules

These come from the threat model in `docs/SPEC.md` and are not negotiable.

- All SQL is parameterized. String-building a query is a review blocker.
- No `eval`, `exec`, `pickle`, `yaml.load` without `SafeLoader`, or shell
  interpolation of any value that came from downloaded data.
- Every outbound request goes through the allowlisted HTTP client in
  `curbcheck.net`. Direct use of `httpx` or `urllib` elsewhere is a blocker.
- The server binds `127.0.0.1`. A `0.0.0.0` anywhere in config is a blocker.
- No telemetry, analytics, or remote asset loading of any kind.

## 6. Repository hygiene

- Commits are small and single-purpose. Message subject in the imperative,
  under 72 characters, with a body that explains why when the diff does not.
- Every behavior change comes with a test or an explicit note on why it cannot
  be tested.
- Documentation lives next to the code it describes. `docs/` holds the spec,
  the architecture overview, and decision records. A decision that overrides
  the spec gets a short entry in `docs/DECISIONS.md` with the evidence.
- Do not check in data, secrets, or generated artifacts. `data/` is
  gitignored; `.env` is gitignored; snapshots are gitignored.

## 7. Guidance specific to LLM contributors

- Read `CLAUDE.md`, this file, and `docs/DECISIONS.md` before editing.
- Prefer editing the existing function to adding a parallel one.
- Do not widen scope. If a change reveals a second problem, note it in the
  handoff rather than fixing it silently.
- When a fact about the data is uncertain, measure it and record the number
  rather than guessing. The data is small enough to query directly.
- Write for the next reader. If it took you three tool calls to understand a
  block, add the one comment that would have saved them.
