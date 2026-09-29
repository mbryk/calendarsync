# JXA / Calendar.app scripting quirks

Reference doc, not orientation — only read this if you're about to write or debug a new
`.js` or `.applescript` file that drives `Calendar.app`. Linked from `docs/ai/README.md`.

These cost real time to discover and aren't documented anywhere Apple publishes.

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
- **EventKit via the AppleScript ⟷ Objective-C bridge (`use framework "EventKit"`) does not give
  real calendar identifiers from a bare `osascript` process.** `EKEventStore`'s
  `calendarIdentifier` comes back as the literal string `"VIRTUAL_APP_CALENDAR_UUID"` for every
  calendar — this is macOS's privacy placeholder for a process that isn't an entitled, properly
  `NSCalendarsUsageDescription`-declared app bundle. It looks like it's working (no error, no
  permission prompt) but the data is fake. This approach is faster and cleaner *in general* (per
  reports elsewhere), but not usable here — stick with driving `Calendar.app` itself via
  `Application('Calendar')` (JXA) / `tell application "Calendar"` (AppleScript).
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
