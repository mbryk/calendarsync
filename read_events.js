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
// printed per index (raw sequence/stampDate/recurrence dump), a "SUPPRESSED ..." line
// is printed per dropped occurrence from a non-primary same-uid index, and a
// "DUALWINNER ..." line is printed once per uid group where a recurring-master winner
// and a single-occurrence-override winner both survive together (see run()'s same-uid
// handling below — up to two indices per uid can survive now, not just one). All three
// go via console.log BEFORE the JSON result. console.log output always lands ahead of
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
  // split, leftover copies from past edits of a recurring series, or a
  // detached single-occurrence override that still carries a stale copy of
  // the master's `recurrence` field (see docs/ai/jxa-calendar-quirks.md and
  // isSingleOccurrenceOverride below).
  //
  // A uid group is partitioned by isSingleOccurrenceOverride into two
  // disjoint subsets, and EACH subset gets its own max-sequence winner,
  // independently:
  //   - recurringIndices: ordinary recurring-master copies, including stale
  //     ones left behind by past edits. Max-seq among just these picks the
  //     one live copy — confirmed reliable against real clusters the user
  //     flagged as wrong (2026-09-25); this holds even when a non-winning
  //     index has a different `summary` (e.g. an "FW:" copy).
  //   - singleOccIndices: detached single-occurrence overrides. Max-seq
  //     among just these picks the one live override, if more than one
  //     exists (a tiebreak only — not yet seen live with >1).
  // BOTH winners are expanded and emitted when both subsets are non-empty.
  // This is a deliberate approximation, not the fully correct fix: a
  // single-occurrence override conceptually REPLACES one specific occurrence
  // of the recurring master (RFC 5545 RECURRENCE-ID), but JXA exposes
  // nothing like RECURRENCE-ID here, and the master's own excludedDates() is
  // confirmed unreliable for exactly this case (see
  // docs/ai/jxa-calendar-quirks.md), so there's no reliable way to know
  // *which* master occurrence a given override replaces and exclude just
  // that one. Emitting both winners unconditionally is correct whenever the
  // override's own date is already in the past (expandIndexOccurrences'
  // window check then makes it contribute nothing) but could in principle
  // produce an extra phantom placeholder for a still-future override's
  // original slot, alongside the corrected one — logDualWinner below prints
  // both winners' full occurrence lists so that case is visible in the log
  // if/when it actually happens, instead of only being caught by chance.
  //
  // An earlier revision of this same logic (same day, 2026-09-25) had ONE
  // winner for the whole uid group: if ANY single-occurrence override
  // existed, it became the sole primary and every recurring-master sibling
  // was suppressed outright, regardless of sequence. Confirmed live to
  // silently drop an entire ongoing weekly series (`Digital RFI Grooming`,
  // `Digital RFI Pilot Project Check-In Meetings`) whenever a past one-off
  // reschedule happened to share their uid — the override (already in the
  // past) became primary and contributed zero occurrences, while the real
  // ongoing series got suppressed as "non-primary". Don't reintroduce that
  // shape (a single `bestIndexForUid` per uid, with the override subset
  // taking an all-or-nothing precedence over the recurring subset).
  var indicesByUid = {};
  for (var i = 0; i < uids.length; i++) {
    var uid = uids[i];
    (indicesByUid[uid] = indicesByUid[uid] || []).push(i);
  }

  var primariesForUid = {};
  var reasonForIndex = {};
  for (var uidKey in indicesByUid) {
    var uidGroup = indicesByUid[uidKey];
    var singleOccIndices = [];
    var recurringIndices = [];
    for (var gi = 0; gi < uidGroup.length; gi++) {
      if (isSingleOccurrenceOverride(uidGroup[gi])) singleOccIndices.push(uidGroup[gi]);
      else recurringIndices.push(uidGroup[gi]);
    }

    var winners = [];
    if (recurringIndices.length > 0) {
      var recurringWinner = pickMaxSeq(recurringIndices);
      reasonForIndex[recurringWinner] = "recurring-master winner, max-seq among " +
        recurringIndices.length + " recurring-master sibling(s)" +
        (singleOccIndices.length > 0
          ? "; group also has " + singleOccIndices.length + " single-occurrence override(s), emitted separately"
          : "");
      winners.push(recurringWinner);
    }
    if (singleOccIndices.length > 0) {
      var singleOccWinner = pickMaxSeq(singleOccIndices);
      reasonForIndex[singleOccWinner] = "single-occurrence-override winner, max-seq among " +
        singleOccIndices.length + " such sibling(s)" +
        (recurringIndices.length > 0
          ? "; group also has a recurring-master winner, emitted separately"
          : "");
      winners.push(singleOccWinner);
    }
    // Push order is always [recurringWinner, singleOccWinner] when both
    // exist (recurring is always pushed first above) — logDualWinner below
    // relies on that order.
    primariesForUid[uidKey] = winners;
  }

  var result = [];
  var seen = {}; // safety net for exact (summary, start, end) duplicates

  for (var uid2 in indicesByUid) {
    var group = indicesByUid[uid2];
    var primaries = primariesForUid[uid2];
    if (group.length > 1) logUidGroup(uid2, group, primaries, reasonForIndex);

    var primarySet = {};
    var occsByPrimary = {};
    for (var pi = 0; pi < primaries.length; pi++) {
      var pIdx = primaries[pi];
      primarySet[pIdx] = true;
      var occs = expandIndexOccurrences(pIdx);
      occsByPrimary[pIdx] = occs;
      for (var p = 0; p < occs.length; p++) {
        pushUnique(result, seen, occs[p]);
      }
    }

    if (primaries.length === 2) {
      logDualWinner(uid2, primaries[0], occsByPrimary[primaries[0]], primaries[1], occsByPrimary[primaries[1]]);
    }

    for (var g = 0; g < group.length; g++) {
      var idx = group[g];
      if (primarySet[idx]) continue;
      var secOccs = expandIndexOccurrences(idx);
      for (var s = 0; s < secOccs.length; s++) {
        logSuppressed(uid2, primaries, idx, secOccs[s]);
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

// Picks the index with the highest `sequence` from a non-empty list —
// shared tiebreak used independently for each of run()'s two winner
// subsets (recurring-master and single-occurrence-override).
function pickMaxSeq(indices) {
  var best = indices[0];
  for (var i = 1; i < indices.length; i++) {
    if (sequences[indices[i]] > sequences[best]) best = indices[i];
  }
  return best;
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
// `primaries` is the array of 0-2 winner indices from run() (recurring-
// master winner and/or single-occurrence-override winner); `reasonForIndex`
// maps each winner index to why it won (see run()'s comment). Printed on
// each winner's own line so it's possible to tell after the fact, from the
// log alone, which subset(s) had a winner and why.
function logUidGroup(uid, group, primaries, reasonForIndex) {
  var primarySet = {};
  for (var pi = 0; pi < primaries.length; pi++) primarySet[primaries[pi]] = true;
  for (var i = 0; i < group.length; i++) {
    var idx = group[i];
    console.log(
      "UIDGROUP uid=" + uid + " idx=" + idx +
      (primarySet[idx] ? " (primary: " + reasonForIndex[idx] + ")" : "") +
      " seq=" + sequences[idx] + " stamp=" + stampDates[idx] +
      " recurrence=" + (recurrences[idx] ? JSON.stringify(recurrences[idx]) : "null") +
      " start=" + starts[idx].toISOString() + " end=" + ends[idx].toISOString() +
      " summary=" + JSON.stringify(summaries[idx])
    );
  }
}

// Prints one line per occurrence dropped from a non-primary same-uid index
// (see run()'s same-uid handling) — the actual expanded time that occurrence
// would have landed on, next to the identity of every surviving winner
// (`primaries`, 1 or 2 indices). This is the audit trail for the "highest
// sequence always wins (within a subset)" assumption: if a suppressed
// occurrence ever turns out to be the one that should've synced, this line
// has the uid/idx/seq/stamp/summary/time needed to catch it after the fact.
function logSuppressed(uid, primaries, secondaryIdx, occ) {
  var keptDesc = primaries.map(function (idx) {
    return "idx=" + idx + " seq=" + sequences[idx] + " stamp=" + stampDates[idx];
  }).join(" & ");
  console.log(
    "SUPPRESSED uid=" + uid +
    " | kept primary " + keptDesc +
    " || dropped idx=" + secondaryIdx + " seq=" + sequences[secondaryIdx] + " stamp=" + stampDates[secondaryIdx] +
    " time=" + occ.startISO + "-" + occ.endISO + " summary=" + JSON.stringify(occ.summary)
  );
}

// Printed once per uid group where BOTH a recurring-master winner and a
// single-occurrence-override winner survive together — the "emit both"
// approximation described in run()'s comment above. Lists every occurrence
// date each winner actually produced, side by side, so a human (or a future
// pass) can check whether the recurring-master winner ever produced an
// occurrence suspiciously close to the override's own date — that would be
// the "phantom original slot" duplicate this approximation risks for a
// still-future override (an already-past override, as seen so far, always
// produces zero occurrences here, so this line is expected to show an empty
// list on the override side in every case observed to date — a non-empty
// override-side list paired with a nearby recurring-side date is the signal
// to go build the real RECURRENCE-ID-less exclusion heuristic).
function logDualWinner(uid, recurringIdx, recurringOccs, singleOccIdx, singleOccOccs) {
  var fmt = function (occs) {
    var dates = [];
    for (var i = 0; i < occs.length; i++) dates.push(occs[i].startISO);
    return dates.length ? dates.join(", ") : "(none)";
  };
  console.log(
    "DUALWINNER uid=" + uid +
    " | recurring-master idx=" + recurringIdx + " occurrences=[" + fmt(recurringOccs) + "]" +
    " || single-occurrence-override idx=" + singleOccIdx + " occurrences=[" + fmt(singleOccOccs) + "]"
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
