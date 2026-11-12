# Orientation for AI agents

This file exists to let an agent get oriented in this repo quickly, without re-reading every
script from scratch. It's a supplement to `README.md` (setup/usage, human-facing) and the
comments in each script (implementation detail) — read those too if you need specifics; this
doc is the map, not the territory.

## What this project is

A small tool that keeps two macOS `Calendar.app` calendars (typically a work Outlook/Exchange
account and a personal Google account) mutually blocked, without ever copying event content
across the privacy boundary. It only ever creates generic "🔒 Busy" placeholder events on each
side — never titles, attendees, locations, or descriptions. That's the entire value proposition:
it's safe to run even when one calendar's content is not allowed to leave its source system.

There is no Google API, no Microsoft Graph API, no OAuth, no cloud project. Both calendars are
already synced into `Calendar.app` via macOS's own account system (Internet Accounts / Exchange),
so this tool only ever talks to the local macOS calendar store. All the real sync latency and
reliability (Google/Exchange <-> Calendar.app) is Apple's problem, not this tool's.

## Architecture

```
calendar_sync.py      orchestrator + CONFIG (runs via launchd or by hand)
        |
        v
eventkit_calendar.py  all calendar access, via macOS EventKit (PyObjC)
        |
        v
   EKEventStore -> the same local store Calendar.app itself reads and writes
```

Two files, one dependency (`pyobjc-framework-EventKit`, pinned in `requirements.txt` and
installed into a gitignored `.venv/`). Everything runs as `.venv/bin/python`, never `python3` —
both Homebrew's and Apple's Python are marked `EXTERNALLY-MANAGED` (PEP 668) and refuse
`pip install --user`.

`eventkit_calendar.py` is also the operator tool: run it directly
(`.venv/bin/python eventkit_calendar.py`) to print each calendar's name, account and stable id
for the CONFIG block.

## The sync algorithm (`calendar_sync.py`)

Calendars are identified by **stable internal id, not display name** — Exchange periodically
overwrites a calendar's local display name back to whatever the server-side folder is named, so
name-based matching is unreliable. In practice the work calendar's title is just `"Calendar"`.

On every run, for each direction (Outlook → Google placeholders, and Google → Outlook
placeholders):

1. Read the real events on the source calendar in the sync window (`read_events()`), which
   already excludes the other script-created placeholders and canceled events, and returns
   recurring series as concrete resolved occurrences.
2. List the existing placeholders on the destination calendar (`list_placeholders()`) —
   identified purely by title prefix (`PLACEHOLDER_PREFIX`, default `"🔒 Busy"`).
3. Diff by `(startDate, endDate)` as a set: placeholders with no matching source event anymore
   are deleted, source events with no matching placeholder are created. Anything unchanged is
   left untouched — this is why re-running the script is idempotent and safe.

`--dry-run` reports both sides of that diff and writes nothing.

Consequences worth knowing:
- Nothing is title/content-aware on the destination side — a placeholder is just a time range.
  Two source events that happen to share the same `(start, end)` collapse to one placeholder;
  that's intentional, not a bug.
- Deleting a placeholder by hand is safe — the next run just recreates it if the source event
  still exists.
- Changing `PLACEHOLDER_PREFIX` after the first run orphans old placeholders (the script can no
  longer identify them to clean up) — needs manual cleanup if done.
- **Placeholder scanning is bounded**, over `[now - PLACEHOLDER_LOOKBACK_DAYS, now +
  SYNC_WINDOW_DAYS]`. Since the read window starts at "now", a placeholder whose event has
  already ended is never "wanted" and so gets cleaned up while it's inside the lookback; once it
  falls out of that range it is left alone permanently and accumulates as history. The bound is
  not cosmetic — see the EventKit predicate-span trap in `jxa-calendar-quirks.md`.

## `eventkit_calendar.py` — the part worth understanding

Key implementation choices, and why:

- **ISO strings must match JavaScript's `Date.toISOString()` byte-for-byte**
  (`2026-11-12T15:00:00.000Z`), which is what `iso_utc()` produces. `sync_direction()` diffs
  source events against existing placeholders by exact **string** equality, so a
  different-but-equivalent spelling of the same instant (`+00:00`, or dropping the milliseconds)
  would make every placeholder look stale and be deleted and recreated on every run. Both sides
  of that diff now come from `iso_utc()`, so they can't drift apart — but don't introduce a
  second date formatter.
