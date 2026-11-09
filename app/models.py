"""Data models. Sheet-backed models use the exact sheet header text as field aliases."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

Status = Literal[
    "Proposed",
    "Not Started",
    "In Progress",
    "In Review",
    "Approved",
    "Returned",
    "On Hold",
    "Done",
    "Cancelled",
]
# v1.4 (DESIGN section 19): the review statuses are open too; Returned works like In Progress.
OPEN_STATUSES: frozenset[str] = frozenset(
    {"Not Started", "In Progress", "In Review", "Approved", "Returned", "On Hold"}
)
Role = Literal["Manager", "Full time", "Intern", "Additional help", "Staff"]
RequestSource = Literal["Email", "Google Chat", "WhatsApp", "Recurring", "Manual"]


def _split_list(value: object) -> object:
    """Comma-separated sheet cell -> list of trimmed, non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def wa_number(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[2:] if digits.startswith("00") else digits


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


_TEXT_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y")


def _text_date(value: object) -> object:
    """A date typed as text (e.g. `31/12/2026`, `31 Dec 2026`) instead of a real date cell.
    Day first, as written in Hong Kong and Indonesia. Anything else is left for pydantic to
    refuse."""
    value = _blank_to_none(value)
    if not isinstance(value, str):
        return value
    text = " ".join(value.split())
    for fmt in _TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return value


class _SheetRow(BaseModel):
    """A row read from a tab. Blank cells arrive as "" and numbers in text columns become text."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", coerce_numbers_to_str=True)


# --- Intake -----------------------------------------------------------------------------------


class Attachment(BaseModel):
    filename: str
    mime: str
    data: bytes = Field(repr=False)

    @property
    def size(self) -> int:
        return len(self.data)


class RawRequest(BaseModel):
    source: RequestSource
    source_id: str
    received_at: datetime
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    sender_name: str = ""
    sender_email: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    submitted_by: str = ""
    # Set only by the recurring job: the Catalog row the request is for (routing rule 1).
    catalog_id: str | None = None
    # WhatsApp PROMPT (v1.3): a person the sender named ("assign this to …"), matched against
    # Staff by rules, and the whole PROMPT text, which guides the AI and goes to Notes.
    assign_to: str | None = None
    sender_note: str = ""


class OutgoingEmail(BaseModel):
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str
    text: str
    html: str
    attachments: list[Attachment] = Field(default_factory=list)  # v1.4: real file attachments


class Contact(BaseModel):
    name: str | None = None
    email: str | None = None

    def label(self) -> str:
        if self.name and self.email:
            return f"{self.name} <{self.email}>"
        return self.name or self.email or ""


# --- Reference tabs ----------------------------------------------------------------------------


class StaffMember(_SheetRow):
    name: str = Field(alias="Name")
    email: str = Field(default="", alias="Email")
    main_division: str = Field(default="", alias="Main division")
    role: Role = Field(default="Full time", alias="Role")
    internship_end: date | None = Field(default=None, alias="Internship end date")
    capacity: float = Field(default=1.0, alias="Capacity (1 = full time)")
    weighted_open_load: float = Field(default=0.0, alias="Weighted open load")
    # v1.3, optional column: the WhatsApp number in international format (e.g. +852 9123 4567).
    whatsapp: str = Field(default="", alias="WhatsApp")
    # v1.4 roles and authority (DESIGN section 19), all optional columns.
    title: str = Field(default="", alias="Title")
    department: str = Field(default="", alias="Department")
    divisions: str = Field(default="", alias="Divisions")  # ";"-separated; blank = Main division
    reports_to: str = Field(default="", alias="Reports to")
    reminder_channel: str = Field(default="", alias="Reminder channel")
    # Free text (e.g. `Yes`, or the date consent was given); for the reminder engine (v1.4 part 2).
    whatsapp_opt_in: str = Field(default="", alias="WhatsApp opt-in")
    reminders_paused: str = Field(default="", alias="Reminders paused")

    _dates = field_validator("internship_end", mode="before")(_text_date)

    @property
    def division_list(self) -> list[str]:
        raw = self.divisions if self.divisions.strip() else self.main_division
        return [part.strip() for part in raw.replace(",", ";").split(";") if part.strip()]

    @property
    def is_full_time(self) -> bool:
        return self.role in ("Full time", "Staff")

    @property
    def paused(self) -> bool:
        return self.reminders_paused.strip().lower() in {"yes", "y", "ya", "true"}

    @property
    def wa_id(self) -> str:
        """The number as WhatsApp sends it in webhooks: digits only, no + or leading 00."""
        return wa_number(self.whatsapp)

    @field_validator("role", mode="before")
    @classmethod
    def _role_default(cls, value: object) -> object:
        # The Role column is optional in the sheet; a blank role (and the old "Staff") is full time.
        if _blank_to_none(value) is None or str(value).strip() == "Staff":
            return "Full time"
        return str(value).strip()

    @field_validator("capacity", mode="before")
    @classmethod
    def _capacity_default(cls, value: object) -> object:
        # Blank or zero capacity would make the load ratio meaningless; treat it as full time.
        return 1.0 if _blank_to_none(value) in (None, 0, "0") else value

    @field_validator("weighted_open_load", mode="before")
    @classmethod
    def _load_default(cls, value: object) -> object:
        return 0.0 if _blank_to_none(value) is None else value


class CatalogEntry(_SheetRow):
    catalog_id: str = Field(alias="Catalog ID")
    report_type: str = Field(default="", alias="Report type")
    division: str = Field(default="", alias="Division")
    regulator: str = Field(default="", alias="Regulator / Institution")
    recurrence: str = Field(default="", alias="Recurrence")
    default_owner: str = Field(default="", alias="Default owner")
    backup: str = Field(default="", alias="Backup")
    typical_due_day: str = Field(default="", alias="Typical due day")
    due_months_seen: str = Field(default="", alias="Due months seen (2026)")
    match_keywords: list[str] = Field(default_factory=list, alias="Match keywords")
    known_contacts: list[str] = Field(default_factory=list, alias="Known contacts")
    confirmed: bool = Field(default=False, alias="Confirmed by manager?")

    _lists = field_validator("match_keywords", "known_contacts", mode="before")(_split_list)

    @field_validator("typical_due_day", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> object:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    @field_validator("confirmed", mode="before")
    @classmethod
    def _yes_no(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() in {"yes", "y", "true"}
        return bool(value)

    @property
    def is_adhoc(self) -> bool:
        return self.catalog_id.upper().startswith("ADHOC-")


class LetterCode(_SheetRow):
    letter_code: str = Field(alias="Letter code")
    route_to_division: str = Field(alias="Route to division")


class RecurringRule(_SheetRow):
    """One row of the optional `Recurring` tab (v1.4 part 3, DESIGN section 20): a task that is
    created again every period, already approved, with a fixed Owner and Doer."""

    name: str = Field(alias="Name")
    active: str = Field(default="", alias="Active")
    title: str = Field(alias="Title")
    repeat: str = Field(alias="Repeat")
    first_due: date | None = Field(alias="First due date")
    title_period: str = Field(default="", alias="Title period")
    create_days_before: int | None = Field(default=None, alias="Create days before")
    owner: str = Field(default="", alias="Owner")
    doer: str = Field(alias="Doer")
    needs_review: str = Field(default="", alias="Needs review")
    reviewer: str = Field(default="", alias="Reviewer")
    catalog_id: str = Field(default="", alias="Catalog ID")
    notes: str = Field(default="", alias="Notes")

    _date = field_validator("first_due", mode="before")(_text_date)

    @field_validator("create_days_before", mode="before")
    @classmethod
    def _whole_days(cls, value: object) -> object:
        value = _blank_to_none(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    @property
    def is_active(self) -> bool:
        """Blank counts as Yes: a row is there to be used; `No` pauses it."""
        return self.active.strip().lower() not in {"no", "n", "false", "tidak"}


# Columns a Recurring tab must have; the others are optional.
RECURRING_REQUIRED_COLUMNS: tuple[str, ...] = ("Name", "Title", "Repeat", "First due date", "Doer")


# --- Master ------------------------------------------------------------------------------------


class Task(_SheetRow):
    """One Master row. `weight` is a formula column and is never written from this model."""

    id: str = Field(default="", alias="ID")
    type: str = Field(default="", alias="Type")
    title: str = Field(default="", alias="Title")
    catalog_id: str = Field(default="", alias="Catalog ID")
    category: str = Field(default="", alias="Category")
    division: str = Field(default="", alias="Division")
    requester: str = Field(default="", alias="Requester / Regulator")
    owner: str = Field(default="", alias="Owner")
    doer: str = Field(default="", alias="Doer")
    support: str = Field(default="", alias="Support")
    whole_team: str = Field(default="", alias="Whole team")
    letter_no: str = Field(default="", alias="Letter no.")
    letter_code: str = Field(default="", alias="Letter code")
    request_reference: str = Field(default="", alias="Request reference")
    start: date | None = Field(default=None, alias="Start / received")
    due_date: date | None = Field(default=None, alias="Due date")
    submitted_date: date | None = Field(default=None, alias="Submitted date")
    priority: str = Field(default="", alias="Priority")
    effort: str = Field(default="", alias="Effort")
    progress: str = Field(default="", alias="Progress")
    status: Status = Field(default="Proposed", alias="Status")
    last_updated: datetime | None = Field(default=None, alias="Last updated")
    link: str = Field(default="", alias="Link")
    send_to: str = Field(default="", alias="Send to")
    notes: str = Field(default="", alias="Notes")
    flag: str = Field(default="", alias="Flag")
    source: str = Field(default="", alias="Source")
    routing_note: str = Field(default="", alias="Routing note")
    original_message: str = Field(default="", alias="Original message")
    files: str = Field(default="", alias="Files")
    ai_summary: str = Field(default="", alias="AI summary")
    deliverables: str = Field(default="", alias="Deliverables")
    contacts: str = Field(default="", alias="Contacts")
    source_id: str = Field(default="", alias="Source ID")
    # v1.4 review lifecycle (DESIGN section 19), appended at the right of Master.
    needs_review: str = Field(default="", alias="Needs review")
    reviewer: str = Field(default="", alias="Reviewer")
    review_notes: str = Field(default="", alias="Review notes")
    reminder_profile: str = Field(default="", alias="Reminder profile")
    review_file: str = Field(default="", alias="Review file")

    _dates = field_validator("start", "due_date", "submitted_date", "last_updated", mode="before")(
        _blank_to_none
    )

    @property
    def flags(self) -> list[str]:
        return [part.strip() for part in self.flag.split(";") if part.strip()]

    @property
    def review_needed(self) -> bool:
        return self.needs_review.strip().lower() in {"yes", "y", "ya", "true"}

    @property
    def progress_value(self) -> float | None:
        """Progress as a fraction (0.5), from 0.5, 50 or "50%"; None when blank or unreadable."""
        text = self.progress.strip().rstrip("%").strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return value / 100 if value > 1 or self.progress.strip().endswith("%") else value


# v1.4 columns are optional so a sheet without them keeps working: /health warns, and a command
# that needs one says which column to add.
REVIEW_COLUMNS: tuple[str, ...] = (
    "Needs review", "Reviewer", "Review notes", "Reminder profile", "Review file",
)  # fmt: skip
MASTER_REQUIRED_COLUMNS: tuple[str, ...] = (
    *(f.alias for f in Task.model_fields.values() if f.alias and f.alias not in REVIEW_COLUMNS),
    "Weight",
)


# --- Results -----------------------------------------------------------------------------------


class RouteResult(BaseModel):
    owner: str | None = None
    doer: str | None = None  # v1.4: an intern under a full-time supervisor (owner); None = owner
    division: str | None = None
    catalog_id: str | None = None
    rule: int
    note: str
    flags: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    task_id: str
    created: bool
    title: str
    owner: str | None = None
    due_date: date | None = None
    flags: list[str] = Field(default_factory=list)
    files_url: str = ""


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _trim_words(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters at a word boundary, ending with an ellipsis."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


class AIResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    # The Gemini API ignores maxLength (DESIGN section 6); the validators below enforce it.
    title_en: str = Field(json_schema_extra={"maxLength": 90})
    summary_en: str = Field(json_schema_extra={"maxLength": 400})
    deliverables: list[str] = Field(default_factory=list, json_schema_extra={"maxItems": 10})
    deadline: date | None = None
    deadline_quote: str | None = None
    requester_org: str | None = None
    contacts: list[Contact] = Field(default_factory=list)
    catalog_id: str = "NONE"
    language: Literal["id", "en", "mixed"]

    @field_validator("title_en", mode="after")
    @classmethod
    def _title_limit(cls, value: str) -> str:
        return _trim_words(value, 90)

    @field_validator("summary_en", mode="after")
    @classmethod
    def _two_sentences(cls, value: str) -> str:
        sentences = _SENTENCE_END.split(value.strip())
        return " ".join(sentences[:2])


# --- Service tabs and Config -------------------------------------------------------------------


class StateRow(_SheetRow):
    """One `_state` row: what the service last saw and notified for a task."""

    id: str = Field(alias="ID")
    status: str = Field(default="", alias="Status")
    owner: str = Field(default="", alias="Owner")
    doer: str = Field(default="", alias="Doer")
    notified_doer: str = Field(default="", alias="Notified doer")
    notified_at: datetime | None = Field(default=None, alias="Notified at")

    _dates = field_validator("notified_at", mode="before")(_blank_to_none)


class WaPending(_SheetRow):
    """One `_wa_pending` row (v1.3): an open WhatsApp draft or a deadline question.

    `draft`: the messages one sender sent within the 30-second window, logged as one request.
    `confirm`: the deadline question for a task just logged, waiting for YES or a date.
    """

    key: str = Field(alias="Key")
    # "sent" (v1.4): a bot message about one task, so a reply to it knows the task.
    kind: Literal["draft", "confirm", "sent"] = Field(alias="Kind")
    sender: str = Field(default="", alias="Sender")
    text: str = Field(default="", alias="Text")
    prompt: str = Field(default="", alias="Prompt")
    media: str = Field(default="", alias="Media")  # JSON list of {id, filename, mime}
    message_ids: str = Field(default="", alias="Message IDs")
    task_id: str = Field(default="", alias="Task ID")
    status: Literal["open", "processing", "done"] = Field(default="open", alias="Status")
    created_at: datetime | None = Field(default=None, alias="Created at")
    updated_at: datetime | None = Field(default=None, alias="Updated at")

    _dates = field_validator("created_at", "updated_at", mode="before")(_blank_to_none)


class FailureRow(_SheetRow):
    """One `_failures` row: a failure counter (key = Source ID) or alert throttle (`alert:…`)."""

    key: str = Field(alias="Key")
    count: int = Field(default=0, alias="Count")
    last_error: str = Field(default="", alias="Last error")
    last_at: datetime | None = Field(default=None, alias="Last at")

    _dates = field_validator("last_at", mode="before")(_blank_to_none)

    @field_validator("count", mode="before")
    @classmethod
    def _count_default(cls, value: object) -> object:
        return 0 if _blank_to_none(value) is None else value


class AuditEntry(_SheetRow):
    timestamp: datetime = Field(alias="Timestamp")
    user: str = Field(alias="User")
    task_id: str = Field(default="", alias="Task ID")
    action: str = Field(alias="Action")
    field: str = Field(default="", alias="Field")
    old_value: str = Field(default="", alias="Old value")
    new_value: str = Field(default="", alias="New value")
    reason: str = Field(default="", alias="Reason")


def _yes(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"yes", "y", "true"}


DEFAULT_AI_MODEL = "gemini-3.8-flash"


class AppConfig(_SheetRow):
    """The Config tab (Setting → Value). New v1 settings have defaults; the rest are required."""

    effort_small: float = Field(alias="Effort: Small")
    effort_medium: float = Field(alias="Effort: Medium")
    effort_large: float = Field(alias="Effort: Large")
    support_share: float = Field(alias="Support share")
    overload_threshold: float = Field(alias="Overload threshold")
    stale_after_days: int = Field(alias="Stale after (days)")
    reminder_time: str = Field(alias="Reminder time")
    leaving_warning_days: int = Field(alias="Leaving warning (days)")
    unknown_letter_code: str = Field(alias="Unknown letter code")
    manager_email: str = Field(alias="Manager email")
    allowed_email_domain: str = Field(alias="Allowed email domain")
    # v1.2: the bot reads its whole inbox; the old intake label setting is ignored if present.
    gmail_intake_label: str = Field(default="", alias="Gmail intake label")
    gmail_done_label: str = Field(alias="Gmail done label")
    recurring_lead_days: int = Field(alias="Recurring lead (days)")
    default_due_days: int = Field(alias="Default due (days)")
    ai_provider: Literal["Gemini API key", "Vertex", "Off"] = Field(
        default="Off", alias="AI provider"
    )
    ai_model: str = Field(default=DEFAULT_AI_MODEL, alias="AI model")
    ai_region: str = Field(default="", alias="AI region")
    sender_name: str = Field(default="GRC Tracker", alias="Sender name")
    mask_emails_for_ai: bool = Field(default=False, alias="Mask emails for AI")
    requester_domains: str = Field(default="", alias="Requester domains")
    known_requesters: list[str] = Field(default_factory=list, alias="Known requesters")
    builder_email: str = Field(default="", alias="Builder email")
    allowed_senders: list[str] = Field(default_factory=list, alias="Allowed senders")
    # v1.2 decision A: no Drive by default. `Drive` turns task folders back on (DESIGN D5).
    file_storage: Literal["Drive", "None"] = Field(default="None", alias="File storage")
    # v1.2 item 11: email PDFs go to the AI unmasked. Blank or missing = on; FALSE/No turns it off.
    ai_reads_pdfs: bool = Field(default=True, alias="AI reads PDFs")

    _lists = field_validator("known_requesters", mode="before")(_split_list)

    @field_validator("allowed_senders", mode="before")
    @classmethod
    def _senders(cls, value: object) -> object:
        if isinstance(value, str):
            parts = value.replace(";", ",").replace("\n", ",").split(",")
            return [part.strip().lower() for part in parts if part.strip()]
        return value if value is not None else []

    def sender_allowed(self, email: str) -> bool:
        """Exact address, or `@domain` for a whole domain (D16). Empty list: nobody."""
        email = email.strip().lower()
        if not email or "@" not in email:
            return False
        domain = "@" + email.rpartition("@")[2]
        return any(entry in (email, domain) for entry in self.allowed_senders)

    @field_validator(
        "ai_provider", "ai_model", "sender_name", "file_storage", "ai_reads_pdfs", mode="before"
    )
    @classmethod
    def _blank_uses_default(cls, value: object, info: ValidationInfo) -> object:
        if _blank_to_none(value) is None and info.field_name:
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("reminder_time", mode="before")
    @classmethod
    def _time_text(cls, value: object) -> object:
        # Sheets returns a time cell as a fraction of a day (09:00 -> 0.375).
        if isinstance(value, int | float) and 0 <= value < 1:
            minutes = round(value * 24 * 60)
            return f"{minutes // 60:02d}:{minutes % 60:02d}"
        return value

    @field_validator("mask_emails_for_ai", mode="before")
    @classmethod
    def _yes_no(cls, value: object) -> object:
        return value if isinstance(value, bool) else _yes(value)

    def effort_weight(self, effort: str) -> float:
        """Weight of an Effort value; blank or unknown counts as Medium (as the sheet formula)."""
        return {"Small": self.effort_small, "Large": self.effort_large}.get(
            effort.strip(), self.effort_medium
        )


REQUIRED_CONFIG_SETTINGS: tuple[str, ...] = tuple(
    field.alias for field in AppConfig.model_fields.values() if field.alias and field.is_required()
)

OPTIONAL_STAFF_COLUMNS = frozenset(
    {"WhatsApp", "Title", "Department", "Divisions", "Reports to", "Reminder channel",
     "WhatsApp opt-in", "Reminders paused"}
)  # fmt: skip

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "Master": MASTER_REQUIRED_COLUMNS,
    # `WhatsApp` (v1.3) and the v1.4 role and channel columns are optional.
    "Staff": tuple(
        f.alias
        for f in StaffMember.model_fields.values()
        if f.alias and f.alias not in OPTIONAL_STAFF_COLUMNS
    ),
    "Catalog": tuple(f.alias for f in CatalogEntry.model_fields.values() if f.alias),
    "LetterCodes": ("Letter code", "Route to division"),
    "AuditLog": tuple(f.alias for f in AuditEntry.model_fields.values() if f.alias),
    "Config": ("Setting", "Value"),
    "_state": tuple(f.alias for f in StateRow.model_fields.values() if f.alias),
    "_failures": tuple(f.alias for f in FailureRow.model_fields.values() if f.alias),
}

# Read only by the WhatsApp bot (v1.3), so a sheet without it still serves email and Chat.
WA_PENDING_COLUMNS: tuple[str, ...] = tuple(
    f.alias for f in WaPending.model_fields.values() if f.alias
)
