#!/usr/bin/env python3
"""
calendar_sync.py

Keeps a macOS-Calendar-based Outlook calendar and a macOS-Calendar-based
Google calendar (added via System Settings > Internet Accounts) mutually
blocked, WITHOUT copying any event details across. It only creates generic
"Busy" placeholder events on each side to mark the times you're unavailable
on the other calendar.

Both calendars live in Calendar.app, so this script only ever talks to the
local macOS calendar store via EventKit (see eventkit_calendar.py) — there is
no Google API, no OAuth, and no cloud project involved. Note that Calendar.app
syncs Google via Apple's own CalDAV refresh cycle, so a newly created Google
event may take a few minutes to appear here (and vice versa for placeholders
showing up on the Google side).

How it works
------------
On every run, for each side:
  1. Read the real (non-placeholder) events in the sync window.
  2. List the placeholder events this script previously created on the
     OTHER calendar (identified by a title prefix).
  3. Diff by (start, end) time: delete placeholders with no matching real
     event anymore (the meeting moved or was cancelled), and create
     placeholders for real events that don't already have one. Untouched
     placeholders are left alone.

Usage
-----
  .venv/bin/python calendar_sync.py
  .venv/bin/python calendar_sync.py --dry-run   # report changes, write nothing

Setup
-----
See README.md in this folder for full setup instructions. Quick summary:
  1. Add your Google account in System Settings > Internet Accounts (or
     Calendar > Settings > Accounts) with Calendar syncing enabled, so it
     shows up as a calendar in Calendar.app's sidebar.
  2. Create the virtualenv and install the EventKit bindings:
       python3 -m venv .venv
       .venv/bin/pip install -r requirements.txt
  3. Find your calendar ids:
       .venv/bin/python eventkit_calendar.py
  4. Edit the CONFIG block below (calendar ids, sync window, etc.)
  5. Run once by hand: .venv/bin/python calendar_sync.py
     - Approve the macOS Calendars prompt. It must be answered from a terminal
       you can see, and it must be FULL access, not "add events only".
  6. Install the launchd job (see README.md) to run it automatically.
"""

import argparse
import datetime
import logging
import sys
import time
from pathlib import Path

import eventkit_calendar

# ============================= CONFIG =======================================

# Stable Calendar.app internal ids for the two calendars, NOT their display
# names — Exchange periodically overwrites the local display name back to
# whatever the server-side folder is named (e.g. "Calendar"), so matching by
# name is unreliable. Run `.venv/bin/python eventkit_calendar.py` to print
# each calendar's name and id, and paste the right ones below.
OUTLOOK_CALENDAR_ID = "3F5D9A23-9D3A-42F2-95B3-7AC25596D995"
GOOGLE_CALENDAR_ID = "3B690F1C-C2D6-4160-9034-4B8A3283C60B"

# Human-readable labels for these calendars, used only in log output.
OUTLOOK_CALENDAR_LABEL = "NJIA Calendar"
GOOGLE_CALENDAR_LABEL = "MAD Mark"

# Your own email address as it appears in each calendar's attendee lists, used
# to look up your RSVP status (myStatus: "accepted"/"declined"/"tentative"/
# "pending"/"unknown", or null) per event. Leave as "" to skip the lookup —
# which also means the INCLUDE_DECLINED/TENTATIVE filters below can never
# exclude anything on that calendar, since nothing there has a known status.
OUTLOOK_OWNER_EMAIL = "Mark.Bryk@Innovation.nj.gov"
GOOGLE_OWNER_EMAIL = "mark@mightyacorn.com"

# How many days ahead (from "now") to keep synced.
SYNC_WINDOW_DAYS = 14

# How far back to keep tidying up placeholders. Placeholders are only ever
# created from "now" forward, so this only governs cleanup of ones whose event
# has already passed; anything older than this is left alone.
PLACEHOLDER_LOOKBACK_DAYS = 30

# Whether all-day events (e.g. "Out of office", holidays) should be synced as
# placeholders on the other calendar. Off by default since these tend to be
# informational rather than actual time-blocking commitments.
INCLUDE_ALL_DAY_EVENTS = False

