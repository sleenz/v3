import base64
import json
import logging
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.extract import ai as ai_module
from app.extract.ai import (
    AI_UNAVAILABLE_FLAG,
    PROMPT_VERSION,
    GeminiProvider,
    NotConfigured,
    NullProvider,
    apply_guardrails,
    build_prompt,
    run_ai,
)
from app.models import AIResult, Attachment, CatalogEntry
from tests.fakes import FakeAIProvider

CATALOG = [
    CatalogEntry.model_validate({"Catalog ID": "HR-DATA", "Report type": "Employee data"}),
    CatalogEntry.model_validate({"Catalog ID": "ADHOC-HR", "Report type": "Ad hoc HR"}),
]
ORIGINAL = "deadline pengumpulan pada** Rabu 06\nNovember 2026.**"


def result(**overrides) -> AIResult:
    payload = {
        "title_en": "Title",
        "summary_en": "Summary.",
        "deliverables": [],
        "deadline": "2026-11-06",
        "deadline_quote": "Rabu 06 November 2026",
        "requester_org": None,
        "contacts": [],
        "catalog_id": "HR-DATA",
        "language": "id",
        **overrides,
    }
    return AIResult.model_validate_json(json.dumps(payload))


# --- guardrails --------------------------------------------------------------------------------


def test_quote_matches_across_line_break_and_emphasis():
    kept = apply_guardrails(result(), ORIGINAL, CATALOG)
    assert kept.deadline == date(2026, 11, 6)


@pytest.mark.parametrize("quote", ["Rabu, 6 Nov 2026", None, "", "  "])
def test_deadline_dropped_when_quote_not_verbatim(quote):
    assert apply_guardrails(result(deadline_quote=quote), ORIGINAL, CATALOG).deadline is None


@pytest.mark.parametrize("catalog_id", ["FAKE-1", "ADHOC-HR", "none"])
def test_unknown_or_adhoc_catalog_becomes_none(catalog_id):
    assert apply_guardrails(result(catalog_id=catalog_id), ORIGINAL, CATALOG).catalog_id == "NONE"


# --- run_ai ------------------------------------------------------------------------------------


def test_null_provider_is_silent():
    outcome = run_ai(NullProvider(), "x", "x", CATALOG, [])
    assert outcome.result is None and outcome.flags == []


def test_success_applies_guardrails():
    outcome = run_ai(FakeAIProvider(result(catalog_id="FAKE")), "x", ORIGINAL, CATALOG, [])
    assert outcome.flags == []
    assert outcome.result.catalog_id == "NONE"


def test_error_retries_once_then_flags():
    provider = FakeAIProvider(error=RuntimeError("boom"))
    outcome = run_ai(provider, "x", "x", CATALOG, [])
    assert outcome.result is None and outcome.flags == [AI_UNAVAILABLE_FLAG]
    assert len(provider.calls) == 2


def test_not_configured_is_not_retried():
    provider = FakeAIProvider(error=NotConfigured("no api"))
    assert run_ai(provider, "x", "x", CATALOG, []).flags == [AI_UNAVAILABLE_FLAG]
    assert len(provider.calls) == 1


def test_timeout():
    provider = FakeAIProvider(result(), delay=0.3)
    outcome = run_ai(provider, "x", "x", CATALOG, [], timeout=0.05)
    assert outcome.flags == [AI_UNAVAILABLE_FLAG]


def test_input_capped_at_8000_chars():
    provider = FakeAIProvider(result())
    run_ai(provider, "y" * 9000, "y", CATALOG, [])
    assert len(provider.calls[0]["text"]) == 8000


def test_logs_metadata_but_never_text(caplog):
    secret = "Rahasia 0812-0000-0000 fajar@example.co.id"
    with caplog.at_level(logging.INFO, logger="grc.ai"):
        run_ai(FakeAIProvider(result()), secret, secret, CATALOG, ["Form.xlsx"])
    [record] = caplog.records
    assert record.getMessage() == "ai_call"
    assert record.provider == "Fake" and record.model == "fake-model"
    assert record.prompt_version == PROMPT_VERSION
    assert record.success is True and isinstance(record.latency_ms, int)
    dumped = json.dumps(record.__dict__, default=str)
    assert "Rahasia" not in dumped and "fajar" not in dumped and "Form.xlsx" not in dumped


# --- providers ---------------------------------------------------------------------------------


# --- GeminiProvider (SDK client mocked) --------------------------------------------------------


