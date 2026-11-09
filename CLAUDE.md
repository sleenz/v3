# CLAUDE.md — grc-tracker

Service that logs GRC requests (Gmail, Google Chat, WhatsApp) into a Google Sheet, proposes an owner by fixed
rules, and sends assignment emails and reminders. Runs on Google Cloud Run for BRI Global Financial
Services, Hong Kong.

**Read `docs/DESIGN.md` before any change. It is the spec. If something in it is unclear or seems
wrong, stop and ask; do not invent behaviour.**

## Rules
- Python 3.12, FastAPI, type hints everywhere, pydantic models for every data shape.
- The Google Sheet is the only store. Access columns **by header name only**, through `SheetStore`.
- The AI never chooses the assignee. Routing lives in `app/route.py` and is deterministic.
- Keep `extract/rules.py`, `extract/clean.py` and `route.py` pure (no network, no Google clients) so
  they are fully unit-testable.
- All Google APIs sit behind small classes (`SheetStore`, `DriveStore`, `GmailClient`, `ChatClient`,
  `AIProvider`, and for WhatsApp `WhatsAppClient` and `TaskQueue`) with fakes in `tests/fakes.py`.
  Tests never touch the network.
- Time zone `Asia/Hong_Kong`. Never use naive "today" outside one `clock` helper that tests can freeze.
- No secrets in code, tests, fixtures, logs or committed files. Secrets come from Secret Manager.
- The Gemini API key: the `GEMINI_API_KEY` environment variable, injected by Cloud Run from Secret
  Manager in production (DESIGN section 18) and from a git-ignored `.env` locally. Never ask the
  user to paste the key, never print it, never write it to a file. Create `.gitignore` with `.env` in Phase 1.
- Live Gemini calls may fail from Hong Kong ("location not supported"); tests use FakeAIProvider.
- Never log message text, names, emails or phone numbers. Log task IDs, counts, timings, error types.
- Test data is fake or masked. Never add real BRI messages, names or numbers to the repo.
- A failure must never lose a request (DESIGN D15).

## Commands
- `pip install -r requirements.txt`
- `pytest -q` — must pass before you say a step is done
- `ruff check . && ruff format --check .`
- Local run: `uvicorn app.main:app --reload` with a `.env` that points at the **dev** sheet

## Working style
- Build in the phases the user gives you. At the end of each phase: run tests and lint, list what
  you built, what you assumed, and anything in DESIGN.md you could not follow.
- Small commits, one per logical change, message says why.
- Do not add dependencies beyond what a phase needs; say why when you add one.