# Whether events you declined, or marked tentative, should still block time on
# the other calendar. Declining means you are not actually busy, so those are
# dropped by default; tentative still holds the slot.
#
# These only ever drop an event whose status is positively known to be
# "declined"/"tentative" — an event with no resolvable status (no attendee
# match, or a blank *_OWNER_EMAIL above) is always kept. Placeholder titles are
# unaffected either way: a synced event's placeholder is always exactly
# PLACEHOLDER_PREFIX, never annotated with a status.
#
# SHORTCOMING: these seem to catch less than expected on an Exchange calendar. 
# Most events there report a participant status that is not an RSVP answer at all
# (EventKit's delegated/completed/in-process), which PARTICIPANT_STATUS collapses 
# to "unknown" and this filter therefore keeps — so INCLUDE_DECLINED_EVENTS = False does
# not reliably mean "no declined meeting is ever mirrored"; it only catches the
# ones Exchange happens to report a real "declined" for. Treat it as
# best-effort tidying, not a guarantee.
INCLUDE_DECLINED_EVENTS = False
INCLUDE_TENTATIVE_EVENTS = True

# Title prefix is used to mark placeholder events created by this script, on
# BOTH calendars. Must be distinctive so it never collides with a real
# meeting title. Do not change this after first run without also manually
# cleaning up old placeholders under the old prefix.
PLACEHOLDER_PREFIX = "\U0001F512 Busy"  # "🔒 Busy"

# How many days of per-run debug logs to keep in LOG_DIR before pruning.
LOG_RETENTION_DAYS = 14

# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

logger = logging.getLogger("calendarsync")


def setup_logging():
    """Configure two handlers: a concise stdout stream (what launchd's
    sync.log has always shown) and a detailed per-run file under LOG_DIR
    with full event/placeholder tracing, for debugging mismatches after
    the fact."""
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"sync_{timestamp}.log"

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for f in LOG_DIR.glob("sync_*.log"):
        if f.stat().st_mtime < cutoff:
            f.unlink()

    return log_path


def local_str(iso_str):
    """Render a UTC ISO timestamp (as returned by eventkit_calendar) in the
    machine's local timezone, for humans reading the log."""
    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def fmt_range(start_iso, end_iso):
    return f"{local_str(start_iso)} -> {local_str(end_iso)}"


def get_events(calendar_id, label, owner_email=""):
    data = eventkit_calendar.read_events(
        calendar_id, PLACEHOLDER_PREFIX, SYNC_WINDOW_DAYS, owner_email
    )
    if not INCLUDE_ALL_DAY_EVENTS:
        excluded = sum(1 for ev in data if ev.get("allDay"))
        data = [ev for ev in data if not ev.get("allDay")]
        if excluded:
            logger.debug("  %s: excluded %d all-day event(s)", label, excluded)
    # Only an exact match drops an event, so a null/"unknown" status (no
    # attendee match, or no owner email configured for this calendar) is
    # always kept rather than being treated as a non-acceptance.
    for status, include in (
        ("declined", INCLUDE_DECLINED_EVENTS),
        ("tentative", INCLUDE_TENTATIVE_EVENTS),
    ):
        if include:
            continue
        excluded = sum(1 for ev in data if ev.get("myStatus") == status)
        data = [ev for ev in data if ev.get("myStatus") != status]
        if excluded:
            logger.debug("  %s: excluded %d %s event(s)", label, excluded, status)
    logger.info("  %s: %d real event(s) in window", label, len(data))
    for ev in sorted(data, key=lambda e: e["startDate"]):
        allday = " (all-day)" if ev.get("allDay") else ""
        status = f" [{ev['myStatus']}]" if ev.get("myStatus") else ""
        uid = f" uid={ev['uid']}" if ev.get("uid") else ""
        logger.debug("    [%s] %s%s%s%s  %r", label, fmt_range(ev["startDate"], ev["endDate"]), allday, status, uid, ev["summary"])
    return data


