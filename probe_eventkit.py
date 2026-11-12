#!/usr/bin/env python3
"""
probe_eventkit.py

Throwaway diagnostic, not part of the sync. It answers two questions about
whether this project could move off the Calendar.app AppleScript interface and
onto EventKit.

1. ACCESS -- can a plain script read REAL data?
   An earlier EventKit attempt returned the placeholder calendar id
   "VIRTUAL_APP_CALENDAR_UUID" for every calendar (see
   docs/ai/jxa-calendar-quirks.md), which was read at the time as "EventKit
   needs a signed, entitled app bundle". But that placeholder is also just
   what macOS hands a process that has no calendar authorization yet. If an
   ordinary TCC grant was the only thing missing, this script prints real
   calendar ids and the app-bundle theory is wrong.

2. RESOLUTION -- does EventKit answer the question read_events.js can't?
   Exchange leaves many event objects under one uid (stale series clones,
   detached single-occurrence overrides). The AppleScript interface exposes
   all of them and no field saying which occurrence an override replaces,
   which is why run() has to guess via sequence/stampDate. EventKit's
   eventsMatchingPredicate: instead returns EXPANDED, ALREADY-RESOLVED
   occurrences -- the same thing Calendar.app's UI draws. So this prints, per
   event: isDetached and occurrenceDate (the RECURRENCE-ID equivalent JXA
   never exposes), grouped by calendarItemExternalIdentifier (EventKit's name
   for what JXA calls uid).

   The interesting output is the group sizes. JXA reports up to 19 objects
   under a single uid; if EventKit reports one occurrence per real meeting,
   the whole dedup layer in read_events.js becomes unnecessary rather than
   needing a better heuristic.

Usage
-----
  .venv/bin/python probe_eventkit.py                        # next 14 days
  .venv/bin/python probe_eventkit.py --start 2026-09-29 --days 1
  .venv/bin/python probe_eventkit.py --days 3 --calendar NJIA

A known-wrong case to check against: on 2026-09-29, uid
6D29DED7-7A15-4785-9B91-666A3634E5CD ("Pipeline Sync") got a placeholder at
3:30 PM when the real meeting was at 3:00 PM, because the max-sequence picker
chose a stale sibling. Run with --start 2026-09-29 --days 1 and see which time
EventKit reports.

If the authorization request hangs or returns denied, re-run from Terminal.app
directly -- macOS attributes the permission prompt to the process that
launched the script, so a prompt raised under a non-interactive parent may
never be displayed.
"""

import argparse
import datetime
import sys
import threading
from collections import defaultdict

import objc
from EventKit import EKEventStore
from Foundation import NSDate

# EKEntityType / EKAuthorizationStatus / EKEventStatus constants. Hardcoded
# rather than imported because the names move around between SDK versions.
EK_ENTITY_TYPE_EVENT = 0

AUTH_STATUS = {
    0: "notDetermined (never asked)",
    1: "restricted (blocked by policy/MDM)",
    2: "denied (explicitly refused)",
    3: "authorized / fullAccess",
    4: "writeOnly (can add events, cannot read them)",
}

EVENT_STATUS = {0: "none", 1: "confirmed", 2: "tentative", 3: "canceled"}

# The placeholder macOS substitutes when a process is not allowed real data.
FAKE_CALENDAR_ID = "VIRTUAL_APP_CALENDAR_UUID"

# Calendar.app internal ids from calendar_sync.py's CONFIG, so we can check
# whether EventKit uses the same identifier namespace as JXA does.
JXA_CALENDAR_IDS = {
    "3F5D9A23-9D3A-42F2-95B3-7AC25596D995": "OUTLOOK_CALENDAR_ID (NJIA Calendar)",
    "3B690F1C-C2D6-4160-9034-4B8A3283C60B": "GOOGLE_CALENDAR_ID (MAD Mark)",
}


def to_datetime(nsdate):
    """NSDate -> naive local datetime, or None."""
    if nsdate is None:
        return None
    return datetime.datetime.fromtimestamp(nsdate.timeIntervalSince1970())


def fmt(nsdate):
    dt = to_datetime(nsdate)
    return dt.strftime("%Y-%m-%d %H:%M %a") if dt else "-"


def request_access(store, timeout=60):
    """
    Trigger the TCC prompt and block until the user answers.

    Returns (granted, error_string, timed_out). The completion handler fires on
    an arbitrary queue, so a plain threading.Event is enough -- no run loop.
    """
    done = threading.Event()
    result = {"granted": False, "error": None}

    def handler(granted, error):
        result["granted"] = bool(granted)
        result["error"] = str(error) if error else None
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(EK_ENTITY_TYPE_EVENT, handler)

    if not done.wait(timeout):
        return False, None, True
    return result["granted"], result["error"], False


def describe_access(store):
    status = EKEventStore.authorizationStatusForEntityType_(EK_ENTITY_TYPE_EVENT)
    print(f"authorization status: {status} = {AUTH_STATUS.get(status, 'unknown')}")

    if status == 3:
        return True

    # writeOnly (4) is NOT enough to read anything: macOS answers a write-only
    # process with one fake calendar called "Calendar" whose id is
    # VIRTUAL_APP_CALENDAR_UUID. Reading requires full access, which on
    # macOS 14+ is a separate request from the write-only grant.
    if status == 4:
        print("writeOnly is insufficient for reading -- requesting FULL access.")

    print("requesting access (a macOS prompt may appear)...")
    granted, error, timed_out = request_access(store)
    if timed_out:
        print("  TIMED OUT waiting for an answer.")
        print("  No prompt was shown, or it was shown somewhere you could not")
        print("  see it. Re-run this from Terminal.app directly.")
        return False
    print(f"  granted={granted}" + (f" error={error}" if error else ""))
    if not granted:
        print("  Check System Settings > Privacy & Security > Calendars.")
    return granted


