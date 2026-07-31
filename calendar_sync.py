#!/usr/bin/env python3
"""
calendar_sync.py

Keeps a macOS-Calendar-based Outlook calendar and a macOS-Calendar-based
Google calendar (added via System Settings > Internet Accounts) mutually
blocked, WITHOUT copying any event details across. It only creates generic
"Busy" placeholder events on each side to mark the times you're unavailable
on the other calendar.

Both calendars live in Calendar.app, so this script only ever talks to
Calendar.app via JXA (osascript) — there is no Google API, no OAuth, and
no cloud project involved. Note that Calendar.app's sync with Google runs
on Apple's own CalDAV refresh cycle, so a newly created Google event may
take a few minutes to appear here (and vice versa for placeholders showing
up on the Google side).

How it works
------------
On every run, for each side:
  1. Read the real (non-placeholder) events in the sync window.
  2. Delete every placeholder event this script previously created in that
     window (identified by a title prefix).
  3. Recreate one fresh placeholder per real event on the OTHER calendar.

Wiping and recreating (rather than diffing) means moved/cancelled meetings
are handled automatically, at the cost of placeholder event IDs changing
each run — which is fine since nothing else references them.

Setup
-----
See README.md in this folder for full setup instructions. Quick summary:
  1. Add your Google account in System Settings > Internet Accounts (or
     Calendar > Settings > Accounts) with Calendar syncing enabled, so it
     shows up as a calendar in Calendar.app's sidebar.
  2. Edit the CONFIG block below (calendar names, sync window, etc.)
  3. Run once by hand: python3 calendar_sync.py
     - Approve the macOS Automation permission prompt for Calendar
  4. Install the launchd job (see README.md) to run it automatically.
"""

import datetime
import json
import subprocess
import sys
from pathlib import Path

# ============================= CONFIG =======================================

# Exact names of the two calendars as they appear in Calendar.app's sidebar
# (Calendar.app > View > Show Calendar List to check).
OUTLOOK_CALENDAR_NAME = "NJIA Calendar"
GOOGLE_CALENDAR_NAME = "MAD Mark"

# How many days ahead (from "now") to keep synced.
SYNC_WINDOW_DAYS = 4

# Title prefix used to mark placeholder events created by this script, on
# BOTH calendars. Must be distinctive so it never collides with a real
# meeting title. Do not change this after first run without also manually
# cleaning up old placeholders under the old prefix.
PLACEHOLDER_PREFIX = "\U0001F512 Busy"  # "🔒 Busy"

# ==============================================================================

JXA_DIR = Path(__file__).resolve().parent


def run_jxa(script_name, args):
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(JXA_DIR / script_name), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{script_name} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"{script_name} returned non-JSON output: {result.stdout!r}")


def get_events(calendar_name):
    data = run_jxa(
        "read_events.js",
        [calendar_name, PLACEHOLDER_PREFIX, str(SYNC_WINDOW_DAYS)],
    )
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data


def delete_placeholders(calendar_name):
    data = run_jxa("delete_placeholders.js", [calendar_name, PLACEHOLDER_PREFIX])
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data.get("deleted", 0)


def create_placeholder(calendar_name, start_iso, end_iso):
    data = run_jxa(
        "create_event.js",
        [calendar_name, PLACEHOLDER_PREFIX, start_iso, end_iso],
    )
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])


# ------------------------------------ main -----------------------------------

def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"[{now.isoformat()}] Reading events...")
    outlook_events = get_events(OUTLOOK_CALENDAR_NAME)
    google_events = get_events(GOOGLE_CALENDAR_NAME)
    print(f"  {OUTLOOK_CALENDAR_NAME}: {len(outlook_events)} real event(s) in window")
    print(f"  {GOOGLE_CALENDAR_NAME}:  {len(google_events)} real event(s) in window")

    print("Clearing old placeholders...")
    n1 = delete_placeholders(OUTLOOK_CALENDAR_NAME)
    n2 = delete_placeholders(GOOGLE_CALENDAR_NAME)
    print(f"  Removed {n1} {OUTLOOK_CALENDAR_NAME} placeholder(s), {n2} {GOOGLE_CALENDAR_NAME} placeholder(s)")

    print("Creating fresh placeholders...")
    for ev in outlook_events:
        create_placeholder(GOOGLE_CALENDAR_NAME, ev["startDate"], ev["endDate"])
    for ev in google_events:
        create_placeholder(OUTLOOK_CALENDAR_NAME, ev["startDate"], ev["endDate"])

    print(
        f"Done. Created {len(outlook_events)} {GOOGLE_CALENDAR_NAME} placeholder(s) and "
        f"{len(google_events)} {OUTLOOK_CALENDAR_NAME} placeholder(s)."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
