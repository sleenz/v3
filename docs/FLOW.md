# How a request flows through the GRC Tracker

Six phases, from a request arriving to the task being done. The overview shows how they
connect; each phase then has its own diagram. Steps marked **(AI)** use Gemini; steps marked
**(person)** need someone to act. Every other step is fixed code. Names are examples
(`Staff A`), never real people.

## Overview

```mermaid
flowchart LR
    P1["1. Intake<br/>email, WhatsApp, Chat,<br/>Recurring tab"] --> P2["2. Understanding<br/>and logging"]
    P2 --> P3["3. Approval and<br/>assignment"]
    P3 --> P4["4. Reminders<br/>on working days"]
    P4 --> P5["5. Updates from<br/>the team"]
    P5 --> P4
    P5 --> P6["6. Review<br/>and done"]
    P6 -->|returned| P5
```

---

## Phase 1: Intake

```mermaid
flowchart TD
    A1([Email to the bot mailbox]) --> E1{"From an allowed sender?<br/>(Config Allowed senders)"}
    A2([WhatsApp message or file]) --> W1{"Number in Staff<br/>WhatsApp column?"}
    A3([Google Chat message]) --> C1{"Company domain?"}
    A4([Recurring tab row, daily 07:00]) --> P2
    E1 -->|no| X1[Label GRC/Ignored, nothing logged]
    E1 -->|yes| E2{"Reply in a thread<br/>already logged or sent by the bot?"}
    E2 -->|yes| R1[Reply path: Notes, command,<br/>or Phase 5 update]
    E2 -->|no| E3{"PROMPT line from<br/>a Staff sender?"}
    E3 -->|yes| E4[Keep the PROMPT line apart<br/>from the request]
    E3 -->|no| P2
    E4 --> P2
    W1 -->|no| X2["Reply: number not registered"]
    W1 -->|yes| W2{"Update, button tap<br/>or command?"}
    W2 -->|yes| R1
    W2 -->|no| W3["Open a 30-second draft:<br/>more messages, files, PROMPT join it"]
    W3 --> P2
    C1 -->|yes| P2
    P2[["Phase 2"]]
```

- **Email** comes in within about a minute (Gmail push) or at the 10-minute backup run.
  Senders not in Config `Allowed senders` are ignored (DESIGN D16, section 20).
- A **reply** in a thread the bot already logged, or a reply to a bot email (reminder,
  notice), is never a new request: it goes to Notes or to Phase 5 (sections 20 and 22).
- **WhatsApp** only accepts numbers in the Staff `WhatsApp` column. A forwarded request opens a
  30-second draft so the sender can add files and a `PROMPT:` line (section 17).
- A **PROMPT line** (`PROMPT: assign this to Staff P, reviewer Staff D, deadline Friday`) only
  counts from a Staff sender, and only from the text they typed, never from a PDF (section 21).
- **Recurring tab** rows create their task a set number of days before each deadline, already
  approved (section 20).

---

## Phase 2: Understanding and logging

```mermaid
flowchart TD
    S([Request from Phase 1]) --> D{"Already logged?<br/>(same Source ID)"}
    D -->|yes| Same[Return the existing task]
    D -->|no| U[Unwrap the forward:<br/>the inner message is the request]
    U --> CL[Clean the text, mask phone<br/>and account numbers for the AI]
    CL --> RU[Rules: dates, letter code,<br/>contacts, requester]
    RU --> AI["Read text and PDFs (AI):<br/>English title, summary, deadline,<br/>deliverables, report type"]
    AI --> PR{"PROMPT line?"}
    PR -->|yes| PA["Read owner, reviewer, deadline<br/>from that line only (AI);<br/>rules check and fall back"]
    PR -->|no| RT
    PA --> RT["Routing rules 1–5 propose an owner<br/>(never the AI)"]
    RT --> WR[Write the Master row:<br/>Status Proposed, flags, files]
    WR --> ACK[Reply to the sender:<br/>task ID, owner, due date]
    ACK --> P3[["Phase 3"]]
```

- **Rules first, AI second**: dates and letter codes are read by fixed rules; Gemini adds a
  readable title, a summary and the deadline from PDFs. With Config `AI provider` = `Off`, the
  rules do everything (section 6).
- **Routing** (section 5): rule 1 a known report's default owner, rule 2 its backup, rule 3 the
  least-loaded person in the division, rule 4 the AI's report type, rule 5 nobody
  (`Needs review: no owner`). A PROMPT's owner overrides the rules but is flagged.
- **Nothing is lost**: a failure leaves the email unread for the next run (D15).

---

## Phase 3: Approval and assignment

```mermaid
flowchart TD
    PR{"PROMPT from the Manager, or full-time<br/>staff for themselves or their reports?"}
    PR -->|"yes, owner clear"| NS["Status Not Started<br/>at once (section 26)"]
    PR -->|"no, or unsure"| S([Proposed task in Master])
    S --> M["Manager checks Owner, Doer,<br/>Due date (person)"]
    M --> ST["Manager sets Status<br/>Proposed → Not Started (person)"]
    NS --> CJ["Change job, every 2 minutes"]
    ST --> CJ
    CJ --> WH{"Working hours?<br/>(Workday start–end, working day)"}
    WH -->|no| WAIT[Wait for the next Workday start]
    WAIT --> CJ
    WH -->|yes| AE["Assignment email to the Doer, cc Owner,<br/>with the task's files attached"]
    AE --> WA{"Doer on WhatsApp?"}
    WA -->|yes| WC[Also a WhatsApp card with buttons]
    WA -->|no| P4
    WC --> P4[["Phase 4"]]
```