def report_calendars(store):
    cals = store.calendarsForEntityType_(EK_ENTITY_TYPE_EVENT) or []
    print(f"\n{len(cals)} calendar(s) visible to EventKit:")

    fake = 0
    for cal in cals:
        cal_id = str(cal.calendarIdentifier())
        source = cal.source().title() if cal.source() else "?"
        note = ""
        if cal_id == FAKE_CALENDAR_ID:
            fake += 1
            note = "  <-- PLACEHOLDER, not real data"
        elif cal_id in JXA_CALENDAR_IDS:
            note = f"  <-- matches {JXA_CALENDAR_IDS[cal_id]}"
        print(f"  {cal.title()!r} [{source}] id={cal_id}{note}")

    print()
    if fake:
        print(f"VERDICT (access): FAKE. {fake} calendar(s) returned the")
        print("placeholder id, so this process is not authorized for real data.")
        print("The app-bundle/entitlement theory stands.")
        return None

    matched = [c for c in cals if str(c.calendarIdentifier()) in JXA_CALENDAR_IDS]
    print("VERDICT (access): REAL. No placeholder ids -- a plain Python script")
    print("can read live calendar data from EventKit. No app bundle, no signing,")
    print("no Apple Developer account needed for local use.")
    if matched:
        print(f"Bonus: {len(matched)} EventKit id(s) match calendar_sync.py's CONFIG")
        print("ids exactly, so JXA and EventKit share one identifier namespace.")
    else:
        print("Note: no EventKit id matched calendar_sync.py's CONFIG ids, so a")
        print("port would need to re-resolve the two calendars (by title/source).")
    return cals


def report_events(store, cals, start, end, title_filter):
    if title_filter:
        cals = [c for c in cals if title_filter.lower() in str(c.title()).lower()]
        print(f"\nfiltered to {len(cals)} calendar(s) matching {title_filter!r}")
        if not cals:
            return

    ns_start = NSDate.dateWithTimeIntervalSince1970_(start.timestamp())
    ns_end = NSDate.dateWithTimeIntervalSince1970_(end.timestamp())
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        ns_start, ns_end, cals
    )
    events = store.eventsMatchingPredicate_(predicate) or []

    print(f"\n{'=' * 78}")
    print(f"{len(events)} occurrence(s) from {start:%Y-%m-%d %H:%M} "
          f"to {end:%Y-%m-%d %H:%M}")
    print(f"{'=' * 78}")

    groups = defaultdict(list)
    for ev in events:
        ext_id = ev.calendarItemExternalIdentifier()
        groups[str(ext_id) if ext_id else "<no external id>"].append(ev)

    for ext_id, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        flag = "  <-- MULTIPLE under one uid" if len(group) > 1 else ""
        print(f"\nuid {ext_id}  ({len(group)} occurrence(s)){flag}")
        for ev in sorted(group, key=lambda e: e.startDate().timeIntervalSince1970()):
            recurring = "recurring" if ev.hasRecurrenceRules() else "single"
            detached = "DETACHED" if ev.isDetached() else "-"
            occ = fmt(ev.occurrenceDate()) if ev.isDetached() else "-"
            print(f"  {ev.title()}")
            print(f"    start={fmt(ev.startDate())}  end={fmt(ev.endDate())}"
                  f"  allDay={bool(ev.isAllDay())}")
            print(f"    {recurring}  isDetached={detached}  occurrenceDate={occ}")
            print(f"    status={EVENT_STATUS.get(ev.status(), ev.status())}"
                  f"  calendar={ev.calendar().title()!r}")

    multi = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n{'=' * 78}")
    if multi:
        print(f"VERDICT (resolution): {len(multi)} uid(s) returned more than one")
        print("occurrence in this window. Inspect them above -- repeats of a real")
        print("weekly series are expected and fine; two occurrences of the SAME")
        print("meeting at different times is the ghost/stale-clone problem")
        print("surviving into EventKit.")
    else:
        print("VERDICT (resolution): every uid returned exactly one occurrence.")
        print("EventKit resolved the stale clones and overrides for us -- no")
        print("sequence/stampDate guessing required.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    parser.add_argument("--start", help="window start, YYYY-MM-DD (default: now)")
    parser.add_argument("--days", type=int, default=14, help="window length")
    parser.add_argument("--calendar", help="only calendars whose title contains this")
    args = parser.parse_args()

    if args.start:
        start = datetime.datetime.strptime(args.start, "%Y-%m-%d")
    else:
        start = datetime.datetime.now()
    end = start + datetime.timedelta(days=args.days)

    print(f"pyobjc {objc.__version__}, python {sys.version.split()[0]}\n")

    store = EKEventStore.alloc().init()
    if not describe_access(store):
        return 1

    cals = report_calendars(store)
    if cals is None:
        return 1

    report_events(store, cals, start, end, args.calendar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