- **Resolved occurrences, not stored objects.** `eventsMatchingPredicate_` returns the expanded,
  already-deduplicated occurrence stream Calendar.app's own UI draws. This is the whole reason
  the project moved off AppleScript/JXA, which exposed raw stored objects and therefore needed
  ~130 lines of hand-rolled RRULE expansion plus `sequence`/`stampDate` guesswork to pick the
  live copy among Exchange's same-`uid` clones. Both of those are gone; don't reintroduce either.
- **Cancellation via `status()`**, not a `"Canceled:"` title match. The title check is kept as a
  cheap second test only because it isn't confirmed that every Exchange-renamed
  `"Canceled: <title>"` event also carries `status == canceled`.
- **Writes are batched behind one commit.** `create_placeholders()`/`delete_placeholders()` stage
  every change with `commit=False` and call `commit()` once, so N placeholders cost one trip to
  the store. On failure they `reset()` the store to drop the staged batch rather than leaving it
  half-applied. All writes use `EKSpanThisEvent`, which is safe because placeholders are never
  recurring — `EKSpanFutureEvents` on a recurring event would delete a whole series.
- **`myStatus` matches attendees by email**, because `isCurrentUser()` is not trustworthy (on a
  real Exchange event it flagged the organizer, not the account owner).
- **A single cached `EKEventStore`** for the process, which is what makes batched commits
  possible across both calendars.

## Files

| File | Role |
|---|---|
| `calendar_sync.py` | Orchestrator. CONFIG block at the top (calendar ids, labels, sync window, lookback, placeholder prefix). Entry point for manual runs, `--dry-run`, and launchd. |
| `eventkit_calendar.py` | All calendar access: auth, calendar lookup, `read_events`, `list_placeholders`, `create_placeholders`, `delete_placeholders`. Run directly to list calendar ids. |
| `requirements.txt` | The one dependency. Install into `.venv`, not system python. |
| `com.you.calendarsync.plist` | launchd job template; user copies + edits with their own paths. Must point at `.venv/bin/python`. |
| `README.md` | Human setup/usage instructions. |

## Permissions

One permission, **Calendars** (TCC), and it must be *full* access — write-only access is handed a
single fictional calendar and silently sees none of the real ones. Nothing shells out to
`osascript` anymore, so macOS's separate **Automation** permission is no longer involved at all.

macOS shows a permission prompt to whatever launched the process, so the first run has to happen
by hand from a terminal; a prompt raised under launchd may never be displayed. No app bundle,
code signing, or Apple Developer account is needed.

## Testing changes

There's no automated test suite — this is a personal utility script, not a library. Verify
changes by hand:

```bash
.venv/bin/python calendar_sync.py --dry-run   # full diff, writes nothing
.venv/bin/python eventkit_calendar.py         # calendars + ids
```

Then run it for real and check both calendars in `Calendar.app`. **Re-run it a second time and
confirm it reports `+0/-0`** — that's the real test, since it's what catches a date-formatting
regression (which shows up as everything being deleted and recreated every run rather than as an
error).

## Calendar.app quirks

Before changing calendar access code, read [`jxa-calendar-quirks.md`](jxa-calendar-quirks.md) —
a reference doc of EventKit and Calendar.app scripting traps that cost real time to discover and
aren't documented anywhere Apple publishes. The live ones are the EventKit predicate span limit
(silently returns *fewer* results the wider the range) and the write-only-access trap. It also
records the JXA-era landmines, which matter only if someone reintroduces AppleScript here.

## Status

The read and write paths both run on EventKit. Validated against the AppleScript implementation
over a live window before it was removed: EventKit resolved a canceled-occurrence phantom and a
wrong-time placeholder (`Mark / Catherine 1:1` on 2026-11-24, which the old picker placed at
13:30 instead of 13:00) that the old path got wrong, and got nothing wrong that the old path got
right.

Not addressed, and deliberately so: two *different*-`uid` events that are really the same
underlying meeting still produce two placeholders. EventKit does not help with that one — it
stays a heuristic. See the handoff doc for the untried signals.
