// Usage: osascript -l JavaScript list_placeholders.js "<CalendarId>" "<PlaceholderPrefix>"
// Prints JSON array of { startDate, endDate } for placeholder events (any time,
// not just within the sync window) previously created by this script.

function run(argv) {
  var app = Application('Calendar');
  var calId = argv[0];
  var prefix = argv[1];

  var cal = app.calendars.byId(calId);
  if (!cal.name()) {
    return JSON.stringify({ error: "Calendar not found: " + calId });
  }

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
