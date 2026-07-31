# Calendar Sync (Outlook ↔ Google, busy/free only)

Keeps your macOS-Calendar-based Outlook calendar and your macOS-Calendar-based
Google calendar mutually blocked. It never copies event titles, attendees,
locations, or descriptions — only opaque "🔒 Busy" placeholder events marking
the time as taken. This is what makes it safe to use even though your Outlook
calendar isn't allowed to be exported: no meeting content ever leaves either
system.

Both calendars are scripted through Calendar.app (via JXA/`osascript`) — there
is no Google API, no OAuth credentials, and no cloud project involved, so
there's nothing to cost you anything.

## 1. Add your Google account to Calendar.app

1. Open System Settings > Internet Accounts (or Calendar > Settings >
   Accounts) and add your Google account, with Calendar syncing enabled.
2. It will now show up as a calendar in Calendar.app's sidebar, alongside
   your Outlook calendar (Calendar.app > View > Show Calendar List).

## 2. Configure the script

Open `calendar_sync.py` and edit the CONFIG block near the top:

- `OUTLOOK_CALENDAR_NAME` — the exact name of your Outlook calendar as shown
  in Calendar.app's sidebar.
- `GOOGLE_CALENDAR_NAME` — the exact name of your Google calendar as shown
  in Calendar.app's sidebar.
- `SYNC_WINDOW_DAYS` — how far ahead to keep synced (default 14).

## 3. Run it once by hand

```bash
cd calendarsync
python3 calendar_sync.py
```

The first run will trigger a macOS permission prompt asking to let
Terminal/Python control Calendar.app — approve it (or check System Settings >
Privacy & Security > Automation if you miss the dialog).

Check both calendars — you should see "🔒 Busy" placeholders appear for each
real event on the other side. Run it a second time and confirm it doesn't
duplicate them (it wipes and recreates cleanly).

## 4. Automate it with launchd

1. Edit `com.you.calendarsync.plist`:
   - Replace `/Users/YOURNAME/calendar-sync/...` with the actual path to
     wherever you put this folder.
   - Adjust `StartInterval` (in seconds) if you want a different frequency
     — 10800 = every 3 hours. For once a day, use 86400, or switch to
     `StartCalendarInterval` for a fixed time (e.g. 7am) — see `man launchd.plist`.
2. Install it:
   ```bash
   cp com.you.calendarsync.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.you.calendarsync.plist
   ```
3. Check `sync.log` in this folder after a bit to confirm it's running.

To stop it later:
```bash
launchctl unload ~/Library/LaunchAgents/com.you.calendarsync.plist
```

## Notes / gotchas

- **Calendar.app scripting can be slow** on calendars with a huge amount of
  history — the read script only looks forward from "now", which keeps it
  reasonably fast, but very large calendars may still take a few seconds.
- **Google sync lag**: since Calendar.app syncs Google via Apple's own CalDAV
  refresh cycle (not a live API call), a newly created event on either side
  may take a few minutes to show up before this script can see it. Fine for
  a "block time on my calendar" use case.
- **All-day events**: currently included and mirrored as all-day placeholders
  where the source is all-day. If that's too aggressive (e.g. you don't want
  a single "OOO" day blocking your whole calendar), you can filter those out
  in `read_events.js`.
- **Deleting the script's placeholders manually is safe** — the next run
  just recreates whatever's still needed.
- **Don't rename `PLACEHOLDER_PREFIX`** after the first run without manually
  cleaning up old placeholders created under the previous prefix, since the
  script uses it to identify (and safely delete) only its own events.
