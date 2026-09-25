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
  2. List the placeholder events this script previously created on the
     OTHER calendar (identified by a title prefix).
  3. Diff by (start, end) time: delete placeholders with no matching real
     event anymore (the meeting moved or was cancelled), and create
     placeholders for real events that don't already have one. Untouched
     placeholders are left alone.

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
import logging
import subprocess
import sys
import time
from pathlib import Path

# ============================= CONFIG =======================================

# Stable Calendar.app internal ids for the two calendars, NOT their display
# names — Exchange periodically overwrites the local display name back to
# whatever the server-side folder is named (e.g. "Calendar"), so matching by
# name is unreliable. Run `osascript list_calendar_ids.applescript` to print
# each calendar's name, id, and a couple of upcoming event titles (to tell
# apart same-named calendars) and paste the right id below.
OUTLOOK_CALENDAR_ID = "3F5D9A23-9D3A-42F2-95B3-7AC25596D995"
GOOGLE_CALENDAR_ID = "3B690F1C-C2D6-4160-9034-4B8A3283C60B"

# Human-readable labels for these calendars, used only in log output.
OUTLOOK_CALENDAR_LABEL = "NJIA Calendar"
GOOGLE_CALENDAR_LABEL = "MAD Mark"

# Your own email address as it appears in each calendar's attendee lists, used
# to look up your RSVP status (myStatus: "accepted"/"declined"/"tentative"/
# "unknown"/null) per event. Leave as "" to skip the lookup (myStatus omitted).
OUTLOOK_OWNER_EMAIL = "Mark.Bryk@Innovation.nj.gov"
GOOGLE_OWNER_EMAIL = ""

# How many days ahead (from "now") to keep synced.
SYNC_WINDOW_DAYS = 14

# Whether all-day events (e.g. "Out of office", holidays) should be synced as
# placeholders on the other calendar. Off by default since these tend to be
# informational rather than actual time-blocking commitments.
INCLUDE_ALL_DAY_EVENTS = False

# Title prefix used to mark placeholder events created by this script, on
# BOTH calendars. Must be distinctive so it never collides with a real
# meeting title. Do not change this after first run without also manually
# cleaning up old placeholders under the old prefix.
PLACEHOLDER_PREFIX = "\U0001F512 Busy"  # "🔒 Busy"

# How many days of per-run debug logs to keep in LOG_DIR before pruning.
LOG_RETENTION_DAYS = 14

# ==============================================================================

JXA_DIR = Path(__file__).resolve().parent
LOG_DIR = JXA_DIR / "logs"

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
    """Render a UTC ISO timestamp (as returned by the JXA scripts) in the
    machine's local timezone, for humans reading the log."""
    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def fmt_range(start_iso, end_iso):
    return f"{local_str(start_iso)} -> {local_str(end_iso)}"


def run_jxa(script_name, args):
    t0 = time.perf_counter()
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(JXA_DIR / script_name), *args],
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - t0
    logger.debug("  %s (%.2fs, exit=%d)", script_name, elapsed, result.returncode)
    if result.returncode != 0:
        raise RuntimeError(f"{script_name} failed: {result.stderr.strip()}")
    # read_events.js can print diagnostic lines (e.g. "UIDGROUP ...") via
    # console.log — under osascript, console.log output goes to stderr, not
    # stdout (confirmed 2026-09-25); only the script's actual return value
    # lands on stdout. Log stderr as debug context and parse stdout as JSON.
    for line in result.stderr.splitlines():
        if line.strip():
            logger.debug("  %s: %s", script_name, line)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{script_name} returned no output")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        raise RuntimeError(f"{script_name} returned non-JSON output: {result.stdout!r}")


def get_events(calendar_id, label, owner_email=""):
    data = run_jxa(
        "read_events.js",
        [calendar_id, PLACEHOLDER_PREFIX, str(SYNC_WINDOW_DAYS), owner_email],
    )
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    if not INCLUDE_ALL_DAY_EVENTS:
        excluded = sum(1 for ev in data if ev.get("allDay"))
        data = [ev for ev in data if not ev.get("allDay")]
        if excluded:
            logger.debug("  %s: excluded %d all-day event(s)", label, excluded)
    logger.info("  %s: %d real event(s) in window", label, len(data))
    for ev in sorted(data, key=lambda e: e["startDate"]):
        allday = " (all-day)" if ev.get("allDay") else ""
        status = f" [{ev['myStatus']}]" if ev.get("myStatus") else ""
        uid = f" uid={ev['uid']}" if ev.get("uid") else ""
        logger.debug("    [%s] %s%s%s%s  %r", label, fmt_range(ev["startDate"], ev["endDate"]), allday, status, uid, ev["summary"])
    return data