- A PROMPT from the Manager (or from full-time staff for themselves or their reports) starts the
  task at once; anything else, or a PROMPT the bot is unsure about, only **proposes** and waits
  for the Manager (sections 2 and 26). Interns and additional help cannot assign.
- The assignment email attaches the files from the bot mailbox (up to 20 MB), so the Doer gets
  the forwarded PDFs (v1.4 F4).
- Notices outside working hours wait in `_event_queue` until 10:00 on the next working day
  (section 22).

---

## Phase 4: Reminders on working days

```mermaid
flowchart TD
    J(["Reminder job, every 15 minutes,<br/>Mon–Fri, not Holidays"]) --> SL{"Which slot is due?"}
    SL -->|10:00| MO["Morning: one message per person<br/>(due in 7/3/1 days, overdue,<br/>not updated, review waiting)"]
    SL -->|10:15| MS["Manager summary: overdue, due today,<br/>waiting for review, awaiting approval,<br/>blocked"]
    SL -->|14:00| DT["Due today, not Done:<br/>Doer and Owner"]
    SL -->|16:30| LC["Last call:<br/>Doer, Owner and Manager"]
    MO --> CK{"Snoozed, paused, already sent today,<br/>or over the daily cap?"}
    DT --> CK
    LC --> CK
    CK -->|over the cap| DF[Kept for the next message]
    CK -->|skip| N[Nothing sent]
    CK -->|send| CH{"Channel?"}
    MS --> CH
    CH -->|WhatsApp| T["Template with<br/>'Lihat tugas' button"]
    CH -->|email| EM[Email listing the tasks]
    T --> P5[["Phase 5"]]
    EM --> P5
```

- Times, holidays and the daily cap are Config settings, editable without a deploy
  (section 22).
- The same rule for the same task never reaches the same person twice in one day
  (`_reminders_sent`).
- WhatsApp is used when the person has a number and `WhatsApp opt-in`; if the send fails, the
  reminder goes by email.

---

## Phase 5: Updates from the team

```mermaid
flowchart TD
    I([Reply by email or WhatsApp]) --> K{"Button or list tap?"}
    K -->|yes| B["Read by code only<br/>(Update progres, Selesai, Lainnya…)"]
    B --> AP
    K -->|no, typed text| MAP["Find the task: message replied to,<br/>ID in the text, or the only open task"]
    MAP --> AIR["Read the message (AI);<br/>rules if the AI is off or unsure"]
    AIR --> KD{"What is it?"}
    KD -->|question| QA[Answer from the sheet]
    KD -->|new request| P1[["Back to Phase 1"]]
    KD -->|unclear task| LIST[Send the list: which task?]
    KD -->|change| CF{"Confirmed?<br/>WhatsApp Ya / email YES (person)"}
    CF -->|Batal / NO| NO[Nothing changes]
    CF -->|Ya / YES| AP["Apply: progress, blocked, extension,<br/>snooze, note, undo"]
    AP --> LOG[ProgressLog and AuditLog rows]
    LOG --> EXT{"Extension asked?"}
    EXT -->|yes| MG["Manager: Setuju / Tolak (person)"]
    EXT -->|no| P6[["Phase 6 when sent for review or done"]]
```

- **Typed changes are never saved without confirmation**; button taps save at once
  (section 22, decision 2).
- The AI only reads the message and the person's own open tasks; it cannot invent a task or a
  date, and anything under 0.6 confidence goes to the rules.
- **Blocked** puts the task On Hold and tells the Owner and Manager; an **extension** changes
  the due date only when the Manager agrees; **snooze** pauses reminders up to 2 working days;
  **undo** reverses the person's last change within 24 hours.

---

## Phase 6: Review and done

```mermaid
flowchart TD
    S([Doer sends for review<br/>with the file]) --> NR{"Needs review = Yes?"}
    NR -->|no| DN
    NR -->|yes| IR["Status In Review;<br/>reviewer gets the file"]
    IR --> RV{"Reviewer decides (person)"}
    RV -->|Approve| APR["Status Approved;<br/>Doer told"]
    RV -->|Return with notes| RET["Status Returned;<br/>Doer gets the notes"]
    RET --> P5[["Back to Phase 5"]]
    APR --> DN{"Proof attached<br/>or Link filled?"}
    DN -->|no| ASK[Ask for the proof file]
    ASK --> DN
    DN -->|yes| DONE["Status Done,<br/>Submitted date = today"]
```

- The **reviewer** is the task's Reviewer, else the Doer's full-time supervisor, else the
  Manager (section 19).
- **Done** needs proof: a file, or a `Link` already filled. The sheet's Apps Script undoes a
  hand-set Done without it.
- Done and Proposed tasks get no reminders.
