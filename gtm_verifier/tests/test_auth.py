"""Unit tests for the sign-in gate.

No OAuth round trip happens here: these cover the allowlist parsing, the
fail-closed startup checks, and the fact that every real route needs a session.
"""

import importlib

import pytest
from flask import Flask

from webapp import auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AUTH_DISABLED", "ALLOWED_USERS", "OAUTH_CLIENT_ID",
                "OAUTH_CLIENT_SECRET", "FLASK_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)


def _configured(monkeypatch, users="a@example.com"):
    monkeypatch.setenv("OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("FLASK_SECRET_KEY", "sekrit")
    monkeypatch.setenv("ALLOWED_USERS", users)


def _app(monkeypatch):
    """A minimal app wired with the real auth gate."""
    app = Flask(__name__, template_folder="../webapp/templates")
    auth.init_app(app)

    @app.route("/")
    def index():
        return "secret page"

    return app


# ── Allowlist parsing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("a@x.com", {"a@x.com"}),
    ("a@x.com,b@y.com", {"a@x.com", "b@y.com"}),
    ("a@x.com, b@y.com", {"a@x.com", "b@y.com"}),
    ("a@x.com\nb@y.com", {"a@x.com", "b@y.com"}),
    ("  A@X.com  ", {"a@x.com"}),          # case-insensitive, trimmed
    ("", set()),
    (",, ,", set()),                        # stray separators are not members
])
def test_allowed_users_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("ALLOWED_USERS", raw)
    assert auth.allowed_users() == expected


# ── Fail-closed startup ───────────────────────────────────────────────────────

def test_init_raises_when_unconfigured(monkeypatch):
    """No credentials and no explicit opt-out must not yield an open app."""
    with pytest.raises(RuntimeError) as exc:
        auth.init_app(Flask(__name__))
    assert "OAUTH_CLIENT_ID" in str(exc.value)


def test_init_raises_on_empty_allowlist(monkeypatch):
    _configured(monkeypatch, users="")
    with pytest.raises(RuntimeError) as exc:
        auth.init_app(Flask(__name__))
    assert "ALLOWED_USERS" in str(exc.value)


def test_auth_disabled_skips_setup(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    app = Flask(__name__)
    auth.init_app(app)          # must not raise despite no credentials
    assert auth.auth_disabled()


# ── The gate itself ───────────────────────────────────────────────────────────

def test_anonymous_request_redirects_to_login(monkeypatch):
    _configured(monkeypatch)
    client = _app(monkeypatch).test_client()
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_signed_in_user_reaches_the_page(monkeypatch):
    _configured(monkeypatch)
    client = _app(monkeypatch).test_client()
    with client.session_transaction() as sess:
        sess["user"] = "a@example.com"
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"secret page" in resp.data


def test_healthz_is_public(monkeypatch):
    """The health endpoint is keyed on the *endpoint name*, not the path, so
    the route can move (it did — Cloud Run reserves /healthz) without the
    allowlist in auth.py needing to change."""
    _configured(monkeypatch)
    app = _app(monkeypatch)

    @app.route("/_health")
    def healthz():
        return "ok", 200

    resp = app.test_client().get("/_health")
    assert resp.status_code == 200


def test_original_destination_is_remembered(monkeypatch):
    """After signing in the user should land where they were headed."""
    _configured(monkeypatch)
    client = _app(monkeypatch).test_client()
    client.get("/audit/abc/report.pptx")
    with client.session_transaction() as sess:
        assert sess.get("next", "").startswith("/audit/abc/report.pptx")


def test_logout_clears_the_session(monkeypatch):
    _configured(monkeypatch)
    client = _app(monkeypatch).test_client()
    with client.session_transaction() as sess:
        sess["user"] = "a@example.com"
    client.get("/logout")
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_session_cookie_is_hardened(monkeypatch):
    _configured(monkeypatch)
    app = _app(monkeypatch)
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