class StatusError(Exception):
    """Shaped like the SDK's GenAiError: a message and a numeric status_code."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def gemini(output_text=None, error=None, mode="api_key"):
    client = MagicMock()
    if error is not None:
        client.interactions.create.side_effect = error
    else:
        client.interactions.create.return_value = SimpleNamespace(output_text=output_text)
    provider = GeminiProvider(mode, "gemini-3.8-flash", client_factory=lambda: client)
    return provider, client


def valid_json(**overrides):
    return result(**overrides).model_dump_json()


def test_gemini_request_shape_and_parsing():
    provider, client = gemini(valid_json())
    parsed = provider.extract("Isi pesan", CATALOG, ["Form.xlsx"])
    assert parsed is not None and parsed.deadline == date(2026, 11, 6)
    kwargs = client.interactions.create.call_args.kwargs
    assert kwargs["model"] == "gemini-3.8-flash"
    assert "Isi pesan" in kwargs["input"] and "- Form.xlsx" in kwargs["input"]
    assert kwargs["response_format"]["type"] == "text"
    assert kwargs["response_format"]["mime_type"] == "application/json"
    assert kwargs["response_format"]["schema"] == ai_module.RESPONSE_SCHEMA
    assert kwargs["generation_config"] == {"temperature": 0}
    assert kwargs["store"] is False


def test_response_schema_is_derived_from_ai_result_and_self_contained():
    schema = ai_module.RESPONSE_SCHEMA
    assert set(schema["properties"]) == set(AIResult.model_fields)
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert schema["properties"]["title_en"]["maxLength"] == 90
    assert schema["properties"]["contacts"]["items"]["type"] == "object"


def test_invalid_ai_json_counts_as_unavailable():
    for bad in ('{"title_en": 1}', "not json", '{"title_en": "x"}', ""):
        provider, _ = gemini(bad)
        outcome = run_ai(provider, "x", "x", CATALOG, [])
        assert outcome.result is None and outcome.flags == [AI_UNAVAILABLE_FLAG]


def test_over_long_ai_title_is_trimmed_not_rejected():
    provider, _ = gemini(valid_json(title_en="Manpower planning submission " * 10))
    outcome = run_ai(provider, "x", ORIGINAL, CATALOG, [])
    assert outcome.result is not None
    assert len(outcome.result.title_en) <= 90 and outcome.result.title_en.endswith("…")


def test_location_not_supported_falls_back_without_retry():
    error = StatusError("User location is not supported for the API use.", 400)
    provider, client = gemini(error=error)
    outcome = run_ai(provider, "x", "x", CATALOG, [])
    assert outcome.result is None and outcome.flags == [AI_UNAVAILABLE_FLAG]
    assert client.interactions.create.call_count == 1  # a 4xx will not succeed on retry


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_errors_fall_back_without_retry(status):
    provider, client = gemini(error=StatusError("API key not valid", status))
    assert run_ai(provider, "x", "x", CATALOG, []).flags == [AI_UNAVAILABLE_FLAG]
    assert client.interactions.create.call_count == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_errors_are_retried_once(status):
    provider, client = gemini(error=StatusError("busy", status))
    assert run_ai(provider, "x", "x", CATALOG, []).flags == [AI_UNAVAILABLE_FLAG]
    assert client.interactions.create.call_count == 2


def test_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = ai_module.provider_from_config("Gemini API key", "", "", "proj", api_key="")
    outcome = run_ai(provider, "x", "x", CATALOG, [])
    assert outcome.flags == [AI_UNAVAILABLE_FLAG]


def test_vertex_mode_needs_project_and_region():
    provider = ai_module.provider_from_config("Vertex", "m", "", "proj")
    with pytest.raises(NotConfigured):
        provider.extract("x", CATALOG, [])


def test_provider_mode_switch_is_config_only():
    off = ai_module.provider_from_config("Off", "", "", "proj")
    assert isinstance(off, NullProvider)
    key = ai_module.provider_from_config("Gemini API key", "", "", "proj", "gemini-key")
    assert isinstance(key, GeminiProvider) and key.mode == "api_key"
    assert key.name == "Gemini API key" and key.model == "gemini-3.8-flash"  # default model
    vertex = ai_module.provider_from_config("Vertex", "gemini-x", "asia-southeast1", "proj")
    assert isinstance(vertex, GeminiProvider) and vertex.mode == "vertex"
    assert (vertex.model, vertex.name) == ("gemini-x", "Vertex")


def test_real_sdk_clients_are_built_per_mode(monkeypatch):
    """Builds the real google-genai clients (no network call is made)."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    key_provider = ai_module.provider_from_config("Gemini API key", "", "", "proj", "secret")
    client = key_provider._get_client()
    assert hasattr(client, "interactions")
    vertex = ai_module.provider_from_config("Vertex", "", "asia-southeast1", "proj")
    monkeypatch.setattr("google.auth.default", lambda *a, **k: (MagicMock(), "proj"))
    assert hasattr(vertex._get_client(), "interactions")


