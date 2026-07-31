// Usage: osascript -l JavaScript delete_placeholders.js "<CalendarName>" "<PlaceholderPrefix>"

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
  var count = events.length;

  // Delete back-to-front so indices don't shift under us mid-loop.
  for (var i = events.length - 1; i >= 0; i--) {
    events[i].delete();
  }

  return JSON.stringify({ deleted: count });
}
