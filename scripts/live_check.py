"""Live check against the real Google services. Run by hand on your own machine; not part of pytest.

Reads settings from the environment or a git-ignored `.env` in the repo root (see `.env.example`).
Secrets are only ever reported as "set" or "missing"; nothing secret is printed or written.

Read-only by default:
    python scripts/live_check.py
checks the env, the sheet (tabs, columns, Config, Staff), the bot mailbox (login, unread count,
labels), Gemini (one call with a fake message) and that the Drive folder is reachable.

Opt-in steps that write:
    --write-drive        create a test folder with one file in SHARED_DRIVE_FOLDER_ID, delete it
    --send-to ADDRESS    send one test email from the bot mailbox to ADDRESS
    --gmail-once [N]     run the real intake once on up to N unread emails (default 1): writes rows
                         to the sheet, files to Drive, replies to senders. Use the DEV sheet.

Sheets and Drive use Application Default Credentials. Impersonate the runtime service account so
the check sees exactly what production sees:
    gcloud auth application-default login \
      --impersonate-service-account=grc-tracker-run@<project>.iam.gserviceaccount.com
Gemini may answer "location is not supported" from Hong Kong (DESIGN D17); run that step from
Cloud Shell or the Jakarta service if so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402 (after the sys.path line above)
from app.models import DEFAULT_AI_MODEL, OutgoingEmail  # noqa: E402

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
SECRETS = ("GMAIL_REFRESH_TOKEN", "GEMINI_API_KEY")
results: list[tuple[str, bool | None, str]] = []


def report(step: str, ok: bool | None, detail: str = "") -> None:
    """ok=True pass, False fail, None skipped/info."""
    mark = {True: "PASS", False: "FAIL", None: "----"}[ok]
    results.append((step, ok, detail))
    print(f"[{mark}] {step}" + (f": {detail}" if detail else ""))


ENV_NOTES: dict[str, str] = {}  # key -> how its .env line was read (never the value)


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, optional quotes and spaces. A non-empty variable
    already in the environment wins; an empty one does not block the .env value."""
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        note = f"line {number}"
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value.startswith(("'", '"')):
            note += ": opening quote without a closing one on the same line"
            value = value.strip("\"'")
        elif value.startswith("#"):
            note += ": the value is empty (everything after = is a # comment)"
            value = ""
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()  # inline comment after an unquoted value
        if not value and "empty" not in note and "quote" not in note:
            note += ": empty after the ="
        ENV_NOTES[key] = note
        if os.environ.get(key, "").strip():
            ENV_NOTES[key] += " (overridden by a non-empty variable already in your shell)"
            continue
        os.environ[key] = value


def id_from(value: str) -> str:
    """Accept a full Google Sheets or Drive URL where an ID is expected."""
    match = re.search(r"/(?:spreadsheets/d|folders)/([A-Za-z0-9_-]{10,})", value)
    return match.group(1) if match else value.strip()


def client_file_path() -> Path | None:
    """OAUTH_CLIENT_FILE as given, else relative to the repo root."""
    value = os.environ.get("OAUTH_CLIENT_FILE", "").strip().strip("\"'")
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.exists() and not path.is_absolute() and (ROOT / path).exists():
        path = ROOT / path
    return path


def error_text(error: Exception) -> str:
    status = getattr(error, "status_code", None) or getattr(
        getattr(error, "resp", None), "status", ""
    )
    # googleapiclient's HttpError keeps the API's own message in .reason; str() starts with the URL.
    detail = getattr(error, "reason", None) or str(error)
    return f"{type(error).__name__} {status} {str(detail)[:300]}".strip()


