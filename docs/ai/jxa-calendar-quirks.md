# Calendar.app quirks (EventKit and AppleScript/JXA)

Reference doc, not orientation — only read this if you're about to write or debug code that
touches `Calendar.app`. Linked from `docs/ai/README.md`. These cost real time to discover and
aren't documented anywhere Apple publishes.

Two sections. **EventKit** is the live code path — read that part. **AppleScript / JXA** is kept
for the case where someone reintroduces `osascript` here (an operator script, a property EventKit
doesn't expose); it describes landmines in an interface this project no longer uses, and several
bullets name `.js` files that no longer exist.

## EventKit (the live code path)

- **`predicateForEventsWithStartDate:endDate:` silently returns FEWER results the wider the range
  gets, and eventually none.** There is no error, no warning, and no documented limit. Measured
  2026-11-12 on one live calendar, total events returned by span: `1y=134`, `2y=421`, `3y=581`,
  `4y=633`, `6y=560`, `8y=482`, `10y=143`, `12y=0` — it peaks around a 4-year span and decays
  from there. Worse for this project's purposes, the *placeholder* count specifically went from
  24 to 0 once the span passed ~8 years. So a "just query everything" range is not merely slow,
  it quietly returns wrong answers. Keep any single predicate well under ~3 years of span
  (`MAX_PREDICATE_SPAN_DAYS` in `eventkit_calendar.py` enforces this and raises rather than
  returning a truncated result); if a genuinely wide range is ever needed, split it into chunks
  and union the results.
- **`EKEventStore` returning one calendar named `"Calendar"` with the id
  `"VIRTUAL_APP_CALENDAR_UUID"` means the process has WRITE-ONLY calendar access, not that it
  needs to be an entitled app bundle.** macOS 14 split calendar permission into write-only and
  full access; a write-only process is handed a single fictional calendar to add events to and is
  never shown the real ones. There is no error and no permission prompt, so it reads as an
  entitlement wall when it is really just the wrong request.
  `authorizationStatusForEntityType_(0)` returns `4` (writeOnly) in this state; calling
  `requestFullAccessToEventsWithCompletion_` raises a normal TCC prompt and, once granted,
  returns every real calendar. **Confirmed 2026-11-09 from a plain `python3` script (PyObjC,
  `pyobjc-framework-EventKit`) — no app bundle, no code signing, and no Apple Developer account
  is required to read real calendar data.** Once full access is granted, two of the returned
  `calendarIdentifier`s match `calendar_sync.py`'s `OUTLOOK_CALENDAR_ID`/`GOOGLE_CALENDAR_ID`
  exactly — EventKit and Calendar.app's scripting interface share one identifier namespace.
- **EventKit returns resolved occurrences, which is why none of the same-`uid` mess in the
  AppleScript section applies to it.** `eventsMatchingPredicate_` hands back the expanded,
  already-deduplicated occurrence stream that Calendar.app's own UI draws — not the raw stored
  objects the scripting interface exposes. It also exposes the fields JXA had no equivalent for:
  `isDetached()` (this object is a single-occurrence override), `occurrenceDate()` (which
  occurrence it replaces, i.e. `RECURRENCE-ID`), and `status()` for an explicit `canceled` flag
  instead of matching `"Canceled:"` in titles. Measured 2026-11-09 over a live 14-day window: 43
  occurrences, and the only `uid`s returning more than one occurrence were legitimate weekly
  repeats — zero phantom clones, on the same calendar where bulk JXA fetch reports up to 19
  objects under one `uid`.
- **`EKParticipant.isCurrentUser()` is not trustworthy — match attendees by email instead.** On a
  real Exchange event with 13 attendees it returned true for the *organizer*, not for this
  machine's account owner. `eventkit_calendar.py`'s `owner_status()` matches
  `OUTLOOK_OWNER_EMAIL`/`GOOGLE_OWNER_EMAIL` against `emailAddress()` (falling back to the
  participant's `mailto:` URL, since `emailAddress` is absent on some macOS versions).
  **Case-fold both sides of that comparison.** Exchange returns the owner's address capitalized
  (`Mark.Bryk@Innovation.nj.gov`), so comparing it against a lowercased attendee address matches
  nothing — and the failure is silent, because "no attendee matched" is also the legitimate
  result for an event you created yourself, so every event simply reports no status.
- **`EKParticipant.participantStatus()` frequently returns a task state rather than an RSVP
  answer on Exchange events, and there is no flag distinguishing the two.** Alongside the
  expected pending/accepted/declined/tentative (1-4) you get delegated/completed/in-process
  (5/6/7) — a real Exchange event here reported `7` ("in process"), where Calendar.app's
  scripting interface had said `"unknown"`. These are answers to a *task*, not to an invitation,
  so `PARTICIPANT_STATUS` in `eventkit_calendar.py` collapses 5/6/7 (and 0) to `"unknown"`.
  Measured over a live 14-day window: 21 of 26 Exchange events landed on `"unknown"` this way,
  and only 2 reported a real `accepted`. Anything built on RSVP status therefore has to treat
  "unknown" as the common case and degrade gracefully, not as a rare edge case — don't design a
  feature that assumes a usable status is normally available on an Exchange calendar.
- **`calendarItemExternalIdentifier` is the real iCalendar UID, which is NOT the same string
  Calendar.app's scripting interface calls `uid`.** For a Google event it looks like
  `1n48un6v8jnnieph3hids3rkhn@google.com`, where JXA reported a local UUID
  (`9B125AF8-...`) for the same meeting. For a detached single-occurrence override it also
  carries a `/RID=<epoch>` suffix identifying which occurrence it replaces. Don't try to
  correlate the two forms across old and new log output.
- **PyObjC turns Cocoa `error:` out-parameters into extra return values.** `saveEvent:span:commit:error:`,
  `removeEvent:span:commit:error:` and `commit:` are called with `None` for the error argument and
  return a `(ok, error)` tuple — a truthiness check on the single return value would always pass
  and silently swallow every write failure.
- **Use `EKSpanThisEvent` (0) for placeholder writes.** `EKSpanFutureEvents` (1) on a recurring
  event deletes or rewrites the whole remaining series. Placeholders are never recurring, so
  there is no case here where the wider span is correct.
- **Install PyObjC into a virtualenv, not system python.** Both Homebrew's and Apple's Python are
  marked `EXTERNALLY-MANAGED` (PEP 668), so `pip install --user pyobjc-framework-EventKit` is
  refused outright. `python3 -m venv` works on both, which also makes it the more portable setup
  instruction for a second machine.
- **Nothing here needs macOS's Automation permission.** That permission governs one app driving
  another via Apple Events (`osascript` → `Calendar.app`). EventKit talks to the calendar store
  directly, so the Calendars (TCC) grant is the only one involved. Reintroducing a single
  `osascript` call anywhere in the normal run would bring the second prompt back.

## AppleScript / JXA (historical — only if `osascript` is reintroduced)

- **`sdef` needs full Xcode, not just Command Line Tools.** `sdef /System/Applications/Calendar.app`
  errors out with "requires Xcode" on a CLT-only machine. There is no fast way to read Calendar.app's
  scripting terminology on such a machine — you have to discover property names by probing
  candidates in a `try`/`catch` loop (see the pattern in `list_calendar_ids.applescript` and the
  probing done for `read_events.js`'s `uid`/`sequence` fields).
- **Calendar-level `uid` is broken in JXA; event-level `uid` is fine.** `app.calendars.byId(...)`
  works, but calling `.uid()` on a *calendar* object throws `"AppleEvent handler failed."` — every
  time, on every calendar, no exceptions found. Calling `.uid()` (or `.sequence()`, `.stampDate()`)
  on an *event* object works fine, including bulk-fetched across `cal.events`. Don't assume the
  calendar-level bug generalizes to events, and don't assume event-level `uid` access implies
  calendar-level `uid` access will also work.
  - `properties()` on a calendar object is *also* broken (same "AppleEvent handler failed"), so
    you can't work around the `uid` bug by grabbing everything at once and picking through it.
  - Workaround for getting a calendar's real id from AppleScript (not JXA): force a coercion to
    text and parse the id out of the resulting error message — see
    `list_calendar_ids.applescript`. This only works in classic AppleScript, not JXA (JXA's
    `.toString()` on a calendar just returns `"[object Function]"`, no useful error).
  - Once you have the id (from the AppleScript trick above), `app.calendars.byId(id)` in JXA works
    fine for actually using it — the bug is specifically in *reading* `uid`, not in looking things
    up *by* id.
- **`app.calendars.byId(id)` never throws for an unknown id**, and `.exists()` is not a reliable
  check either (observed returning `true` for a made-up id). The only reliable "not found" signal
  found was `!cal.name()` returning falsy/empty — real calendars never have an empty name.
- **Multiple raw entries sharing one `uid` is the general shape of Exchange's calendar-object
  weirdness**, not just a recurrence-specific glitch. The concrete case found: rescheduling a
  recurring series "this and future occurrences" in Outlook leaves an old, still-indefinitely-
  recurring phantom copy of the series behind locally (same `uid`, `sequence` stuck at `0`,
  original pre-reschedule start time), alongside the real current series (higher `sequence`).
  Whenever you're deduping Exchange-sourced events, dedupe by `uid` + max `sequence` first — don't
  rely on `(summary, start, end)` equality, since the whole problem is that the duplicates have
  *different* start/end times.
- **Bulk property fetch (`cal.events.summary()` etc. across the whole collection) is ~60x faster
  than per-event `.summary()` calls in a loop**, and is required for anything beyond trivial
  calendars — see `read_events.js`. This is already implemented; if you add a new property to
  read, fetch it in bulk alongside the existing ones, don't loop.
- **Bound any `whose(...)` date-range query you write** (e.g. in a new operator/debug script) —
  an unbounded `events whose start date > (current date)` against a calendar with a large or
  long-running history (Birthdays, Holidays calendars especially) is slow enough to feel hung.
  `list_calendar_ids.applescript` bounds its upcoming-events preview to the next 7 days for this
  reason.
- **Per-item property access is unreliable when multiple event objects share a `uid`; bulk
  fetch is not.** Exchange can leave many stale copies of a series behind under one `uid` (not
  just one phantom — one real calendar had 19 objects sharing a `uid`, spanning 7 distinct
  `sequence` values as the series got edited repeatedly over time). Bulk-fetching a property
  across the whole collection (`cal.events.startDate()`) correctly returns each object's own
  distinct value at its index. But materializing `cal.events()` once and then calling
  `events[i].startDate()` (or `.recurrence()`, `.sequence()`) per item returns the **same**
  value — belonging to the highest-`sequence` ("live") copy — for every index that shares that
  `uid`, regardless of which object index `i` actually points to. Confirmed empirically: 19
  indices with one `uid`, 19 distinct correct values from bulk fetch, but per-item access
  returned the live copy's date at all 19. Don't use per-item access as a "ground truth" to
  sanity-check bulk fetch's index alignment — bulk fetch is the reliable one here; per-item
  access is what's aliasing. This is the same family of bug as the calendar-level `uid`
  brokenness above, just at the event-property-cache level instead.
- **`sequence` does not track recency across an object's lifetime — don't treat "highest
  `sequence` wins" as "most current" when multiple objects share a `uid`.** `sequence` is a count
  of edits made to that specific object, not a wall-clock time. It works for the case
  `read_events.js` was built to handle (a long-lived series accumulates edits and ends up with a
  high `sequence`; an abandoned phantom copy left behind at the moment of a "this and future
  occurrences" split is frozen at whatever it was, often `0` — confirmed live: `ResX Monthly
  Meeting` has 5 dead clones at `sequence: 0` and one live copy at `sequence: 16`, all one `uid`).
  It fails for a single-occurrence reschedule: a freshly detached override object can have a
  *lower* `sequence` than the sibling it's replacing, because it's its own object with its own
  short edit history, not a continuation of the master's count. Confirmed live: rescheduling
  `Digital RFI Grooming`'s and `Lizzy <> Mark`'s next occurrence produced a same-`uid` sibling
  with the new (correct) time at `sequence: 8`, while the stale sibling at the old time sat at
  `sequence: 14` / `13` respectively — max-`sequence` picks the *wrong* one in both cases.
- **`stampDate` (DTSTAMP) is a better recency signal than `sequence` in principle, but is
  unreliably populated — don't treat it as a safe drop-in replacement either.** For `Digital RFI
  Grooming`'s reschedule above, max-`stampDate` does correctly pick the current copy (later real
  timestamp on the moved-time sibling). But for `Lizzy <> Mark`'s reschedule, the sibling with the
  new (correct) time has `stampDate: null` while the stale sibling has a real timestamp — max-
  `stampDate`-treating-null-as-oldest picks the *wrong* one, same failure mode as `sequence`, just
  triggered by missing data instead of a low count. Several of the dead `ResX Monthly Meeting`
  clones also have `stampDate: null` while others in the same dead group have real timestamps
  clustered within a 3-second window (looks like a bulk local resync burst, not genuine edit
  times) — don't assume `stampDate` is populated consistently across same-`uid` siblings, or that
  its presence/absence itself is a meaningful signal.
- **`myStatus` (attendee `participationStatus` for a given owner email) is not a discriminator
  for same-`uid` collisions either — at least not on its own.** Added specifically to probe for a
  fix to the `sequence`/`stampDate` dead end above. First real data (5 collisions logged via the
  new `COLLISION` diagnostic in `read_events.js`): `myStatus` was identical on both the primary
  and secondary side in every single case (`"accepted"`/`"accepted"`, `"unknown"`/`"unknown"`,
  `null`/`null`) — makes sense in retrospect, since both sides are the same underlying person's
  RSVP to (functionally) the same meeting series, just captured on two different object copies.
  Kept in the diagnostic output anyway in case a future case shows a split, but don't expect it to
  resolve this alone.
- **`cal.events.attendees()` bulk-fetched across the whole collection times out** (`AppleEvent
  timed out.` (-1712)), even on a calendar with well under 200 events — unlike the scalar
  properties (`summary`, `startDate`, etc.), attendee lists can't be bulk-fetched the same way.
  Per-item `event.attendees()` (after materializing `cal.events()` once) works fine and returns
  correctly-aligned data (~2s/event observed) — the per-item aliasing bug documented above is
  specific to scalar properties on same-`uid` indices, not to nested attendee sub-objects. Keep
  per-item attendee fetches scoped to only the events that survive filtering (a handful), not the
  whole collection, to bound the cost. `attendee` objects expose `.email()`, `.displayName()`, and
  `.participationStatus()` (values seen: `"accepted"`, `"declined"`, `"unknown"`) — candidates
  like `.status()`, `.role()`, `.rsvp()`, `.attendeeStatus()` all throw.
- **Classic AppleScript's `properties of every event of cal` returns one atomically-grouped
  record per event in a single Apple Event round trip** (no cross-array alignment risk by
  construction, unlike JXA's per-property bulk arrays) — confirmed this actually works:
  ```applescript
  tell application "Calendar"
      set cal to calendar id "3F5D9A23-9D3A-42F2-95B3-7AC25596D995"
      properties of every event of cal
  end tell
  ```
  But it's slower: **32s vs. 19.5s** for the current per-property-array bulk fetch
  (`evSpec.summary()`, `evSpec.startDate()`, etc., one call per property) on the same 193-event
  calendar — roughly 1.6x. It's also JXA-incompatible: `cal.events.properties()` in JXA throws
  `Invalid key form (-10002)`; the trick only exists in AppleScript's object model. Using it would
  mean rewriting the fetch in AppleScript (or shelling out to it from JXA and parsing the result)
  — a real cost, not a drop-in swap, so only worth it if a *confirmed* alignment bug ever
  justifies the atomicity guarantee. (It doesn't currently — see the per-item-access bullet above;
  bulk arrays are the reliable side of that bug, not the risky one.)
- **`cal.events.whose({startDate: {_greaterThan: ...}})` silently drops some recurring masters,
  not just old ones.** This isn't only a performance footgun — it's a correctness one. A recurring
  master's raw `startDate` is always its original series-start date, which is normally in the
  past; `whose()` sometimes still includes such a master (apparently treating it as "has an
  occurrence past the filter date") and sometimes doesn't, for no discernible rule-based reason
  (confirmed two near-identical weekly/monthly recurring series in the same calendar, one included
  one excluded). No error is thrown either way. `read_events.js` no longer pre-filters with
  `whose()` for this reason — it fetches the whole collection and filters in JS by expanded
  occurrence date instead. Don't reintroduce a `whose()` date filter on `events` as an
  optimization without re-verifying this against real recurring data.
- **Moving a single occurrence of a recurring event does not reliably populate the master's
  `excludedDates()`.** Dragging one occurrence of a recurring series to a new date/time in
  Calendar.app creates a detached standalone event (own `startDate`, `recurrence` is
  empty/missing) for that occurrence — but the master series was observed to still report
  `excludedDates(): []`, i.e. the original slot is *not* marked excluded. Naively expanding the
  master's RRULE will regenerate that stale original occurrence in addition to the real moved one
  — a ghost duplicate. Don't trust `excludedDates()` alone to account for moved occurrences;
  cross-check for detached events sharing the series' summary near the expected occurrence window.
- **`console.log` inside a JXA script invoked via `osascript` writes to stderr, not stdout** —
  only the script's actual return value lands on stdout. Confirmed empirically (2026-09-25):
  `read_events.js`'s diagnostic lines (`UIDGROUP`/`SUPPRESSED`) survived `1>/dev/null` but
  vanished under `2>/dev/null`. This is invisible when running `osascript` directly in a terminal
  (both streams merge onto the same tty), but bites any caller that captures stdout/stderr
  separately, e.g. Python's `subprocess.run(..., capture_output=True)` — `calendar_sync.py`'s
  `run_jxa()` was only reading `result.stdout` and silently dropping every diagnostic line for
  this reason until fixed. Any future JXA script that wants diagnostic output visible to a Python
  (or other subprocess) caller must have that caller read `result.stderr`, not `result.stdout`.
- **`osascript` kills the process (SIGKILL) if a single command-line argument is longer than
  ~995 characters — well below macOS's own `ARG_MAX` (~1MB) and unrelated to Calendar.app.**
  Confirmed empirically (2026-09-29) via `delete_events.js`, which used to receive a whole
  JSON-encoded array of `{startDate, endDate}` as one argv string: a 984-char payload (12 items)
  succeeded; a 1066-char payload (13 items) got SIGKILLed (`exit=137`), reproducing identically
  whether the command was pasted into an interactive shell or run from a script file — ruling out
  a TTY/paste-length artifact. Hitting real `ARG_MAX` instead fails at `execve`/`subprocess.run()`
  itself (`OSError: Argument list too long`) and never produces a completed process to SIGKILL, so
  don't mistake a fast silent `exit=-9`/`exit=137` with empty stderr for that — it's this much
  smaller per-argument limit. Fix: never pass a variable-length bulk payload (JSON array, long
  string, etc.) as a raw argv value. Write it to a temp file and pass the file *path* as the argv
  instead; read it back in the JXA script via the ObjC/Foundation bridge:
  `ObjC.import('Foundation'); $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js`
  (note: JXA's ObjC method-name mapping removes the selector's colons and camel-cases what
  follows — `stringWithContentsOfFile:encoding:error:` becomes
  `stringWithContentsOfFileEncodingError`, not `stringWithContentsOfFile_encoding_error`).
  `delete_events.js`/`calendar_sync.py`'s `delete_events()` use this pattern now — any new script
  taking a bulk/variable-length payload should follow it too rather than reintroducing a raw argv
  string.
