"""Every environment variable the service reads, in one place (DESIGN section 10).

Locally they come from a git-ignored `.env` (`uvicorn --env-file .env`); on Cloud Run the
non-secret values come from `deploy/env.prod.yaml` (kept in Secret Manager as one secret) and the
secrets are injected by Cloud Run from Secret Manager (`--set-secrets`, see `deploy/secrets.txt`).
The code reads both the same way. Secrets are `SecretStr`: they never show in a repr or a log.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Always needed. OAUTH_CLIENT_SECRET may be replaced by OAUTH_CLIENT_FILE on a laptop.
REQUIRED = (
    "GCP_PROJECT",
    "GCP_REGION",
    "SHEET_ID",
    "SERVICE_URL",
    "SCHEDULER_SA_EMAIL",
    "CHAT_PROJECT_NUMBER",
    "GMAIL_REFRESH_TOKEN",
    "OAUTH_CLIENT_SECRET",
)
# The WhatsApp bot (DESIGN section 17) is optional: all of these, or none.
WHATSAPP = (
    "WA_PHONE_NUMBER_ID",
    "WA_GRAPH_VERSION",
    "WA_VERIFY_TOKEN",
    "WA_ACCESS_TOKEN",
    "WA_APP_SECRET",
    "RUNTIME_SA_EMAIL",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Google Cloud and the sheet (not secret)
    gcp_project: str = ""
    gcp_region: str = ""
    sheet_id: str = ""
    shared_drive_folder_id: str = ""  # only with Config `File storage` = Drive
    service_url: str = ""  # audience of Scheduler and Cloud Tasks tokens
    scheduler_sa_email: str = ""
    runtime_sa_email: str = ""  # Cloud Tasks calls /tasks/* as this account
    chat_project_number: str = ""  # audience of Google Chat tokens
    # v1.4: Pub/Sub topic Gmail pushes new-mail notes to (projects/<p>/topics/grc-gmail-push).
    # Blank: no push, the 10-minute Gmail job alone (DESIGN section 20).
    gmail_push_topic: str = ""
    # WhatsApp (not secret)
    wa_phone_number_id: str = ""
    wa_waba_id: str = ""
    wa_graph_version: str = ""
    wa_verify_token: str = ""
    wa_tasks_queue: str = ""  # default wa-process
    # Secrets (Secret Manager on Cloud Run, .env locally)
    gmail_refresh_token: SecretStr = SecretStr("")
    oauth_client_secret: SecretStr = SecretStr("")  # the OAuth client JSON itself
    oauth_client_file: str = ""  # laptop only: path to that JSON instead
    gemini_api_key: SecretStr = SecretStr("")
    wa_access_token: SecretStr = SecretStr("")
    wa_app_secret: SecretStr = SecretStr("")

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"

    @property
    def wa_enabled(self) -> bool:
        return not any(name in missing_settings(self) for name in WHATSAPP) and bool(
            self.wa_phone_number_id
        )

    def oauth_client(self) -> dict[str, str]:
        """The OAuth client JSON (from OAUTH_CLIENT_SECRET, else the OAUTH_CLIENT_FILE path)."""
        raw = self.oauth_client_secret.get_secret_value().strip()
        if not raw and self.oauth_client_file:
            raw = Path(self.oauth_client_file).expanduser().read_text(encoding="utf-8")
        client = json.loads(raw)
        return client.get("installed") or client.get("web") or client


def _value(settings: Settings, name: str) -> str:
    value = getattr(settings, name.lower())
    return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


def missing_settings(settings: Settings) -> list[str]:
    """Names (never values) of the variables that must be set and are empty."""
    missing = [name for name in REQUIRED if not _value(settings, name).strip()]
    if "OAUTH_CLIENT_SECRET" in missing and settings.oauth_client_file.strip():
        missing.remove("OAUTH_CLIENT_SECRET")
    # RUNTIME_SA_EMAIL is filled for every deploy, so only a WA_* value means WhatsApp is wanted.
    if any(_value(settings, name).strip() for name in WHATSAPP if name.startswith("WA_")):
        missing += [name for name in WHATSAPP if not _value(settings, name).strip()]
    return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def secret_path(project: str, name: str) -> str:
    """A bare secret name becomes `projects/<project>/secrets/<name>/versions/latest`."""
    if name.startswith("projects/"):
        return name if "/versions/" in name else f"{name}/versions/latest"
    return f"projects/{project}/secrets/{name}/versions/latest"


def read_secret(name: str, project: str | None = None) -> str:
    """Read a secret from Secret Manager directly (scripts only; the service gets env vars)."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    path = secret_path(project or get_settings().gcp_project, name)
    response = client.access_secret_version(request={"name": path})
    return response.payload.data.decode("utf-8")