def runtime_sa() -> str:
    # Cloud Shell sets GOOGLE_CLOUD_PROJECT. No <placeholder>: bash reads < > as redirection.
    project = (
        os.environ.get("GCP_PROJECT", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or "YOUR-PROJECT-ID"
    )
    return f"grc-tracker-run@{project}.iam.gserviceaccount.com"


# --- Steps -------------------------------------------------------------------------------------


def check_env() -> None:
    needed = {
        "SHEET_ID": "sheet",
        "SHARED_DRIVE_FOLDER_ID": "only if Config File storage = Drive",
        "GMAIL_REFRESH_TOKEN": "mailbox",
        "OAUTH_CLIENT_FILE": "mailbox",
        "GEMINI_API_KEY": "Gemini",
    }
    if not (ROOT / ".env").exists():
        report(".env found", None, f"no .env in {ROOT} (on Windows check it is not .env.txt)")
    for name, used_for in needed.items():
        value = os.environ.get(name, "").strip()
        where = ENV_NOTES.get(name, "not in .env")
        if name == "OAUTH_CLIENT_FILE" and value:
            path = client_file_path()
            raw = value.strip("\"'")
            looks_like_path = raw.lower().endswith(".json") or "/" in raw or "\\" in raw
            if path is None or not path.is_file():
                if not looks_like_path:
                    # Never echo it: a client ID or secret pasted here by mistake must not print.
                    report(
                        f"env {name}",
                        False,
                        "the value is not a file path (it looks like a "
                        "client ID or secret); put the full path to the downloaded client "
                        "JSON here",
                    )
                else:
                    report(f"env {name}", False, f"no file at {path} (use the full path)")
            else:
                report(f"env {name}", True, f"file found: {path.name}")
            continue
        if not value:
            report(f"env {name}", None, f"missing [{where}] (needed for {used_for})")
        else:
            report(f"env {name}", True, f"set [{where}]")
    if (ROOT / ".env").exists():
        tracked = os.popen(f"git -C {ROOT} ls-files .env").read().strip()
        report(".env is not committed", not tracked, "git-ignored" if not tracked else "TRACKED!")


def check_sheet():
    from app.sheets import GoogleSheetsBackend, SchemaError, SheetStore

    sheet_id = id_from(os.environ.get("SHEET_ID", ""))
    if not sheet_id:
        report("sheet", None, "skipped: SHEET_ID missing")
        return None
    backend = GoogleSheetsBackend(sheet_id)
    try:
        titles = backend.sheet_titles()
    except Exception as error:
        text = error_text(error)
        if type(error).__name__ == "DefaultCredentialsError":
            report(
                "sheet reachable",
                False,
                "no Google login for Sheets/Drive yet: run "
                f"`gcloud auth application-default login --impersonate-service-account="
                f"{runtime_sa()}`",
            )
        elif "end user credentials" in text or "quota project" in text:
            # Cloud Shell's and plain gcloud user logins are refused by the Sheets API.
            report(
                "sheet reachable",
                False,
                "the Sheets API refuses this login (plain gcloud / Cloud Shell user credentials). "
                f"Run `gcloud auth application-default login --impersonate-service-account="
                f"{runtime_sa()}` and share the sheet with that account",
            )
        else:
            report(
                "sheet reachable",
                False,
                text + " (is the sheet shared with the account you signed in as?)",
            )
        return None
    report("sheet reachable", True, f"{len(titles)} tabs")
    store = SheetStore(backend)
    try:
        store.load()
    except SchemaError as error:
        report("sheet schema", False, f"{len(error.problems)} problem(s)")
        for problem in error.problems:
            print(f"         - {problem}")
        return None
    report("sheet schema", True, "all tabs, columns and settings present")
    config = store.config()
    staff = store.staff()
    catalog = store.catalog()
    report(
        "Config Allowed senders",
        bool(config.allowed_senders),
        f"{len(config.allowed_senders)} entr(y/ies)"
        if config.allowed_senders
        else "empty: every email will be ignored",
    )
    report(
        "Config Manager email",
        bool(config.manager_email.strip()) or None,
        "set" if config.manager_email.strip() else "blank: no manager digest or alerts",
    )
    report(
        "Config Allowed email domain",
        bool(config.allowed_email_domain.strip()) or None,
        "set" if config.allowed_email_domain.strip() else "blank: the Chat bot refuses everyone",
    )
    report("Config AI provider", None, f"{config.ai_provider}, model {config.ai_model}")
    no_email = sum(1 for p in staff if not p.email.strip())
    managers = sum(1 for p in staff if p.role == "Manager")
    report(
        "Staff emails",
        no_email == 0 or None,
        f"{len(staff)} people, {no_email} without email (they get no assignment emails)",
    )
    report(
        "Staff managers",
        managers > 0 or None,
        f"{managers} marked Manager (unmarked managers can be proposed as owners)",
    )
    confirmed = sum(1 for e in catalog if e.confirmed)
    contacts = sum(1 for e in catalog if e.known_contacts)
    report(
        "Catalog",
        None,
        f"{len(catalog)} rows, {confirmed} confirmed (recurring), {contacts} with Known contacts",
    )
    report("Master", None, f"{len(store.tasks())} task rows")
    return store


def gmail_client():
    from google.oauth2.credentials import Credentials

    from app.gmail import GoogleGmailClient

    token = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()
    path = client_file_path()
    if not token or path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data = data.get("installed") or data.get("web") or data
    credentials = Credentials(
        token=None,
        refresh_token=token,
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=GMAIL_SCOPES,
    )
    return GoogleGmailClient(credentials)


def check_gmail(store):
    try:
        client = gmail_client()
    except Exception as error:
        report("mailbox", False, f"cannot read the OAuth client file: {type(error).__name__}")
        return None
    if client is None:
        report("mailbox", None, "skipped: GMAIL_REFRESH_TOKEN or OAUTH_CLIENT_FILE missing")
        return None
    try:
        address = client.address()
    except Exception as error:
        report("mailbox login", False, error_text(error))
        return None
    report("mailbox login", True, f"signed in as {address}")
    if store is not None:
        config = store.config()
        report(
            "bot is not an allowed sender",
            not config.sender_allowed(address) or None,
            "ok" if not config.sender_allowed(address) else "its own mail is ignored anyway",
        )
    try:
        unread = client.unread_inbox(100)
        report("mailbox inbox", True, f"{len(unread)} unread message(s) waiting (max 100 shown)")
        labels = client._execute(client._users().labels().list(userId="me")).get("labels", [])
        grc = sorted(label["name"] for label in labels if label["name"].startswith("GRC/"))
        report("mailbox GRC labels", None, ", ".join(grc) or "none yet (created on first use)")
    except Exception as error:
        report("mailbox read", False, error_text(error))
    return client


def check_gemini(store) -> None:
    from app.extract.ai import GeminiProvider, apply_guardrails
    from app.models import CatalogEntry

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        report("Gemini", None, "skipped: GEMINI_API_KEY missing")
        return
    model = store.config().ai_model if store is not None else DEFAULT_AI_MODEL
    message = (
        "Mohon kirimkan Form MPP dan PPT Joint Planning Session paling lambat Jumat 13 November "
        "2026. PIC: Contoh Nama ([phone])."
    )
    catalog = [
        CatalogEntry.model_validate({"Catalog ID": "HR-DATA", "Report type": "Employee data"})
    ]
    provider = GeminiProvider("api_key", model, api_key=lambda: key)
    started = time.monotonic()
    try:
        result = provider.extract(message, catalog, ["Form_MPP.xlsx"])
    except Exception as error:
        hint = ""
        if "location" in str(error).lower():
            hint = " -> run this step from Cloud Shell (DESIGN D17)"
        report(f"Gemini {model}", False, error_text(error) + hint)
        return
    seconds = time.monotonic() - started
    if result is None:
        report(f"Gemini {model}", False, f"empty answer after {seconds:.1f}s")
        return
    checked = apply_guardrails(result, message, catalog)
    report(f"Gemini {model}", True, f"valid AIResult in {seconds:.1f}s")
    print(f"         title: {checked.title_en}")
    print(
        f"         deadline: {checked.deadline} | catalog: {checked.catalog_id}"
        f" | deliverables: {len(checked.deliverables)}"
    )


def drive_store():
    from app.drive import GoogleDriveStore

    parent = id_from(os.environ.get("SHARED_DRIVE_FOLDER_ID", ""))
    return GoogleDriveStore(parent) if parent else None


def check_drive(write: bool, sheet) -> None:
    if sheet is not None and sheet.config().file_storage != "Drive":
        report("Drive", None, "not used (Config File storage = None)")
        return
    store = drive_store()
    if store is None:
        report("Drive", None, "skipped: SHARED_DRIVE_FOLDER_ID missing")
        return
    try:
        store.file_names(id_from(os.environ["SHARED_DRIVE_FOLDER_ID"]))
        report("Drive folder reachable", True, "listing works")
    except Exception as error:
        report("Drive folder reachable", False, error_text(error))
        return
    if not write:
        report(
            "Drive write", None, "skipped (add --write-drive to create and delete a test folder)"
        )
        return
    source_id = f"live-check-{datetime.now():%Y%m%d%H%M%S}"
    try:
        folder = store.task_folder(source_id, "Live check – safe to delete")
        store.upload(folder.id, "live-check.txt", "text/plain", b"GRC tracker live check")
        again = store.task_folder(source_id, "ignored")  # must reuse, not duplicate
        names = store.file_names(folder.id)
        store._files().delete(fileId=folder.id, supportsAllDrives=True).execute()
    except Exception as error:
        report(
            "Drive write",
            False,
            error_text(error)
            + " (a 403/404 on the parent can mean drive.file is too narrow; tell Claude)",
        )
        return
    report(
        "Drive write",
        again.id == folder.id and "live-check.txt" in names,
        "created, reused by Source ID, uploaded, deleted",
    )


def send_test(client, address: str) -> None:
    if client is None:
        report("send test email", None, "skipped: mailbox not available")
        return
    email = OutgoingEmail(
        to=[address],
        subject="[GRC tracker] live check",
        text=f"Test email from the GRC tracker live check at {clock.now():%Y-%m-%d %H:%M} HKT.",
        html="<p>Test email from the GRC tracker live check.</p>",
    )
    try:
        client.send(email, "GRC Tracker")
        report("send test email", True, f"sent to {address}; check that inbox")
    except Exception as error:
        report("send test email", False, error_text(error))


def gmail_once(client, store, limit: int) -> None:
    from app.extract.ai import provider_from_config
    from app.gmail import process_inbox
    from app.pipeline import Services

    try:
        drive = drive_store()
    except Exception:
        drive = None
    if store is not None and store.config().file_storage != "Drive":
        from app.drive import NoFileStorage

        drive = NoFileStorage()
    if client is None or store is None or drive is None:
        report("intake run", None, "skipped: needs the mailbox, a valid sheet and the Drive folder")
        return
    config = store.config()
    ai = provider_from_config(
        config.ai_provider,
        config.ai_model,
        config.ai_region,
        os.environ.get("GCP_PROJECT", ""),
        os.environ.get("GEMINI_API_KEY", ""),
    )
    try:
        run = process_inbox(client, Services(store=store, drive=drive, ai=ai), limit=limit)
    except Exception as error:
        report("intake run", False, error_text(error))
        return
    report("intake run", run.failed == 0, json.dumps(run.model_dump()))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Live check of the GRC tracker's Google services.")
    parser.add_argument("--write-drive", action="store_true")
    parser.add_argument("--send-to", metavar="ADDRESS")
    parser.add_argument("--gmail-once", nargs="?", const=1, type=int, metavar="N")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    print("GRC tracker live check (secrets are never printed)\n")
    check_env()
    store = check_sheet()
    client = check_gmail(store)
    check_gemini(store)
    check_drive(args.write_drive, store)
    if args.send_to:
        send_test(client, args.send_to)
    if args.gmail_once:
        gmail_once(client, store, args.gmail_once)

    failed = [step for step, ok, _ in results if ok is False]
    print(
        f"\n{sum(1 for _, ok, _ in results if ok)} passed, {len(failed)} failed, "
        f"{sum(1 for _, ok, _ in results if ok is None)} skipped/info"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
