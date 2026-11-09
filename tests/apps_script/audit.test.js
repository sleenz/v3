// Runs apps_script/audit.gs against an in-memory spreadsheet (node --test, no dependencies).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../apps_script/audit.gs'), 'utf8');
const HEADER = ['ID', 'Title', 'Status', 'Submitted date', 'Link', 'Last updated', 'Doer'];

function makeSheet(name, rows) {
  const data = rows.map((r) => r.slice());
  const sheet = {
    getName: () => name,
    data,
    getLastRow: () => data.length,
    getLastColumn: () => Math.max(...data.map((r) => r.length)),
    getRange: (row, col, numRows = 1, numCols = 1) => makeRange(sheet, row, col, numRows, numCols),
  };
  return sheet;
}

function makeRange(sheet, row, col, numRows, numCols) {
  const cell = (r, c) => {
    while (sheet.data.length < r) sheet.data.push([]);
    const line = sheet.data[r - 1];
    while (line.length < c) line.push('');
    return line;
  };
  return {
    getSheet: () => sheet,
    getRow: () => row,
    getColumn: () => col,
    getNumRows: () => numRows,
    getNumColumns: () => numCols,
    getValues: () =>
      Array.from({ length: numRows }, (_, i) =>
        Array.from({ length: numCols }, (_, j) => {
          const v = (sheet.data[row - 1 + i] || [])[col - 1 + j];
          return v === undefined ? '' : v;
        })
      ),
    setValue: (v) => {
      cell(row, col)[col - 1] = v;
    },
    setValues: (values) =>
      values.forEach((line, i) =>
        line.forEach((v, j) => {
          cell(row + i, col + j)[col - 1 + j] = v;
        })
      ),
  };
}

function setup(masterRows, opts = {}) {
  const master = makeSheet('Master', [HEADER, ...masterRows]);
  const audit = makeSheet('AuditLog', [['Timestamp', 'User', 'Task ID', 'Action', 'Field', 'Old value', 'New value', 'Reason']]);
  const toasts = [];
  const triggers = [];
  const staff = opts.staff ? makeSheet('Staff', opts.staff) : null;
  const validations = [];
  master.getMaxRows = () => 1000;
  const realGetRange = master.getRange;
  master.getRange = (row, col, numRows = 1, numCols = 1) => {
    const range = realGetRange(row, col, numRows, numCols);
    range.setDataValidation = (rule) => validations.push({ col, row, numRows, rule });
    return range;
  };
  const spreadsheet = {
    getSheetByName: (n) => ({ Master: master, AuditLog: audit, Staff: staff }[n] || null),
    toast: (msg) => toasts.push(msg),
  };
  const builder = () => {
    const rule = {};
    const b = {
      requireValueInList: (list) => { rule.list = list; return b; },
      setAllowInvalid: (allow) => { rule.allowInvalid = allow; return b; },
      build: () => rule,
    };
    return b;
  };
  const context = {
    SpreadsheetApp: { getActive: () => spreadsheet, newDataValidation: builder },
    Session: { getActiveUser: () => ({ getEmail: () => opts.sessionEmail || '' }) },
    ScriptApp: {
      getProjectTriggers: () => triggers,
      deleteTrigger: (t) => triggers.splice(triggers.indexOf(t), 1),
      newTrigger: (fn) => ({
        forSpreadsheet: () => ({ onEdit: () => ({ create: () => triggers.push({ getHandlerFunction: () => fn }) }) }),
      }),
    },
    module: { exports: {} },
  };
  vm.createContext(context);
  vm.runInContext(SOURCE, context);
  return { master, audit, toasts, triggers, validations, api: context.module.exports };
}

// Simulate a user typing: write the new values first, as Sheets does, then fire the trigger.
function edit(env, row, col, value, oldValue, email = 'staff.a@example.com', numRows = 1, numCols = 1) {
  const range = env.master.getRange(row, col, numRows, numCols);
  if (numRows === 1 && numCols === 1) range.setValue(value);
  else range.setValues(value);
  const e = { range, user: { getEmail: () => email } };
  if (numRows === 1 && numCols === 1) {
    e.value = value;
    if (oldValue !== undefined) e.oldValue = oldValue;
  }
  env.api.onMasterEdit(e);
}

const auditRows = (env) => env.audit.data.slice(1);
const isDate = (v) => Object.prototype.toString.call(v) === '[object Date]'; // dates come from the vm realm
const colOf = (name) => HEADER.indexOf(name) + 1;

test('a single edit is audited with who, field, old and new, and Last updated is set', () => {
  const env = setup([['G-0001', 'Title', 'Not Started', '', '', '', 'Staff A']]);
  edit(env, 2, colOf('Status'), 'In Progress', 'Not Started');
  const [row] = auditRows(env);
  assert.deepStrictEqual(row.slice(1), ['staff.a@example.com', 'G-0001', 'Edited', 'Status', 'Not Started', 'In Progress', '']);
  assert.ok(isDate(row[0]));
  assert.ok(isDate(env.master.data[1][colOf('Last updated') - 1]));
});

