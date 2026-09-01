// Usage: osascript -l JavaScript read_events.js "<CalendarId>" "<PlaceholderPrefix>" <daysAhead>
// Usage: osascript -l JavaScript read_events.js "3F5D9A23-9D3A-42F2-95B3-7AC25596D995" "\U0001F512 Busy" 30
// Prints JSON array of { summary, startDate, endDate, allDay } for real (non-placeholder)
// events in the window, expanding recurring events into their individual occurrences.

var DAY_MAP = { SU: 0, MO: 1, TU: 2, WE: 3, TH: 4, FR: 5, SA: 6 };

function run(argv) {
  var app = Application('Calendar');
  var calId = argv[0];
  var prefix = argv[1];
  var daysAhead = parseInt(argv[2]) || 14;

  var cal = app.calendars.byId(calId);
  if (!cal.name()) {
    return JSON.stringify({ error: "Calendar not found: " + calId });
  }

  var now = new Date();
  var future = new Date(now.getTime() + daysAhead * 24 * 60 * 60 * 1000);

  // whose({startDate: ...}) unreliably drops recurring masters whose series
  // start is in the past (backend-dependent — seen it silently exclude some
  // recurring events but not others), so fetch everything and filter in JS.
  //
  // Fetching one property at a time per-event is extremely slow over the
  // Calendar.app scripting bridge (~800ms/event/property). Bulk-fetching
  // each property across the whole collection in one Apple Event round trip
  // is ~60x cheaper per event, so we do that instead of materializing
  // individual event references and looping.
  var evSpec = cal.events;
  var summaries = evSpec.summary();
  var starts = evSpec.startDate();
  var ends = evSpec.endDate();
  var allDays = evSpec.alldayEvent();
  var recurrences = evSpec.recurrence();
  var excludedDatesLists = evSpec.excludedDates();
  var uids = evSpec.uid();
  var sequences = evSpec.sequence();

  // Exchange leaves stale duplicate copies of a series behind (same uid)
  // after a "this and future occurrences" reschedule, at the old time slot,
  // still recurring indefinitely. The live copy is the one with the highest
  // iCalendar SEQUENCE number (stale phantom copies are stuck at 0), so keep
  // only that index per uid.
  var bestIndexForUid = {};
  for (var i = 0; i < uids.length; i++) {
    var uid = uids[i];
    if (!(uid in bestIndexForUid) || sequences[i] > sequences[bestIndexForUid[uid]]) {
      bestIndexForUid[uid] = i;
    }
  }

  var result = [];
  var seen = {}; // safety net for exact (summary, start, end) duplicates
  for (var i = 0; i < summaries.length; i++) {
    if (bestIndexForUid[uids[i]] !== i) continue; // stale duplicate of another entry's uid

    var summary = summaries[i] || "";
    var start = starts[i];
    var end = ends[i];
    var allDay = allDays[i];
    var recurrence = recurrences[i];
    var excludedDates = excludedDatesLists[i] || [];
    if (summary.indexOf(prefix) === 0) continue; // skip our own placeholders
    if (summary.indexOf("Canceled:") === 0) continue; // Exchange keeps cancelled meetings as separate events instead of removing them

    if (!recurrence) {
      if (start < now || start > future) continue;
      pushUnique(result, seen, summary, start.toISOString(), end.toISOString(), allDay);
      continue;
    }

    var duration = end.getTime() - start.getTime();
    var excludedKeys = {};
    for (var k = 0; k < excludedDates.length; k++) {
      excludedKeys[excludedDates[k].toISOString()] = true;
    }

    var occurrences = expandRecurrence(recurrence, start, now, future);
    for (var j = 0; j < occurrences.length; j++) {
      var occStart = occurrences[j];
      if (excludedKeys[occStart.toISOString()]) continue;
      var occEnd = new Date(occStart.getTime() + duration);
      pushUnique(result, seen, summary, occStart.toISOString(), occEnd.toISOString(), allDay);
    }
  }
  return JSON.stringify(result);
}

function pushUnique(result, seen, summary, startISO, endISO, allDay) {
  var key = summary + "|" + startISO + "|" + endISO;
  if (seen[key]) return;
  seen[key] = true;
  result.push({ summary: summary, startDate: startISO, endDate: endISO, allDay: allDay });
}

function parseRRule(rule) {
  var out = {};
  var parts = rule.split(';');
  for (var i = 0; i < parts.length; i++) {
    var kv = parts[i].split('=');
    out[kv[0]] = kv[1];
  }
  return out;
}

// Parses a BYDAY token like "1WE" (first Wednesday), "-1FR" (last Friday),
// or plain "WE" (no nth, i.e. every occurrence) into { nth, day }.
function parseByDayToken(token) {
  var m = /^(-?\d+)?(SU|MO|TU|WE|TH|FR|SA)$/.exec(token);
  if (!m) return null;
  return { nth: m[1] ? parseInt(m[1], 10) : null, day: DAY_MAP[m[2]] };
}

