"""Tests for ``tools/github_app_workspace.py``.

The module talks to live GitHub + runs ``git`` subprocesses, so we mock
both layers and just verify the orchestration / fallbacks behave as
documented in the module's docstring.

Critical invariants under test:
  1. ``is_configured()`` is True iff all 3 App env vars are present.
  2. ``repo_name_for_board`` slugifies + prefixes deterministically.
  3. ``ensure_remote_repo``:
       * returns None (no-op) when not configured
       * returns the existing repo URL when GitHub HEAD returns 200
       * creates the repo when HEAD returns 404, returns the URL
       * returns None on any unexpected HTTP status
  4. ``init_workspace_repo`` skips when ``is_configured`` is False.
  5. ``commit_and_push`` is a no-op when the App isn't configured,
     never raises on subprocess errors.

These tests stay process-local — no network, no real git, no
filesystem dance beyond ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# Convenience fixture: a fully-configured env so is_configured() == True.
@pytest.fixture
def configured_env(monkeypatch, tmp_path):
    key = tmp_path / "fake.pem"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key))
    # Don't carry over a real prefix from the developer's shell.
    monkeypatch.delenv("HERMES_WORKSPACE_REPO_PREFIX", raising=False)
    monkeypatch.delenv("HERMES_GITHUB_ORG", raising=False)
    return key


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------

def test_is_configured_all_three_present(configured_env):
    from tools.github_app_workspace import is_configured
    assert is_configured() is True


@pytest.mark.parametrize("missing", [
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
])
def test_is_configured_missing_one_returns_false(monkeypatch, configured_env, missing):
    """Removing any single var must disable the integration."""
    monkeypatch.delenv(missing, raising=False)
    from tools.github_app_workspace import is_configured
    assert is_configured() is False


def test_is_configured_default_empty_env(monkeypatch):
    for key in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(key, raising=False)
    from tools.github_app_workspace import is_configured
    assert is_configured() is False


# ---------------------------------------------------------------------------
# repo_name_for_board — slugify + prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("board,expected", [
    ("smoke-tests",       "TEST-smoke-tests"),
    ("Smoke Tests",       "TEST-smoke-tests"),     # spaces → hyphen, lowercased
    ("my.cool board.123", "TEST-my-cool-board-123"),  # dots + spaces
    ("UPPER_CASE",        "TEST-upper-case"),       # underscore → hyphen, lower
    ("---trim---",        "TEST-trim"),             # strip leading/trailing
    ("",                  "TEST-default"),          # empty falls back
    (None,                "TEST-default"),          # None too
])
def test_repo_name_default_prefix(monkeypatch, board, expected):
    monkeypatch.delenv("HERMES_WORKSPACE_REPO_PREFIX", raising=False)
    from tools.github_app_workspace import repo_name_for_board
    assert repo_name_for_board(board) == expected


def test_repo_name_with_custom_prefix(monkeypatch):
    monkeypatch.setenv("HERMES_WORKSPACE_REPO_PREFIX", "prod-")
    from tools.github_app_workspace import repo_name_for_board
    assert repo_name_for_board("smoke-tests") == "prod-smoke-tests"


def test_repo_name_with_empty_prefix(monkeypatch):
    """Operator may explicitly want NO prefix in production."""
    monkeypatch.setenv("HERMES_WORKSPACE_REPO_PREFIX", "")
    from tools.github_app_workspace import repo_name_for_board
    assert repo_name_for_board("smoke-tests") == "smoke-tests"


# ---------------------------------------------------------------------------
# ensure_remote_repo
# ---------------------------------------------------------------------------

def _httpx_response(status_code: int, body: dict | str = ""):
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=body if isinstance(body, dict) else {})
    r.text = body if isinstance(body, str) else ""
    return r


def test_ensure_remote_repo_not_configured(monkeypatch):
    """No env → returns None without touching the network."""
    for key in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(key, raising=False)
    from tools.github_app_workspace import ensure_remote_repo
    # If this hit the network it would either hang or error — the bool
    # check before any I/O is what we're verifying.
    assert ensure_remote_repo("smoke-tests") is None


def test_ensure_remote_repo_existing(monkeypatch, configured_env):
    """Repo exists (200) → return clone URL with embedded token."""
    monkeypatch.setenv("HERMES_GITHUB_ORG", "hermes-bot-lab")  # skip JWT lookup
    fake_token = "ghs_FAKETOKEN12345"

    with patch("tools.github_app_workspace._get_app_token", return_value=fake_token), \
         patch("tools.github_app_workspace.httpx.get",
               return_value=_httpx_response(200, {"name": "TEST-smoke-tests"})):
        from tools.github_app_workspace import ensure_remote_repo
        result = ensure_remote_repo("smoke-tests")
    assert result is not None
    url, token = result
    assert token == fake_token
    assert "x-access-token:" + fake_token in url
    assert "hermes-bot-lab/TEST-smoke-tests.git" in url


def test_ensure_remote_repo_creates_on_404(monkeypatch, configured_env):
    """Repo doesn't exist (404) → POST creates it, returns clone URL."""
    monkeypatch.setenv("HERMES_GITHUB_ORG", "hermes-bot-lab")
    fake_token = "ghs_NEWREPO12345"

    create_resp = _httpx_response(201, {"name": "TEST-new-board"})
    head_resp = _httpx_response(404)
    with patch("tools.github_app_workspace._get_app_token", return_value=fake_token), \
         patch("tools.github_app_workspace.httpx.get", return_value=head_resp) as mock_get, \
         patch("tools.github_app_workspace.httpx.post", return_value=create_resp) as mock_post:
        from tools.github_app_workspace import ensure_remote_repo
        result = ensure_remote_repo("new-board")
    assert result is not None
    url, _ = result
    assert "TEST-new-board.git" in url
    mock_get.assert_called_once()
    mock_post.assert_called_once()
    # POST sent the right shape (private=True, auto_init=False)
    sent = mock_post.call_args.kwargs["json"]
    assert sent["name"] == "TEST-new-board"
    assert sent["private"] is True
    assert sent["auto_init"] is False


