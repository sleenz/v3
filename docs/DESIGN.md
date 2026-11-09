# GRC Request Intake and Task Assignment — Build Spec v1.2

Status: final design for the build (v1.2: bot mailbox intake, AI Studio Gemini API key; v1.3:
WhatsApp bot, section 17; v1.4: roles, review lifecycle and commands, section 19). Source of truth
for Claude Code. Sections 15 to 19 record the decisions made while building; where they are more
specific than the text above, they win. Lines changed for them are marked **(v1.1)** to **(v1.4)**.
Business context: see `docs/PRD.docx` (PRD with the TRD as Appendix A).
Owner: Enzo Taslim (builder). Hosting: BRI HK IT (Google Cloud).

---

## 1. What the system does (one paragraph)

The GRC manager forwards requests from BRI Pusat, regulators and auditors to the system, either by
forwarding or sending an email to the GRC bot mailbox (e.g. `grc-bot@<company domain>`) or by pasting
WhatsApp text to a Google Chat bot. A Python
service on Cloud Run reads each request, saves its attachments to a Drive folder, extracts the
deadline, letter number and contacts by fixed rules, optionally asks an AI for an English title,
summary and deliverables, proposes an owner by fixed rules, and writes one row to the `Master` tab of
the GRC Google Sheet with status `Proposed`. When the manager approves (or someone hands the task
off), the doer gets an email with the full original message and file links. Daily digests remind
everyone. Every change is logged.

**The AI never chooses who does the work.** Fixed rules propose; the manager approves.

---

## 2. Final design decisions

| # | Decision |
|---|---|
| D1 | Python 3.12, FastAPI, one container on Cloud Run. `max-instances=1`, `min-instances=0`. All sheet writes go through one in-process lock. |
| D2 | The Google Sheet is the only data store. The service reads and writes columns **by header name**, never by position. |
| D3 | Intake is a **dedicated Google Workspace bot mailbox** (company domain, created by IT; never a personal @gmail.com). The service reads every new email in its inbox using an **OAuth refresh token for that mailbox**, stored in Secret Manager. Scopes: `gmail.modify`, `gmail.send`. No domain-wide delegation. |
| D4 | Acknowledgements, assignment emails and digests are sent from the bot mailbox. The display name comes from Config. |
| D5 | Sheets and Drive use the runtime service account (`grc-tracker-run`). Attachments go to a Shared Drive folder per task: `G-0012 – <title>`. **(v1.2, decision A)** Drive is **off by default**: Config `File storage` = `None` stores no files (see section 16, item 7); `Drive` turns the folders back on. |
| D6 | AI goes through one interface, `AIProvider.extract(text, catalog) -> AIResult`, with two implementations: `GeminiProvider` (the `google-genai` SDK in either **API-key mode**, key from Secret Manager, or **Vertex mode**) and `NullProvider`. Chosen by the Config setting `AI provider` (`Gemini API key` / `Vertex` / `Off`). Version 1 uses API-key mode. |
| D7 | Google Chat app over HTTP to `POST /chat`; synchronous replies; Google-signed token verified on every call. |
| D8 | Edits made by hand in the sheet are audited by a small Apps Script edit trigger (`apps_script/audit.gs`), because only Apps Script sees the editor's identity. The service writes its own actions to AuditLog. |
| D9 | Approvals and handoffs are detected by comparing `Master` with a hidden `_state` tab every 5 minutes. |
| D10 | Timed jobs: Cloud Scheduler with OIDC tokens → `POST /jobs/*`. |
| D11 | Time zone `Asia/Hong_Kong` everywhere. Dates are stored as real sheet dates. |
| D12 | Original message: up to 45,000 characters in the `Original message` cell (Sheets limit is 50,000); the full text is always saved as `original.txt` in the task folder. |
| D13 | A weekday that does not match its date (e.g. "Rabu 06 November 2026" = Friday) keeps the written date as the deadline and adds a flag. Never silently change a deadline. |
| D14 | Routing can match on **known contacts** (emails or full names) stored per Catalog row, as well as keywords. |
| D15 | Failure never loses a request: an email is only labelled and archived after the row is written; a job retry is always safe (idempotent by `Source ID`). |
| D16 | Only emails from **allowed senders** (Config `Allowed senders`: addresses or `@domain`) become tasks. Others get the label `GRC/Ignored` and no task. |
| D17 | Cloud Run region: `asia-southeast1` (Singapore) while the AI Studio key is used. Hong Kong is not in the Gemini API's available regions; Singapore and Indonesia are. **(v1.2, decided at deploy)** The service runs in **`asia-southeast2` (Jakarta)** as `v2-git`. |
| D18 | The Gemini API terms allow access only from an available region. A Singapore server serving Hong Kong staff is a grey area: **Compliance/IT must confirm before go-live**; the fallback is Vertex mode (Config switch), which Google recommends for unlisted regions. |

---

## 3. Data model (Google Sheet)

Tabs already exist (from `GRC_Tracker_Tabs_v2.xlsx`). The service must not depend on column order.
Missing required columns → the job stops and alerts (section 11).

### Master (one row per task)
Required columns (exact header text):

`ID`, `Type`, `Title`, `Catalog ID`, `Category`, `Division`, `Requester / Regulator`, `Owner`, `Doer`,
`Support`, `Whole team`, `Letter no.`, `Letter code`, `Request reference`, `Start / received`,
`Due date`, `Submitted date`, `Priority`, `Effort`, `Weight` (formula), `Progress`, `Status`,
`Last updated`, `Link`, `Send to`, `Notes`, `Flag`, `Source`, `Routing note`,
**new:** `Original message`, `Files`, `AI summary`, `Deliverables`, `Contacts`, `Source ID`.

- `ID`: `G-0001` upward. The next number is the highest existing `G-` number + 1, computed under the write lock.
- `Status` values: `Proposed`, `Not Started`, `In Progress`, **(v1.4)** `In Review`, `Approved`,
  `Returned`, `On Hold`, `Done`, `Cancelled`. **(v1.4)** New columns at the right: `Needs review`,
  `Reviewer`, `Review notes`, `Reminder profile`, `Review file` (section 19). They are optional:
  without them the service runs, `/health` warns, and a command that needs one names it.
- `Source`: `Email`, `Google Chat`, **(v1.3)** `WhatsApp`, **(v1.4 part 3)**
  `Recurring <Recurring row Name> <yyyy-mm-dd>` (section 20), `Manual`.
