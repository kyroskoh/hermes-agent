"""Tests for the GitHub App auth adapter integration in webhook.py.

Covers the four cases that matter:
  1. No GitHub App creds → helper just returns os.environ.copy()
  2. GitHub App creds present + token mint succeeds → GH_TOKEN injected
  3. Token mint fails (e.g. bad PEM) → falls back to gateway env, logs error
  4. HERMES_PR_REVIEW_USE_PERSONAL=1 → bypasses App mode entirely
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from gateway.platforms.webhook import WebhookAdapter
from gateway.config import PlatformConfig


def _make_adapter(**extra_kw) -> WebhookAdapter:
    extra = {"host": "0.0.0.0", "port": 0, "routes": {}}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip GitHub App vars so the helper falls back to gateway env."""
    for k in (
        "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH", "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_NAME", "HERMES_PR_REVIEW_USE_PERSONAL",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "fake.pem"))
    (tmp_path / "fake.pem").write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("GITHUB_APP_NAME", "hermes-pr-review")
    return tmp_path


def test_no_app_creds_returns_gateway_env(clean_env, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "personal-pat")
    adapter = _make_adapter()
    env = adapter._build_gh_subprocess_env("owner/repo")
    assert env.get("GH_TOKEN") == "personal-pat"


def test_app_creds_mint_token_and_inject(app_env, monkeypatch):
    """When App creds are present and mint succeeds, GH_TOKEN is the App token."""
    adapter = _make_adapter()

    fake_token = "ghs_installation_xxxxxxxxxxxx"
    fake_proc = MagicMock(returncode=0, stdout=fake_token + "\n", stderr="")

    with patch("gateway.platforms.webhook.subprocess.run", return_value=fake_proc) as mock_run:
        env = adapter._build_gh_subprocess_env("owner/repo")

    # Token injected on both keys so gh and raw curl both see it
    assert env["GH_TOKEN"] == fake_token
    assert env["GITHUB_TOKEN"] == fake_token
    # Mint subprocess was called once with the right script path
    assert mock_run.call_count == 1
    cmd = mock_run.call_args.args[0]
    assert cmd[1].endswith("github-app-token.py")
    assert "--repo" in cmd and "owner/repo" in cmd


def test_app_creds_mint_failure_falls_back(app_env, monkeypatch):
    """If token mint fails, fall back to gateway env and log the error."""
    adapter = _make_adapter()
    monkeypatch.setenv("GH_TOKEN", "personal-pat")

    fake_proc = MagicMock(returncode=1, stdout="", stderr="private key is malformed")

    with patch("gateway.platforms.webhook.subprocess.run", return_value=fake_proc):
        env = adapter._build_gh_subprocess_env("owner/repo")

    # Falls back to gateway env, does NOT clobber existing GH_TOKEN
    assert env["GH_TOKEN"] == "personal-pat"


def test_personal_override_bypasses_app_mode(app_env, monkeypatch):
    monkeypatch.setenv("HERMES_PR_REVIEW_USE_PERSONAL", "1")
    monkeypatch.setenv("GH_TOKEN", "personal-pat")
    adapter = _make_adapter()

    with patch("gateway.platforms.webhook.subprocess.run") as mock_run:
        env = adapter._build_gh_subprocess_env("owner/repo")

    # No mint attempt — App mode was bypassed
    assert mock_run.call_count == 0
    assert env["GH_TOKEN"] == "personal-pat"


def test_repo_passes_through_to_token_script(app_env, monkeypatch):
    """The repo arg is forwarded to github-app-token.py --repo for
    per-installation repository scoping (GITHUB_APP_TOKEN_REPOS check)."""
    adapter = _make_adapter()
    fake_proc = MagicMock(returncode=0, stdout="ghs_xxx\n", stderr="")

    with patch("gateway.platforms.webhook.subprocess.run", return_value=fake_proc) as mock_run:
        adapter._build_gh_subprocess_env("myorg/backend-api")

    cmd = mock_run.call_args.args[0]
    assert "myorg/backend-api" in cmd
    assert "--repo" in cmd
