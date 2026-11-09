"""One-time OAuth consent for the GRC bot mailbox; prints the refresh token for Secret Manager.

Run this on a trusted laptop, signed in to the browser **as the bot mailbox** (the dedicated
Workspace account, e.g. grc-bot@<company domain>; DESIGN D3), never as a person. Nothing is
written to disk.

    pip install google-auth-oauthlib        # only needed for this script
    python scripts/mint_gmail_token.py path/to/oauth_client.json

In Cloud Shell (no local browser) it prints a sign-in link instead. Open it in a private window,
sign in as the bot, allow access; the page then fails to load ("localhost refused to connect").
That is expected: copy the whole address from the address bar and paste it back here. The client
JSON can come straight from Secret Manager, so it never touches the disk:

    S=grc-gmail-oauth-client
    python scripts/mint_gmail_token.py <(gcloud secrets versions access latest --secret=$S)

It prints the mailbox the token belongs to, then the token. Store the token as a new version of
the secret named by GMAIL_TOKEN_SECRET: in the Cloud console (Security > Secret Manager > the
secret > New version, paste, Add) or with `gcloud secrets versions add`.

The OAuth client must belong to an *Internal* consent screen (DESIGN D3): an external app in
"Testing" status gets refresh tokens that expire after 7 days.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def main(argv: list[str]) -> int:
    paste = "--paste" in argv or bool(os.environ.get("CLOUD_SHELL"))
    files = [arg for arg in argv[1:] if arg != "--paste"]
    if len(files) != 1:
        print(__doc__)
        return 2
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(files[0], scopes=SCOPES)
    if paste:
        credentials = _paste_flow(flow)
        if credentials is None:
            return 1
    else:
        credentials = flow.run_local_server(
            port=0, access_type="offline", prompt="consent", open_browser=True
        )
    if not credentials.refresh_token:
        print("No refresh token returned. Revoke the app's access and run again.")
        return 1
    mailbox = _mailbox(credentials.token)
    print(f"\nThis token reads and sends as: {mailbox}")
    print("If that is not the bot mailbox, do NOT store it: run again in a private browser")
    print("window and sign in as the bot.\n")
    print("Refresh token: copy only the next line (it starts with 1//), store it in Secret")
    print("Manager as a new version of the Gmail token secret, then clear your terminal:\n")
    print(credentials.refresh_token)
    return 0


# A Desktop OAuth client accepts any localhost port; nothing listens there, the browser only shows
# the code in its address bar.
PASTE_REDIRECT = "http://localhost:8085/"


def _paste_flow(flow: Any) -> Any:
    """Sign-in without a local browser (Cloud Shell): print the link, read back the redirect."""
    flow.redirect_uri = PASTE_REDIRECT
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("1. Open this link in a PRIVATE browser window and sign in as the BOT mailbox:\n")
    print(url)
    print("\n2. Allow access. The page then fails to load (localhost refused to connect).")
    print("3. Copy the whole address from the address bar and paste it here.\n")
    pasted = input("Address: ").strip()
    code = parse_qs(urlparse(pasted).query).get("code", [""])[0]
    if not code:
        print("No code in that address. Copy the full address (it contains code=) and run again.")
        return None
    flow.fetch_token(code=code)
    return flow.credentials


def _mailbox(access_token: str) -> str:
    """The address the token belongs to, so a token minted as the wrong account is caught here."""
    import json
    import urllib.request

    request = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())["emailAddress"]
    except Exception as error:  # the token is still valid; only the check failed
        return f"(could not check: {type(error).__name__})"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