def test_ensure_remote_repo_create_fails(monkeypatch, configured_env):
    """Creation returns non-201/202 → log + return None (caller falls back)."""
    monkeypatch.setenv("HERMES_GITHUB_ORG", "hermes-bot-lab")
    with patch("tools.github_app_workspace._get_app_token", return_value="ghs_x"), \
         patch("tools.github_app_workspace.httpx.get", return_value=_httpx_response(404)), \
         patch("tools.github_app_workspace.httpx.post",
               return_value=_httpx_response(422, "Validation Failed")):
        from tools.github_app_workspace import ensure_remote_repo
        assert ensure_remote_repo("oops") is None


def test_ensure_remote_repo_unexpected_status(monkeypatch, configured_env):
    """HEAD returns 500-ish → log + None (don't recklessly try to create)."""
    monkeypatch.setenv("HERMES_GITHUB_ORG", "hermes-bot-lab")
    with patch("tools.github_app_workspace._get_app_token", return_value="ghs_x"), \
         patch("tools.github_app_workspace.httpx.get",
               return_value=_httpx_response(502, "Bad Gateway")):
        from tools.github_app_workspace import ensure_remote_repo
        assert ensure_remote_repo("transient") is None


def test_ensure_remote_repo_no_token(monkeypatch, configured_env):
    """Token lookup fails → return None (don't loop on impossible work)."""
    monkeypatch.setenv("HERMES_GITHUB_ORG", "hermes-bot-lab")
    with patch("tools.github_app_workspace._get_app_token", return_value=None):
        from tools.github_app_workspace import ensure_remote_repo
        assert ensure_remote_repo("board") is None


# ---------------------------------------------------------------------------
# init_workspace_repo
# ---------------------------------------------------------------------------

def test_init_workspace_repo_skips_when_not_configured(monkeypatch, tmp_path):
    for key in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(key, raising=False)
    from tools.github_app_workspace import init_workspace_repo
    # Path doesn't even need to exist — we never reach mkdir.
    assert init_workspace_repo(tmp_path / "doesntmatter", "b") is False


def test_init_workspace_repo_remote_setup_failure(monkeypatch, configured_env, tmp_path):
    """ensure_remote_repo returns None → init returns False, no git invoked."""
    ws = tmp_path / "ws"
    with patch("tools.github_app_workspace.ensure_remote_repo", return_value=None), \
         patch("tools.github_app_workspace._run_git") as mock_git:
        from tools.github_app_workspace import init_workspace_repo
        assert init_workspace_repo(ws, "board") is False
    mock_git.assert_not_called()


def test_init_workspace_repo_happy_path(monkeypatch, configured_env, tmp_path):
    """All git calls succeed → init returns True, writes .gitignore, pushes."""
    ws = tmp_path / "ws"
    fake_remote = ("https://x-access-token:ghs_x@github.com/org/TEST-b.git", "ghs_x")

    def _git_stub(args, *, cwd, check=True):
        # ``git rev-parse --verify HEAD`` should report no commits yet so
        # the initial commit + push path runs.
        if args == ["rev-parse", "--verify", "HEAD"]:
            return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="")
        if args == ["remote"]:
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    with patch("tools.github_app_workspace.ensure_remote_repo", return_value=fake_remote), \
         patch("tools.github_app_workspace._run_git", side_effect=_git_stub) as mock_git:
        from tools.github_app_workspace import init_workspace_repo
        assert init_workspace_repo(ws, "board") is True

    # .gitignore should have been written by init
    assert (ws / ".gitignore").exists()
    # Sequence sanity: at minimum init, remote, commit, push were called
    called_args = [c.args[0] for c in mock_git.call_args_list]
    assert ["init", "-b", "main"] in called_args
    assert any(a[0] == "remote" for a in called_args)
    assert any(a[0] == "push" for a in called_args)


