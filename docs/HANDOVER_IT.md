# GRC Tracker: handover to IT

You are deploying the GRC Tracker in the **company's** Google Cloud project. It already runs as
a prototype in the GRC team's own project; you replace it. This page is the order of work. The
detailed commands are in [`SETUP_GUIDE.md`](SETUP_GUIDE.md) (steps 1–10); this page says what to
do around them.

Time: about 2 hours of work, in one sitting if possible. Two moments need the **GRC team
owner** with you (marked 👤).

---

## 1. What you receive

- [ ] Access to the GitHub repository **`sleenz/v3`** (you are added as a collaborator). The
      branch `main` is the code to deploy.
- [ ] The **GRC Google Sheet**, shared with you as Editor, and its **Sheet ID** (the part of the
      link between `/d/` and `/edit`).
- [ ] The **bot mailbox** address: a company Google Workspace account the service reads and
      sends mail as.
- [ ] A decision on **Gemini** (the AI that reads emails, PDFs and replies): Compliance has
      approved sending email text and PDFs to Google's Gemini API in the Jakarta region, **or**
      you start with the sheet's Config `AI provider` = `Off` (rules only).

## 2. What you do NOT receive, and why

No keys or tokens are handed over. Every one is created new in the company project, so the
service does not depend on the prototype:

| Secret | You create it in | Why not copied |
|---|---|---|
| Gmail OAuth client | Guide step 3a–3b | It belongs to the prototype project |
| Gmail token for the bot | Guide step 3c | It only works with that project's OAuth client |
| Gemini API key | Guide step 4 | It is tied to the prototype's quota and billing |
| Settings `grc-env-prod` | Guide step 6 | It holds the prototype's project ID and URLs |
| WhatsApp token and app secret | Not needed now | WhatsApp is not set up yet |

Keys are only ever typed into the hidden prompt of `deploy/setup_secrets.sh`. Never into a
file, chat or email.

## 3. Before you start: check with the company's admins

- [ ] A Google Cloud project with **billing**, where you are **Owner**. Ideally inside the
      company's Google Workspace organisation (then the Gmail sign-in can be *Internal* and never
      expires).
- [ ] **Domain restricted sharing** (organisation policy): two exceptions are needed, or two
      features are limited:
      - `allUsers` → role `run.invoker` on the Cloud Run service `v2-git`: required. The service
        must be reachable; every call is checked by the app itself (Google-signed tokens, Meta
        signatures).
      - `gmail-api-push@system.gserviceaccount.com` → Publisher on the topic `grc-gmail-push`:
        optional. Without it, email is read every 10 minutes instead of within a minute.
- [ ] **Workspace third-party app access**: if restricted, the Workspace admin trusts the new
      OAuth client ID (made in step 3a) for the Gmail scopes `gmail.modify` and `gmail.send`.
- [ ] **The bot's sign-in** for guide step 3c: either the Workspace admin gives you a way to
      sign in as the bot, or 👤 the GRC owner signs in for you during that step (5 minutes).

## 4. Order of work

Follow `SETUP_GUIDE.md` in order, in **Cloud Shell** of the company project:

1. **Step 1**: clone `https://github.com/sleenz/v3.git` to `~/V2`, select the project.
2. **Step 2**: `deploy/setup_gcp.sh` (APIs, service accounts, empty secrets, queue, topic).
   Region and all names are fixed in `deploy/config.sh` (`asia-southeast2`, service `v2-git`);
   do not rename them.
3. **Step 3**: OAuth client (as yourself), then the bot's token. 👤 Signing in as the bot.
4. **Step 4**: Gemini key in AI Studio on the company project (skip if AI is not approved).
5. **Step 5**: share the GRC sheet with `grc-tracker-run@<company-project-id>.iam.gserviceaccount.com`
   as Editor. Keep the prototype's account on it until the switchover is done.
6. **Step 6**: `deploy/make_env.sh` (asks only for the Sheet ID), then `deploy/setup_secrets.sh env`.
7. **Step 7**: `deploy/deploy.sh`. The Health line must say `"ok"`. Warnings about Staff
   divisions are for the GRC team to fix in the sheet, not for you.
8. **Stop here** until the switchover (section 5). Do **not** run step 8 yet: it starts the
   timed runs and the instant email, and two services must never read the bot's inbox at the
   same time.
9. **Step 9**: the Apps Script is already in the sheet. Nothing to do unless the GRC team asks.

## 5. Switchover (both of you, about 15 minutes)

1. 👤 **The GRC owner pauses the prototype** (in their own Cloud Shell; you have no access to it):

   ```bash
   for j in grc-gmail grc-changes grc-reminders grc-recurring grc-gmail-watch grc-wa-drafts; do gcloud scheduler jobs pause $j --location=asia-southeast2 --project=electric-cortex-513009-d7; done
   ```

2. **You start the company service**: guide **step 8**, `deploy/setup_scheduler.sh`. It creates
   the timed runs and points the bot's Gmail push at the company project.
3. **Test together**: guide **step 10**:
   - a new email to the bot is logged in Master and answered;
   - an approved task emails its Doer within 2 minutes, with the files attached;
   - a reminder can be forced with `gcloud scheduler jobs run grc-reminders --location=asia-southeast2`.
4. **When it works:**
   - 👤 In the bot's Google Account > Security > Third-party connections, remove the **older**
     "GRC Tracker" entry (the prototype's).
   - 👤 The GRC owner removes the prototype's `grc-tracker-run@electric-cortex-513009-d7…` account
     from the sheet's sharing.
   - The prototype project can then be shut down.

If the test fails, nothing is lost: resume the prototype (`gcloud scheduler jobs resume …` with
the same loop) while you fix it. Unread mail waits in the bot's inbox.

## 6. After go-live: what IT owns

| When | What |
|---|---|
| A code update is merged | `cd ~/V2 && git pull`, then `deploy/deploy.sh` (and `deploy/setup_scheduler.sh` if the PR says so) |
| The bot's password changes | The Gmail token stops: redo guide step 3c, then `deploy/deploy.sh --skip-tests` |
| Something stops working | Guide "When something does not work": the log command first |
| The Apps Script changes | The GRC team pastes it into the sheet (it lives in the sheet, not in Cloud Run) |

The GRC team handles the sheet itself (Staff, Config, Holidays, reminder times): no deploy
needed for those.

**Logs** carry only task IDs, counts and error types, never message text, names or addresses.
**WhatsApp** (optional, later) needs a Meta business app and number: README section 9b.
