/**
 * GRC tracker – audit trail for hand edits in the Master tab (DESIGN D8).
 *
 * Only Apps Script sees who edited a cell, so this installable edit trigger:
 *   1. writes one AuditLog row per edited cell (who, task ID, field, old value, new value);
 *   2. sets "Last updated" on every edited row;
 *   3. undoes Status = Done when the row has no "Submitted date" or no "Link" (proof), and
 *      (v1.4) when "Needs review" is Yes and the task was not Approved first;
 *   4. (v1.4) undoes Status = Approved or Returned set by anyone but the task's reviewer or a
 *      Manager (reviewer: the Reviewer column, else the Doer's full-time supervisor in Staff
 *      "Reports to", else the first Manager; DESIGN section 19).
 *
 * Columns are found by header name, never by position. Edits made by the service through the
 * Sheets API do not fire this trigger; the service writes its own AuditLog rows.
 *
 * Setup (once, by someone who can edit the protected AuditLog tab):
 *   Extensions → Apps Script → paste this file → run installAuditTrigger() → accept permissions.
 *   Then run installValidation() once: dropdowns for Status, Needs review and Reminder profile.
 * Do NOT rename onMasterEdit to onEdit: a simple trigger cannot see the editor's email and
 * would run a second time next to the installable one.
 */

var MASTER = 'Master';
var AUDIT = 'AuditLog';
var MULTI_CELL_OLD = '(not available: multi-cell edit)';
var DONE_BLOCKED = 'Done needs Submitted date and Link';
var REVIEW_BLOCKED = 'Done needs the review to be Approved first';
var REVIEWER_BLOCKED = 'Only the reviewer or a Manager can approve or return';
var STATUSES = ['Proposed', 'Not Started', 'In Progress', 'In Review', 'Approved', 'Returned',
  'On Hold', 'Done', 'Cancelled'];
var DROPDOWNS = {
  'Status': STATUSES,
  'Needs review': ['Yes', 'No'],
  'Reminder profile': ['Standard', 'Light', 'Off']
};

/** Dropdown validation on Master for the v1.4 columns (run once, and after adding columns). */
function installValidation() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(MASTER);
  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(function (h) {
    return String(h).trim();
  });
  Object.keys(DROPDOWNS).forEach(function (name) {
    var index = header.indexOf(name);
    if (index < 0) return;
    var rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(DROPDOWNS[name], true)
      .setAllowInvalid(false)
      .build();
    sheet.getRange(2, index + 1, Math.max(sheet.getMaxRows() - 1, 1), 1).setDataValidation(rule);
  });
}

/** Staff rows by lower-case name: {name, email, role, reportsTo}. */
function staff_() {
  var sheet = SpreadsheetApp.getActive().getSheetByName('Staff');
  if (!sheet) return {};
  var values = sheet.getRange(1, 1, sheet.getLastRow(), sheet.getLastColumn()).getValues();
  var head = values[0].map(function (h) { return String(h).trim(); });
  var at = function (row, name) {
    var i = head.indexOf(name);
    return i < 0 ? '' : String(row[i] || '').trim();
  };
  var people = {};
  values.slice(1).forEach(function (row) {
    var name = at(row, 'Name');
    if (!name) return;
    var role = at(row, 'Role') || 'Full time';
    people[name.toLowerCase()] = {
      name: name, email: at(row, 'Email').toLowerCase(), role: role === 'Staff' ? 'Full time' : role,
      reportsTo: at(row, 'Reports to')
    };
  });
  return people;
}

/** The reviewer's Staff row (same rule as app/route.py default_reviewer), or null. */
function reviewer_(people, reviewerName, doerName) {
  if (reviewerName && people[reviewerName.toLowerCase()]) return people[reviewerName.toLowerCase()];
  var doer = people[(doerName || '').toLowerCase()];
  if (doer && doer.role === 'Intern' && doer.reportsTo) {
    var boss = people[doer.reportsTo.toLowerCase()];
    if (boss && boss.role !== 'Manager') return boss;
  }
  var names = Object.keys(people);
  for (var i = 0; i < names.length; i++) {
    if (people[names[i]].role === 'Manager') return people[names[i]];
  }
  return null;
}

/** May `email` set Approved/Returned on a task with this reviewer and doer? */
function canReview_(email, reviewerName, doerName) {
  var people = staff_();
  var who = (email || '').toLowerCase();
  if (!who) return false;
  var names = Object.keys(people);
  for (var i = 0; i < names.length; i++) {
    var p = people[names[i]];
    if (p.role === 'Manager' && p.email === who) return true;
  }
  var reviewer = reviewer_(people, reviewerName, doerName);
  return !!reviewer && reviewer.email === who;
}