def test_init_workspace_repo_git_error(configured_env, tmp_path):
    """git subprocess raises → init returns False (never propagates)."""
    ws = tmp_path / "ws"
    fake_remote = ("https://x-access-token:ghs_x@github.com/org/TEST-b.git", "ghs_x")
    with patch("tools.github_app_workspace.ensure_remote_repo", return_value=fake_remote), \
         patch("tools.github_app_workspace._run_git",
               side_effect=subprocess.CalledProcessError(1, ["git"], stderr="boom")):
        from tools.github_app_workspace import init_workspace_repo
        assert init_workspace_repo(ws, "board") is False


# ---------------------------------------------------------------------------
# commit_and_push
# ---------------------------------------------------------------------------

def test_commit_and_push_skips_when_not_configured(monkeypatch, tmp_path):
    for key in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(key, raising=False)
    from tools.github_app_workspace import commit_and_push
    assert commit_and_push(tmp_path / "ws", "msg") is False


def test_commit_and_push_not_a_repo(configured_env, tmp_path):
    """Path is not under a git repo → bail returning False."""
    ws = tmp_path / "no-git"
    ws.mkdir()
    from tools.github_app_workspace import commit_and_push
    assert commit_and_push(ws, "msg") is False


def test_commit_and_push_happy_path(configured_env, tmp_path):
    """In a git repo + push succeeds → True. Verify add+commit+push called."""
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".git").mkdir()  # marker so _is_git_repo returns True
    fake_remote = ("https://x-access-token:ghs_x@github.com/org/TEST-b.git", "ghs_x")

    def _git_stub(args, *, cwd, check=True):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, stdout="M  hello.txt\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("tools.github_app_workspace.ensure_remote_repo", return_value=fake_remote), \
         patch("tools.github_app_workspace._run_git", side_effect=_git_stub) as mock_git:
        from tools.github_app_workspace import commit_and_push
        assert commit_and_push(ws, "task done", board_slug="board") is True
    called = [c.args[0] for c in mock_git.call_args_list]
    assert ["add", "-A"] in called
    assert ["commit", "-m", "task done"] in called
    assert ["push", "origin", "HEAD:main"] in called


def test_commit_and_push_no_changes(configured_env, tmp_path):
    """``git status`` empty → skip the commit but still push HEAD."""
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".git").mkdir()
    fake_remote = ("https://x-access-token:ghs_x@github.com/org/TEST-b.git", "ghs_x")

    def _git_stub(args, *, cwd, check=True):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("tools.github_app_workspace.ensure_remote_repo", return_value=fake_remote), \
         patch("tools.github_app_workspace._run_git", side_effect=_git_stub) as mock_git:
        from tools.github_app_workspace import commit_and_push
        assert commit_and_push(ws, "msg", board_slug="board") is True
    called = [c.args[0] for c in mock_git.call_args_list]
    assert ["add", "-A"] in called
    # No commit invoked because status is empty
    assert not any(a[0] == "commit" for a in called)
    assert ["push", "origin", "HEAD:main"] in called


def test_commit_and_push_subprocess_error(configured_env, tmp_path):
    """git push fails → False, no exception leaks out."""
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".git").mkdir()
    fake_remote = ("https://x-access-token:ghs_x@github.com/org/TEST-b.git", "ghs_x")

    def _git_stub(args, *, cwd, check=True):
        if args[0] == "push":
            raise subprocess.CalledProcessError(1, args, stderr="remote rejected")
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, stdout="M  f\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("tools.github_app_workspace.ensure_remote_repo", return_value=fake_remote), \
         patch("tools.github_app_workspace._run_git", side_effect=_git_stub):
        from tools.github_app_workspace import commit_and_push
        assert commit_and_push(ws, "msg", board_slug="board") is False


# ---------------------------------------------------------------------------
# _build_app_jwt — exercised indirectly above; covers the early-return paths
# ---------------------------------------------------------------------------

def test_build_app_jwt_missing_key_file(monkeypatch, tmp_path):
    """Key path env present but file missing → returns None (no crash)."""
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    from tools.github_app_workspace import _build_app_jwt
    assert _build_app_jwt() is None


def test_build_app_jwt_no_env(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    from tools.github_app_workspace import _build_app_jwt
    assert _build_app_jwt() is None
