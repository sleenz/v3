"""Every variable the code reads is documented in .env.example and placed in production: secrets
in deploy/secrets.txt, the rest in deploy/env.prod.yaml.example. The deploy files agree."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.settings import REQUIRED, WHATSAPP, Settings

ROOT = Path(__file__).resolve().parents[1]
LAPTOP_ONLY = {"OAUTH_CLIENT_FILE"}  # a path on a laptop; production uses OAUTH_CLIENT_SECRET
# Set by Cloud Shell itself: live_check.py reads the project, mint_gmail_token.py detects the shell.
PLATFORM = {"GOOGLE_CLOUD_PROJECT", "CLOUD_SHELL"}
_ENV_READ = re.compile(
    r"""(?:os\.environ\.get|os\.getenv|os\.environ\.setdefault)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""
)


def env_example_keys() -> list[str]:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    return [line.split("=", 1)[0].strip() for line in lines if re.match(r"^[A-Z][A-Z0-9_]*=", line)]


def secrets_txt() -> dict[str, str]:
    pairs = {}
    for line in (ROOT / "deploy" / "secrets.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            var, name = line.split()[:2]
            pairs[var] = name
    return pairs


def prod_example_keys() -> list[str]:
    text = (ROOT / "deploy" / "env.prod.yaml.example").read_text(encoding="utf-8")
    return re.findall(r"^([A-Z][A-Z0-9_]*):", text, flags=re.MULTILINE)


def settings_variables() -> dict[str, bool]:
    """{ENV_NAME: is_secret} for every field of the settings class."""
    return {
        name.upper(): field.annotation in (SecretStr, "SecretStr")
        for name, field in Settings.model_fields.items()
    }


def variables_read_in_code() -> set[str]:
    names = set(settings_variables())
    for folder in ("app", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            for match in _ENV_READ.finditer(path.read_text(encoding="utf-8")):
                names.add(match.group(1) or match.group(2))
    return names


def test_env_example_loads_into_the_settings_class():
    settings = Settings(_env_file=ROOT / ".env.example")
    assert settings.sheet_id == "" and settings.gmail_refresh_token.get_secret_value() == ""
    assert len(env_example_keys()) == len(set(env_example_keys()))  # no duplicates


def test_every_variable_the_code_reads_is_in_env_example():
    missing = variables_read_in_code() - set(env_example_keys()) - PLATFORM
    assert not missing, f"add to .env.example: {sorted(missing)}"


def test_every_variable_has_a_production_home():
    variables = settings_variables()
    secrets = secrets_txt()
    prod = set(prod_example_keys())
    for name, is_secret in variables.items():
        if name in LAPTOP_ONLY:
            continue
        if is_secret:
            assert name in secrets, f"secret {name} missing from deploy/secrets.txt"
        else:
            assert name in prod, f"{name} missing from deploy/env.prod.yaml.example"
    assert set(secrets) <= {n for n, s in variables.items() if s}, "secrets.txt names unknown vars"
    assert not set(secrets) & prod, "a secret must never be in env.prod.yaml"
    assert set(REQUIRED) | set(WHATSAPP) <= set(variables)


def test_prod_example_has_no_values_that_look_secret():
    text = (ROOT / "deploy" / "env.prod.yaml.example").read_text(encoding="utf-8")
    for key in ("SHEET_ID", "SERVICE_URL", "WA_VERIFY_TOKEN", "WA_PHONE_NUMBER_ID"):
        assert re.search(rf'^{key}: ""', text, flags=re.MULTILINE), f"{key} must ship empty"


def config_sh() -> dict[str, str]:
    text = (ROOT / "deploy" / "config.sh").read_text(encoding="utf-8")
    return dict(re.findall(r'^([A-Z_]+)="([^"$]*)"', text, flags=re.MULTILINE))


def test_cloudbuild_uses_deploy_sh_and_the_same_names():
    cloudbuild = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    names = config_sh()
    assert f"_REGION: {names['REGION']}" in cloudbuild
    assert f"_SERVICE: {names['SERVICE']}" in cloudbuild
    assert f"_REPO: {names['REPO']}" in cloudbuild
    assert "deploy/deploy.sh" in cloudbuild and "--set-secrets" not in cloudbuild
    assert names["REGION"] == "asia-southeast2" and names["SERVICE"] == "v2-git"


def test_secrets_and_local_settings_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", "deploy/env.prod.yaml", "secrets/"):
        assert entry in ignored


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_deploy_scripts_parse():
    for script in sorted((ROOT / "deploy").glob("*.sh")):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_secret_values_are_never_command_line_arguments():
    """setup_secrets.sh reads values from a file or `read -s`, never from "$1"/"$2"."""
    script = (ROOT / "deploy" / "setup_secrets.sh").read_text(encoding="utf-8")
    assert "read -r -s" in script and "--data-file=-" in script
    assert 'echo "$value"' not in script and "set -x" not in script


def _run_make_env(tmp_path: Path, answer: str) -> subprocess.CompletedProcess[str]:
    """deploy/make_env.sh in a copy of deploy/, with a fake gcloud (no network, no real project)."""
    deploy = tmp_path / "deploy"
    shutil.copytree(ROOT / "deploy", deploy, ignore=shutil.ignore_patterns("env.prod.yaml"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text(
        '#!/usr/bin/env bash\ncase "$*" in\n'
        '  "config get-value project") echo proj-x ;;\n'
        '  "projects describe"*) echo 123456789 ;;\nesac\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(
        ["bash", str(deploy / "make_env.sh")],
        input=answer, capture_output=True, text=True, env=env, check=False,
    )  # fmt: skip


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_make_env_fills_everything_but_the_sheet_from_the_project(tmp_path):
    link = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz_-0123/edit#gid=0\n"
    result = _run_make_env(tmp_path, link)
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "deploy" / "env.prod.yaml").read_text(encoding="utf-8")
    assert re.findall(r"^([A-Z][A-Z0-9_]*):", text, flags=re.MULTILINE) == prod_example_keys()
    for line in (
        'GCP_PROJECT: "proj-x"',
        'SHEET_ID: "1AbCdEfGhIjKlMnOpQrStUvWxYz_-0123"',
        'SERVICE_URL: "https://v2-git-123456789.asia-southeast2.run.app"',
        'SCHEDULER_SA_EMAIL: "grc-tracker-scheduler@proj-x.iam.gserviceaccount.com"',
        'RUNTIME_SA_EMAIL: "grc-tracker-run@proj-x.iam.gserviceaccount.com"',
        'CHAT_PROJECT_NUMBER: "123456789"',
        'GMAIL_PUSH_TOPIC: "projects/proj-x/topics/grc-gmail-push"',
        'WA_PHONE_NUMBER_ID: ""',
    ):
        assert line in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_make_env_refuses_something_that_is_not_a_sheet_id(tmp_path):
    result = _run_make_env(tmp_path, "<SHEET_ID>\n")
    assert result.returncode == 1
    assert not (tmp_path / "deploy" / "env.prod.yaml").exists()


def test_instant_email_is_wired_end_to_end():
    """Topic (setup_gcp) -> subscription to /jobs/gmail-push and a daily watch (setup_scheduler),
    under the names in config.sh, signed as the scheduler account the app checks."""
    config = (ROOT / "deploy" / "config.sh").read_text(encoding="utf-8")
    gcp = (ROOT / "deploy" / "setup_gcp.sh").read_text(encoding="utf-8")
    scheduler = (ROOT / "deploy" / "setup_scheduler.sh").read_text(encoding="utf-8")
    assert 'GMAIL_TOPIC="grc-gmail-push"' in config
    assert "pubsub.googleapis.com" in gcp and "gmail-api-push@system.gserviceaccount.com" in gcp
    assert "grc-gmail-watch|23 6 * * *|/jobs/gmail-watch" in scheduler
    assert '--push-endpoint "$url/jobs/gmail-push"' in scheduler
    assert '--push-auth-service-account "$SCHEDULER_SA"' in scheduler
