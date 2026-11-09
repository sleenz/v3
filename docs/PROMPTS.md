# Claude Code prompts — grc-tracker

How to use: create the repo folder, put `CLAUDE.md` at the root and `DESIGN.md` + `PRD.docx` in
`docs/`. Open Claude Code in that folder. Paste **one phase at a time**. Review and test each phase
before pasting the next. Phases 1–3 need no Google Cloud access, so you can start before IT is ready.

---

## Phase 0 — Kickoff (paste first)

```
Read CLAUDE.md and docs/DESIGN.md fully. Do not write code yet.

Then reply with:
1. A summary of the system in 5 bullets, in your own words.
2. Any contradictions, gaps or risky assumptions you see in DESIGN.md.
3. Your build plan following the phases I will give you (1 to 7).
4. The Python packages you plan to use, with one line each on why.

Wait for my OK before starting Phase 1.
```

## Phase 1 — Skeleton, models, rules (no Google APIs)

```
Phase 1. Follow DESIGN.md sections 2, 4 (steps 2, 3, 4, 5), 10 and 12.

Build:
- Repo skeleton exactly as DESIGN section 10 (empty modules are fine for later phases).
- app/models.py with all pydantic models.
- app/extract/clean.py: unwrap forwarded emails (innermost block, original sender, manager note),
  clean greetings/sign-offs/emphasis/quoted replies, mask phone/account/ID numbers.
- app/extract/rules.py: letter number and code, due date (Indonesian + English formats, deadline
  words), weekday check, contacts, rule title.
- A clock helper that tests can freeze to 2026-10-26 Asia/Hong_Kong.
- tests/fixtures: manpower_planning.txt (I paste it below, already masked), plus 6 fake messages
  you write yourself: a formal letter with B.91.e-EMR/IRM/10/2026, an English email with
  "due 15/11/2026", a message with no date, a nested forward, an unrelated message
  ("Tolong cek printer"), and one with a correct weekday ("Jumat 06 November 2026").
- Unit tests for every function, including all DESIGN section 12 cases that only need rules.

Do not call any Google API. Run pytest and ruff, then report as CLAUDE.md says.

Fixture text (forwarded by the manager with the note "Tolong dicek ya", sender
fadhil@example.co.id, 2 attachments: Form_MPP.xlsx, JPS_MPP.pptx):
<<<
Selamat siang Bapak dan Ibu Anggota Konglomerasi Keuangan Level 1 dan Level 2,
Dalam rangka telah disosialisasikannya pada 23 Oktober 2026 dengan format form dan PPT Joint
Planning Session Manpower Planning Terintegrasi BRI Group, berikut kami lampirkan format form dan
PPT yang perlu diisi oleh masing-masing perusahaan anak, adapun deadline pengumpulan pada** Rabu 06
November 2026.**

Terkait mekanisme pengumpulan agar mengirimkan email ke fadhil@example.co.id dan/ atau menghubungi
PIC terkait a.n. Fadhil Athallah Sakti (0812-0000-0000) dan Hirza Sitaningtyas (0811-0000-0000)

Terima kasih atas perhatiannya Bapak dan Ibu
>>>
```

## Phase 2 — Sheet store and routing

```
Phase 2. Follow DESIGN.md sections 3 and 5.

Build:
- app/sheets.py: SheetStore with header-based reads/writes, one batch read per tab per job,
  a write lock, next ID, append task row (with the Weight formula), update cells by ID, AuditLog
  append, _state read/write, schema check that raises a clear error naming missing tabs/columns.
  Use the Google Sheets API via google-api-python-client with the runtime service account.
- tests/fakes.py: FakeSheetStore with the same interface, loaded from tests/fixtures/*.json.
- tests/fixtures/catalog.json, staff.json, lettercodes.json, config.json: build them from the
  attached GRC_Tracker_Tabs_v2.xlsx (Catalog, Staff, LetterCodes, Config tabs). Add
  "Hirza Sitaningtyas" as a Known contact on HR-DATA. Replace real staff names with fake ones
  (Staff A, Staff B…) and keep a mapping only in your head, not in the repo.
- app/route.py: the five routing rules exactly as DESIGN section 5.
- Tests for every rule, availability (intern end date, manager), overload → backup, ties,
  AI-suggested rule 4 with a fake catalog_id, and unknown letter code.

Run pytest and ruff, then report.
```

## Phase 3 — Pipeline end to end (fakes only)

