// Usage: osascript -l JavaScript read_events.js "<CalendarName>" "<PlaceholderPrefix>" <daysAhead>
// Prints JSON array of { summary, startDate, endDate, allDay } for real (non-placeholder) events.

function run(argv) {
  var app = Application('Calendar');
  var calName = argv[0];
  var prefix = argv[1];
  var daysAhead = parseInt(argv[2]) || 14;

  var cals = app.calendars.whose({ name: calName });
  if (cals.length === 0) {
    return JSON.stringify({ error: "Calendar not found: " + calName });
  }
  var cal = cals[0];

  var now = new Date();
  var future = new Date(now.getTime() + daysAhead * 24 * 60 * 60 * 1000);

  // whose() date filtering in Calendar.app is unreliable across macOS versions,
  // so we ask for events starting after "now" and then filter the far end in JS.
  var events;
  try {
    events = cal.events.whose({ startDate: { _greaterThan: now } })();
  } catch (err) {
    // Fallback: grab everything and filter in JS (slower on huge calendars).
    events = cal.events();
  }

  var result = [];
  for (var i = 0; i < events.length; i++) {
    var e = events[i];
    var start;
    try {
      start = e.startDate();
    } catch (err) {
      continue;
    }
    if (start < now || start > future) continue;

    var summary = "";
    try { summary = e.summary() || ""; } catch (err) {}
    if (summary.indexOf(prefix) === 0) continue; // skip our own placeholders

    result.push({
      summary: summary,
      startDate: start.toISOString(),
      endDate: e.endDate().toISOString(),
      allDay: e.alldayEvent()
    });
  }
  return JSON.stringify(result);
}
