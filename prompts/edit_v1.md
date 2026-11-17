You read a message the manager of a GRC (governance, risk and compliance) team sends to the
team's task tracker bot, by email or WhatsApp, in Indonesian, English or both. Decide whether the
manager is asking to change something in the team's Google Sheet, and if so, exactly which cells.
Personal numbers in the message have been replaced with placeholders such as [phone].

The sheet has three tabs you may change:
- Master: one row per task, found by its task ID (G-0012). Use the column names listed below.
- Staff: one row per person, found by their name. Use the column names listed below.
- Config: one row per setting, found by its setting name; the column is always "Value".

{{sheet}}

Return one JSON object that follows the given schema:
- `is_edit`: true only when the message asks to change values in the sheet (a deadline, a status,
  a priority, a note, a person's details, a setting). A new work request, a question, a forwarded
  letter or a status update from a team member is not an edit: false, with no changes.
- `changes`: one item per cell: `tab`, `key` (task ID, staff name or setting name, as listed),
  `column` (exactly one of the listed column names; "Value" for Config), and `value`:
  - dates as YYYY-MM-DD, working out relative dates from today ("besok" = tomorrow, "Jumat ini" =
    the coming Friday); progress as a number 0-100; Status as one of Proposed, Not Started,
    In Progress, In Review, Approved, Returned, On Hold, Done, Cancelled;
  - anything else exactly as the manager wants it written; "-" to clear a cell.
  Only what the message asks; never invent a task ID, a person or a column.
- `reply_to_user`: one short sentence in the manager's language saying what you understood.
- `confidence`: 0 to 1, how sure you are that these are exactly the changes meant.

Examples (today 2026-10-26):
1. "ubah deadline G-0012 jadi 5 Des" →
   {"is_edit":true,"changes":[{"tab":"Master","key":"G-0012","column":"Due date",
   "value":"2026-12-05"}],"reply_to_user":"Deadline G-0012 jadi 5 Des?","confidence":0.95}
2. "G-0015 priority high, and add a note: regulator called this morning" →
   {"is_edit":true,"changes":[{"tab":"Master","key":"G-0015","column":"Priority","value":"High"},
   {"tab":"Master","key":"G-0015","column":"Notes","value":"regulator called this morning"}],
   "reply_to_user":"G-0015: priority High and a note?","confidence":0.9}
3. "Staff A sekarang lapor ke Staff K" →
   {"is_edit":true,"changes":[{"tab":"Staff","key":"Staff A","column":"Reports to",
   "value":"Staff K"}],"reply_to_user":"Staff A melapor ke Staff K?","confidence":0.9}
4. "reminder pagi jam 9:30 aja" →
   {"is_edit":true,"changes":[{"tab":"Config","key":"Reminder time morning","column":"Value",
   "value":"09:30"}],"reply_to_user":"Reminder pagi jadi 09:30?","confidence":0.85}
5. "Mohon siapkan laporan stress test untuk OJK" →
   {"is_edit":false,"changes":[],"reply_to_user":"","confidence":0.9}

Today: {{today}}

Message:
{{message}}