```
Phase 3. Follow DESIGN.md section 4 (all steps) and section 12.

Build:
- app/drive.py (DriveStore: create task folder in the Shared Drive, upload original.txt and
  attachments, return folder URL) + FakeDriveStore.
- app/extract/ai.py: AIProvider protocol, AIResult model, NullProvider, BriAgentProvider stub
  (raises NotConfigured), VertexGeminiProvider (google-genai in Vertex mode, JSON schema, temperature
  0, 20 s timeout, 1 retry), guardrails, and FakeAIProvider for tests.
- prompts/extract_v1.md (now extract_v2.md, section 16 item 11): the extraction prompt (Indonesian and English input, output schema,
  "choose catalog_id only from the list or NONE", "deadline_quote must be copied exactly").
- app/pipeline.py: process_request(raw) running steps 1–11.
- Tests: the full section 12 acceptance list with fakes, including AI off, valid fake AI, AI with a
  bad deadline quote, AI timeout, duplicate source_id, file over 25 MB, original message over
  45,000 characters.

Run pytest and ruff, then report.
```

## Phase 4 — Gmail intake and emails

```
Phase 4. Follow DESIGN.md sections 2 (D3, D4, D15), 7 (/jobs/gmail) and 8 (assignment email).

Build:
- app/gmail.py: GmailClient using an OAuth refresh token read from Secret Manager
  (GMAIL_TOKEN_SECRET); list threads with the intake label, parse the first message (text, html,
  sender, attachments), change labels only after the row is written, send email (plain + HTML),
  GRC/Failed label after 3 failures.
- scripts/mint_gmail_token.py: one-time local OAuth consent for the intake mailbox that prints the
  refresh token for IT to put in Secret Manager. Never write the token to disk.
- app/notify.py: assignment email exactly as DESIGN section 8.
- app/main.py: POST /jobs/gmail, GET /healthz.
- Tests with a FakeGmailClient: label flow, retry safety, failure labelling, email content includes
  the full original message and the Files link.

Run pytest and ruff, then report.
```

## Phase 5 — Change detection, recurring, digests

```
Phase 5. Follow DESIGN.md section 7 and section 8 (digests).

Build app/jobs.py and endpoints POST /jobs/changes, /jobs/recurring, /jobs/digest.
Tests: approval sends one email and only once; doer handoff sends to the new doer; recurring creates
once per period and respects the lead days and month-end capping; digests skip empty ones, exclude
Proposed, and the manager digest lists leaving interns who still own Catalog reports.

Run pytest and ruff, then report.
```

## Phase 6 — Google Chat bot and auth

```
Phase 6. Follow DESIGN.md sections 8 (Chat) and 9.

Build:
- app/chat.py and POST /chat: domain check, help, my tasks, log a request, attachment download
  with fallback, 25-second reply rule with background completion.
- app/auth.py: verify Scheduler OIDC tokens and Chat bearer tokens; every /jobs/* and /chat route
  requires one. /healthz is open.
- Tests with sample Chat event JSON (both message and added-to-space events) and token verification
  using test keys.

Run pytest and ruff, then report.
```

## Phase 7 — Apps Script audit, deploy, README

```
Phase 7. Follow DESIGN.md sections 8 (D8), 9, 10 and 11.

Build:
- apps_script/audit.gs: installable onEdit trigger for Master that writes AuditLog (who, field,
  old, new), updates Last updated, and blocks Status = Done without Submitted date and Link.
- Dockerfile (python:3.12-slim, non-root user, uvicorn), cloudbuild.yaml (test, build, push to
  Artifact Registry, deploy to Cloud Run with max-instances 1).
- README.md: what it is, local dev with the dev sheet, env vars, the full IT setup checklist
  (APIs, service accounts and roles, Shared Drive, Secret Manager entries, Scheduler jobs with their
  cron in Asia/Hong_Kong, Chat app configuration, OAuth consent for Gmail), deploy steps, and how to
  switch AI provider in Config.
- Structured JSON logging with no personal data; alert emails per section 11.

Run pytest and ruff. Then give me a go-live checklist I can walk through with IT.
```

---

## When something goes wrong

- Tests fail after a phase: paste the failing output and say "fix without changing DESIGN.md
  behaviour; if the spec is wrong, tell me instead".
- Claude Code wants to change the design: make it explain the trade-off first, then update
  DESIGN.md in the same change so the spec stays the source of truth.
- When the BRI agent API details arrive: "Implement BriAgentProvider using these API details:
  <paste>. Keep the AIProvider interface unchanged. Add tests with a fake HTTP server."
