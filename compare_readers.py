#!/usr/bin/env python3
"""
compare_readers.py

Validation harness for the EventKit port: runs the EventKit reader and the
legacy JXA reader (read_events.js) over the same calendar and window, and
diffs their output.

This exists because "EventKit's live output looks clean" is a weaker claim
than it sounds — the known wrong-time case (uid 6D29DED7-..., "Pipeline Sync"
placed at 3:30 PM instead of 3:00 PM) no longer reproduces, so there is no
failing example left to prove a fix against. A side-by-side diff is the only
way to size what actually changed.

What to look for in the output:
  - ONLY IN EVENTKIT  -- occurrences JXA missed. Expected where JXA's RRULE
    expansion has gaps (BYMONTHDAY, BYSETPOS, YEARLY+BYDAY, RDATE).
  - ONLY IN JXA       -- usually the phantoms: stale series clones and
    split-artifact "ghost" slots that EventKit resolved away. Each one here
    is a placeholder that would have been created at a time with no meeting.
  - TIME DISAGREEMENT -- same title, different time. This is the max-sequence
    picker having chosen the wrong sibling; EventKit's time is authoritative.

Usage
-----
  .venv/bin/python compare_readers.py                 # both calendars
  .venv/bin/python compare_readers.py --calendar outlook
  .venv/bin/python compare_readers.py --days 1

Run it from Terminal.app the first time: the JXA side raises an Automation
prompt and the EventKit side raises a Calendars privacy prompt, and macOS
attributes both to the launching process.
"""

import argparse
import logging
import sys
from collections import defaultdict

import calendar_sync as cs


def key_of(ev):
    return (ev["startDate"], ev["endDate"])


def label_of(ev):
    allday = " (all-day)" if ev.get("allDay") else ""
    return f"{cs.fmt_range(ev['startDate'], ev['endDate'])}{allday}  {ev['summary']!r}"


def by_title(events):
    groups = defaultdict(set)
    for ev in events:
        groups[ev["summary"]].add(key_of(ev))
    return groups


def compare(calendar_id, label, owner_email):
    print(f"\n{'=' * 78}\n{label}  ({calendar_id})\n{'=' * 78}")

    ek = cs.get_events(calendar_id, label, owner_email, reader="eventkit")
    jxa = cs.get_events(calendar_id, label, owner_email, reader="jxa")

    ek_by_key = {key_of(e): e for e in ek}
    jxa_by_key = {key_of(e): e for e in jxa}

    print(f"\nEventKit: {len(ek)} event(s)   JXA: {len(jxa)} event(s)")

    only_ek = sorted(set(ek_by_key) - set(jxa_by_key))
    only_jxa = sorted(set(jxa_by_key) - set(ek_by_key))
    shared = set(ek_by_key) & set(jxa_by_key)

    print(f"agreeing on {len(shared)} (start, end) slot(s)")

    if only_ek:
        print(f"\nONLY IN EVENTKIT ({len(only_ek)}) — JXA missed these:")
        for k in only_ek:
            print(f"  + {label_of(ek_by_key[k])}")

    if only_jxa:
        print(f"\nONLY IN JXA ({len(only_jxa)}) — candidate phantoms/ghosts:")
        for k in only_jxa:
            print(f"  - {label_of(jxa_by_key[k])}")

    # A title present on both sides at different times is the wrong-winner
    # bug, which is more serious than a plain add/drop: it means a placeholder
    # was being written at a time nothing was scheduled.
    ek_titles, jxa_titles = by_title(ek), by_title(jxa)
    disagreements = [
        title
        for title in set(ek_titles) & set(jxa_titles)
        if ek_titles[title] != jxa_titles[title]
    ]
    if disagreements:
        print(f"\nTIME DISAGREEMENT ({len(disagreements)}) — same title, different time(s):")
        for title in sorted(disagreements):
            print(f"  {title!r}")
            for k in sorted(ek_titles[title] - jxa_titles[title]):
                print(f"    EventKit only: {cs.fmt_range(*k)}")
            for k in sorted(jxa_titles[title] - ek_titles[title]):
                print(f"    JXA only:      {cs.fmt_range(*k)}")

    # myStatus is part of the output contract, and the two readers derive it
    # differently: JXA read a string straight off the attendee, EventKit maps
    # an EKParticipantStatus int. Compare it on the slots they agree on, where
    # a difference can only come from the status lookup itself.
    status_diffs = [
        (k, jxa_by_key[k].get("myStatus"), ek_by_key[k].get("myStatus"))
        for k in sorted(shared)
        if jxa_by_key[k].get("myStatus") != ek_by_key[k].get("myStatus")
    ]
    if status_diffs:
        print(f"\nmyStatus DISAGREEMENT ({len(status_diffs)} of {len(shared)} shared):")
        for k, jxa_status, ek_status in status_diffs:
            print(f"  {cs.fmt_range(*k)}  JXA={jxa_status!r} -> EventKit={ek_status!r}"
                  f"  {ek_by_key[k]['summary']!r}")

    if not (only_ek or only_jxa or disagreements or status_diffs):
        print("\nIdentical output. The port is a no-op on this window — which is")
        print("not the same as it being pointless; it means no phantom happened")
        print("to be live here. Try a wider --days.")

    return len(only_ek), len(only_jxa), len(disagreements)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    parser.add_argument(
        "--calendar", choices=["outlook", "google", "both"], default="both"
    )
    parser.add_argument(
        "--days", type=int, help=f"window length (default: {cs.SYNC_WINDOW_DAYS})"
    )
    args = parser.parse_args()

    if args.days:
        cs.SYNC_WINDOW_DAYS = args.days

    # cs.get_events() logs through the "calendarsync" logger; without a handler
    # its per-event debug lines would be dropped and only the diff would print.
    logging.basicConfig(level=logging.DEBUG, format="%(message)s", stream=sys.stdout)
    cs.logger.setLevel(logging.DEBUG)

    targets = []
    if args.calendar in ("outlook", "both"):
        targets.append(
            (cs.OUTLOOK_CALENDAR_ID, cs.OUTLOOK_CALENDAR_LABEL, cs.OUTLOOK_OWNER_EMAIL)
        )
    if args.calendar in ("google", "both"):
        targets.append(
            (cs.GOOGLE_CALENDAR_ID, cs.GOOGLE_CALENDAR_LABEL, cs.GOOGLE_OWNER_EMAIL)
        )

    totals = [compare(*t) for t in targets]

    print(f"\n{'=' * 78}")
    ek_only = sum(t[0] for t in totals)
    jxa_only = sum(t[1] for t in totals)
    disagree = sum(t[2] for t in totals)
    print(f"TOTAL: {ek_only} only-in-EventKit, {jxa_only} only-in-JXA, "
          f"{disagree} title(s) with time disagreements")
    print("Nothing was written to either calendar — this script only reads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
