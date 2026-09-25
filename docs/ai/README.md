# Orientation for AI agents

This file exists to let an agent get oriented in this repo quickly, without re-reading every
script from scratch. It's a supplement to `README.md` (setup/usage, human-facing) and the
comments in each script (implementation detail) — read those too if you need specifics; this
doc is the map, not the territory.

## What this project is

A small, dependency-free tool that keeps two macOS `Calendar.app` calendars (typically a work
Outlook/Exchange account and a personal Google account) mutually blocked, without ever copying
event content across the privacy boundary. It only ever creates generic "🔒 Busy" placeholder
events on each side — never titles, attendees, locations, or descriptions. That's the entire
value proposition: it's safe to run even when one calendar's content is not allowed to leave its
source system.

There is no Google API, no Microsoft Graph API, no OAuth, no cloud project. Both calendars are
already synced into `Calendar.app` via macOS's own account system (Internet Accounts / Exchange),
so this tool only ever talks to `Calendar.app` itself, locally, via JXA (`osascript -l
JavaScript`). All the real sync latency and reliability (Google/Exchange <-> Calendar.app) is
Apple's problem, not this tool's.

## Architecture

```
calendar_sync.py   (orchestrator, runs via launchd or by hand)
        |
        | subprocess: osascript -l JavaScript <script>.js <args...>  (JSON in stdout)
        v
   read_events.js       -- list real (non-placeholder) events in the sync window, expanding recurrences
   list_placeholders.js -- list this script's own placeholder events (any time)
   create_event.js      -- create one placeholder event
   delete_events.js     -- delete a specific set of placeholder events (by exact start/end)
```

`list_calendar_ids.applescript` is a one-time/occasional operator tool, not part of the sync
loop — it prints each `Calendar.app` calendar's stable internal id (needed for the CONFIG block,
see below) and a couple of upcoming event titles (to disambiguate same-named calendars).

Each JXA script is invoked fresh per call (no persistent process) and communicates back to Python
purely via a single JSON blob on stdout. There's no shared state file, no database — the only
persistent state is what's actually sitting in the two calendars.

## The sync algorithm (`calendar_sync.py`)

Calendars are identified by **stable internal id, not display name** — Exchange periodically
overwrites a calendar's local display name back to whatever the server-side folder is named, so
name-based matching is unreliable. Get ids via `list_calendar_ids.applescript`.

On every run, for each direction (Outlook → Google placeholders, and Google → Outlook
placeholders):

1. Read the real events on the source calendar in the sync window (`read_events.js`), which
   already excludes the other script-created placeholders and canceled events, and expands
   recurring series into concrete occurrences.
2. List the existing placeholders on the destination calendar (`list_placeholders.js`) —
   identified purely by title prefix (`PLACEHOLDER_PREFIX`, default `"🔒 Busy"`).
3. Diff by `(startDate, endDate)` as a set: placeholders with no matching source event anymore
   are deleted (`delete_events.js`); source events with no matching placeholder are created
   (`create_event.js`). Anything unchanged is left untouched — this is why re-running the script
   is idempotent and safe.

Consequences worth knowing:
- Nothing is title/content-aware on the destination side — a placeholder is just a time range.
  Two source events that happen to share the same `(start, end)` collapse to one placeholder;
  that's intentional, not a bug.
- Deleting a placeholder by hand is safe — the next run just recreates it if the source event
  still exists.
- Changing `PLACEHOLDER_PREFIX` after the first run orphans old placeholders (the script can no
  longer identify them to clean up) — needs manual cleanup if done.

## `read_events.js` — the interesting/fragile part

This is where almost all the real complexity lives: turning `Calendar.app`'s event model into a
flat list of concrete occurrences in the sync window.

Key implementation choices, and why:
- **Bulk property fetch, not per-item.** It fetches each property (`summary`, `startDate`,
  `endDate`, `alldayEvent`, `recurrence`, `excludedDates`, `uid`, `sequence`) as one array across
  the whole `cal.events` collection, rather than materializing individual event objects and
  calling `.summary()` etc. in a loop. Measured ~60x faster (per-item property access over the
  JXA/Apple Event bridge is roughly 800ms/event/property; bulk fetch is roughly 13ms/event/property).
  Bulk fetch's index alignment (each property array's index `i` referring to the same underlying
  event across all the arrays) has been verified against real data and is reliable. Per-item
  access (`events[i].property()`) is the one that's NOT reliable when multiple event objects
  share a `uid` — see `jxa-calendar-quirks.md` — don't use it to "sanity check" bulk fetch.
- **No `whose()` pre-filtering on `cal.events`.** An earlier version filtered by `startDate` at
  the JXA level; that was found to silently drop some recurring series (backend/account-dependent
  behavior, not documented, no error thrown) while keeping others. The whole collection is now
  always fetched and filtered in plain JS instead.
