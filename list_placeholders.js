// Usage: osascript -l JavaScript list_placeholders.js "<CalendarName>" "<PlaceholderPrefix>"
// Prints JSON array of { startDate, endDate } for placeholder events (any time,
// not just within the sync window) previously created by this script.

function run(argv) {
  var app = Application('Calendar');
  var calName = argv[0];
  var prefix = argv[1];

  var cals = app.calendars.whose({ name: calName });
  if (cals.length === 0) {
    return JSON.stringify({ error: "Calendar not found: " + calName });
  }
  var cal = cals[0];

  var events = cal.events.whose({ summary: { _beginsWith: prefix } })();

  var result = [];
  for (var i = 0; i < events.length; i++) {
    var e = events[i];
    result.push({
      startDate: e.startDate().toISOString(),
      endDate: e.endDate().toISOString()
    });
  }
  return JSON.stringify(result);
}