- `Source ID`: Gmail message ID, Chat message name, or the recurring key. Used to skip duplicates.
- `Weight`: write the same formula the existing rows use (looks up Effort in Config; blank = Medium).
- `Link`: proof of submission (filled by people). `Files`: the Drive folder URL (filled by the service).
- `Flag`: `; `-separated short notes (e.g. `Weekday mismatch: Rabu = Wed, 06 Nov 2026 is Fri`).

### Staff
`Name`, `Email`, `Main division`, `Role` (Staff/Intern/Manager), `Internship end date`,
`Capacity (1 = full time)`, `Weighted open load` (formula; read its computed value).
**(v1.3)** Optional `WhatsApp`: the person's number in international format (`+852 …`); needed to
use the WhatsApp bot and to get WhatsApp reminders. **(v1.4)** Optional `Title`, `Department`,
`Divisions`, `Reports to`, `Reminder channel`, `WhatsApp opt-in`, `Reminders paused`; `Role` values
`Manager`, `Full time`, `Intern`, `Additional help` (`Staff` reads as `Full time`). Section 19.

### Catalog
`Catalog ID`, `Report type`, `Division`, `Regulator / Institution`, `Recurrence`, `Default owner`,
`Backup`, `Typical due day`, `Due months seen (2026)`, `Match keywords` (comma-separated),
**new:** `Known contacts` (comma-separated emails or full names), `Confirmed by manager?` (Yes/No).
Rows with IDs starting `ADHOC-` have no default owner and are never used by routing rule 1.

### LetterCodes
`Letter code`, `Route to division`.

### Recurring (optional) **(v1.4 part 3)**
`Name`, `Active`, `Title`, `Repeat`, `First due date`, `Title period`, `Create days before`,
`Owner`, `Doer`, `Needs review`, `Reviewer`, `Catalog ID`, `Notes`. `Name`, `Title`, `Repeat`,
`First due date` and `Doer` are required columns; the tab itself is optional. Section 20.

### AuditLog (protected; service and Apps Script only)
`Timestamp`, `User`, `Task ID`, `Action`, `Field`, `Old value`, `New value`, `Reason`.

### Config (Setting → Value)
Existing: `Effort: Small/Medium/Large`, `Support share`, `Overload threshold`, `Stale after (days)`,
`Reminder time`, `Leaving warning (days)`, `Unknown letter code`, `Manager email`,
`Allowed email domain`, `Gmail intake label`, `Gmail done label`, `Recurring lead (days)`,
`Default due (days)`. (`Gmail intake label` is no longer used: the bot reads its whole inbox.)
**New:** `Allowed senders`, `AI provider` (`Gemini API key` / `Vertex` / `Off`), `AI model`, `AI region`, `Sender name`,
`Mask emails for AI` (Yes/No, default No). `AI model` default `gemini-3.8-flash`.
**(v1.2)** `File storage` (`None` default / `Drive`). **(v1.2 item 11)** `AI reads PDFs`
(Yes/No, blank = Yes).
**(v1.1)** `Requester domains` (`domain=Org` pairs, `;`-separated), `Known requesters`
(comma-separated org names), `Builder email` (alert recipient), all optional.

### _state (hidden, service only)
`ID`, `Status`, `Owner`, `Doer`, `Notified doer`, `Notified at`.

### _failures (hidden, service only) **(v1.1)**
`Key`, `Count`, `Last error`, `Last at`. `Key` is a `Source ID` (failure counter, section 11) or
`alert:<kind>` (alert throttle). Needed because the instance scales to zero. **(v1.3)** Also
`wa-reminders:<yyyy-mm-dd>`: the day's WhatsApp reminders went out.

### _wa_pending (hidden, service only) **(v1.3)**
`Key`, `Kind`, `Sender`, `Text`, `Prompt`, `Media`, `Message IDs`, `Task ID`, `Status`,
`Created at`, `Updated at`. Only the WhatsApp bot reads it (section 17); without it, email and Chat
work as before.

---

## 4. Request processing pipeline

Every request (email, chat, recurring) goes through the same function:
`process_request(raw: RawRequest) -> TaskResult`.

```
RawRequest
  source, source_id, received_at, subject, body_text, body_html,
  sender_name, sender_email, attachments[(filename, mime, bytes)], submitted_by
```

1. **Dedupe.** If `source_id` already exists in `Master.Source ID`, return the existing task.
2. **Unwrap forwards** (email only). If the body contains a forwarded block
   (`---------- Forwarded message ---------`, `-----Original Message-----`, `Dari:`/`From:` header
   block), the **forwarded part is the request**, its original `From` is the requester, and the
   manager's text above it is stored in `Notes` as `Manager note: …`. For nested forwards use the
   innermost block.
3. **Clean** (used for matching, titles and AI; the untouched original is stored separately):
   - drop greeting and sign-off lines: `Selamat (pagi|siang|sore|malam)`, `Yth`, `Dengan hormat`,
     `Dear`, `Hi/Halo`, `Terima kasih…`, `Salam`, `Regards`, `Best`, `Thanks`, signature blocks;
   - drop `**` and `*` emphasis marks, collapse whitespace;
   - drop quoted reply chains below the request (`On … wrote:`, `Pada … menulis:`).
4. **Mask** (copy used only for AI): phone numbers → `[phone]`, account numbers (8+ digits) →
   `[account]`, HKID/KTP/passport patterns → `[id]`. Emails are kept unless Config says otherwise.
5. **Extract by rules** (`extract/rules.py`, pure functions, fully unit-tested):
   - `letter_no`, `letter_code`: regex for `B.91.e-EMR/IRM/10/2026` style → code `EMR/IRM`
     (first two unit parts, upper case). Spaces around `.` and `-` are allowed.
   - `due_date`: dates in `06 November 2026`, `6 Nov 2026`, `06/11/2026` (day first), `2026-11-06`,
     with Indonesian and English month names and abbreviations. A date within 60 characters after a
     deadline word (`deadline`, `paling lambat`, `selambat-lambatnya`, `batas waktu`, `tenggat`,
     `sebelum`, `due`, `by`, `no later than`, `pengumpulan pada`) wins; else **(v1.1)** the
     **earliest** date on or after today, flagged `Deadline guessed (no deadline word)`; else none.
     See section 15 for the extra formats and flags.
   - `weekday_check`: if a weekday name (Senin…Minggu, Monday…Sunday) appears right before the due
     date and does not match, add the flag (D13).
   - `contacts`: email addresses and capitalised full names after `a.n.`, `PIC`, `menghubungi`,
     `contact`.
   - `title_rule`: first sentence of the cleaned text, max 90 characters, cut at a word boundary.