test('Done without proof is undone and logged as Blocked', () => {
  const env = setup([['G-0002', 'Title', 'In Progress', '', '', '', 'Staff A']]);
  edit(env, 2, colOf('Status'), 'Done', 'In Progress');
  assert.strictEqual(env.master.data[1][colOf('Status') - 1], 'In Progress');
  const actions = auditRows(env).map((r) => r[3]);
  assert.deepStrictEqual(actions, ['Edited', 'Blocked']);
  assert.strictEqual(auditRows(env)[1][7], 'Done needs Submitted date and Link');
  assert.strictEqual(env.toasts.length, 1);
});

test('Done with a date but no link is still blocked', () => {
  const env = setup([['G-0003', 'T', 'In Progress', new Date(2026, 10, 5), '', '', 'Staff A']]);
  edit(env, 2, colOf('Status'), 'Done', 'In Progress');
  assert.strictEqual(env.master.data[1][colOf('Status') - 1], 'In Progress');
});

test('Done with Submitted date and Link is allowed', () => {
  const env = setup([['G-0004', 'T', 'In Progress', new Date(2026, 10, 5), 'https://drive/x', '', 'Staff A']]);
  edit(env, 2, colOf('Status'), 'Done', 'In Progress');
  assert.strictEqual(env.master.data[1][colOf('Status') - 1], 'Done');
  assert.deepStrictEqual(auditRows(env).map((r) => r[3]), ['Edited']);
});

test('columns are found by header name, not position', () => {
  const env = setup([]);
  // Move Status to the far right by rewriting the header and the row.
  env.master.data[0] = ['Link', 'ID', 'Submitted date', 'Title', 'Last updated', 'Doer', 'Status'];
  env.master.data.push(['', 'G-0005', '', 'T', '', 'Staff A', 'In Progress']);
  edit(env, 2, 7, 'Done', 'In Progress');
  assert.strictEqual(env.master.data[1][6], 'In Progress');
  assert.strictEqual(auditRows(env)[0][2], 'G-0005');
});

test('a multi-cell paste is audited cell by cell without old values', () => {
  const env = setup([
    ['G-0006', 'T', 'Not Started', '', '', '', 'Staff A'],
    ['G-0007', 'T', 'Not Started', '', '', '', 'Staff A'],
  ]);
  edit(env, 2, colOf('Status'), [['In Progress', 'x'], ['On Hold', 'y']], undefined, 'staff.b@example.com', 2, 2);
  const rows = auditRows(env);
  assert.strictEqual(rows.length, 4);
  assert.deepStrictEqual(rows.map((r) => [r[2], r[4], r[6]]), [
    ['G-0006', 'Status', 'In Progress'], ['G-0006', 'Submitted date', 'x'],
    ['G-0007', 'Status', 'On Hold'], ['G-0007', 'Submitted date', 'y'],
  ]);
  assert.ok(rows.every((r) => r[5] === '(not available: multi-cell edit)'));
});

test('a pasted Done without proof is undone too', () => {
  const env = setup([['G-0008', 'T', 'In Progress', '', '', '', 'Staff A']]);
  const range = env.master.getRange(2, colOf('Status'), 1, 1);
  range.setValues([['Done']]);
  env.api.onMasterEdit({ range, user: { getEmail: () => 'staff.a@example.com' } }); // no e.value
  assert.strictEqual(env.master.data[1][colOf('Status') - 1], 'In Progress');
  assert.deepStrictEqual(auditRows(env).map((r) => r[3]), ['Edited', 'Blocked']);
});

test('header row and other tabs are ignored; editing only Last updated does not touch it', () => {
  const env = setup([['G-0009', 'T', 'Not Started', '', '', '', 'Staff A']]);
  edit(env, 1, 2, 'Renamed', 'Title');
  assert.strictEqual(auditRows(env).length, 0);
  const other = makeSheet('Staff', [['Name']]);
  env.api.onMasterEdit({ range: other.getRange(1, 1), value: 'x' });
  assert.strictEqual(auditRows(env).length, 0);
  const stamp = new Date(2026, 0, 1);
  edit(env, 2, colOf('Last updated'), stamp, '');
  assert.strictEqual(env.master.data[1][colOf('Last updated') - 1], stamp);
  assert.strictEqual(auditRows(env).length, 0);
});

test('formula-looking text is stored as text in AuditLog', () => {
  const env = setup([['G-0010', 'T', 'Not Started', '', '', '', 'Staff A']]);
  edit(env, 2, colOf('Title'), '=HYPERLINK("x")', '+62 812');
  const [row] = auditRows(env);
  assert.strictEqual(row[5], "'+62 812");
  assert.strictEqual(row[6], '\'=HYPERLINK("x")');
});

