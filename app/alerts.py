"""Alert emails (DESIGN section 11): at most one per hour per kind, to the builder and manager.

The throttle lives in `_failures` (key `alert:<kind>`) so it survives scale-to-zero; if the
sheet itself is the problem, an in-memory throttle is used instead. A Gmail auth failure cannot
be emailed through the broken mailbox: it relies on the log-based alert in the README.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from app import clock
from app.gmail import GmailClient, OutgoingEmail
from app.models import FailureRow
from app.sheets import SheetStore

logger = logging.getLogger("grc.alerts")

ALERT_EVERY = timedelta(hours=1)
_memory: dict[str, datetime] = {}
_memory_lock = threading.Lock()

TEXTS = {
    "schema_error": (
        "The GRC tracker stopped a job because the sheet layout is not what it expects. "
        "Fix the items below; jobs resume on their own."
    ),
    "request_gave_up": (
        "An email to the GRC bot failed 3 times and got the label GRC/Failed. Fix the cause "
        "(see the logs), then mark it unread in the bot mailbox to retry, or log it by hand."
    ),
}


def _recipients(store: SheetStore) -> list[str]:
    config = store.config()
    emails = [config.manager_email, *config.builder_email.replace(";", ",").split(",")]
    return sorted({e.strip() for e in emails if e.strip()})


def _due(store: SheetStore | None, key: str, now: datetime) -> bool:
    """True if no alert of this kind went out in the last hour (and records this one)."""
    if store is not None:
        try:
            last = store.failures().get(key)
            if last is not None and last.last_at is not None and now - last.last_at < ALERT_EVERY:
                return False
            count = (last.count if last else 0) + 1
            store.upsert_failure(FailureRow(key=key, count=count, last_error="alert", last_at=now))
            return True
        except Exception:  # the sheet is the problem: fall back to memory
            pass
    with _memory_lock:
        last_sent = _memory.get(key)
        if last_sent is not None and now - last_sent < ALERT_EVERY:
            return False
        _memory[key] = now
        return True


def send_alert(
    kind: str,
    details: list[str],
    gmail: GmailClient,
    store: SheetStore | None,
    recipients: list[str] | None = None,
) -> bool:
    """Email an alert unless one of this kind went out within the hour. Never raises."""
    now = clock.now()
    try:
        to = recipients if recipients is not None else (_recipients(store) if store else [])
    except Exception:
        to = recipients or []
    if not to:
        logger.error("alert_no_recipients", extra={"kind": kind})
        return False
    if not _due(store, f"alert:{kind}", now):
        return False
    body = "\n".join(
        [TEXTS.get(kind, f"GRC tracker alert: {kind}"), "", *(f"- {d}" for d in details)]
    )
    email = OutgoingEmail(
        to=to,
        subject=f"[GRC tracker] Alert: {kind}",
        text=body,
        html="<pre>" + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>",
    )
    try:
        sender = store.config().sender_name if store else "GRC Tracker"
    except Exception:
        sender = "GRC Tracker"
    try:
        gmail.send(email, sender)
    except Exception as error:
        logger.error("alert_send_failed", extra={"kind": kind, "error_type": type(error).__name__})
        return False
    logger.warning("alert_sent", extra={"kind": kind})
    return True
