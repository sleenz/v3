# GRC Tracker: setup guide for IT

This guide installs the GRC Tracker in **your own** Google Cloud project, from nothing to a
working service. Follow the steps in order. Every command is typed in **Cloud Shell**; you do not
need to install anything on your computer.

Plan about **1 to 2 hours**. Most of that is waiting for builds and clicking through Google's
consent screens.

---

## What this service does

People email requests to a **bot mailbox**. Within about a minute of each email (and every 10
minutes as a backup) the service:

1. reads the new, unread mail in the bot's Inbox;
2. logs each request as a row in the **GRC Google Sheet** (tab `Master`), with an English title,
   a due date and a proposed owner. It uses fixed rules, and Gemini reads the text and any PDFs;
3. replies to the sender in the same email thread with the task ID (for example `G-0012`).

When the GRC team approves a task in the sheet, the service emails the assignee, and later it
sends reminders. The Google Sheet is the only database. There is no other server or database.

**What runs where**

| Piece | Where | Name |
|---|---|---|
| The service | Cloud Run, region **asia-southeast2 (Jakarta)** | `v2-git` |
| Timed runs | Cloud Scheduler | `grc-gmail`, `grc-changes`, `grc-digest`, ... |
| Passwords and keys | Secret Manager | `grc-gmail-refresh-token`, `grc-gemini-api-key`, ... |
| Settings (not secret) | Secret Manager | `grc-env-prod` |
| The service's own login | Service accounts | `grc-tracker-run`, `grc-tracker-scheduler` |

All names are set in one file, `deploy/config.sh`. Do not rename anything unless you change it
there.

---

## Before you start

You need:

- [ ] A **Google Cloud project with billing enabled**, where you have the **Owner** role.
- [ ] The **bot mailbox**: a Google Workspace account on the company domain with Gmail
      (for example `grc-bot@<company domain>`). You must be able to sign in to it with its
      password. It must be able to **receive** external and internal mail (check this if the
      address is a "no-reply" one).
- [ ] **Edit access to the GRC Google Sheet** (the GRC team owns it; ask them to share it with you).
- [ ] **Access to this code**: either the GitHub repository (ask the owner to add you), or a zip
      of it.
- [ ] **Approval** from Compliance or IT that email text and PDF attachments may be sent to
      Google's Gemini API from the Jakarta region. If this is not approved, you can still deploy
      with the sheet's Config `AI provider` set to `Off`. Everything then works on rules alone,
      with less polished titles and no reading of PDFs.

**Three rules that save time in Cloud Shell**

1. Every new Cloud Shell tab starts in your home folder. Always run `cd ~/V2` first, or you will
   get `No such file or directory`.
2. Copy **only the command**. Never paste a prompt line like `user@cloudshell:~/V2 (...)$`, or a
   script's output, back into the terminal.
3. When a script asks for a value with `(hidden)`, nothing appears while you paste. That is
   normal; press Enter.

---

## Step 1: Open Cloud Shell, get the code, select the project

Open <https://console.cloud.google.com>, select your project at the top, and click the
**Cloud Shell** icon (`>_`, top right).

Get the code (replace `OWNER/REPO` with the repository you were given). Use **one** of these:

```bash
git clone https://github.com/OWNER/REPO.git ~/V2
```

Or, if you received a zip: in Cloud Shell click **⋮ > Upload**, choose the zip, then
`unzip V2.zip -d ~` (or whatever the zip is called) so the folder is `~/V2`.

Then:

```bash
cd ~/V2
```
```bash
gcloud config set project YOUR-PROJECT-ID
```

Your prompt now ends with `~/V2 (YOUR-PROJECT-ID)$`.

---

## Step 2: Create the Google Cloud pieces (one command)

```bash
deploy/setup_gcp.sh
```

This takes a few minutes and is safe to run again. It:

- enables the APIs (Cloud Run, Scheduler, Tasks, Build, Artifact Registry, Secret Manager,
  Sheets, Drive, Gmail, Chat, IAM);
- creates the two service accounts with only the roles they need;
- creates empty secrets;
- creates the container repository and the Cloud Tasks queue;
- creates the Pub/Sub topic `grc-gmail-push`, through which Gmail announces new mail.

It ends with `Done. Next: deploy/setup_secrets.sh`.

> If it fails on a role binding for `...-compute@developer.gserviceaccount.com`, your project
> builds with a different account. Run it again with that account:
> `BUILD_SA=<build account email> deploy/setup_gcp.sh`.

---

## Step 3: Let the service sign in to Gmail as the bot

The service reads and sends mail as the bot, using a **refresh token**. You make one OAuth client
(the "app"), then sign in once **as the bot** to produce the token.

### 3a. Consent screen and OAuth client (Cloud console, as yourself)

1. **Google Auth Platform > Branding**: app name `GRC Tracker`, support email = yours. Save.
2. **Google Auth Platform > Audience**: User type **Internal**. This is only possible when the
   project belongs to the company's Google Workspace organisation, and it is the right choice:
   tokens never expire and no "unverified app" warning appears.
   - If you can only choose **External**, then: add the bot's address under **Test users**, and
     click **Publish app** so the status reads **In production**. **If you leave it in
     "Testing", the token stops working after 7 days** and intake silently stops.
3. **Google Auth Platform > Clients > Create client**: type **Desktop app**, name
   `grc-tracker`. Click **Download JSON** and save the file.
4. *Only if your Workspace restricts third-party apps:* Workspace Admin console > Security >
   API controls > App access control: trust this client ID for the Gmail scopes
   (`gmail.modify` and `gmail.send`).

### 3b. Store the OAuth client

In Cloud Shell click **⋮ > Upload** and upload the JSON file you just downloaded. Then:

```bash
deploy/setup_secrets.sh OAUTH_CLIENT_SECRET
```

At `Path to a file ...`, type the file's path (for example `~/client_secret_123.json`) and press
Enter. It prints `new version added from the file`. Then delete the uploaded copy:

```bash
rm ~/client_secret_*.json
```

### 3c. Make the bot's token

**Prepare a browser window where only the bot is signed in.** In Chrome: profile icon (top
right) > **Guest**. In that Guest window, go to gmail.com and sign in **as the bot**. Wait until
you see the bot's inbox.

In Cloud Shell:

```bash
pip install --user google-auth-oauthlib
```
```bash
python scripts/mint_gmail_token.py <(gcloud secrets versions access latest --secret=grc-gmail-oauth-client)
```

It prints a long link that starts with `https://accounts.google.com/o/oauth2/auth?` and waits at
`Address:`.