- **Manual RRULE expansion.** `expandRecurrence()` parses the iCalendar `RRULE` string itself
  (`FREQ`, `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY`) rather than relying on `Calendar.app` to expand
  occurrences, because JXA doesn't expose expanded occurrences directly. Support matrix:
  - Handled: `DAILY`, `WEEKLY` (with or without `BYDAY`), `MONTHLY` (with or without `BYDAY`,
    e.g. "second Tuesday"), `YEARLY` (plain interval-stepping only), `INTERVAL`, `COUNT`, `UNTIL`,
    `EXDATE` (via `excludedDates()`).
  - Not handled: `BYMONTHDAY` (e.g. "last day of month"), `YEARLY` combined with `BYDAY`/`BYMONTH`
    (e.g. "first Monday of September"), `RDATE` (explicit added occurrences), `BYSETPOS`, `WKST`.
- **Same-`uid` collision handling: primary by `sequence`, secondary emitted if it collides.**
  Exchange can leave behind multiple event objects sharing one `uid` — either a stale, still-
  recurring copy of a series after a "this and future occurrences" reschedule, or (trickier) two
  competing full copies after a single-occurrence reschedule where `sequence`/`stampDate` don't
  reliably say which is current (see `jxa-calendar-quirks.md`). The highest-`sequence` copy per
  `uid` is treated as "primary" and always expanded/emitted. Every other same-`uid` copy is also
  expanded, but only occurrences landing on a day the primary also has an occurrence on are kept
  — a genuine same-day rival gets emitted alongside the primary (both candidate times, rather than
  guessing) instead of being silently dropped. Each such collision prints a `COLLISION ...`
  diagnostic line via `console.log` (before the final JSON return value in stdout — see the usage
  comment at the top of the file) for building up evidence toward a real fix later.
- **Cancellation filtering.** Skips the script's own placeholders (by title prefix) and anything
  whose summary starts with `Canceled:`/`CANCELED:` (Exchange's convention for a canceled
  occurrence that otherwise still shows up as an event).

## Files

| File | Role |
|---|---|
| `calendar_sync.py` | Orchestrator. CONFIG block at the top (calendar ids, labels, sync window, placeholder prefix). Entry point for both manual runs and launchd. |
| `read_events.js` | Reads + expands real events in the sync window for one calendar. |
| `list_placeholders.js` | Lists this script's own placeholders on one calendar (unbounded time range). |
| `create_event.js` | Creates one placeholder event. |
| `delete_events.js` | Deletes placeholders matching an exact `(start, end)` set. |
| `list_calendar_ids.applescript` | Operator tool: prints calendar name → stable id (+ upcoming events for disambiguation). Run manually when setting up or re-pointing CONFIG. |
| `com.you.calendarsync.plist` | launchd job template for scheduled runs; user copies + edits with their own path/interval. |
| `README.md` | Human setup/usage instructions. |

## Testing changes

There's no automated test suite — this is a personal utility script, not a library. Verify
changes by hand:
```bash
osascript -l JavaScript read_events.js "<calendar-id-or-name>" "🔒 Busy" 30
```
(prints the JSON array of expanded occurrences — inspect it directly), and/or run
`python3 calendar_sync.py` end-to-end and check both calendars in `Calendar.app` for the expected
placeholders. Re-run it a second time and confirm it doesn't duplicate anything.

## JXA / Calendar.app scripting quirks

Before writing or debugging a new `.js` or `.applescript` file against `Calendar.app`, read
[`jxa-calendar-quirks.md`](jxa-calendar-quirks.md) — a reference doc of scripting-bridge bugs and
gotchas (broken calendar-level `uid`, `byId()` never throwing, EventKit's AppleScript bridge
returning fake ids, etc.) that cost real time to discover and aren't worth rediscovering.

## Status

The stale-duplicate-by-`sequence` filtering and `Canceled:` filtering described above in
`read_events.js` are implemented and verified against real Exchange data (as of 2026-09-01).
Calendar identification is id-based (`OUTLOOK_CALENDAR_ID`/`GOOGLE_CALENDAR_ID` in
`calendar_sync.py`), not name-based, for the same reason.

Not yet addressed: the `expandRecurrence()` RRULE coverage gaps listed above (`BYMONTHDAY`,
`YEARLY`+`BYDAY`/`BYMONTH`, `RDATE`, `BYSETPOS`, `WKST`), and a suspected month-end-rollover edge
case in the generic `MONTHLY`/`YEARLY` stepping fallback (e.g. a rule seeded on the 31st stepping
into a 30-day month) — not yet reproduced against real data, just a suspected gap from reading the
code.
