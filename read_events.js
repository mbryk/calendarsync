// Usage: osascript -l JavaScript read_events.js "<CalendarId>" "<PlaceholderPrefix>" <daysAhead> [ownerEmail]
// Usage: osascript -l JavaScript read_events.js "3F5D9A23-9D3A-42F2-95B3-7AC25596D995" "\U0001F512 Busy" 7 "mark.bryk@innovation.nj.gov"
// Prints JSON array of { summary, startDate, endDate, allDay, uid } for real (non-placeholder)
// events in the window, expanding recurring events into their individual occurrences. `uid` is
// the source Exchange/Calendar.app event's uid (diagnostic only — lets log output show whether
// two events flagged as duplicates actually share a uid; not used for any dedup logic itself).
// If ownerEmail is given, each event also gets myStatus (participationStatus of the
// attendee matching that email, e.g. "accepted"/"declined"/"tentative"/"unknown", or
// null if no attendee matches — e.g. an event with no attendees at all).
// NOTE: for any uid shared by more than one calendar object, a "UIDGROUP ..." line is
// printed per index (raw sequence/stampDate/recurrence dump), and a "SUPPRESSED ..."
// line is printed per dropped occurrence from a non-primary same-uid index (see
// run()'s same-uid handling below for why only one index per uid survives). Both go
// via console.log BEFORE the JSON result. console.log output always lands ahead of
// the function's own return value in osascript's stdout, so a caller must parse only
// the LAST line of stdout as JSON, not assume stdout is JSON-only.

var DAY_MAP = { SU: 0, MO: 1, TU: 2, WE: 3, TH: 4, FR: 5, SA: 6 };

// Module-scope so expandIndexOccurrences()/logSuppressed() (called from run(),
// but declared outside it) can see the same bulk-fetched arrays without
// threading them through every call as parameters.
var prefix, now, future;
var summaries, starts, ends, allDays, recurrences, excludedDatesLists, sequences, stampDates, uids;