test('editor email falls back to the session, then to unknown', () => {
  const env = setup([['G-0011', 'T', 'Not Started', '', '', '', 'Staff A']], { sessionEmail: 'mgr@example.com' });
  const range = env.master.getRange(2, colOf('Title'));
  env.api.onMasterEdit({ range, value: 'x', oldValue: 'T' });
  assert.strictEqual(auditRows(env)[0][1], 'mgr@example.com');
  const env2 = setup([['G-0012', 'T', 'Not Started', '', '', '', 'Staff A']]);
  env2.api.onMasterEdit({ range: env2.master.getRange(2, colOf('Title')), value: 'x', oldValue: 'T' });
  assert.strictEqual(auditRows(env2)[0][1], 'unknown (outside the domain)');
});

test('installAuditTrigger replaces an existing trigger instead of adding a second', () => {
  const env = setup([]);
  env.api.installAuditTrigger();
  env.api.installAuditTrigger();
  assert.strictEqual(env.triggers.length, 1);
  assert.strictEqual(env.triggers[0].getHandlerFunction(), 'onMasterEdit');
});


// --- v1.4: review statuses (DESIGN section 19) ---------------------------------------------------

const REVIEW_HEADER = ['ID', 'Status', 'Submitted date', 'Link', 'Last updated', 'Doer', 'Reviewer',
  'Needs review'];
const STAFF = [
  ['Name', 'Email', 'Role', 'Reports to'],
  ['Boss One', 'boss@example.com', 'Manager', ''],
  ['Lead Two', 'lead@example.com', 'Full time', 'Boss One'],
  ['Intern Three', 'intern@example.com', 'Intern', 'Lead Two'],
  ['Other Four', 'other@example.com', 'Full time', 'Boss One'],
];

function reviewSetup(row) {
  const env = setup([], { staff: STAFF });
  env.master.data[0] = REVIEW_HEADER.slice();
  env.master.data.push(row);
  return env;
}
const rcol = (name) => REVIEW_HEADER.indexOf(name) + 1;

test('Approved by someone who is not the reviewer is undone', () => {
  const env = reviewSetup(['G-0101', 'In Review', '', '', '', 'Intern Three', '', 'Yes']);
  edit(env, 2, rcol('Status'), 'Approved', 'In Review', 'other@example.com');
  assert.strictEqual(env.master.data[1][rcol('Status') - 1], 'In Review');
  assert.strictEqual(auditRows(env)[1][7], 'Only the reviewer or a Manager can approve or return');
});

test("the intern's full-time supervisor is the default reviewer", () => {
  const env = reviewSetup(['G-0102', 'In Review', '', '', '', 'Intern Three', '', 'Yes']);
  edit(env, 2, rcol('Status'), 'Returned', 'In Review', 'lead@example.com');
  assert.strictEqual(env.master.data[1][rcol('Status') - 1], 'Returned');
});

test('a Manager can always approve; a named Reviewer replaces the default', () => {
  const env = reviewSetup(['G-0103', 'In Review', '', '', '', 'Intern Three', 'Other Four', 'Yes']);
  edit(env, 2, rcol('Status'), 'Approved', 'In Review', 'lead@example.com');
  assert.strictEqual(env.master.data[1][rcol('Status') - 1], 'In Review'); // not the reviewer now
  edit(env, 2, rcol('Status'), 'Approved', 'In Review', 'other@example.com');
  assert.strictEqual(env.master.data[1][rcol('Status') - 1], 'Approved');
  const env2 = reviewSetup(['G-0104', 'In Review', '', '', '', 'Lead Two', '', 'Yes']);
  edit(env2, 2, rcol('Status'), 'Approved', 'In Review', 'boss@example.com');
  assert.strictEqual(env2.master.data[1][rcol('Status') - 1], 'Approved');
});

test('Done on a task that needs review is undone unless it was Approved', () => {
  const proof = new Date(2026, 10, 5);
  const env = reviewSetup(['G-0105', 'In Progress', proof, 'In the GRC bot mailbox: p.pdf', '',
    'Lead Two', '', 'Yes']);
  edit(env, 2, rcol('Status'), 'Done', 'In Progress');
  assert.strictEqual(env.master.data[1][rcol('Status') - 1], 'In Progress');
  assert.strictEqual(auditRows(env)[1][7], 'Done needs the review to be Approved first');
  edit(env, 2, rcol('Status'), 'Done', 'Approved');
  assert.strictEqual(env.master.data[1][rcol('Status') - 1], 'Done');
});

test('installValidation puts dropdowns on Status, Needs review and Reminder profile', () => {
  const env = setup([]);
  env.master.data[0] = ['ID', 'Status', 'Needs review', 'Reminder profile'];
  env.api.installValidation();
  const lists = env.validations.map((v) => [v.col, v.rule.list.length, v.rule.allowInvalid]);
  assert.deepStrictEqual(lists, [[2, 9, false], [3, 2, false], [4, 3, false]]);
});