// Returns the Date(s) in `year`/`month` (0-indexed) that fall on `weekday`
// (0=Sunday..6=Saturday). If `nth` is given (1-based, or negative to count
// from the end of the month), returns just that single occurrence.
function getWeekdaysInMonth(year, month, weekday, nth) {
  var results = [];
  var daysInMonth = new Date(year, month + 1, 0).getDate();
  if (nth > 0) {
    var firstDay = new Date(year, month, 1).getDay();
    var offset = (weekday - firstDay + 7) % 7;
    var date = 1 + offset + (nth - 1) * 7;
    if (date <= daysInMonth) results.push(new Date(year, month, date));
  } else if (nth < 0) {
    var lastDay = new Date(year, month, daysInMonth).getDay();
    var offsetFromEnd = (lastDay - weekday + 7) % 7;
    var dateFromEnd = daysInMonth - offsetFromEnd + (nth + 1) * 7;
    if (dateFromEnd >= 1) results.push(new Date(year, month, dateFromEnd));
  } else {
    for (var d = 1; d <= daysInMonth; d++) {
      var dt = new Date(year, month, d);
      if (dt.getDay() === weekday) results.push(dt);
    }
  }
  return results;
}

function parseICalDate(s) {
  var y = parseInt(s.substr(0, 4));
  var mo = parseInt(s.substr(4, 2)) - 1;
  var d = parseInt(s.substr(6, 2));
  if (s.length <= 8) return new Date(y, mo, d);
  var h = parseInt(s.substr(9, 2));
  var mi = parseInt(s.substr(11, 2));
  var se = parseInt(s.substr(13, 2));
  return new Date(Date.UTC(y, mo, d, h, mi, se));
}

// Expands an RRULE string into concrete occurrence start Dates that fall
// within [windowStart, windowEnd]. Supports DAILY/WEEKLY/MONTHLY/YEARLY,
// INTERVAL, COUNT, UNTIL, and BYDAY (only meaningful for WEEKLY here, which
// covers the common "recurs on specific weekdays" case).
function expandRecurrence(ruleStr, seedStart, windowStart, windowEnd) {
  var rule = parseRRule(ruleStr);
  var freq = rule.FREQ;
  var interval = parseInt(rule.INTERVAL) || 1;
  var count = rule.COUNT ? parseInt(rule.COUNT) : null;
  var until = rule.UNTIL ? parseICalDate(rule.UNTIL) : null;
  var byDay = rule.BYDAY ? rule.BYDAY.split(',').map(function (d) { return DAY_MAP[d]; }) : null;

  var results = [];
  var maxIterations = 10000;
  var iterations = 0;
  var n = 0; // occurrence index from the series start, for COUNT

  if (freq === 'WEEKLY' && byDay) {
    var weekStart = new Date(seedStart.getTime());
    weekStart.setHours(0, 0, 0, 0);
    weekStart.setDate(weekStart.getDate() - weekStart.getDay()); // back to Sunday
    var weekIndex = 0;
    var done = false;
    while (!done && iterations++ < maxIterations && weekStart <= windowEnd) {
      if (weekIndex % interval === 0) {
        for (var d = 0; d < byDay.length && !done; d++) {
          var occDate = new Date(weekStart.getTime());
          occDate.setDate(occDate.getDate() + byDay[d]);
          occDate.setHours(
            seedStart.getHours(), seedStart.getMinutes(),
            seedStart.getSeconds(), seedStart.getMilliseconds()
          );
          if (occDate < seedStart) continue; // before series start
          if (until && occDate > until) { done = true; break; }
          if (count !== null && n >= count) { done = true; break; }
          if (occDate >= windowStart && occDate <= windowEnd) {
            results.push(occDate);
          }
          n++;
        }
      }
      weekIndex++;
      weekStart.setDate(weekStart.getDate() + 7);
    }
    return results;
  }

  if (freq === 'MONTHLY' && rule.BYDAY) {
    var byDayTokens = rule.BYDAY.split(',').map(parseByDayToken).filter(Boolean);
    var currentMonth = new Date(seedStart.getFullYear(), seedStart.getMonth(), 1);
    var done = false;

    while (!done && iterations++ < maxIterations && currentMonth <= windowEnd) {
      var year = currentMonth.getFullYear();
      var month = currentMonth.getMonth();

      for (var i = 0; i < byDayTokens.length && !done; i++) {
        var token = byDayTokens[i];
        var monthOccurrences = getWeekdaysInMonth(year, month, token.day, token.nth);

        for (var j = 0; j < monthOccurrences.length && !done; j++) {
          var occDate = monthOccurrences[j];
          occDate.setHours(
            seedStart.getHours(), seedStart.getMinutes(),
            seedStart.getSeconds(), seedStart.getMilliseconds()
          );

          if (occDate < seedStart) continue; // Skip occurrences before start date
          if (until && occDate > until) { done = true; break; }
          if (count !== null && n >= count) { done = true; break; }

          if (occDate >= windowStart && occDate <= windowEnd) {
            results.push(occDate);
          }
          n++;
        }
      }

      // Advance by INTERVAL months
      currentMonth.setMonth(currentMonth.getMonth() + interval);
    }
    return results;
  }

  // Generic stepping for DAILY / WEEKLY (no BYDAY) / MONTHLY / YEARLY.
  var occ = new Date(seedStart.getTime());
  while (iterations++ < maxIterations) {
    if (until && occ > until) break;
    if (count !== null && n >= count) break;
    if (occ > windowEnd) break;
    if (occ >= windowStart) results.push(new Date(occ.getTime()));
    n++;
    if (freq === 'DAILY') occ.setDate(occ.getDate() + interval);
    else if (freq === 'WEEKLY') occ.setDate(occ.getDate() + 7 * interval);
    else if (freq === 'MONTHLY') occ.setMonth(occ.getMonth() + interval);
    else if (freq === 'YEARLY') occ.setFullYear(occ.getFullYear() + interval);
    else break; // unknown frequency, stop
  }
  return results;
}