function run(argv) {
  var app = Application('Calendar');
  var calId = argv[0];
  prefix = argv[1];
  var daysAhead = parseInt(argv[2]) || 14;
  var ownerEmail = (argv[3] || '').toLowerCase();

  var cal = app.calendars.byId(calId);
  if (!cal.name()) {
    return JSON.stringify({ error: "Calendar not found: " + calId });
  }

  now = new Date();
  future = new Date(now.getTime() + daysAhead * 24 * 60 * 60 * 1000);

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
  summaries = evSpec.summary();
  starts = evSpec.startDate();
  ends = evSpec.endDate();
  allDays = evSpec.alldayEvent();
  recurrences = evSpec.recurrence();
  excludedDatesLists = evSpec.excludedDates();
  uids = evSpec.uid();
  sequences = evSpec.sequence();
  stampDates = evSpec.stampDate();

  // Exchange can leave multiple event objects behind under the same uid:
  // stale duplicate series copies after a "this and future occurrences"
  // split, or leftover copies from past edits of a recurring series (see
  // docs/ai/jxa-calendar-quirks.md). Confirmed 2026-09-25 against real
  // clusters the user flagged as wrong on their calendar: the highest-
  // `sequence` index per uid is usually the live copy, and EVERY other
  // same-uid index is stale noise that should be dropped outright — this
  // holds even when a non-primary index has a different `summary` (e.g. an
  // "FW:" copy).
  //
  // Exception: a single-occurrence override (see `isSingleOccurrenceOverride`
  // below) can have a *lower* `sequence` than the recurring-master sibling
  // it's replacing, because it's its own object with its own short edit
  // history, not a continuation of the master's count — confirmed live for
  // the `Digital RFI Grooming` and `Lizzy <> Mark` reschedules (see
  // docs/ai/jxa-calendar-quirks.md). So within a uid group, if any index is a
  // single-occurrence override, the primary is chosen by max-sequence among
  // just that subset (ignoring recurring-master siblings' sequence
  // entirely); only when no single-occurrence override exists in the group
  // do we fall back to max-sequence across the whole group.
  //
  // This is a real assumption, not a certainty — if it's ever wrong (some
  // future same-uid cluster where the suppressed index was actually the
  // right one), `logUidGroup`/`logSuppressed` below print enough raw detail
  // per dropped occurrence (seq, stamp, summary, actual expanded time) to
  // manually audit against the calendar and catch it.
  var indicesByUid = {};
  for (var i = 0; i < uids.length; i++) {
    var uid = uids[i];
    (indicesByUid[uid] = indicesByUid[uid] || []).push(i);
  }

  var bestIndexForUid = {};
  var bestReasonForUid = {};
  for (var uidKey in indicesByUid) {
    var uidGroup = indicesByUid[uidKey];
    var singleOccIndices = [];
    for (var gi = 0; gi < uidGroup.length; gi++) {
      if (isSingleOccurrenceOverride(uidGroup[gi])) singleOccIndices.push(uidGroup[gi]);
    }
    var candidates = singleOccIndices.length > 0 ? singleOccIndices : uidGroup;
    var best = candidates[0];
    for (var ci = 1; ci < candidates.length; ci++) {
      if (sequences[candidates[ci]] > sequences[best]) best = candidates[ci];
    }
    bestIndexForUid[uidKey] = best;
    // Recorded per uid (not just for the primary) so logUidGroup can explain
    // the pick even when the group has only one candidate in the winning
    // subset — makes it possible to tell, after the fact from the log alone,
    // whether the single-occurrence-override guard fired for this group and,
    // if it fired with >1 single-occurrence sibling, that the tiebreak among
    // them was still plain max-sequence (see run()'s comment above).
    bestReasonForUid[uidKey] = singleOccIndices.length > 0
      ? "single-occurrence override, max-seq among " + singleOccIndices.length + " such sibling(s)"
      : "no single-occurrence siblings in group, max-seq across whole group";
  }

  var result = [];
  var seen = {}; // safety net for exact (summary, start, end) duplicates

  for (var uid2 in indicesByUid) {
    var group = indicesByUid[uid2];
    var primaryIdx = bestIndexForUid[uid2];
    if (group.length > 1) logUidGroup(uid2, group, primaryIdx, bestReasonForUid[uid2]);

    var primaryOccs = expandIndexOccurrences(primaryIdx);
    for (var p = 0; p < primaryOccs.length; p++) {
      pushUnique(result, seen, primaryOccs[p]);
    }

    for (var g = 0; g < group.length; g++) {
      var idx = group[g];
      if (idx === primaryIdx) continue;
      var secOccs = expandIndexOccurrences(idx);
      for (var s = 0; s < secOccs.length; s++) {
        logSuppressed(uid2, primaryIdx, idx, secOccs[s]);
      }
    }
  }

  if (ownerEmail) attachMyStatus(result, cal, ownerEmail);
  for (var r = 0; r < result.length; r++) {
    delete result[r]._srcIndex;
  }
  return JSON.stringify(result);
}

// True if index idx should be treated as a single-occurrence override rather
// than a recurring master: either it has no `recurrence` at all, or it has a
// WEEKLY+BYDAY recurrence whose BYDAY doesn't include its own `start`'s
// weekday. The latter is a real, confirmed case (2026-09-25, "Mark /
// Catherine 90-Day check in" vs its "1:1" same-uid sibling): a detached,
// renamed/moved single occurrence can retain a stale copy of the master's
// `recurrence` property even though its own start no longer falls on that
// rule's day. Left un-detected, `expandRecurrence`'s WEEKLY+BYDAY branch only
// ever emits dates on the RRULE's BYDAY, so the object's own (real,
// intended) date is never generated at all — not suppressed by dedup, just
// silently dropped. Scoped to WEEKLY+BYDAY only because that's the only
// pattern confirmed to misbehave this way so far; other FREQ/BYxxx
// combinations fall through unchanged.
function isSingleOccurrenceOverride(idx) {
  var recurrence = recurrences[idx];
  if (!recurrence) return true;
  var rule = parseRRule(recurrence);
  if (rule.FREQ !== 'WEEKLY' || !rule.BYDAY) return false;
  var byDay = rule.BYDAY.split(',').map(function (d) { return DAY_MAP[d]; });
  return byDay.indexOf(starts[idx].getDay()) === -1;
}