function installAuditTrigger() {
  var spreadsheet = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    if (trigger.getHandlerFunction() === 'onMasterEdit') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('onMasterEdit').forSpreadsheet(spreadsheet).onEdit().create();
}

function onMasterEdit(e) {
  if (!e || !e.range) return;
  var range = e.range;
  var sheet = range.getSheet();
  if (sheet.getName() !== MASTER) return;

  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(function (h) {
    return String(h).trim();
  });
  var col = function (name) {
    var i = header.indexOf(name);
    return i < 0 ? null : i + 1;
  };
  var idCol = col('ID');
  var statusCol = col('Status');
  var submittedCol = col('Submitted date');
  var linkCol = col('Link');
  var updatedCol = col('Last updated');
  var needsReviewCol = col('Needs review');
  var reviewerCol = col('Reviewer');
  var doerCol = col('Doer');

  var firstRow = Math.max(range.getRow(), 2); // never audit the header row
  var lastRow = range.getRow() + range.getNumRows() - 1;
  if (lastRow < firstRow) return;
  var firstCol = range.getColumn();
  var numCols = range.getNumColumns();
  var single = range.getNumRows() === 1 && numCols === 1;
  var user = editorEmail_(e);
  var now = new Date();
  var audit = [];

  for (var row = firstRow; row <= lastRow; row++) {
    var values = sheet.getRange(row, 1, 1, header.length).getValues()[0];
    var taskId = idCol ? String(values[idCol - 1]) : '';
    var touchedOnlyLastUpdated = true;

    for (var c = firstCol; c < firstCol + numCols; c++) {
      var field = header[c - 1] || 'Column ' + c;
      if (c === updatedCol) continue;
      touchedOnlyLastUpdated = false;
      var newValue = single && e.value !== undefined ? e.value : values[c - 1];
      var oldValue = single ? (e.oldValue === undefined ? '' : e.oldValue) : MULTI_CELL_OLD;
      audit.push([now, user, taskId, 'Edited', field, safe_(oldValue), safe_(newValue), '']);

      var status = String(newValue).trim();
      var previous = single && e.oldValue !== undefined ? String(e.oldValue).trim() : null;
      var restore = previous === null ? 'In Progress' : previous;
      var block = function (reason, toast) {
        sheet.getRange(row, statusCol).setValue(restore);
        audit.push([now, user, taskId, 'Blocked', field, status, safe_(restore), reason]);
        SpreadsheetApp.getActive().toast((taskId || 'Row ' + row) + ': ' + toast, 'GRC tracker', 8);
      };
      if (c === statusCol && status === 'Done') {
        var submitted = submittedCol ? values[submittedCol - 1] : '';
        var link = linkCol ? String(values[linkCol - 1]).trim() : '';
        var needsReview = needsReviewCol
          ? /^(yes|y|ya|true)$/i.test(String(values[needsReviewCol - 1]).trim())
          : false;
        if (submitted === '' || submitted === null || link === '') {
          block(DONE_BLOCKED, 'fill Submitted date and Link before setting Done.');
        } else if (needsReview && previous !== null && previous !== 'Approved') {
          block(REVIEW_BLOCKED, 'this task needs review; it must be Approved before Done.');
        }
      } else if (c === statusCol && (status === 'Approved' || status === 'Returned')) {
        var reviewerName = reviewerCol ? String(values[reviewerCol - 1]).trim() : '';
        var doerName = doerCol ? String(values[doerCol - 1]).trim() : '';
        if (!canReview_(user, reviewerName, doerName)) {
          block(REVIEWER_BLOCKED, 'only the reviewer or a Manager can approve or return.');
        }
      }
    }
    if (updatedCol && !touchedOnlyLastUpdated) {
      sheet.getRange(row, updatedCol).setValue(now);
    }
  }

  if (audit.length) {
    var log = SpreadsheetApp.getActive().getSheetByName(AUDIT);
    if (!log) throw new Error('GRC tracker: AuditLog tab is missing');
    log.getRange(log.getLastRow() + 1, 1, audit.length, audit[0].length).setValues(audit);
  }
}

function editorEmail_(e) {
  var email = '';
  try {
    if (e.user && typeof e.user.getEmail === 'function') email = e.user.getEmail();
  } catch (ignored) {}
  if (!email) {
    try {
      email = Session.getActiveUser().getEmail();
    } catch (ignored) {}
  }
  return email || 'unknown (outside the domain)';
}

/** Text that Sheets would read as a formula is stored as text. */
function safe_(value) {
  if (value instanceof Date || typeof value !== 'string') return value;
  return /^[=+\-@]/.test(value) ? "'" + value : value;
}

if (typeof module !== 'undefined') {
  module.exports = {
    onMasterEdit: onMasterEdit,
    installAuditTrigger: installAuditTrigger,
    installValidation: installValidation
  };
}
