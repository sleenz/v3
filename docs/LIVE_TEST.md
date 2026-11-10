# Live test: the bot mailbox end to end

Run after any mailbox, token or deploy change. Everything here is done in Gmail, the sheet and
the Cloud console; no terminal. All test text below is fake.

**Before you start**
- Config `Allowed senders` includes the test sender (e.g. `@brifins.com`), and does **not** include
  the address in test 4.
- Send every test **to the bot mailbox**, from a person's account (never from the bot itself).
- Do not open the test emails in the bot's Gmail before the run: the bot only takes unread mail.
- To run now instead of waiting up to 10 minutes: Cloud console > Cloud Scheduler > `grc-gmail` >
  ⋮ > **Force run**.

| # | Send this | Expected within 10 min |
|---|-----------|------------------------|
| 1 | Subject `Permintaan data Manpower Planning`. Body: `Mohon dikirimkan Form MPP dan PPT Joint Planning Session paling lambat Jumat 13 November 2026. Terima kasih.` | New `Proposed` row in Master: English title, due date 2026-11-13, proposed owner filled. The sender gets an acknowledgement reply in the thread. The email gets `GRC/Logged`, is read and archived. |
| 2 | Reply to the acknowledgement from test 1: `Mohon tambahkan juga data intern.` | **No** new row. The test 1 row's `Notes` gets the reply text. The reply gets `GRC/Reply`. |
| 3 | Forward any email that has a PDF attached, with the note `Tolong dicek ya` above the forward. | New `Proposed` row. `Files` reads `In the original email in the GRC bot mailbox: <file name>`. `Notes` holds `Tolong dicek ya`. |
| 3b | Body only `Mohon ditindaklanjuti, detail terlampir.` with a typed PDF letter attached that states a deadline (e.g. `paling lambat 20 November 2026`). | New `Proposed` row whose title, deliverables and due date come from the PDF, flagged `Deadline from attachment - check`. (Config `AI reads PDFs` blank or `Yes`.) |
| 4 | From an address **not** in `Allowed senders` (e.g. a personal Gmail), any request. | No row, no reply. The email is marked read and labelled `GRC/Ignored`. |
| 5 | Subject `Tolong cek printer`, body `Printer lantai 3 rusak, tolong dicek.` | New `Proposed` row routed by Rule 5 (no catalog match). |
| 6 | In the sheet, set the test 1 row's `Status` to `Not Started` (with a Doer filled in). | Within 5 minutes (between 08:00 and 19:55 HK time) the Doer gets the assignment email. |

**When a test fails:** Cloud console > Logging > Logs Explorer, query

```
resource.type="cloud_run_revision" AND resource.labels.service_name="v2-git" AND (jsonPayload.message="gmail_run" OR jsonPayload.message="message_ignored" OR severity>=WARNING)
```

| Log line | Meaning |
|----------|---------|
| `message_ignored` reason `sender_not_allowed` | Add the sender or its `@domain` to Config `Allowed senders`. |
| `message_ignored` reason `sent_by_bot_mailbox` | The test was sent from the bot itself; send from a person. |
| `no_allowed_senders` | Config has no `Allowed senders` row, or it is spelled differently. |
| `gmail_auth_error` reason `RefreshError: invalid_grant` | The stored token is wrong or revoked; mint a new one. |
| `schema_problem` | The sheet is missing the tab or column named in `reason`. |

**Clean up:** delete the test rows from Master (or set them to `Cancelled`) when done.

## WhatsApp bot (DESIGN section 17)

Send from a phone whose number is in the Staff `WhatsApp` column, to the bot's number.