def test_key_from_env_first_then_secret_manager(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.settings.read_secret", lambda name, project=None: calls.append(name) or "from-secret"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert ai_module.gemini_api_key("gemini-key", "proj")() == "from-env"
    monkeypatch.delenv("GEMINI_API_KEY")
    assert ai_module.gemini_api_key("gemini-key", "proj")() == "from-secret"
    assert calls == ["gemini-key"]


def test_key_never_reaches_the_logs(monkeypatch, caplog):
    from app.logs import JsonFormatter

    secret = "AIzaSy-super-secret-test-value"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    provider = ai_module.provider_from_config("Gemini API key", "", "", "proj", "s")
    provider._client_factory = None
    monkeypatch.setattr(
        provider, "_build_client", lambda: (_ for _ in ()).throw(StatusError(secret, 400))
    )
    with caplog.at_level(logging.DEBUG, logger="grc.ai"):
        run_ai(provider, "x", "x", CATALOG, [])
    formatted = "".join(JsonFormatter().format(r) for r in caplog.records)
    assert secret not in formatted and "AIzaSy" not in formatted


def test_prompt_lists_only_non_adhoc_catalog_and_file_names():
    prompt = build_prompt("Isi pesan", CATALOG, ["Form.xlsx"])
    assert "HR-DATA — Employee data" in prompt
    assert "ADHOC-HR" not in prompt
    assert "- Form.xlsx" in prompt
    assert "Isi pesan" in prompt
    assert "{{" not in prompt


# --- PDFs to the AI (v1.2 item 11) ---------------------------------------------------------------


def attachment(name, mime="application/pdf", data=b"%PDF-1.4 fake"):
    return Attachment(filename=name, mime=mime, data=data)


def test_pdf_documents_picks_pdfs_within_limits(monkeypatch):
    files = [
        attachment("a.pdf"),
        attachment("Form.xlsx", mime="application/vnd.ms-excel"),
        attachment("b.PDF", mime="application/octet-stream"),  # by extension
        attachment("names-only.pdf", data=b""),  # Chat placeholder: no bytes
        attachment("c.pdf"),
        attachment("d.pdf"),  # a fourth PDF is over MAX_PDFS
    ]
    picked, skipped = ai_module.pdf_documents(files)
    assert [f.filename for f in picked] == ["a.pdf", "b.PDF", "c.pdf"] and skipped
    monkeypatch.setattr(ai_module, "MAX_PDF_BYTES", 20)
    picked, skipped = ai_module.pdf_documents([attachment("big.pdf", data=b"x" * 21)])
    assert picked == [] and skipped
    assert ai_module.pdf_documents([attachment("Form.xlsx", mime="x")]) == ([], False)


def test_gemini_sends_pdfs_as_documents_next_to_the_prompt():
    provider, client = gemini(valid_json())
    pdf = attachment("Surat.pdf", data=b"%PDF-1.4 surat")
    provider.extract("Isi pesan", CATALOG, ["Surat.pdf", "Form.xlsx"], [pdf])
    content = client.interactions.create.call_args.kwargs["input"]
    text, document = content
    assert text["type"] == "text" and "Isi pesan" in text["text"]
    assert "- Surat.pdf (attached in full)" in text["text"]
    assert "- Form.xlsx (name only)" in text["text"]
    assert document == {
        "type": "document",
        "mime_type": "application/pdf",
        "data": base64.b64encode(b"%PDF-1.4 surat").decode("ascii"),
    }


def test_gemini_without_pdfs_sends_plain_text():
    provider, client = gemini(valid_json())
    provider.extract("Isi pesan", CATALOG, [])
    assert isinstance(client.interactions.create.call_args.kwargs["input"], str)


def test_deadline_quoted_from_a_pdf_is_kept_and_flagged():
    from_pdf = result(deadline="2026-11-20", deadline_quote="paling lambat 20 November 2026")
    provider = FakeAIProvider(from_pdf)
    outcome = run_ai(provider, "x", ORIGINAL, CATALOG, ["a.pdf"], documents=[attachment("a.pdf")])
    assert outcome.result.deadline == date(2026, 11, 20)
    assert outcome.flags == [ai_module.PDF_DEADLINE_FLAG]
    assert provider.calls[0]["documents"] == ["a.pdf"]
    # the same answer without PDFs: the quote is not in the email, so the deadline is dropped
    assert run_ai(FakeAIProvider(from_pdf), "x", ORIGINAL, CATALOG, []).result.deadline is None


def test_deadline_quoted_from_the_email_is_not_flagged_when_pdfs_were_sent():
    outcome = run_ai(
        FakeAIProvider(result()), "x", ORIGINAL, CATALOG, ["a.pdf"], documents=[attachment("a.pdf")]
    )
    assert outcome.result.deadline == date(2026, 11, 6) and outcome.flags == []


def test_pdf_deadline_without_a_quote_is_still_dropped():
    no_quote = result(deadline="2026-11-20", deadline_quote=None)
    outcome = run_ai(
        FakeAIProvider(no_quote), "x", ORIGINAL, CATALOG, [], documents=[attachment("a.pdf")]
    )
    assert outcome.result.deadline is None and outcome.flags == []


def test_pdf_calls_get_the_longer_timeout(monkeypatch):
    seen = []
    real = ai_module._executor.submit

    class Future:
        def __init__(self, inner):
            self.inner = inner

        def result(self, timeout):
            seen.append(timeout)
            return self.inner.result()

    monkeypatch.setattr(ai_module._executor, "submit", lambda *a: Future(real(*a)))
    run_ai(FakeAIProvider(result()), "x", ORIGINAL, CATALOG, [])
    run_ai(FakeAIProvider(result()), "x", ORIGINAL, CATALOG, [], documents=[attachment("a.pdf")])
    assert seen == [ai_module.TIMEOUT_SECONDS, ai_module.PDF_TIMEOUT_SECONDS]
