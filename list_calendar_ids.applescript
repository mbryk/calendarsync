-- One-time helper: prints each Calendar.app calendar's name alongside its
-- stable internal id, so you can put the id (not the name) into
-- calendar_sync.py's CONFIG block. Needed because JXA's own `uid` property
-- getter is broken on calendars (throws "AppleEvent handler failed"), so we
-- extract the id from the error message AppleScript raises when coercing a
-- calendar reference to text instead.
--
-- Also prints the titles of the next couple of upcoming events on each
-- calendar, since multiple calendars can share the same display name (e.g.
-- an Exchange calendar Apple keeps renaming back to "Calendar") and the
-- upcoming events are usually the fastest way to tell them apart.
--
-- Usage: osascript list_calendar_ids.applescript

on run
	set ATID to AppleScript's text item delimiters
	set AppleScript's text item delimiters to {"id \"", "\" of application \"Calendar\" into type text."}
	tell application "Calendar"
		set output to ""
		repeat with aCalendar in calendars
			try
				set theID to uid of aCalendar
			on error number -10000
				try
					aCalendar as text
					set theID to "<unknown>"
				on error errorMessage
					set theID to text item 2 of errorMessage
				end try
			end try

			set upcoming to (events of aCalendar whose start date > (current date) and start date < ((current date) + 7 * days))
			set preview to ""
			repeat with i from 1 to 2
				if i <= (count of upcoming) then
					if preview is not "" then set preview to preview & "; "
					set preview to preview & (summary of item i of upcoming)
				end if
			end repeat
			if preview is "" then set preview to "(no upcoming events)"

			set output to output & (name of aCalendar) & "  ->  " & theID & "  [" & preview & "]" & linefeed
		end repeat
	end tell
	set AppleScript's text item delimiters to ATID
	return output
end run
