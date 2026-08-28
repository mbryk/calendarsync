// Usage: osascript -l JavaScript delete_events.js "<CalendarName>" "<PlaceholderPrefix>" '<JSON array of {startDate, endDate}>'
// Deletes only the placeholder events (matched by prefix AND exact start/end)
// listed in the JSON argument. Prints JSON { deleted: <count> }.

function run(argv) {
  var app = Application('Calendar');
  var calName = argv[0];
  var prefix = argv[1];
  var toDelete = JSON.parse(argv[2] || "[]");

  var cals = app.calendars.whose({ name: calName });
  if (cals.length === 0) {
    return JSON.stringify({ error: "Calendar not found: " + calName });
  }
  var cal = cals[0];

  var keys = {};
  for (var i = 0; i < toDelete.length; i++) {
    keys[toDelete[i].startDate + "|" + toDelete[i].endDate] = true;
  }

  var events = cal.events.whose({ summary: { _beginsWith: prefix } })();
  var count = 0;

  // Delete back-to-front so indices don't shift under us mid-loop.
  for (var i = events.length - 1; i >= 0; i--) {
    var e = events[i];
    var key = e.startDate().toISOString() + "|" + e.endDate().toISOString();
    if (keys[key]) {
      e.delete();
      count++;
    }
  }

  return JSON.stringify({ deleted: count });
}