6. **Extract by AI** (if `AI provider` ≠ `Off`), see section 6. Merge: rule values win for
   `letter_no` and `letter_code`; for `due_date`, use the rule date if present, else the AI date;
   if both exist and differ, keep the rule date and flag `AI deadline differs: <date>`; the AI title,
   summary and deliverables are used when present.
7. **Missing deadline:** today + `Default due (days)`, flag `No deadline found, default used`.
8. **Store files.** Create the task folder; save `original.txt` and every attachment; put the folder
   URL in `Files`. (A file over 25 MB: skip it and flag.) **(v1.2)** Only when `File storage` =
   `Drive`; with `None` this step stores nothing (section 16, item 7).
9. **Route** (section 5) → `Owner`, `Doer` (= Owner), `Division`, `Catalog ID`, `Routing note`.
10. **Write** the row (status `Proposed`, `Last updated` = now, `Weight` formula), then AuditLog
    `Created` with the routing note as the reason.
11. **Acknowledge.** Chat: reply (section 8). Email: reply to the sender in the same thread with the
    task ID, title, proposed owner, due date and flags; then label `GRC/Logged`, mark read and archive,
    all **after** step 10. **(v1.2)** The reply also carries the Files link; "the sender" is whoever
    emailed the bot (for a forward, the forwarder). See section 16.

---

## 5. Routing rules (`route.py`, pure function, fully unit-tested)

Inputs: cleaned text, contacts, letter code, due date, Catalog, Staff, LetterCodes, Config,
optional `ai_catalog_id`. Output: `RouteResult(owner, division, catalog_id, rule, note)`.

A person is **available** if: role is not Manager; and if they have an internship end date, it is
on or after both today and the due date. **Load ratio** = Weighted open load ÷ Capacity.

1. **Rule 1 — known report.** Score each non-ADHOC Catalog row: + length of every keyword found as a
   whole word (case-insensitive, hyphens and underscores treated as spaces), + 100 for every known
   contact found (email exact, or full name). Highest score > 0 wins. If its default owner is
   available and load ratio ≤ `Overload threshold` → that owner.
   Note: `Rule 1: catalog "<Report type>" (<matched words>) → default owner`.
2. **Rule 2 — backup.** Same Catalog row, but the owner is unavailable or overloaded, and the backup
   is available → backup. Note says why.
3. **Rule 3 — division.** Division from the Catalog row (if matched) else from LetterCodes by
   letter code. Pick the available person in that division with the lowest load ratio (tie: name A–Z).
4. **Rule 4 — AI suggestion.** Only if rules 1–3 found nothing and `ai_catalog_id` is a real
   non-ADHOC Catalog ID: apply rules 1–2 to that row. Note starts `Rule 4 (AI suggested): …`.
5. **Rule 5 — needs review.** No owner. Division if known. Flag `Needs review: no owner`.

---

## 6. AI extraction (`extract/ai.py`)

Interface:
```python
class AIProvider(Protocol):
    name: str

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None: ...
```
`AIResult` (pydantic, strict): `title_en` (≤ 90 chars), `summary_en` (≤ 2 sentences),
`deliverables: list[str]`, `deadline: date | None`, `deadline_quote: str | None`,
`requester_org: str | None`, `contacts: list[{name, email}]`, `catalog_id: str` (an ID from the list
or `NONE`), `language: "id" | "en" | "mixed"`.