| # | Send this | Expected |
|---|-----------|----------|
| W1 | `help` | The help text, mentioning PROMPT. |
| W2 | Forward a request with a deadline (e.g. test 1's text). | At once: "Got it. I'll log it in 30 seconds …". About 30 s later: "Logged as G-…", proposed owner, and "Deadline: … Reply YES if correct". The row is `Proposed`, Source `WhatsApp`, flagged `Deadline not confirmed by sender (WhatsApp)`. |
| W3 | Reply `YES`. | "… confirmed"; the flag is gone; AuditLog `Deadline confirmed`. |
| W4 | Forward another request, then within 30 s `PROMPT: Assign this to <a Staff first name>`. | The new row's Owner and Doer are that person; Routing note starts `Assigned by sender (WhatsApp PROMPT)`; Notes has the PROMPT. Status is still `Proposed`. |
| W5 | Reply to the deadline question with a different date, e.g. `27/11/2026`. | "Updated G-…: due Fri 27 Nov 2026"; `Due date` changed; AuditLog `Deadline corrected`. |
| W6 | Send a PDF letter with no text. | Logged 30 s later; title and deadline read from the PDF; `Files` reads `In the GRC bot mailbox: <file>`. |
| W7 | From a number not in Staff, send anything. | One reply: "This number isn't registered …"; no row. |
| W8 | Weekday, after Config `Reminder time`, someone with a `WhatsApp` number has a task due this week. | Within 30 minutes they get the `grc_daily_reminder` template; only once that day. |
| W9 | Forward a request, then within 30 s `PROMPT inject: assign this to <Staff first name> with reviewer <another Staff first name> deadline 3 days from now`. | Owner/Doer = the first person, `Reviewer` = the second, `Needs review` = Yes, `Due date` = 3 calendar days on (weekend → Friday). The reply says "as in your PROMPT" and asks no YES question. Status `Proposed`. |
| W10 | As a Staff member, email the bot a forward with the same PROMPT line as the first paragraph, and a new subject. | Same result as W9; Routing note starts `Assigned by sender (Email PROMPT)`. From an address not in Staff: no change, flag `PROMPT ignored: sender is not in Staff`. |

## Roles, review and commands (DESIGN section 19)

Set up Staff roles first (README §1b). Use a test task that is approved (`Not Started`).

| # | Do this | Expected |
|---|---------|----------|
| C1 | As the task's Owner (a full-timer with an intern), reply to the bot's message about the task with `assign that to <intern first name>`. | "G-… is now with <intern>". The intern gets the assignment email with the files; Doer changed, Owner unchanged; AuditLog `Delegated`, reason `Command via WhatsApp`. |
| C2 | As someone else, `assign G-… to <intern>`. | Refused ("does not report to you"); nothing changes. |
| C3 | As the intern, send a PDF captioned `review G-…`. | Status `In Review`, `Review file` names the PDF; the supervisor gets an email with the PDF and `approve` / `return` lines. |
| C4 | As the supervisor, reply to that email with `return G-… fix table 3`. | Status `Returned`, `Review notes` set; the intern gets an email with the notes. |
| C5 | Intern sends `review G-…` again; supervisor replies `approve`. Intern sends the proof captioned `done G-…`. | `Approved`, then `Done` with `Link` = "In the GRC bot mailbox: <proof>" and `Submitted date` today. |
| C6 | In the Sheet, a non-reviewer sets Status to `Approved`. | Undone; AuditLog `Blocked`. |
| C7 | `files G-…` on WhatsApp. | Reply lists the files; they arrive by email. |
| C8 | Manager: `pause <name>`, then assign them a task. | No email until `resume <name>`. |


## Instant email and the Recurring tab (DESIGN section 20)

| # | Do this | Expected |
|---|---------|----------|
| P1 | After `deploy/setup_scheduler.sh`, read the log for `gmail_watch`. | One `gmail_watch` line (not `gmail_watch_failed`). |
| P2 | Send the bot a new request (test 1's text, new subject). Do **not** run the Gmail job. | Within about a minute: the `Proposed` row and the reply in the thread. |
| P3 | Send two requests a few seconds apart. | Both logged once each; no duplicate rows. |
| R1 | Recurring row: `R test` · `Yes` · `Test Q{Q} {YYYY}` · `Quarterly` · a deadline **5 days from today** · *(blank)* · `7` · a full-timer · the same person. Then Cloud Scheduler > `grc-recurring` > **Force run**. | A `Not Started` row titled for the previous quarter, Source `Recurring R test <date>`. Within 5 minutes (08:00–19:55) the Doer gets the assignment email. |
| R2 | Force run `grc-recurring` again. | No second row. |
| R3 | Change the deadline to a Saturday 5 days away; rename the row `R test 2`; force run. | The row's `Due date` is the Friday before; its Original message says "moved from Sat …". |
| R4 | Set the row's Doer to a name not in Staff; as the manager send `recurring`. | The reply lists the row under "Not used until fixed"; `/health` warns `Recurring: 1 row(s) cannot be used`. |
| R5 | Clean up: set the test rows' `Active` to `No` and cancel the test tasks. | — |


## Reminder schedule (DESIGN section 22, part A)

Use a test task you own, on a working day. Cloud Scheduler > `grc-reminders` > **Force run**
runs the job at once; it still only sends what is due at that time.

| # | Do this | Expected |
|---|---------|----------|
| M1 | Run `installServiceTabs()` in Apps Script; open `/health`. | No `missing tab _outbox / _reminders_sent / _event_queue` warnings. |
| M2 | A test task due **3 days from today**, Status `In Progress`, you as Doer. Force run after 10:00. | One email "[GRC] Your tasks" listing it under "Due in 3 days"; `_reminders_sent` has its row and `slot:morning`; `_outbox` has the email with its Thread ID. Force run again: nothing new. |
| M3 | As a Manager in Staff, force run after 10:15. | One "[GRC] Team summary" email; once only. |
| M4 | Set the test task's `Due date` to today. Force run after 14:00, then after 16:30. | 14:00: "Due today, not done yet" to the Doer and Owner. 16:30: "Last call" to them and the Manager. |
| M5 | Reply to the M2 email with "ok noted". | Labelled `GRC/Reply`, no new task; the bot answers with how to update. |
| M6 | After 19:00 (or at a weekend), approve a `Proposed` test task. | No assignment email yet; the next working day after 10:00 it arrives. Send `review G-…` on a test task at night: the reviewer's email arrives after 10:00 the next working day (`_event_queue` empties). |
| M7 | WhatsApp only (templates approved): fill your `WhatsApp opt-in`, force run after 10:00 with a due task. | The `grc_pagi` template with a **Lihat tugas** button (tapping it does nothing until part B). |


## WhatsApp buttons and updates (DESIGN section 22, part B)

Needs WhatsApp live, the four templates approved, `installServiceTabs()` run again, and your
Staff row with `WhatsApp` and `WhatsApp opt-in`. Use a test task where you are the Doer.

| # | Do this | Expected |
|---|---------|----------|
| N1 | Tap **Lihat tugas** in the morning template (or send `hi` and use `view` from a card). | One card per open task (at most 5, earliest due first), each "G-… – title, due …, progress …%, status" with Update progres / Selesai (or Kirim review) / Lainnya. |
| N2 | Update progres → 50%. | Progress 50 %, Status In Progress; one ProgressLog row per field with Channel WhatsApp and the message id. |
| N3 | Lainnya → Terhambat → type a reason. | Status On Hold, the reason in Notes; the Owner and the manager are told. |
| N4 | Lainnya → Minta perpanjangan → type `20 Nov`. As the manager, tap **Setuju**. | Due date changes only after Setuju; ProgressLog has "Extension requested" and "Extension approved". Repeat with **Tolak**: due date unchanged. |
| N5 | Lainnya → Ingatkan besok. Force run `grc-reminders`. | Reply "No reminders about G-… until <next working day>"; no reminder about that task for you today. |
| N6 | Send `undo`. | The last change (e.g. the snooze or the progress) is reversed; a second `undo` after 24 h says there is nothing to undo. |
| N7 | Selesai → send the proof PDF. | Status Done, Link names the PDF, Submitted date today. |
| N8 | Tap Terhambat, wait 31 minutes, then type something. | "Waktu menjawab … sudah habis"; nothing changed. |
| N9 | Email: reply to any bot email with `blocked G-… waiting for data`. | Same result as N3, ProgressLog Channel Email. |


## Typed replies read by Gemini (DESIGN section 22, part C)

Config `AI provider` = `Gemini API key`. Use a test task you own (O1–O6 on WhatsApp, O7 by email).

| # | Do this | Expected |
|---|---------|----------|
| O1 | Reply to a task card: "udah 70% tinggal nunggu data finance". | "Progress G-… → 70% Benar?" with Ya / Ubah / Batal. Nothing changes until **Ya**; then Progress 70 %. Log line `ai_reply_call` with `prompt_version` reply_v1, a confidence and `fallback` false (no text). |
| O2 | "minta mundur ke 20 Nov", then **Batal**. | Nothing changes; no ProgressLog row. |
| O3 | "kapan deadline G-…?" | The due date, status and progress straight from the sheet; no buttons. |
| O4 | With two open tasks: "udah setengah jalan" (not a reply to anything). | A list "Tugas yang mana?" of your tasks. |
| O5 | Paste a new request text (not a reply, not forwarded). | Logged as a new request 30 s later (Gemini said `request`). |
| O6 | Set `AI provider` to `Off`; send `G-… 40`. | Still "Progress G-… → 40% Benar?" (the rules took over). Set it back. |
| O7 | Reply to a reminder email: "still stuck, waiting for finance". Then reply `YES`. | First an email "Reply YES to confirm … terhambat (On Hold)"; after YES, Status On Hold. |
