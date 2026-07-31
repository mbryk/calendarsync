// Usage: osascript -l JavaScript create_event.js "<CalendarName>" "<Title>" "<ISOStart>" "<ISOEnd>"

function run(argv) {
  var app = Application('Calendar');
  var calName = argv[0];
  var title = argv[1];
  var startISO = argv[2];
  var endISO = argv[3];

  var cals = app.calendars.whose({ name: calName });
  if (cals.length === 0) {
    return JSON.stringify({ error: "Calendar not found: " + calName });
  }
  var cal = cals[0];

  var newEvent = app.Event({
    summary: title,
    startDate: new Date(startISO),
    endDate: new Date(endISO)
  });
  cal.events.push(newEvent);

  return JSON.stringify({ ok: true });
}
