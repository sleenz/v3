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
