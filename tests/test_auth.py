"""Token checks with locally generated keys (no network)."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from app.auth import (
    CHAT_CERTS_URL,
    CHAT_ISSUER,
    GOOGLE_CERTS_URL,
    TokenVerifier,
    Unauthorized,
    get_verifier,
)
from app.main import app
from app.settings import Settings

SETTINGS = Settings(
    service_url="https://grc.example.run.app",
    scheduler_sa_email="scheduler@proj.iam.gserviceaccount.com",
    runtime_sa_email="run@proj.iam.gserviceaccount.com",
    chat_project_number="123456789",
)


def key_and_cert(kid: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, kid)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    signer = crypt.RSASigner.from_string(pem_key, key_id=kid)
    return signer, cert.public_bytes(serialization.Encoding.PEM).decode()


GOOGLE_SIGNER, GOOGLE_CERT = key_and_cert("google-1")
CHAT_SIGNER, CHAT_CERT = key_and_cert("chat-1")
OTHER_SIGNER, _ = key_and_cert("google-1")  # same kid, different key: a forgery
CERTS = {GOOGLE_CERTS_URL: {"google-1": GOOGLE_CERT}, CHAT_CERTS_URL: {"chat-1": CHAT_CERT}}


def token(signer, **claims):
    now = int(time.time())
    payload = {"iat": now, "exp": now + 300, **claims}
    return "Bearer " + jwt.encode(signer, payload).decode()


def scheduler_token(signer=GOOGLE_SIGNER, **overrides):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": SETTINGS.service_url,
        "email": SETTINGS.scheduler_sa_email,
        "email_verified": True,
    }
    claims.update(overrides)
    return token(signer, **claims)


def chat_token(signer=CHAT_SIGNER, **overrides):
    claims = {"iss": CHAT_ISSUER, "aud": SETTINGS.chat_project_number}
    claims.update(overrides)
    return token(signer, **claims)


@pytest.fixture
def verifier():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return CERTS[url]

    v = TokenVerifier(SETTINGS, fetch=fetch)
    v.fetched = fetched
    return v


def test_valid_scheduler_token(verifier):
    verifier.verify_scheduler(scheduler_token())


def test_cloud_tasks_token_is_the_runtime_account_and_not_interchangeable(verifier):
    task_token = scheduler_token(email=SETTINGS.runtime_sa_email)
    verifier.verify_task(task_token)
    with pytest.raises(Unauthorized):
        verifier.verify_task(scheduler_token())  # the scheduler cannot call /tasks/*
    with pytest.raises(Unauthorized):
        verifier.verify_scheduler(task_token)  # and tasks cannot call /jobs/*


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "Basic abc",
        "Bearer ",
        "Bearer not-a-jwt",
        scheduler_token(aud="https://other.run.app"),
        scheduler_token(email="someone@proj.iam.gserviceaccount.com"),
        scheduler_token(email_verified=False),
        scheduler_token(iss="https://evil.example.com"),
        scheduler_token(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200),
        scheduler_token(signer=OTHER_SIGNER),
        scheduler_token(signer=CHAT_SIGNER),
        chat_token(),
    ],
)
def test_bad_scheduler_tokens(verifier, bad):
    with pytest.raises(Unauthorized):
        verifier.verify_scheduler(bad)


def test_valid_chat_token(verifier):
    verifier.verify_chat(chat_token())


@pytest.mark.parametrize(
    "bad",
    [
        None,
        chat_token(aud="999"),
        chat_token(iss="https://accounts.google.com"),
        chat_token(signer=GOOGLE_SIGNER),
        scheduler_token(),
    ],
)
def test_bad_chat_tokens(verifier, bad):
    with pytest.raises(Unauthorized):
        verifier.verify_chat(bad)


def test_unconfigured_verifier_rejects_everything():
    v = TokenVerifier(Settings(), fetch=lambda url: CERTS[url])
    with pytest.raises(Unauthorized):
        v.verify_scheduler(scheduler_token())
    with pytest.raises(Unauthorized):
        v.verify_chat(chat_token())


def test_certs_are_cached(verifier):
    verifier.verify_scheduler(scheduler_token())
    verifier.verify_scheduler(scheduler_token())
    assert verifier.fetched == [GOOGLE_CERTS_URL]


# --- routes ------------------------------------------------------------------------------------


@pytest.fixture
def client(verifier):
    app.dependency_overrides[get_verifier] = lambda: verifier
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "path",
    [
        "/jobs/gmail", "/jobs/changes", "/jobs/recurring", "/jobs/digest",
        "/jobs/gmail-push", "/jobs/gmail-watch",
    ],
)  # fmt: skip
def test_jobs_need_scheduler_token(client, path):
    assert client.post(path).status_code == 401
    assert client.post(path, headers={"Authorization": chat_token()}).status_code == 401
    assert client.post(path).json() == {"detail": "unauthorized"}


def test_chat_needs_chat_token(client):
    assert client.post("/chat", json={"type": "MESSAGE"}).status_code == 401
    response = client.post("/chat", json={}, headers={"Authorization": scheduler_token()})
    assert response.status_code == 401


def test_health_is_open(client):
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200