1. Copy that link **exactly as printed** and open it in the **Guest window**.
2. Check that the bot's address is the account shown, then click **Continue** or **Allow**.
3. The page then **fails to load** ("This site can't be reached / localhost refused to
   connect"). **This is expected.**
4. Copy the **whole address** from that failed page's address bar. It starts with
   `http://localhost:8085/?state=...&code=...`.
5. Paste it at `Address:` in Cloud Shell and press Enter. Do this right away, in the same run.

It then prints:

```
This token reads and sends as: <bot address>
...
1//0e....
```

- **Check the address.** If it is not the bot, do not store the token. Repeat 3c and sign in as
  the bot.
- Copy the line that starts with `1//`, then store it:

```bash
deploy/setup_secrets.sh GMAIL_REFRESH_TOKEN
```

Press **Enter** at the file question, paste the `1//...` line at `Value (hidden):`, and press
Enter. Then clear the screen:

```bash
clear
```

**Common problems in 3c**

| You see | Fix |
|---|---|
| `Invalid code verifier` | You pasted the address from an **older** link. Every run makes a new link. Run the command again and use only the newest link. The `state=` in the link and in the pasted address must match. |
| `400. That's an error. ... malformed` | The link was changed or cut while copying. Copy it again, unchanged. |
| A page at `accounts.google.com/.../signin/rejected` | Google refused the sign-in. Read the heading. **"Access blocked ... organization"**: the bot is outside the project's organisation (set Audience to External plus a test user, see 3a). **"has not completed verification"**: add the bot as a test user. **"This browser or app may not be secure"**: use a normal Chrome Guest window. **"Couldn't sign you in"**: sign in to gmail.com as the bot first and complete any phone check. |
| `ModuleNotFoundError: google_auth_oauthlib` | Run the `pip install --user google-auth-oauthlib` line. |

---

## Step 4: Gemini API key (skip if AI is not approved)

1. Go to <https://aistudio.google.com/apikey> and click **Create API key**. Choose this project.
   Copy the key.
2. Store it:

```bash
deploy/setup_secrets.sh GEMINI_API_KEY
```

Press **Enter** at the file question, paste the key at `Value (hidden):`, and press Enter.

3. Check that Google accepts it. This sends the stored key without printing it:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "x-goog-api-key: $(gcloud secrets versions access latest --secret=grc-gemini-api-key)" https://generativelanguage.googleapis.com/v1beta/models
```

It must print **200**. A **400** or **401** means the key is wrong: make a new one in AI Studio.
Keys may start with `AIza` or with `AQ.`; both work, as long as this check prints 200.

---

## Step 5: The Google Sheet

The service uses the GRC team's existing sheet. You do not need a new one.

1. **Share the sheet** (Share button) with the runtime account as **Editor**:
   `grc-tracker-run@YOUR-PROJECT-ID.iam.gserviceaccount.com`. Untick "Notify people".
2. **Find the Sheet ID** in the sheet's link: `docs.google.com/spreadsheets/d/`**`<this part>`**`/edit`.
   You will paste it in step 6. Pasting the whole link also works.
3. Ask the GRC team to confirm these points. The service checks the sheet and refuses to run
   while something is wrong, and `/health` (step 7) names the exact problem:
   - **Master** row 2 holds the **Weight** formula, even if the rest of that row is empty. New rows
     copy it from there. If it is missing, nothing can be logged.
   - **Config** has `Allowed senders` (for example `@company-domain.com`); mail from anyone else is
     ignored. `AI provider` = `Gemini API key` (or `Off`).
   - **Staff** has every team member with `Email`, `Role` (`Manager`, `Full time`, `Intern` or
     `Additional help`) and `Divisions`. The division names must be **spelled exactly** as in the
     Catalog `Division` column. `Internship end date` is a real date or blank.
   - Hidden tabs `_state` and `_failures` exist (README section 1 has their headers).
   - *Optional:* a `Recurring` tab for repeating tasks (README section 1c has its columns).

---

## Step 6: Settings file

This writes the settings for your project. It asks only for the Sheet ID:

```bash
deploy/make_env.sh
```

Then store the settings in Secret Manager. Every deploy reads them from there:

```bash
deploy/setup_secrets.sh env
```

It prints `env.prod.yaml stored as grc-env-prod`. The file `deploy/env.prod.yaml` holds no
secrets and is never committed. **Do not put keys or tokens in it**; they live only in the secrets
from steps 3 and 4.

---

## Step 7: Deploy

```bash
deploy/deploy.sh
```

This takes 3 to 6 minutes (build plus deploy). The two lines saying `WA_ACCESS_TOKEN not set` and
`WA_APP_SECRET not set` are expected while WhatsApp is not set up. It ends with a **Health** line:

| Health says | Meaning |
|---|---|
| `{"status":"ok"}` | Ready. Go to step 8. |
| `"ok"` with `warnings` | Running. The warnings are sheet wording to fix with the GRC team (for example "nobody in Staff covers division X" = a Staff `Divisions` spelling differs from the Catalog). |
| `unhealthy` with `"missing":[...]` | A setting or secret is empty. Store the named one (step 3, 4 or 6) and run `deploy/deploy.sh` again. |
| `unhealthy` with `"sheet":[...]` | The sheet needs a fix. The message names the tab, row and column. Fix the cell; **no redeploy needed**. Check again after a minute (command below). |
| An HTML page with **403** | Cloud Run blocks callers. The service must be public at the network level (every call is checked by the app itself). If your organisation enforces **Domain restricted sharing**, ask for an exception that allows `allUsers` the `run.invoker` role on this service. |

Check health at any time:

```bash
curl -s "$(gcloud run services describe v2-git --region=asia-southeast2 --format='value(status.url)')/health"
```

---

## Step 8: Timed runs

```bash
deploy/setup_scheduler.sh
```

This creates or updates the Cloud Scheduler jobs, and the Pub/Sub push subscription that makes
email **instant**: Gmail announces each new email and the service logs it within about a minute.
The script ends by starting that push. Check it worked:

```bash
gcloud logging read 'resource.labels.service_name="v2-git" AND (jsonPayload.message="gmail_watch" OR jsonPayload.message="gmail_watch_failed")' --freshness=10m --limit=2 --format='table(timestamp, jsonPayload.message, jsonPayload.status)'
```

- `gmail_watch`: instant email is on.
- `gmail_watch_failed` with status **403**, or step 2 printed `WARNING: Gmail may not publish`:
  the organisation policy *Domain restricted sharing* blocks it. Ask for an exception for
  `gmail-api-push@system.gserviceaccount.com`, then run `deploy/setup_gcp.sh` and
  `deploy/setup_scheduler.sh` again. Until then nothing breaks: email is read every 10 minutes.

The 10-minute Gmail job stays as a safety net. To run it now:

```bash
gcloud scheduler jobs run grc-gmail --location=asia-southeast2
```

---

## Step 9: Sheet guard (Apps Script)

This stops people from marking tasks Done without proof, and records every edit.

1. In the sheet: **Extensions > Apps Script**. Delete what is there and paste the whole content of
   `apps_script/audit.gs` (open it in Cloud Shell with `cloudshell edit apps_script/audit.gs`,
   select all, copy). Save.
2. In the function menu choose `installAuditTrigger` and click **Run**. Accept the permissions.
3. Choose `installValidation` and click **Run** once.

---

## Step 10: Test it

1. From an address in `Allowed senders`, send the **bot** a **new** email (not a reply, with a new
   subject), with a PDF attached, for example a letter with a deadline in it.
2. Run the job now:

```bash
gcloud scheduler jobs run grc-gmail --location=asia-southeast2
```

3. Within about a minute:
   - a new row with Status `Proposed` appears in **Master**, with an English title and a due
     date;
   - the sender gets a reply from the bot with the task ID;
   - the email in the bot's Gmail has the label `GRC/Logged`.

More tests (replies, ignored senders, assignment emails, review commands) are in
`docs/LIVE_TEST.md`.

---

## When something does not work

**Step one is always the log.** This shows what the last runs did:

```bash
gcloud logging read 'resource.labels.service_name="v2-git" AND (jsonPayload.message="gmail_run" OR jsonPayload.message="message_ignored" OR jsonPayload.message="ai_call" OR severity>=WARNING)' --freshness=1h --limit=15 --format='table(timestamp, jsonPayload.message, jsonPayload.reason, jsonPayload.error_type, jsonPayload.status, jsonPayload.n_created)'
```

Lines that say `httplib2 transport does not support per-request timeout` are harmless; ignore
them.

| Symptom or log line | Cause | Fix |
|---|---|---|
| Nothing happens for a few minutes | Instant email is off or blocked, so the 10-minute job is the only reader | Check step 8 (`gmail_watch`); meanwhile run `gcloud scheduler jobs run grc-gmail --location=asia-southeast2` |
| A Recurring row creates nothing | Not yet `Create days before` its deadline, `Active` = No, or the row has a problem | The manager sends `recurring` to the bot: it lists each row's next deadline and any problem. `/health` shows how many rows cannot be used. |
| `gmail_run` with every count 0 | No unread mail in the bot's **Inbox** | The bot only takes unread mail in the Inbox. Mark it unread, or move it out of Spam. Check the mail actually arrived. |
| `message_ignored` `sender_not_allowed` | Sender not allowed | Add the address or `@domain` to Config `Allowed senders`; mark the mail unread |
| `message_ignored` `sent_by_bot_mailbox` | The test was sent from the bot itself | Send from a person's address |
| `no_allowed_senders` | Config has no `Allowed senders` row | Add it |
| Email labelled `GRC/Reply`, no new row | It was in the same thread as an earlier request, so it was added to that task's `Notes` (by design) | Send a new email with a new subject |
| `gmail_auth_error` `invalid_grant` | The Gmail token is wrong, revoked or expired. **Changing the bot's password revokes it too.** | Redo step 3c, then `deploy/deploy.sh --skip-tests`. If it happens again after 7 days, publish the app (step 3a). |
| `schema_problem` ... `Weight formula to copy` | Master has no row with the Weight formula | Put the Weight formula back in Master row 2 |
| `schema_problem` ... `bad value in row N` | A cell the service cannot read | Fix that cell (dates: real date or `31/12/2026`) |
| `schema_problem` ... `missing tab` / `column` | The sheet layout differs | Add the named tab or column header |
| Row flagged `AI unavailable`; `ai_call` `401` | The Gemini key is not valid | Redo step 4, then `deploy/deploy.sh --skip-tests` |
| `ai_call` `403` / `429` | Key restricted or API not enabled / quota | Check the key in AI Studio; enable billing on its project for quota |
| Row has `Needs review: no owner` / `Rule 5` | No Staff entry covers the request's division | Fix Staff `Divisions` spelling (see the `/health` warnings) |

**A secret changed?** Cloud Run reads secrets only when it starts. After any
`deploy/setup_secrets.sh ...`, run `deploy/deploy.sh --skip-tests`.

**A sheet change** (Config, Staff, Catalog) needs no redeploy. It is read at every run.

---

## Day-to-day

| Task | How |
|---|---|
| Install a code update | `cd ~/V2 && git pull`, then `deploy/deploy.sh` |
| Replace the Gmail token (also needed after **any password change on the bot account**) | Step 3c, then `deploy/deploy.sh --skip-tests` |
| Replace the Gemini key | Step 4, then `deploy/deploy.sh --skip-tests` |
| Change a setting (Sheet ID, ...) | `deploy/make_env.sh`, `deploy/setup_secrets.sh env`, `deploy/deploy.sh --skip-tests` |
| Turn the AI off | Sheet Config `AI provider` = `Off` (no redeploy) |
| See what happened | The log command above, or Cloud console > Cloud Run > `v2-git` > Logs |
| Automatic deploy on every merge | Cloud Build trigger on `main` with `cloudbuild.yaml` (README "Deploy to Cloud Run") |

**Security notes**

- Tokens and keys go only into the hidden `setup_secrets.sh` prompt. Never into files, chat or
  email. If one leaks, replace it (steps 3c or 4). For the Gmail token, also remove the app under
  the bot's Google Account > Security > Third-party connections.
- The service logs only task IDs, counts and error types, never message text, names or addresses.
- The service is reachable from the internet, but every call is checked: Cloud Scheduler and Cloud
  Tasks by Google-signed tokens, Google Chat by its token, WhatsApp by Meta's signature.

**Optional, later**: the Google Chat bot (README section 9) and the WhatsApp bot (README section
9b). Both run on the same deployment; they only need their own settings and secrets.