// Filters + expands one bulk-fetched index into candidate occurrence
// records, applying the placeholder/canceled/window/excludedDates rules.
function expandIndexOccurrences(idx) {
  var summary = summaries[idx] || "";
  if (summary.indexOf(prefix) === 0) return [];
  if ((summary.indexOf("Canceled:") === 0) || (summary.indexOf("CANCELED:") === 0)) return [];

  var start = starts[idx];
  var end = ends[idx];
  var allDay = allDays[idx];
  var recurrence = recurrences[idx];
  var out = [];

  if (isSingleOccurrenceOverride(idx)) {
    if (start < now || start > future) return out;
    out.push({ summary: summary, startISO: start.toISOString(), endISO: end.toISOString(), allDay: allDay, srcIndex: idx });
    return out;
  }

  var duration = end.getTime() - start.getTime();
  var excludedDates = excludedDatesLists[idx] || [];
  var excludedKeys = {};
  for (var k = 0; k < excludedDates.length; k++) {
    excludedKeys[excludedDates[k].toISOString()] = true;
  }

  var occurrences = expandRecurrence(recurrence, start, now, future);
  for (var j = 0; j < occurrences.length; j++) {
    var occStart = occurrences[j];
    if (excludedKeys[occStart.toISOString()]) continue;
    var occEnd = new Date(occStart.getTime() + duration);
    out.push({ summary: summary, startISO: occStart.toISOString(), endISO: occEnd.toISOString(), allDay: allDay, srcIndex: idx });
  }
  return out;
}

function pushUnique(result, seen, occ) {
  var key = occ.summary + "|" + occ.startISO + "|" + occ.endISO;
  if (seen[key]) return false;
  seen[key] = true;
  result.push({ summary: occ.summary, startDate: occ.startISO, endDate: occ.endISO, allDay: occ.allDay, uid: uids[occ.srcIndex], _srcIndex: occ.srcIndex });
  return true;
}

// Dumps the raw fields for every index sharing a uid, unconditionally —
// lets a same-uid cluster be audited from the raw Calendar.app data alone
// (sequence/stampDate/recurrence/summary/start/end per index), independent
// of whatever the primary-selection heuristic below decides to do with it.
// `reason` explains why `primaryIdx` won (see run()'s bestReasonForUid) —
// printed on the primary's own line so a group with only one candidate in
// the winning subset still records why, and so it's possible to tell after
// the fact whether the single-occurrence-override guard fired for this
// group at all.
function logUidGroup(uid, group, primaryIdx, reason) {
  for (var i = 0; i < group.length; i++) {
    var idx = group[i];
    console.log(
      "UIDGROUP uid=" + uid + " idx=" + idx +
      (idx === primaryIdx ? " (primary: " + reason + ")" : "") +
      " seq=" + sequences[idx] + " stamp=" + stampDates[idx] +
      " recurrence=" + (recurrences[idx] ? JSON.stringify(recurrences[idx]) : "null") +
      " start=" + starts[idx].toISOString() + " end=" + ends[idx].toISOString() +
      " summary=" + JSON.stringify(summaries[idx])
    );
  }
}

// Prints one line per occurrence dropped from a non-primary same-uid index
// (see run()'s same-uid handling) — the actual expanded time that occurrence
// would have landed on, next to the primary's identity. This is the audit
// trail for the "highest sequence always wins" assumption: if a suppressed
// occurrence ever turns out to be the one that should've synced, this line
// has the uid/idx/seq/stamp/summary/time needed to catch it after the fact.
function logSuppressed(uid, primaryIdx, secondaryIdx, occ) {
  console.log(
    "SUPPRESSED uid=" + uid +
    " | kept primary idx=" + primaryIdx + " seq=" + sequences[primaryIdx] + " stamp=" + stampDates[primaryIdx] +
    " || dropped idx=" + secondaryIdx + " seq=" + sequences[secondaryIdx] + " stamp=" + stampDates[secondaryIdx] +
    " time=" + occ.startISO + "-" + occ.endISO + " summary=" + JSON.stringify(occ.summary)
  );
}