def list_placeholders(calendar_id, label):
    data = run_jxa("list_placeholders.js", [calendar_id, PLACEHOLDER_PREFIX])
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    logger.debug("  %s: %d existing placeholder(s)", label, len(data))
    for p in sorted(data, key=lambda e: e["startDate"]):
        logger.debug("    [%s] placeholder %s", label, fmt_range(p["startDate"], p["endDate"]))
    return data


def delete_events(calendar_id, events, label):
    data = run_jxa(
        "delete_events.js",
        [calendar_id, PLACEHOLDER_PREFIX, json.dumps(events)],
    )
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    deleted = data.get("deleted", 0)
    if deleted != len(events):
        logger.warning(
            "  %s: asked to delete %d placeholder(s) but only %d matched",
            label, len(events), deleted,
        )
    return deleted


def create_placeholder(calendar_id, start_iso, end_iso, label):
    data = run_jxa(
        "create_event.js",
        [calendar_id, PLACEHOLDER_PREFIX, start_iso, end_iso],
    )
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])


def sync_direction(source_events, dest_calendar_id, source_label, dest_label):
    """Make dest_calendar_id's placeholders match source_events, touching
    only what changed. Returns (created, deleted) counts."""
    logger.info("Syncing %s -> %s placeholders...", source_label, dest_label)

    wanted = {(ev["startDate"], ev["endDate"]) for ev in source_events}
    source_title_by_time = {(ev["startDate"], ev["endDate"]): ev["summary"] for ev in source_events}
    existing_placeholders = list_placeholders(dest_calendar_id, dest_label)
    existing = {(p["startDate"], p["endDate"]) for p in existing_placeholders}

    stale = [{"startDate": s, "endDate": e} for (s, e) in existing - wanted]
    missing = sorted(wanted - existing)

    if stale:
        logger.info("  deleting %d stale placeholder(s) from %s:", len(stale), dest_label)
        for s in sorted(stale, key=lambda e: e["startDate"]):
            logger.info("    - %s", fmt_range(s["startDate"], s["endDate"]))
    deleted = delete_events(dest_calendar_id, stale, dest_label) if stale else 0

    if missing:
        logger.info("  creating %d placeholder(s) on %s:", len(missing), dest_label)
    for start_iso, end_iso in missing:
        # Source title is only ever written to this local debug log, never
        # to the placeholder itself — the whole point of this tool is that
        # event content never crosses the calendar boundary.
        title = source_title_by_time.get((start_iso, end_iso), "?")
        logger.info("    + %s  (source: %r)", fmt_range(start_iso, end_iso), title)
        create_placeholder(dest_calendar_id, start_iso, end_iso, dest_label)

    return len(missing), deleted


# ------------------------------------ main -----------------------------------

def main():
    log_path = setup_logging()
    now = datetime.datetime.now().astimezone()
    logger.info("[%s] Reading events, this might take a few minutes... (details: %s)", now.strftime("%Y-%m-%d %H:%M %Z"), log_path)

    outlook_events = get_events(OUTLOOK_CALENDAR_ID, OUTLOOK_CALENDAR_LABEL, OUTLOOK_OWNER_EMAIL)
    google_events = get_events(GOOGLE_CALENDAR_ID, GOOGLE_CALENDAR_LABEL, GOOGLE_OWNER_EMAIL)

    created1, deleted1 = sync_direction(
        outlook_events, GOOGLE_CALENDAR_ID, OUTLOOK_CALENDAR_LABEL, GOOGLE_CALENDAR_LABEL
    )
    created2, deleted2 = sync_direction(
        google_events, OUTLOOK_CALENDAR_ID, GOOGLE_CALENDAR_LABEL, OUTLOOK_CALENDAR_LABEL
    )

    logger.info(
        "Done. %s: +%d/-%d placeholder(s). %s: +%d/-%d placeholder(s).",
        GOOGLE_CALENDAR_LABEL, created1, deleted1,
        OUTLOOK_CALENDAR_LABEL, created2, deleted2,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("ERROR: %s", exc)
        sys.exit(1)
