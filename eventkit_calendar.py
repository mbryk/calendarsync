#!/usr/bin/env python3
"""
eventkit_calendar.py

All Calendar.app access for this project: reading real events, and listing,
creating and deleting this script's own placeholders. Everything goes through
macOS's EventKit framework.

Run it directly to print each calendar's name and stable id, which is what you
need for calendar_sync.py's CONFIG block:

    .venv/bin/python eventkit_calendar.py

Why EventKit and not AppleScript/JXA
------------------------------------
Calendar.app's scripting interface exposes the RAW STORED OBJECTS. On an
Exchange account that means many objects under one uid (stale series clones,
detached single-occurrence overrides) with no field saying which occurrence an
override replaces, so the only way to pick the live copy was to guess from
`sequence`/`stampDate` — and both are confirmed not to track recency. It also
never exposed expanded occurrences, so RRULEs had to be parsed and expanded by
hand.

EventKit's eventsMatchingPredicate: returns the EXPANDED, ALREADY-RESOLVED
occurrence stream that Calendar.app's own UI draws, which removes both
problems. It also exposes `status()` for a real cancellation flag, and
`isDetached()`/`occurrenceDate()` (the RECURRENCE-ID that JXA had no
equivalent for).

Output contract
---------------
read_events() and list_placeholders() return dicts whose date fields are
formatted exactly like JavaScript's Date.toISOString()
("2026-11-12T15:00:00.000Z"). sync_direction() diffs source events against
existing placeholders by exact STRING equality, so a different-but-equivalent
spelling of the same instant (e.g. "+00:00", or dropping the milliseconds)
would make every placeholder look stale and get deleted and recreated on every
run. See iso_utc().

Permissions
-----------
Needs FULL calendar access, not write-only. A write-only process is handed one
fictional calendar with the id VIRTUAL_APP_CALENDAR_UUID and never sees the
real ones, with no error and no prompt — detected and reported explicitly
below, because it otherwise looks like an entitlement wall. No app bundle or
code signing is required; a plain python3 script with a TCC grant is enough.

Since nothing here shells out to osascript, macOS's *Automation* permission is
not involved at all — the Calendars privacy grant is the only one needed.
"""

import datetime
import logging
import threading

from EventKit import EKEvent, EKEventStore
from Foundation import NSDate

# EKEntityType / EKAuthorizationStatus / EKEventStatus / EKSpan /
# EKParticipantStatus constants, hardcoded rather than imported because the
# symbol names move around between SDK versions.
EK_ENTITY_TYPE_EVENT = 0

EK_AUTH_AUTHORIZED = 3
EK_AUTH_WRITE_ONLY = 4

EK_EVENT_STATUS_CANCELED = 3

# Placeholders are never recurring, so every write targets just the one event.
EK_SPAN_THIS_EVENT = 0

# predicateForEventsWithStartDate:endDate: degrades SILENTLY past a few years
# of span: it returns progressively FEWER events the wider the range gets, and
# eventually none, with no error. Measured on a live calendar 2026-11-12,
# events returned by total span: 1y=134, 2y=421, 3y=581, 4y=633, 6y=560,
# 8y=482, 10y=143, 12y=0 — and placeholders specifically went to 0 past ~8y.
# Nothing here asks for a range remotely near this, but a hand-edited
# SYNC_WINDOW_DAYS could, so it is checked rather than assumed.
MAX_PREDICATE_SPAN_DAYS = 1200

# The placeholder calendar macOS substitutes for a process that is not allowed
# to read real calendar data.
FAKE_CALENDAR_ID = "VIRTUAL_APP_CALENDAR_UUID"

# EKParticipantStatus -> the string read_events.js emitted for myStatus, so
# existing log output and any future consumer sees the same vocabulary.
#
# delegated/completed/in-process (5/6/7) are deliberately collapsed to
# "unknown": they are task-ish states, not answers to an invitation, and a real
# Exchange event here came back as 7 ("in process") where the scripting
# interface said "unknown" — so emitting them verbatim would put nonsense like
# "🔒 Busy [in process]" in front of a user if the placeholder
# status-flagging idea is ever built. "pending" is kept distinct because
# invited-but-unanswered is a genuine RSVP state that JXA could not express.
PARTICIPANT_STATUS = {
    0: "unknown",
    1: "pending",
    2: "accepted",
    3: "declined",
    4: "tentative",
    5: "unknown",
    6: "unknown",
    7: "unknown",
}