def list_placeholders(calendar_id, label):
    data = eventkit_calendar.list_placeholders(
        calendar_id, PLACEHOLDER_PREFIX, PLACEHOLDER_LOOKBACK_DAYS, SYNC_WINDOW_DAYS
    )
    logger.debug("  %s: %d existing placeholder(s)", label, len(data))
    for p in sorted(data, key=lambda e: e["startDate"]):
        logger.debug("    [%s] placeholder %s", label, fmt_range(p["startDate"], p["endDate"]))
    return data


def sync_direction(source_events, dest_calendar_id, source_label, dest_label, dry_run=False):
    """Make dest_calendar_id's placeholders match source_events, touching
    only what changed. Returns (created, deleted) counts."""
    logger.info("Syncing %s -> %s placeholders...", source_label, dest_label)

    wanted = {(ev["startDate"], ev["endDate"]) for ev in source_events}
    source_title_by_time = {(ev["startDate"], ev["endDate"]): ev["summary"] for ev in source_events}
    existing_placeholders = list_placeholders(dest_calendar_id, dest_label)
    existing = {(p["startDate"], p["endDate"]) for p in existing_placeholders}

    stale = sorted(existing - wanted)
    missing = sorted(wanted - existing)

    if stale:
        logger.info("  deleting %d stale placeholder(s) from %s:", len(stale), dest_label)
        for start_iso, end_iso in stale:
            logger.info("    - %s", fmt_range(start_iso, end_iso))

    if missing:
        logger.info("  creating %d placeholder(s) on %s:", len(missing), dest_label)
        for start_iso, end_iso in missing:
            # Source title is only ever written to this local debug log, never
            # to the placeholder itself — the whole point of this tool is that
            # event content never crosses the calendar boundary.
            title = source_title_by_time.get((start_iso, end_iso), "?")
            logger.info("    + %s  (source: %r)", fmt_range(start_iso, end_iso), title)

    if dry_run:
        if stale or missing:
            logger.info("  (dry run: nothing written)")
        return len(missing), len(stale)

    deleted = eventkit_calendar.delete_placeholders(
        dest_calendar_id, PLACEHOLDER_PREFIX, stale,
        PLACEHOLDER_LOOKBACK_DAYS, SYNC_WINDOW_DAYS,
    )
    if deleted != len(stale):
        logger.warning(
            "  %s: asked to delete %d placeholder(s) but only %d matched",
            dest_label, len(stale), deleted,
        )

    created = eventkit_calendar.create_placeholders(
        dest_calendar_id, PLACEHOLDER_PREFIX, missing
    )
    return created, deleted


# ------------------------------------ main -----------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without creating or deleting anything",
    )
    args = parser.parse_args()

    log_path = setup_logging()
    now = datetime.datetime.now().astimezone()
    logger.info(
        "[%s]%s Reading events... (details: %s)",
        now.strftime("%Y-%m-%d %H:%M %Z"),
        " DRY RUN —" if args.dry_run else "",
        log_path,
    )

    outlook_events = get_events(OUTLOOK_CALENDAR_ID, OUTLOOK_CALENDAR_LABEL, OUTLOOK_OWNER_EMAIL)
    google_events = get_events(GOOGLE_CALENDAR_ID, GOOGLE_CALENDAR_LABEL, GOOGLE_OWNER_EMAIL)

    created1, deleted1 = sync_direction(
        outlook_events, GOOGLE_CALENDAR_ID, OUTLOOK_CALENDAR_LABEL,
        GOOGLE_CALENDAR_LABEL, args.dry_run,
    )
    created2, deleted2 = sync_direction(
        google_events, OUTLOOK_CALENDAR_ID, GOOGLE_CALENDAR_LABEL,
        OUTLOOK_CALENDAR_LABEL, args.dry_run,
    )

    logger.info(
        "Done%s. %s: +%d/-%d placeholder(s). %s: +%d/-%d placeholder(s).",
        " (dry run — nothing written)" if args.dry_run else "",
        GOOGLE_CALENDAR_LABEL, created1, deleted1,
        OUTLOOK_CALENDAR_LABEL, created2, deleted2,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("ERROR: %s", exc)
        sys.exit(1)