// Fetching attendees can't be done as a bulk property fetch across the whole
// collection like summary/startDate/etc. — cal.events.attendees() times out
// ("AppleEvent timed out", -1712) even on a calendar with under 200 events.
// So this falls back to one per-item .attendees() call (~2s each observed),
// but only for the small set of events that actually survived filtering
// above, not the whole collection — keeps the cost bounded to the sync
// window's real event count rather than the calendar's full history.
function attachMyStatus(result, cal, ownerEmail) {
  var srcIndices = {};
  for (var r = 0; r < result.length; r++) srcIndices[result[r]._srcIndex] = true;

  var events = cal.events();
  var myStatusByIndex = {};
  for (var idxStr in srcIndices) {
    var idx = parseInt(idxStr, 10);
    var attendees;
    try { attendees = events[idx].attendees(); } catch (e) { attendees = []; }
    var status = null;
    for (var a = 0; a < attendees.length; a++) {
      var email = '';
      try { email = (attendees[a].email() || '').toLowerCase(); } catch (e) { /* no email on this attendee */ }
      if (email === ownerEmail) {
        status = attendees[a].participationStatus();
        break;
      }
    }
    myStatusByIndex[idx] = status;
  }

  for (var r2 = 0; r2 < result.length; r2++) {
    result[r2].myStatus = myStatusByIndex[result[r2]._srcIndex];
  }

  return myStatusByIndex;
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

  // Generic stepping for MONTHLY / YEARLY (no BYDAY). Stepped by reconstructing
  // year/month/day from the seed each time rather than repeated setMonth()/
  // setFullYear(), which silently roll a nonexistent day (e.g. Jan 31 + 1 month)
  // into the following month (Mar 3) instead of producing no occurrence — RFC
  // 5545 says a month/year lacking the seed day simply yields no occurrence.
  if (freq === 'MONTHLY' || freq === 'YEARLY') {
    var stepIndex = 0;
    var seedYear = seedStart.getFullYear();
    var seedMonth = seedStart.getMonth();
    var seedDay = seedStart.getDate();

    while (iterations++ < maxIterations) {
      var year, month;
      if (freq === 'MONTHLY') {
        var totalMonths = seedYear * 12 + seedMonth + stepIndex * interval;
        year = Math.floor(totalMonths / 12);
        month = totalMonths % 12;
      } else {
        year = seedYear + stepIndex * interval;
        month = seedMonth;
      }
      stepIndex++;

      if (new Date(year, month, 1) > windowEnd) break; // 1st of month already past window

      var daysInTargetMonth = new Date(year, month + 1, 0).getDate();
      if (seedDay > daysInTargetMonth) continue; // this month/year has no such day; skip, don't roll over

      var occ = new Date(year, month, seedDay,
        seedStart.getHours(), seedStart.getMinutes(), seedStart.getSeconds(), seedStart.getMilliseconds());
      if (occ > windowEnd) break;
      if (until && occ > until) break;
      if (count !== null && n >= count) break;
      if (occ >= windowStart) results.push(occ);
      n++;
    }
    return results;
  }

  // Generic stepping for DAILY / WEEKLY (no BYDAY).
  var occ = new Date(seedStart.getTime());
  while (iterations++ < maxIterations) {
    if (until && occ > until) break;
    if (count !== null && n >= count) break;
    if (occ > windowEnd) break;
    if (occ >= windowStart) results.push(new Date(occ.getTime()));
    n++;
    if (freq === 'DAILY') occ.setDate(occ.getDate() + interval);
    else if (freq === 'WEEKLY') occ.setDate(occ.getDate() + 7 * interval);
    else break; // unknown frequency, stop
  }
  return results;
}