logger = logging.getLogger("calendarsync.eventkit")

_store = None


class CalendarAccessError(RuntimeError):
    """Calendar data could not be read or written (permissions, missing calendar)."""


# ----------------------------- dates -----------------------------------------

def iso_utc(nsdate):
    """NSDate -> the exact string JavaScript's Date.toISOString() produces.

    Byte-for-byte parity matters here, not just semantic equality — see this
    module's docstring.
    """
    dt = datetime.datetime.fromtimestamp(
        nsdate.timeIntervalSince1970(), datetime.timezone.utc
    )
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def to_nsdate(value):
    """datetime, or an ISO string as produced by iso_utc(), -> NSDate."""
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value)
    return NSDate.dateWithTimeIntervalSince1970_(value.timestamp())


# ----------------------------- access ----------------------------------------

def request_full_access(store, timeout=120):
    """Trigger the TCC prompt and block until it is answered.

    Returns (granted, error_string, timed_out). The completion handler fires on
    an arbitrary queue, so a threading.Event is enough — no run loop needed.
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


def open_store():
    """Return a process-wide authorized EKEventStore, or raise CalendarAccessError.

    Cached: opening a store is not free, and reusing one is what makes batching
    writes behind a single commit possible.
    """
    global _store
    if _store is not None:
        return _store

    store = EKEventStore.alloc().init()
    status = EKEventStore.authorizationStatusForEntityType_(EK_ENTITY_TYPE_EVENT)

    if status != EK_AUTH_AUTHORIZED:
        if status == EK_AUTH_WRITE_ONLY:
            logger.debug("  calendar access is write-only; requesting full access")
        granted, error, timed_out = request_full_access(store)
        if timed_out:
            raise CalendarAccessError(
                "Timed out waiting for the macOS calendar permission prompt. "
                "macOS shows the prompt to whatever launched this process, so a "
                "prompt raised under a non-interactive parent (e.g. launchd) may "
                "never be displayed. Run this once by hand from Terminal to "
                "grant access; scheduled runs will work afterwards."
            )
        if not granted:
            raise CalendarAccessError(
                "Full calendar access was denied"
                + (f" ({error})" if error else "")
                + ". Grant it in System Settings > Privacy & Security > "
                "Calendars. Write-only access is NOT enough to read events."
            )

    _store = store
    return _store


def resolve_calendar(store, calendar_id):
    """Look up one calendar by its stable identifier.

    EventKit's calendarIdentifier is the same namespace Calendar.app uses, so
    calendar_sync.py's CONFIG ids work verbatim.
    """
    calendar = store.calendarWithIdentifier_(calendar_id)
    if calendar is not None:
        return calendar

    visible = store.calendarsForEntityType_(EK_ENTITY_TYPE_EVENT) or []
    if any(str(c.calendarIdentifier()) == FAKE_CALENDAR_ID for c in visible):
        raise CalendarAccessError(
            "This process only has WRITE-ONLY calendar access: macOS is "
            "returning one fictional calendar (VIRTUAL_APP_CALENDAR_UUID) "
            "instead of the real ones. Remove this script's entry under System "
            "Settings > Privacy & Security > Calendars and re-run to be "
            "re-prompted for full access."
        )
    names = ", ".join(f"{c.title()!r}={c.calendarIdentifier()}" for c in visible)
    raise CalendarAccessError(
        f"Calendar not found: {calendar_id}. Visible calendars: {names or 'none'}"
    )


# ----------------------------- queries ---------------------------------------

def occurrences_between(store, calendar, start, end):
    """Resolved occurrences on one calendar overlapping [start, end]."""
    span_days = (end - start).days
    if span_days > MAX_PREDICATE_SPAN_DAYS:
        raise ValueError(
            f"Refusing to query a {span_days}-day range: EventKit silently "
            f"returns incomplete results past roughly {MAX_PREDICATE_SPAN_DAYS} "
            "days of span (see MAX_PREDICATE_SPAN_DAYS). Narrow the window."
        )
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        to_nsdate(start), to_nsdate(end), [calendar]
    )
    events = store.eventsMatchingPredicate_(predicate) or []
    return sorted(events, key=lambda e: e.startDate().timeIntervalSince1970())


def read_events(calendar_id, placeholder_prefix, window_days, owner_email=""):
    """Resolved real occurrences on one calendar, from now to +window_days.

    Excludes this script's own placeholders and canceled events.
    """
    owner_email = (owner_email or "").lower()
    store = open_store()
    calendar = resolve_calendar(store, calendar_id)

    start = datetime.datetime.now()
    occurrences = occurrences_between(
        store, calendar, start, start + datetime.timedelta(days=window_days)
    )
    logger.debug("  EventKit returned %d occurrence(s)", len(occurrences))

    result = []
    seen = set()
    skipped_placeholder = skipped_canceled = skipped_duplicate = 0

    for event in occurrences:
        summary = str(event.title() or "")

        if summary.startswith(placeholder_prefix):
            skipped_placeholder += 1
            continue

        # status() is the real cancellation flag JXA never exposed. The title
        # check stays as a belt-and-braces second test: Exchange renames a
        # canceled occurrence to "Canceled: <title>", and it is not confirmed
        # that every such event also carries status == canceled.
        if event.status() == EK_EVENT_STATUS_CANCELED or summary.startswith(
            ("Canceled:", "CANCELED:")
        ):
            skipped_canceled += 1
            continue

        occurrence = {
            "summary": summary,
            "startDate": iso_utc(event.startDate()),
            "endDate": iso_utc(event.endDate()),
            "allDay": bool(event.isAllDay()),
            # EventKit's name for uid, and the real iCalendar UID rather than
            # Calendar.app's local one. For a detached (single-occurrence)
            # override it carries a "/RID=<epoch>" suffix identifying which
            # occurrence it replaces. Diagnostic only; nothing dedupes on it.
            "uid": str(event.calendarItemExternalIdentifier() or ""),
        }

        # EventKit resolves the clone/override collisions that made deduping
        # necessary in the first place, so a non-zero count here is worth
        # looking at rather than shrugging off.
        key = (occurrence["summary"], occurrence["startDate"], occurrence["endDate"])
        if key in seen:
            skipped_duplicate += 1
            continue
        seen.add(key)

        occurrence["myStatus"] = owner_status(event, owner_email)
        result.append(occurrence)

    logger.debug(
        "  skipped %d placeholder(s), %d canceled, %d duplicate(s)",
        skipped_placeholder, skipped_canceled, skipped_duplicate,
    )
    if skipped_duplicate:
        logger.warning(
            "  %d occurrence(s) were identical in (summary, start, end) — "
            "EventKit was expected to resolve these",
            skipped_duplicate,
        )
    return result


def owner_status(event, owner_email):
    """This machine's owner's RSVP status for one occurrence, or None.

    None means no attendee matched owner_email — including the common case of
    an event with no attendees at all (something you put on your own calendar).
    """
    if not owner_email:
        return None
    # attendee_email() always lowercases, so the configured address has to be lowered too. 
    # Mail providers do not treat local-parts as case-sensitive, whatever RFC 5321 permits.
    owner_email = owner_email.lower()
    for attendee in event.attendees() or []:
        if attendee_email(attendee) == owner_email:
            return PARTICIPANT_STATUS.get(attendee.participantStatus(), "unknown")
    return None


def attendee_email(attendee):
    """Lowercased email for an EKParticipant, or '' if it has none.

    Matching is by email because isCurrentUser() is not trustworthy here — on a
    real Exchange event it flagged the organizer rather than the account owner.
    emailAddress is also not present on every macOS version, so fall back to the
    participant's mailto: URL, which always is.
    """
    try:
        email = attendee.emailAddress()
        if email:
            return str(email).lower()
    except AttributeError:
        pass
    url = attendee.URL()
    if url is None:
        return ""
    return str(url.resourceSpecifier() or "").lower()


# ----------------------------- placeholders ----------------------------------

def find_placeholders(store, calendar, placeholder_prefix, lookback_days, window_days):
    """This script's own placeholder EKEvents on one calendar.

    Scanned over [now - lookback_days, now + window_days] rather than all of
    time, so a placeholder stays eligible for cleanup for a while after its
    event has passed and is then left in place as history.
    """
    start = datetime.datetime.now() - datetime.timedelta(days=lookback_days)
    end = datetime.datetime.now() + datetime.timedelta(days=window_days)
    return [
        event
        for event in occurrences_between(store, calendar, start, end)
        if str(event.title() or "").startswith(placeholder_prefix)
    ]


def list_placeholders(calendar_id, placeholder_prefix, lookback_days, window_days):
    """[{startDate, endDate}] for this script's placeholders on one calendar."""
    store = open_store()
    calendar = resolve_calendar(store, calendar_id)
    return [
        {"startDate": iso_utc(e.startDate()), "endDate": iso_utc(e.endDate())}
        for e in find_placeholders(
            store, calendar, placeholder_prefix, lookback_days, window_days
        )
    ]


