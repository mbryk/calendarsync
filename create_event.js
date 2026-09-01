// Usage: osascript -l JavaScript create_event.js "<CalendarId>" "<Title>" "<ISOStart>" "<ISOEnd>"

function run(argv) {
  var app = Application('Calendar');
  var calId = argv[0];
  var title = argv[1];
  var startISO = argv[2];
  var endISO = argv[3];

  var cal = app.calendars.byId(calId);
  if (!cal.name()) {
    return JSON.stringify({ error: "Calendar not found: " + calId });
  }

  var newEvent = app.Event({
    summary: title,
    startDate: new Date(startISO),
    endDate: new Date(endISO)
  });
  cal.events.push(newEvent);

  return JSON.stringify({ ok: true });
}
