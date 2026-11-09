# Session notes: where the GRC Tracker stands (handover for the next Claude session)

Read this first, then `CLAUDE.md` and `docs/DESIGN.md` (the spec; section numbers below refer to
it). Real team names, emails and the bot address live only in the Sheet and in chat, never here.

Last updated: 9 Nov 2026 (moved to a new repository).

## 1. What exists

A FastAPI service on Cloud Run that logs GRC requests (email, WhatsApp, Google Chat) into a Google
Sheet, proposes an owner by fixed rules, emails assignments, sends digests, and creates recurring
tasks. Python 3.12, pydantic models, Sheet is the only store, all Google APIs behind small classes
with fakes in `tests/fakes.py`. `pytest -q` (547 tests) and `ruff check . && ruff format --check .`
must pass before saying a step is done. Local venv: `.venv/bin/python -m pytest -q`.

**Live deployment (the user's prototype project)**
- Project `electric-cortex-513009-d7` (number 1085688466076), region `asia-southeast2` (Jakarta).
- Service `v2-git`, URL `https://v2-git-1085688466076.asia-southeast2.run.app`.
- Service accounts `grc-tracker-run` (runtime) and `grc-tracker-scheduler` (Scheduler and Pub/Sub
  push tokens). Names live only in `deploy/config.sh`.
- Settings (not secret) in Secret Manager `grc-env-prod` (from `deploy/env.prod.yaml`, git-ignored);
  secrets listed in `deploy/secrets.txt`.
- Bot mailbox: a dedicated company Workspace account (a "no-reply" address). Its Gmail token was
  minted 4 Nov 2026 in Cloud Shell with the paste-back flow. The user declined to rotate it after it
  appeared in chat; their call.
- Gemini: AI Studio key. Keys starting `AQ.` are valid Gemini keys (verified with the
  `/v1beta/models` curl, which returned 200). The first `AQ.` key, 108 characters long, was bad.
- Last deployed revision: `v2-git-00013-fmg`, from `main` at PR #11. Email intake, PDF reading by
  Gemini and replies were confirmed working end to end on 4 Nov 2026.

## 2. History and the move to a new repository

The code was built in a first repository through PRs #8–#12:
- #8: Gemini reads PDFs.
- #9: the WhatsApp bot and the deploy scripts.
- #10: v1.4 part 1 (roles, review, commands).
- #11: fixes.
- #12: the IT setup guide, `make_env.sh`, the **Recurring tab** and **instant email**.

On 9 Nov 2026 the owner lost access to that repository. #12 was merged into `main` locally
(commit "Merge PR #12 ...", all 547 tests passing), and the full history was moved to a new
repository. **This repository's `main` is the complete, current code.** The old PR numbers
refer to the first repository.

**The live deployment is still on the code from before #12.** To bring it up to date (Cloud
Shell, as the owner's own account):
1. Point the Cloud Shell copy at the new repository, or clone it fresh:
   `git -C ~/V2 remote set-url origin https://github.com/<OWNER>/<REPO>.git`,
   then `git -C ~/V2 fetch origin && git -C ~/V2 checkout -B main origin/main`.
2. `deploy/setup_gcp.sh`. A `WARNING: Gmail may not publish` line means the org policy
   *Domain restricted sharing* blocks it, and IT must add an exception for
   `gmail-api-push@system.gserviceaccount.com`.
3. `deploy/make_env.sh` (answer `y`, paste the Sheet ID; this adds `GMAIL_PUSH_TOPIC`), then
   `deploy/setup_secrets.sh env`.
4. `deploy/deploy.sh`, then `deploy/setup_scheduler.sh`.
5. Tests P1–P3 and R1–R5 in `docs/LIVE_TEST.md`.

If a Cloud Build trigger deployed from the old repository, it no longer runs. Deploy from Cloud
Shell, or create a new trigger on the new repository with `cloudbuild.yaml`.

## 3. Accepted decisions (do not re-ask)

- **v1.2–1.3:** no Drive (`File storage` = `None`); files stay in the bot mailbox (label
  `GRC/Files`). WhatsApp: a 30-second draft window, `PROMPT: assign this to <name>`, deadline
  confirmation by YES or a date.
- **v1.4 part 1 (section 19):** R1–R11, F1–F4, decisions 1–17 (R7 modifies 3, R9 modifies 15).
  - Roles: `Manager`, `Full time`, `Intern`, `Additional help`. Additional help and Manager are
    never proposed by routing.
  - An intern under a full-timer: Doer = the intern, Owner = the supervisor. An intern under the
    Manager owns their own work.
  - Reviewer: the `Reviewer` column, else the supervisor, else the Manager.
  - Commands over WhatsApp or the first line of an email reply: `progress`, `review`, `approve`,
    `return`, `done`, `files`, `assign … to …`, `tugas`/`today`/`week`, and for the Manager
    `semua`, `recurring`, `pause`, `resume`.
- **v1.4 part 3 and instant email (section 20), D1–D5, accepted 9 Nov 2026:**
  - D1: `First due date` plus `Title period` replace the old "Due rule" options.
  - D2: a weekend deadline moves to the Friday before.
  - D3: "remind 7 days before" means `Create days before` = 7 (assignment email), then the daily
    digest, until part 2.
  - D4: Gmail push via Pub/Sub, with the 10-minute run kept as a safety net.
  - D5: both in one PR.

## 4. Still to build: v1.4 part 2, the reminder engine (start only when the user says go)

The spec was agreed in the 4 Nov plan:
- a `Reminder rules` tab with seeded rules;
- hidden tabs `_reminders_sent` and `_wa_window`, and a "Status since" column in `_state`;
- working days and a Config `Holidays` list;
- one combined message per person per channel;
- WhatsApp templates with a **Lihat** button (handled as a `button` message plus a `view` row in
  `_wa_pending`);
- fallback from WhatsApp to email, both on immediate rejection and on a later "failed" status
  webhook;
- `/jobs/reminders`, replacing the digest and the WhatsApp reminder;
- Config `WA templates`, `WA template language` and `Send files via WhatsApp`;
- `whatsapp_templates/*.json`;
- Staff `Reminder channel` and `WhatsApp opt-in` (opt-in is free text now);
- Master `Reminder profile` (the column exists and is unused).

The Recurring tab's `Reminder profile` column also waits for part 2.

## 5. Things learned the hard way (troubleshooting)

`docs/SETUP_GUIDE.md` has the full table. The key ones:
- **Deploying:** merging a PR does not deploy. Cloud Run runs whatever was last deployed; check
  with `/wa/webhook` (FastAPI's "Not Found" means an older build).
- **Cloud Shell habits:**
  - Always run `cd ~/V2` first.
  - Copy only the command, never the prompt or output lines.
  - Hidden prompts show nothing while you paste.
  - Run Cloud Shell as the user's own account; the bot account has no project rights.
- **Minting the Gmail token:**
  - Use a Chrome Guest window signed in only as the bot.
  - The `state=` in the link must match the one in the pasted address, or Google returns
    `Invalid code verifier`.
  - A `/signin/rejected` page means a consent-screen or audience problem.
- **The Sheet:**
  - Master needs one row holding the `Weight` formula (row 2 can be otherwise empty), or nothing
    is logged.
  - Staff `Divisions` must be spelled exactly as in Catalog and LetterCodes:
    `Audit`, `Compliance`, `Finance`, `HR`, `Policy and Risk`.
  - The live sheet still showed "nobody covers division" warnings on 4 Nov; the user was told to
    fix them.
  - Nobody covers `Compliance` yet; the user has to decide who.
- **Email behaviour:**
  - A forward that Gmail groups into an already-logged thread becomes a reply (added to Notes,
    label `GRC/Reply`), not a new row. Test with a new subject.
  - Names written in an email body never choose the owner. Only a WhatsApp `PROMPT` or the
    `assign` command does.
- **Monitoring:**
  - The log line `httplib2 transport does not support per-request timeout` is harmless.
  - `/health` is public: never put names or cell values in its warnings or in logs.

## 6. Optional follow-ups offered, not requested

- Write the Weight formula automatically when no Master row has one, which removes the trap
  above.
- Ask IT for an alias such as `grc@<domain>` for the bot, because a "no-reply" name puts people
  off replying.
- Once IT's own deployment passes its tests, pause the user's prototype Scheduler jobs, so two
  deployments don't read the same mailbox.