def create_placeholders(calendar_id, title, time_ranges):
    """Create one placeholder per (start_iso, end_iso). Returns the count.

    Every event is staged with commit=False and written by a single commit at
    the end, so N placeholders cost one trip to the calendar store instead of N.
    """
    if not time_ranges:
        return 0

    store = open_store()
    calendar = resolve_calendar(store, calendar_id)

    for start_iso, end_iso in time_ranges:
        event = EKEvent.eventWithEventStore_(store)
        event.setTitle_(title)
        event.setStartDate_(to_nsdate(start_iso))
        event.setEndDate_(to_nsdate(end_iso))
        event.setCalendar_(calendar)
        ok, error = store.saveEvent_span_commit_error_(
            event, EK_SPAN_THIS_EVENT, False, None
        )
        if not ok:
            store.reset()
            raise CalendarAccessError(
                f"Failed to stage placeholder {start_iso} -> {end_iso}: {error}"
            )

    commit(store)
    return len(time_ranges)


def delete_placeholders(
    calendar_id, placeholder_prefix, time_ranges, lookback_days, window_days
):
    """Delete this script's placeholders at exactly these (start, end) times.

    Matched by title prefix AND exact start/end, so it can only ever remove
    events this script created at a time it was told to remove.
    """
    if not time_ranges:
        return 0

    store = open_store()
    calendar = resolve_calendar(store, calendar_id)
    wanted = {(start, end) for start, end in time_ranges}

    deleted = 0
    for event in find_placeholders(
        store, calendar, placeholder_prefix, lookback_days, window_days
    ):
        if (iso_utc(event.startDate()), iso_utc(event.endDate())) not in wanted:
            continue
        ok, error = store.removeEvent_span_commit_error_(
            event, EK_SPAN_THIS_EVENT, False, None
        )
        if not ok:
            store.reset()
            raise CalendarAccessError(
                f"Failed to stage deletion of {iso_utc(event.startDate())}: {error}"
            )
        deleted += 1

    commit(store)
    return deleted


def commit(store):
    ok, error = store.commit_(None)
    if not ok:
        store.reset()
        raise CalendarAccessError(f"Failed to commit calendar changes: {error}")


# ----------------------------- operator tool ---------------------------------

def main():
    """Print every calendar's name and stable id, for calendar_sync.py's CONFIG.

    Replaces list_calendar_ids.applescript, which needed an error-message
    parsing trick because reading a calendar's uid throws in JXA.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = open_store()
    calendars = store.calendarsForEntityType_(EK_ENTITY_TYPE_EVENT) or []
    print(f"{len(calendars)} calendar(s):\n")
    for calendar in sorted(calendars, key=lambda c: str(c.title() or "")):
        source = calendar.source().title() if calendar.source() else "?"
        writable = "" if calendar.allowsContentModifications() else "  [read-only]"
        print(f"  {str(calendar.title() or '?')!r} [{source}]{writable}")
        print(f"    {calendar.calendarIdentifier()}")
    print(
        "\nPaste the ids above into OUTLOOK_CALENDAR_ID / GOOGLE_CALENDAR_ID in\n"
        "calendar_sync.py. Two calendars can share a display name (Exchange "
        "overwrites\nlocal names with the server's folder name), so match by id, "
        "not by title."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