- Prompt lives in `prompts/extract_v3.md` **(v1.3: adds the WhatsApp sender's instructions)**; the
  version string is logged with each call.
- Input: masked cleaned text (max 8,000 characters), Catalog IDs with report names, attachment file
  names. **(v1.2 item 11)** An email's PDF attachments are also sent, unmasked, when Config `AI
  reads PDFs` is on (the default). Other file types: names only.
- `GeminiProvider`: `google-genai` SDK (`pip install -U google-genai`). Use the call style documented
  for the installed SDK version: current docs use `client.interactions.create(model=…, input=…,
  response_format={"type": "text", "mime_type": "application/json", "schema":
  AIResult.model_json_schema()})` and read `interaction.output_text`. Model from Config `AI model`,
  default `gemini-3.8-flash` (stable). API-key mode (Google AI Studio key) reads `GEMINI_API_KEY`:
  **(v1.3, section 18)** injected by Cloud Run from Secret Manager in production, from `.env`
  locally; Vertex mode uses the
  runtime service account and `AI region`. Switching mode is a Config change, not a code change.
- The Gemini API does **not** enforce `maxLength` and similar schema limits: always validate the
  response with `AIResult.model_validate_json()` and enforce lengths in pydantic.
- Guardrails: drop `deadline` unless `deadline_quote` appears word-for-word in the original text;
  `catalog_id` must exist; 20-second timeout, one retry, then return `None` (the pipeline continues
  on rules and flags `AI unavailable`).
- Log provider, model, prompt version, latency, success. **Never log message text.**

---

## 7. Jobs and endpoints

| Endpoint | Caller | Schedule (HKT) | What it does |
|---|---|---|---|
| `POST /jobs/gmail` | Scheduler | every 10 min | Up to 20 unread inbox messages: allowed sender → `process_request`; other sender → label `GRC/Ignored` |
| `POST /jobs/gmail-push` **(v1.4)** | Pub/Sub push | on new mail | Answer 204 at once, then the same inbox run (section 20). Never overlaps another run |
| `POST /jobs/gmail-watch` **(v1.4)** | Scheduler | daily 06:23 | Renew Gmail's push to the topic (it lapses after 7 days) |
| `POST /jobs/changes` | Scheduler | every 5 min, 08:00–20:00 | Compare Master with `_state`; send assignment emails (section 8); update `_state` |
| `POST /jobs/recurring` | Scheduler | daily 07:00 | **(v1.4 part 3)** Create each Recurring tab row's task once per deadline, `Create days before` it (section 20) |
| `POST /jobs/digest` | Scheduler | weekdays 09:00 | Personal and manager digests |
| `POST /chat` | Google Chat | on event | Section 8 |
| `GET/POST /wa/webhook` **(v1.3)** | Meta | on event | Verification handshake; signed messages → Cloud Tasks queue `wa-process` (section 17) |
| `POST /tasks/wa-process` **(v1.3)** | Cloud Tasks | on event | One WhatsApp message, or the logging of a draft 30 s later |
| `POST /jobs/wa-drafts` **(v1.3)** | Scheduler | every 10 min | Log drafts whose Cloud Task never ran; prune old rows |
| `POST /jobs/wa-reminders` **(v1.3)** | Scheduler | weekdays, every 30 min 07:00–19:30 | Daily WhatsApp reminder template, once, at the first run after Config `Reminder time` |
| `GET /healthz` | anyone | — | 200 OK, no data |
| `GET /health` **(v1.2)** | anyone | — | Same as `/healthz`; use this one on Cloud Run, which reserves some paths ending in "z" |

Recurring rules: **(v1.4 part 3)** the Recurring tab (section 20) replaces the Catalog-based
recurrence; Catalog `Recurrence`, `Typical due day`, `Due months seen (2026)` and `Confirmed by
manager?` no longer create tasks (`Confirmed by manager?` still breaks routing ties).

Change detection (`/jobs/changes`): an assignment email is sent when (a) Status moves from `Proposed`
to any open status and the doer has not been notified for this task, or (b) `Doer` changes on an
open task. Record `Notified doer` and `Notified at` in `_state` so each assignment emails once.
**(v1.1)** Implemented as one rule with retry and a silent first run (section 15, item 9).

---

## 8. Notifications and Chat

**Assignment email** (to the Doer, cc the Owner if different): subject
`[GRC] G-0012 – <title> (due 06 Nov 2026)`; body: title, due date, flags, division, owner, routing
note, AI summary and deliverables (if any), the Files link, the sheet link, then the **full original
message** under a divider. Plain text plus simple HTML.

**Personal digest** (09:00, only if non-empty): overdue, due within 7 days, In Progress with
`Last updated` older than `Stale after (days)`. Proposed tasks are excluded.
**Manager digest:** awaiting approval, all overdue, people over the load threshold, interns leaving
within `Leaving warning (days)` who are still default owners in the Catalog, count of
`AI unavailable` flags in the last 24 h.

**Chat bot:**
- Only users whose email ends with `@<Allowed email domain>`.
- `help` → usage. `my tasks` → the sender's open tasks (match by Staff email).
- Anything else → `process_request`; reply with ID, title, proposed owner, due date, flags, Files link,
  and "Waiting for manager approval". If the message has attachments, try to download them with
  the Chat API into the task folder; on failure, ask the user to drop files in the Files link.
- Must answer within 25 seconds: if processing exceeds that, reply "Logged as G-xxxx, details by
  email" and finish in the background. **(v1.1)** The ID does not exist until the row is written,
  so the quick reply says the ID follows by email (section 15, item 12).

---

## 9. Security

- Verify on every request: Scheduler OIDC token (audience = service URL, email = scheduler SA);
  Chat bearer token (Google-signed for Chat, audience = project number). **(v1.3)** Cloud Tasks
  OIDC token on `/tasks/*` (audience = service URL, email = runtime SA); Meta's
  `X-Hub-Signature-256` (HMAC of the body with the app secret) on `/wa/webhook`. Reject
  everything else.
- Secrets only from Secret Manager (Gmail refresh token, OAuth client secret, Gemini API key;
  **(v1.3)** WhatsApp access token and app secret), injected by Cloud Run as environment variables
  (section 18).
  Nothing secret in code, the sheet, env files committed to git, or logs.
- Least privilege: runtime SA (Sheets via sharing, Drive Shared Drive, Vertex, Secret accessor);
  scheduler SA (`run.invoker` only).
- Logs: task IDs, counts, timings and error types only. No message bodies, names, emails or phone
  numbers.

---

## 10. Repository layout

```
grc-tracker/
├── CLAUDE.md
├── docs/DESIGN.md            # this file
├── docs/PRD.docx
├── app/
│   ├── main.py               # FastAPI routes
│   ├── settings.py           # env vars (IDs only)
│   ├── clock.py              # (v1.1) Asia/Hong_Kong now/today, freezable in tests
│   ├── models.py             # RawRequest, Task, CatalogEntry, StaffMember, RouteResult, AIResult
│   ├── sheets.py             # SheetStore: header-based read/write, write lock, batch calls
│   ├── drive.py              # task folders, file upload
│   ├── gmail.py              # bot inbox poll, message parse, reply, send
│   ├── chat.py               # event handling, replies, attachment download
│   ├── pipeline.py           # process_request
│   ├── extract/clean.py      # unwrap forwards, clean, mask
│   ├── extract/rules.py      # dates, weekday, letter code, contacts, title
│   ├── extract/ai.py         # AIProvider + implementations
│   ├── route.py              # routing rules
│   ├── jobs.py               # changes, recurring, digests
│   ├── notify.py             # email templates
│   ├── auth.py               # token verification
│   ├── logs.py, alerts.py    # (v1.1) JSON logs, alert emails
├── prompts/extract_v3.md
├── apps_script/audit.gs
├── scripts/mint_gmail_token.py   # one-time OAuth consent for the bot mailbox
├── scripts/smoke_gemini.py       # (v1.2) one live Gemini call, run by hand
├── tests/
│   ├── fixtures/             # sample messages (masked/fake), catalog.json, staff.json
│   ├── fakes.py              # FakeSheetStore, FakeDrive, FakeGmail, FakeAI
│   └── test_*.py
├── Dockerfile
├── cloudbuild.yaml
├── requirements.txt
└── README.md                 # local run, env vars, IT setup checklist, deploy
```

Environment variables: **(v1.3, section 18)** all listed in `app/settings.py` and documented in
`.env.example`; non-secret production values in `deploy/env.prod.yaml`, secrets in
`deploy/secrets.txt`. `TZ=Asia/Hong_Kong`.

---

## 11. Errors and monitoring

- Missing tab or column → stop the job, log `schema_error`, email the builder and manager (at most
  once per hour per error).
- Gmail auth failure → stop the Gmail job, alert within 1 hour; labels stay for retry.
- Any exception inside one request → log, add nothing to the sheet, leave the label, continue with
  the next request. After 3 failures for the same `source_id`, add the label `GRC/Failed` and alert.
  **(v1.2)** "Leave the label" = the email stays unread in the inbox; after 3 failures it gets
  `GRC/Failed` and is marked read (mark it unread to retry). Counts and the alert throttle live in
  `_failures`.
- Sheets quota errors (429) → exponential backoff, max 3 tries. Read each tab once per job run.

---

## 12. Acceptance tests (must pass before deploy)

Fixture `tests/fixtures/manpower_planning.txt` (the 26 Oct 2026 request, phones and email masked),
forwarded by the manager with the note "Tolong dicek ya". With AI `Off`:
- requester org is BRI (from the forwarded sender, via Config `Requester domains` **(v1.1)**),
  manager note goes to `Notes`;
- title is **not** the greeting;
- due date 2026-11-06 with flag containing `Weekday mismatch`;
- Catalog `HR-DATA`, division HR, via Rule 1 known contact `Hana Setyaningsih` **(v1.1: fake name;
  CLAUDE.md forbids real names in the repo)**;
- `Original message` contains the full untouched text; two attachments saved.

With a fake AI returning a valid result: title contains "Manpower Planning", deliverables list has 2
items. With a fake AI returning a deadline whose quote is not in the text: AI deadline dropped.

Also required: email from a sender not in `Allowed senders` → labelled `GRC/Ignored`, no row;
acknowledgement reply goes to the sender's thread; letter `B.91.e-EMR/IRM/10/2026` with no known report → Rule 3 Policy and Risk;
"Tolong cek printer" → Rule 5; duplicate `source_id` → no second row; intern ending before the
deadline is never proposed; Done-without-proof is the Apps Script's job (not the service's).

---

## 13. Out of scope for v1

Web dashboard, automatic assignment without approval, migrating 2026 history,
reading attachment contents (except email PDFs, section 16 item 11), multi-mailbox intake, reading Pak Dicki's personal inbox.

## 14. Open items (build around them, do not block)

| Item | How the code handles it now |
|---|---|
| Gemini key | Paid tier confirmed. Key belongs to BRI Indo's project: Compliance OK on intra-group data flow needed; ask for a project-specific key if possible |
| Bot mailbox address and licence | IT creates it; address goes in Config and the OAuth consent |
| Mask emails for AI? | Config switch `Mask emails for AI` (default No) |
| Retention period | Not enforced in v1 |
| Weekday/date conflict policy | Keep written date + flag (D13) |

## 15. Decisions carried over from v1.1 (approved during the build)

1. **Runtime:** Google API clients are created per thread (httplib2 is not thread-safe); Cloud Run
   keeps CPU allocated so Chat background completion runs; the Chat endpoint needs unauthenticated
   ingress with tokens checked in the app (org-policy exception if Domain Restricted Sharing is on).
2. **OAuth:** the consent screen is *Internal* (a Testing app's refresh tokens expire in 7 days).
3. **`_failures` tab** for failure counts and the once-per-hour alert throttle (section 3).
4. **Requester org:** sender domain looked up in Config `Requester domains` (sub-domains match
   their parent), else the first `Known requesters` name found in the text; fills
   `Requester / Regulator`.
5. **Drive:** the folder carries the `Source ID` as an app property; a retry reuses it and skips
   files already uploaded. It is created as `Pending – <title>` and renamed `G-0012 – <title>`
   after the row is written (the ID does not exist at step 8). Scope `drive.file`.
6. **Rule 1:** each keyword and each known contact counts once; ties go to the manager-confirmed
   row, then Catalog ID A–Z. A recurring request carries its Catalog ID and uses that row directly
   (no AI call). An unknown letter code adds `<Unknown letter code>: unknown letter code <code>`.
   A blank `Role` is Staff; a Catalog name not in Staff is unavailable.
7. **Dates:** also `November 6, 2026`, two-digit years, and dates without a year (this year, or next
   if passed; flag `Deadline year assumed`); deadline word `before` added; several deadline-word
   dates → earliest upcoming (all past → latest, flag `Deadline already passed`); no deadline word
   → earliest upcoming, flag `Deadline guessed (no deadline word)`.
8. **Cleaning:** a greeting removes only its own clause; a sign-off line (≤ 60 chars, no digits)
   drops the rest of the message only when ≤ 12 non-blank lines follow. Rules run on a light clean
   (emphasis, quoted replies, whitespace); titles, routing and AI use the full clean. A letter's
   `Perihal:`/`Hal:`/`Subject:` line is the rule title; letter header lines are skipped; titles are
   capped at 90 characters everywhere, including the subject fallback.
9. **Change detection:** one rule — an open task whose `Doer` differs from `Notified doer` gets the
   email. Failed sends (Gmail error, or no email in Staff, which flags the task once) are retried
   next run; `_state` is saved after each send. The first run (empty `_state`) only records state;
   later rows added by hand are treated as coming from `Proposed`.
10. **Sheets:** every text value is written as text (leading `'`), so content never becomes a
    formula; the `Weight` formula is copied from the last data row; the schema check reports every
    missing tab, column and setting at once.
11. **AI:** an over-long title is trimmed to 90 characters and the summary to two sentences rather
    than rejecting the result; the deadline quote check ignores line breaks, repeated spaces, case
    and `*`.
12. **Chat:** waits 20 s; a slower request gets "Received – … You'll get the task ID and details by
    email" and the worker emails the full reply. A blank `Allowed email domain` rejects everyone.
13. **Pipeline:** `Contacts` lists the forwarded sender first, and it counts as a known contact; a
    failure to write AuditLog or rename the folder after the row exists is logged, not raised.
14. **Digests:** personal = tasks where the person is the Doer; empty digests (manager included)
    are not sent; the "last 24 h" AI count uses `Start / received` ≥ yesterday.
15. **Errors and logs:** `/jobs/*` answers 500 with `schema_error` or `gmail_auth_error`; alert
    emails go to `Manager email` and `Builder email` at most once an hour per kind; Gmail auth
    failures rely on a log-based Cloud Monitoring alert. Logs are JSON with a whitelist of fields
    (numbers, IDs, error types). Token rejections are a bare 401; unset settings reject everything.
16. **Apps Script (D8):** an edit trigger can only undo: Status = Done without `Submitted date` and
    `Link` is set back to its previous value (`In Progress` after a multi-cell paste) and logged
    `Blocked`; multi-cell edits are audited cell by cell.
17. **Tests** use fake names and `example.*` domains only.

## 16. Decisions made for v1.2 (approved before implementation)

1. **Inbox handling, in order:** auto-replies (`Auto-Submitted`, `X-Autoreply`, `Precedence:
   bulk/junk/list`, mailer-daemon/no-reply senders) and the bot's own messages → `GRC/Ignored`,
   marked read. Sender not in `Allowed senders` → `GRC/Ignored`, marked read, kept in the inbox.
   A message in a thread that already has a `GRC/Logged` message → its text, quoted chain
   removed (≤ 500 characters), is appended to that task's `Notes`, AuditLog `Reply noted`, label `GRC/Reply`, read,
   archived; no new task. Anything else → `process_request`.
2. **`Allowed senders`:** comma/semicolon/line separated; an entry is an exact address or
   `@domain` (exact domain, not sub-domains). Empty = nobody (every email is ignored).
3. **After the row is written:** reply in the thread (`Re: <subject>`, `In-Reply-To`/`References`
   set), then `GRC/Logged`, mark read, archive. If the reply fails it is logged and the email is
   still archived (no repeat replies). If the label change fails, the next run finds the row by
   `Source ID`, replies "Already logged as …" and archives.
4. **Up to 20 unread inbox messages per run**, oldest first; failures as section 11 (v1.2 note).
5. **GeminiProvider (google-genai 2.33.0):** `client.interactions.create(model, input,
   response_format={"type": "text", "mime_type": "application/json", "schema": …},
   generation_config={"temperature": 0}, store=False)`, reading `output_text`. The schema is
   `AIResult.model_json_schema()` with `$defs` inlined. The SDK's own retries are off
   (`HttpRetryOptions(attempts=1)`); our 20 s timeout and one retry apply, except that 4xx errors
   other than 408/429 (bad key, location not supported) are not retried. Any failure → `AI
   unavailable`, rules only.
6. **Gemini key:** **(v1.3: superseded by section 18, the key is the env var `GEMINI_API_KEY`
   in both places)** `GEMINI_API_KEY` (local `.env`) wins over Secret Manager `GEMINI_KEY_SECRET`;
   never printed or logged. A missing key, or Vertex mode without project and `AI region`, is
   "not configured" → `AI unavailable`.
7. **No file storage (decision A, `File storage` = `None`, the default):** no Drive calls at all.
   `Files` holds the attachment names and where they are: "In the original email in the GRC bot
   mailbox: a.pdf, b.xlsx" for email (the bot keeps the archived email), "Not stored: …" for Chat
   (the task is flagged `Chat attachments not stored …`, and the Chat bot does not download them).
   An original over 45,000 characters is cut in the cell and flagged `… full text only in the
   original email` (no `original.txt`). Emails show `Files: none` when there are no attachments.
   `SHARED_DRIVE_FOLDER_ID` is only needed for `Drive`.
8. **Gmail auth errors** carry Google's reason code (e.g. `invalid_grant`), never a token.
9. **Deploy:** Cloud Run service `v2-git` in `asia-southeast2` (Jakarta), deployed by the Cloud
   Build trigger on `main` using `cloudbuild.yaml` (tests first; a build without `_SHEET_ID`
   stops). The data-residency side of Jakarta is part of the D18 Compliance check.
10. **Health check:** `GET /health` (same as `/healthz`, which Cloud Run may intercept).
11. **The AI reads email PDFs (approved by the project owner, 3 Nov 2026).** Replaces "never file
    contents" (section 6) and takes "reading attachment contents" out of section 13 for PDFs.
    - Up to 3 PDFs per email, 10 MB in total, are sent to Gemini as documents next to the masked
      text (`extract_v2` prompt), so it can read the report type, deadline and deliverables. PDFs
      over the limit are not sent and the task is flagged `Some PDFs not read by AI …`. Chat
      attachments are never sent (the Chat bot does not download them). Other file types: names
      only.
    - **PDF contents are not masked.** Names, phone numbers and staff data in a PDF reach Google
      as they are. This belongs to the D18 Compliance check. Config `AI reads PDFs` = No turns it
      off without a code change.
    - Deadline: the email's deadline wins over a PDF's. A deadline whose quote is not in the email
      text (so it came from a PDF the service cannot search) is kept only if the AI gives a quote,
      and the task is flagged `Deadline from attachment - check`.
    - Calls with PDFs get a 60 s timeout (text only stays 20 s; one retry either way). A Gmail
      run starts no new message after 150 s, so it ends inside the 300 s Cloud Run and Scheduler
      limits; the rest stay unread for the next run.

## 17. Decisions made for v1.3: WhatsApp bot (approved by the project owner, 3 Nov 2026)

Replaces "WhatsApp bot" in section 13 (PRD Phase 2: "WhatsApp intake without pasting"). Uses the
WhatsApp Cloud API with a number and app that BRI registers in Meta Business Manager.

1. **Intake:** staff send or forward a request (text, PDF, document or photo) to the bot's number.
   `POST /wa/webhook` checks Meta's signature, puts each message on the Cloud Tasks queue
   `wa-process` and answers at once (Meta resends slow answers). Delivery and read receipts are
   ignored. Other message types get "I can read text, PDFs, documents and photos".
2. **Who may use it:** numbers in the Staff `WhatsApp` column. Anyone else gets one reply ("This
   number isn't registered …") and nothing is stored. Phone numbers are never logged.
3. **Commands:** `help`, `my tasks` (as the Chat bot).
4. **One message starts a request, then 30 seconds to add to it.** The first message opens a draft
   (`_wa_pending`) and queues its logging 30 s later; the bot answers "Got it. I'll log it in 30
   seconds …". Within the window, more text, files and PROMPTs from the same sender join the
   draft (a forward of several messages arrives as several messages; Cloud Tasks does not keep
   their order, so a PROMPT that arrives first waits for the request). A draft older than 60 s
   takes no new messages.
5. **PROMPT:** a message starting `PROMPT:` (any case; also as a file's caption).
   - "assign (this) to <name>" (also `kasih ke`, `tugaskan ke`, `give it to`) proposes that
     person as Owner and Doer, matched against Staff by rules (`route.sender_assignment`): full
     name, else a unique first name, else a unique start of a name. A person chose, not the AI;
     Status stays `Proposed` for the manager's approval. The routing note keeps what the rules
     would have proposed. An unknown or ambiguous name keeps the rules' owner and flags
     `PROMPT: "<name>" not found in Staff`; an intern leaving before the deadline is flagged.
   - The whole PROMPT goes to the AI as "Sender's instructions" (classification, title,
     deadline) and to `Notes` as `Sender's PROMPT: …`. Rules read only the forwarded request.
   - A PROMPT within 5 minutes after its request was logged applies to that task while it is
     still `Proposed` (AuditLog `PROMPT applied`).
6. **Logging:** the draft becomes one `process_request` (Source `WhatsApp`, Source ID = the first
   message ID, which dedupes retries). PDFs are downloaded from Meta and read by the AI (item
   11); other files are names only unless `File storage` = Drive. `Files` reads "In the WhatsApp
   chat with the bot: …". The reply is the Chat bot's (ID, title, proposed owner, due, flags,
   "Waiting for manager approval").
7. **Deadline confirmation:** the reply asks "Deadline: <date>. Reply YES if correct, or send the
   correct date". The row is written first and flagged `Deadline not confirmed by sender
   (WhatsApp)`. YES (also ya, ok, betul, benar, correct) clears the flag; a short message that is
   mostly a date (at most 3 other words) sets `Due date`; both write AuditLog. Only the sender's
   newest question counts, for 24 hours (WhatsApp's reply window); a message during an open
   draft belongs to the draft.
8. **Never lose a request (D15):** a failed logging returns the draft to `open` and the Cloud Task
   retries; `/jobs/wa-drafts` (every 10 min) logs open drafts older than 2 minutes and retries
   ones stuck for 10 minutes, and prunes finished rows after 7 days. If the queue is down, the
   webhook answers 500 so Meta resends.
9. **Reminders:** weekdays at the first `/jobs/wa-reminders` run after Config `Reminder time`,
   each Staff member with a `WhatsApp` number and something overdue, due within 7 days or stale
   gets the template `grc_daily_reminder` (language `en`), parameters: name, overdue count,
   due-soon count, stale count, sheet link. Templates are required outside the 24-hour window;
   BRI submits the template in Meta. The email digest stays.
10. **Settings:** `WA_PHONE_NUMBER_ID`, `WA_WABA_ID`, `WA_GRAPH_VERSION`, `WA_VERIFY_TOKEN`,
    `WA_TASKS_QUEUE` (default `wa-process`), `RUNTIME_SA_EMAIL`; secrets `WA_ACCESS_TOKEN` and
    `WA_APP_SECRET` as env vars filled by Cloud Run from Secret Manager. Without them the bot is
    off and its endpoints answer 404 or "skipped".
11. **Compliance:** WhatsApp messages pass through Meta, and PDF contents reach Gemini unmasked
    (item 11 of section 16). Both belong to the D18 Compliance check.

## 18. One-command deploy (v1.3, approved by the project owner, 3 Nov 2026)

1. **Names stay as deployed:** service `v2-git`, region `asia-southeast2` (Jakarta), runtime
   account `grc-tracker-run`, scheduler account `grc-tracker-scheduler`, Artifact Registry
   `cloud-run-source-deploy`, Cloud Tasks queue `wa-process`. They are defined once, in
   `deploy/config.sh`; `cloudbuild.yaml` repeats three of them and a test checks they agree.
2. **Settings:** one `pydantic-settings` class (`app/settings.py`) reads every variable from the
   environment. Secrets are `SecretStr`.
3. **Secrets are environment variables** that Cloud Run fills from Secret Manager
   (`--set-secrets`, mapping in `deploy/secrets.txt`): `GMAIL_REFRESH_TOKEN`,
   `OAUTH_CLIENT_SECRET` (the client JSON), `GEMINI_API_KEY`, `WA_ACCESS_TOKEN`, `WA_APP_SECRET`.
   The code reads them the same way locally (`.env`). This replaces reading secrets by name at
   runtime (`GMAIL_TOKEN_SECRET`, `GEMINI_KEY_SECRET`). A new secret version needs a redeploy.
4. **Non-secret production values** are `deploy/env.prod.yaml` (git-ignored), stored whole as the
   secret `grc-env-prod`. `deploy/deploy.sh` and the merge-to-main build both deploy from it, so
   they are identical; values changed by hand in the console are replaced by the next deploy.
5. **Scripts:** `deploy/setup_gcp.sh` (one-time, idempotent), `deploy/setup_secrets.sh` (values
   from a file or a hidden prompt; never echoed, never arguments, never on disk),
   `deploy/deploy.sh` (tests, build, deploy, prints the URLs), `deploy/setup_scheduler.sh` (creates
   or updates all jobs). `cloudbuild.yaml` runs the tests, builds and calls `deploy/deploy.sh
   --image`. Secrets without a version (e.g. WhatsApp before Meta is set up) are left out.
6. **Startup check:** at boot the service checks every required variable (WhatsApp ones only if
   any is set; `GEMINI_API_KEY` if Config `AI provider` = Gemini API key; `SHARED_DRIVE_FOLDER_ID`
   with `File storage` = Drive) and the sheet's tabs, columns and settings. Missing names are
   logged (names only) and `/health` and `/healthz` answer 503 with the lists; sheet problems are
   re-checked at most once a minute, so fixing the sheet needs no redeploy.
7. **Not done:** `TEST_MODE` (no such mode exists in the code).

## 19. Roles, review lifecycle and commands (v1.4, part 1 of 3; approved 4 Nov 2026)

Part 2 (reminder engine, channels, WhatsApp templates) follows; part 3 is section 20. Until part 2,
the event notices below go by email, and the daily digest and WhatsApp reminder stay as they are.

**Roles and authority (R1–R11).** One Staff row per person; real names live only in the Sheet.
- `Divisions` (`;`-separated) replaces `Main division` (still read when `Divisions` is blank).
  Division names must match Catalog `Division` and LetterCodes `Route to division` exactly;
  `/health` lists divisions nobody covers as warnings (still 200).
- `Additional help` and `Manager` are never proposed by routing; they can be assigned by hand.
- An intern whose `Reports to` is a full-time person: when routing (rules 1–4), a PROMPT or a
  Recurring row picks the intern, **Doer = intern, Owner = supervisor**. An intern who reports to
  the Manager is Owner and Doer. Rule 3 still takes the least-loaded available person; internship
  end dates still apply.
- **Reviewer** of a task: its `Reviewer` column, else the Doer's full-time supervisor, else the
  first Manager in Staff. The Manager may approve or return any task.
- Manager-only commands: `semua`/`all`, `recurring`, `pause`, `resume`.

**Lifecycle.** Open statuses: Not Started, In Progress, In Review, Approved, Returned, On Hold.
- `review` (from Not Started, In Progress, Returned) → In Review; `approve` / `return <notes>`
  (reviewer or Manager, only from In Review) → Approved / Returned with `Review notes`.
- Done needs `Submitted date` and `Link`; a task with `Needs review` = Yes must be Approved first.
  The Apps Script enforces both for Sheet edits, and undoes Approved/Returned set by anyone but
  the reviewer or a Manager (`installValidation()` adds the dropdowns).
- Every change by command is written to AuditLog with `Command via WhatsApp` / `Command via
  Email`; Sheet edits keep the Apps Script audit. Commands record the new status in `_state`, so
  the change job does not announce it again; Sheet edits to In Review / Approved / Returned are
  announced by the change job.

**Commands** (one parser, `app/commands.py`; WhatsApp messages and the first line of an email
reply from a Staff address). A verb that acts on a task needs the task ID, unless the message
replies to a bot message about one task (WhatsApp reply, or an email whose subject has
`[GRC] G-xxxx` / the intake thread); so a forwarded request that starts with "review" is still a
request. Indonesian and English synonyms.
- `progress G-0012 50` → Progress 0.5, Not Started → In Progress.
- `review G-0012` (+ file) → files kept, `Review file` set, In Review, reviewer emailed the file
  with `approve` / `return` instructions.
- `done G-0012` (+ proof) → proof kept, `Link` = "In the GRC bot mailbox: …", `Submitted date` =
  today, Done. Without a file: allowed only if `Link` is already filled.
- `files G-0012` → the task's files: email attachments up to 20 MB in total (the rest by name);
  on WhatsApp the files go to the person's email.
- `tugas` / `today` (`hari ini`) / `week` (`minggu ini`) → the person's open tasks (as Doer or
  Owner); `semua` → all open tasks by Doer; `recurring` → the Recurring tab's rows (section 20).
- `approve G-0012`, `return G-0012 <notes>` → reviewer or Manager only.
- `pause <name>` / `resume <name>` → Staff `Reminders paused` = Yes / blank; paused people get no
  notices (assignment emails included; they wait until `resume`).
- **Delegation:** `assign G-0012 to <name>` (`kasih … ke …`, `tugaskan … ke …`), `assign that to
  <name>` in reply to a bot message about that task (on WhatsApp, also the newest single-task bot
  message within 24 h), `assign G-0012 to me`. The Owner or Doer may hand it only to someone who
  reports to them (or back to themselves); the Manager to anyone (Owner/Doer by the role rule).
  Only after approval (not Proposed). The new Doer gets the assignment email at once.
- A wrong role, an unknown task or a missing value: a clear reply and nothing changed.

**Files (F1–F4): no Drive.** `File storage` stays `None`. Files live in the bot mailbox: email
attachments in their emails; WhatsApp files (intake and commands) are downloaded on arrival and
kept as a never-sent message labelled `GRC/Files`, subject `[GRC files] <task ID>`
(`messages.insert`, not in the inbox). `Files`, `Link` and `Review file` hold the names and
"In the GRC bot mailbox". Assignment, review and `files` messages attach the task's files up to
20 MB in total; WhatsApp sends no files in part 1. Small inline images (< 100 KB) in email replies
are signatures, not proof.

## 20. Recurring tab and instant email (v1.4 part 3; approved 9 Nov 2026)

**Recurring tab (decisions D1–D3; carries accepted decisions 2, 13, 15, 16 and R9).** One row per
repeating task (section 3). The daily `/jobs/recurring` (07:00):
- **Deadlines.** The first is `First due date`; the next ones follow `Repeat` (`Weekly`,
  `Monthly`, `Quarterly`, `Semester`, `Annual`; Indonesian words such as `Triwulan`, `Bulanan`
  accepted) on the same day of the month, capped at a shorter month's last day. A first deadline
  on a month's last day stays on month ends. **D2:** a deadline on a Saturday or Sunday is written
  as the Friday before (no holiday list until part 2); the period key keeps the scheduled date.
- **When.** Each deadline's task is created `Create days before` it (blank: Config `Recurring
  lead (days)`), and never after the deadline has passed: a `First due date` in the past only
  starts the series. A missed run catches up on the next day while the deadline is still ahead.
- **Once.** `Source ID` = `Recurring <Name> <scheduled deadline yyyy-mm-dd>`, so a row is never
  created twice for a deadline. Renaming a row starts a new series.
- **The task.** `Title` with `{Q}` `{S}` `{M}` `{MMM}` `{YYYY}` filled in for the period:
  `Title period` `Previous` (default) is the period before the deadline (a 15 Oct deadline is the
  Q3 report), `Same` the deadline's own. Status **`Not Started`** (setting up the row is the
  approval, decision 2), so the change job sends the Doer the assignment email on its next run.
  Owner and Doer as written; a blank Owner follows the Doer's `Reports to` (R4/R5, R9). `Needs
  review`, `Reviewer` and `Notes` are copied; `Catalog ID` fills `Division` and `Requester`
  (otherwise `Division` is the Doer's first division). Routing and the AI are never used.
- **Skipped rows (decision 15).** Unknown `Repeat`, an empty or unreadable `First due date`, an
  empty `Title`, an empty Doer, an Owner or Doer not in Staff, or a blank Owner whose Doer reports
  to nobody: the row is skipped, the others go on. The manager digest lists them under "Recurring
  rows skipped", the Manager command `recurring` lists every row with its next deadline and the
  problems, and `/health` shows only a count (it is public and its warnings are logged; the
  problems can name people). An intern leaving before the deadline is flagged, not replaced; a
  Manager or Additional help may be the Doer of a recurring row.
- **D3, reminders.** Until the part 2 reminder engine, "remind me 7 days before" is `Create days
  before` = 7: the assignment email then, and the 09:00 digest lists the task every weekday while
  it is due within 7 days.

**Instant email (D4).** Gmail's push notifications through Pub/Sub:
- `deploy/setup_gcp.sh` creates the topic `grc-gmail-push` and gives
  `gmail-api-push@system.gserviceaccount.com` Publisher on it. An organisation enforcing *Domain
  restricted sharing* can refuse that grant: the script warns, and email is read every 10
  minutes until IT adds an exception.
- `deploy/setup_scheduler.sh` creates the push subscription (endpoint `/jobs/gmail-push`, OIDC
  token of `grc-tracker-scheduler`, audience `SERVICE_URL`, so the app checks it like a Scheduler
  call), the daily `grc-gmail-watch` job, and runs that job once. `GMAIL_PUSH_TOPIC` (full topic
  path, written by `deploy/make_env.sh`) turns it on; blank = off.
- `/jobs/gmail-push` answers 204 at once (Pub/Sub would resend a slow answer) and runs the inbox
  right after. The notification body (mailbox address, history ID) is not read. A push with
  nothing unread skips the sheet read; most pushes are the bot's own label changes.
- Inbox runs never overlap: a push or Scheduler call that finds a run in progress returns at
  once and makes that run read the inbox once more. A failed push-triggered run is logged; the
  mail stays unread for the next run (D15).
- **The 10-minute `/jobs/gmail` stays** as the safety net for a lost push or a lapsed watch.
