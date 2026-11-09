"""One live Gemini call with a FAKE message, to check the key, model, region and schema.

Not part of pytest. Run it by hand where the Gemini API is available, e.g. Cloud Shell or the
Jakarta Cloud Run service; from Hong Kong it may fail with "location is not supported".

    # API-key mode, key in the environment (never pass it on the command line):
    export GEMINI_API_KEY=...            # or put it in the git-ignored .env and `set -a; . ./.env`
    python scripts/smoke_gemini.py

    # API-key mode, key from Secret Manager:
    python scripts/smoke_gemini.py --secret grc-gemini-api-key --project <project>

    # Vertex mode:
    python scripts/smoke_gemini.py --mode vertex --project <project> --region asia-southeast2

The key is never printed. Exit code 0 = a valid AIResult came back.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.extract.ai import (
    GeminiProvider,
    apply_guardrails,
    gemini_api_key,
)
from app.models import DEFAULT_AI_MODEL, CatalogEntry

FAKE_MESSAGE = """Dalam rangka penyusunan Manpower Planning Terintegrasi, mohon Bapak/Ibu mengisi
Form MPP dan menyiapkan PPT Joint Planning Session. Pengumpulan paling lambat Jumat 13 November
2026 melalui email ke [email]. PIC: Contoh Nama ([phone])."""

CATALOG = [
    CatalogEntry.model_validate({"Catalog ID": "HR-DATA", "Report type": "Employee data"}),
    CatalogEntry.model_validate({"Catalog ID": "FIN-ATMR", "Report type": "ATMR"}),
]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["api_key", "vertex"], default="api_key")
    parser.add_argument("--model", default=DEFAULT_AI_MODEL)
    parser.add_argument("--secret", default="", help="Secret Manager name of the API key")
    parser.add_argument("--project", default="")
    parser.add_argument("--region", default="asia-southeast2", help="Vertex mode only")
    args = parser.parse_args(argv)

    provider = GeminiProvider(
        args.mode,
        args.model,
        api_key=gemini_api_key(args.secret, args.project),
        project=args.project,
        region=args.region,
    )
    print(f"mode={args.mode} model={provider.model}")
    started = time.monotonic()
    try:
        result = provider.extract(FAKE_MESSAGE, CATALOG, ["Form_MPP.xlsx", "JPS_MPP.pptx"])
    except Exception as error:
        status = getattr(error, "status_code", "")
        print(f"FAILED after {time.monotonic() - started:.1f}s: {type(error).__name__} {status}")
        print(f"  {str(error)[:300]}")  # API error text only; the key is never part of it
        return 1
    print(f"latency {time.monotonic() - started:.1f}s")
    if result is None:
        print("FAILED: empty response")
        return 1
    checked = apply_guardrails(result, FAKE_MESSAGE, CATALOG)
    print(f"title_en      : {checked.title_en}")
    print(f"summary_en    : {checked.summary_en}")
    print(f"deliverables  : {checked.deliverables}")
    print(f"deadline      : {checked.deadline} (quote: {checked.deadline_quote!r})")
    print(f"catalog_id    : {checked.catalog_id}")
    print(f"language      : {checked.language}")
    print("OK: valid AIResult" + ("" if checked.deadline else " (deadline dropped by guardrail)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
