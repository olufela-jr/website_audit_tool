"""Google sign-in and an email allowlist for the web front end.

Replaces IAP. Cloud Run's built-in IAP authenticates against a Google-managed
OAuth client, which only admits identities inside the project's organisation —
so approved users on other domains (or on gmail.com) could never get in, no
matter what the IAM allowlist said. Doing the sign-in here uses our own OAuth
client, which has no such restriction.

The model is the same as the IAP one it replaces: authenticate with Google,
then check the verified email against an explicit allowlist. Being on the list
is the authorisation; a valid Google account alone grants nothing.

Configuration (all via environment, secrets injected from Secret Manager):

  OAUTH_CLIENT_ID       Google OAuth 2.0 client ID
  OAUTH_CLIENT_SECRET   ...and its secret
  ALLOWED_USERS         comma/whitespace-separated emails, case-insensitive
  FLASK_SECRET_KEY      signs the session cookie; rotating it logs everyone out
  AUTH_DISABLED=1       skip auth entirely — LOCAL DEVELOPMENT ONLY

Absent AUTH_DISABLED, missing configuration is a startup error rather than a
silently open service: this app can drive a browser at arbitrary URLs, so
failing closed is the only safe direction.
"""

import logging
import os
import re
from functools import wraps

from authlib.integrations.flask_client import OAuth
from flask import redirect, render_template, request, session, url_for

log = logging.getLogger(__name__)

_GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"

# Endpoints reachable without a session: the sign-in dance itself, plus the
# container health check Cloud Run calls before any user exists.
_PUBLIC_ENDPOINTS = {"auth.login", "auth.callback", "auth.logout", "healthz", "static"}


def auth_disabled() -> bool:
    return os.environ.get("AUTH_DISABLED") == "1"


def allowed_users() -> set:
    """Allowlisted emails, lower-cased. Accepts commas, spaces or newlines so
    the value can be pasted from anywhere without reformatting."""
    raw = os.environ.get("ALLOWED_USERS", "")
    return {e.strip().lower() for e in re.split(r"[,\s]+", raw) if e.strip()}


def init_app(app) -> None:
    """Wire sign-in into `app`. Call once, before serving."""
    if auth_disabled():
        log.warning("AUTH_DISABLED=1 — the web app is UNAUTHENTICATED. "
                    "Never set this on a deployed instance.")
        return

    client_id = os.environ.get("OAUTH_CLIENT_ID")
    client_secret = os.environ.get("OAUTH_CLIENT_SECRET")
    secret_key = os.environ.get("FLASK_SECRET_KEY")
    missing = [name for name, value in (
        ("OAUTH_CLIENT_ID", client_id),
        ("OAUTH_CLIENT_SECRET", client_secret),
        ("FLASK_SECRET_KEY", secret_key),
    ) if not value]
    if missing:
        raise RuntimeError(
            f"Authentication is not configured: {', '.join(missing)} unset. "
            "Set them (Secret Manager in Cloud Run), or AUTH_DISABLED=1 for local use."
        )
    if not allowed_users():
        raise RuntimeError(
            "ALLOWED_USERS is empty — nobody could sign in. Set it to the "
            "comma-separated emails permitted to use the tool."
        )

    app.secret_key = secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Cloud Run is always HTTPS. Locally this would block the cookie, but
        # locally you would be running with AUTH_DISABLED=1.
        SESSION_COOKIE_SECURE=True,
    )

    oauth = OAuth(app)
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=_GOOGLE_METADATA,
        client_kwargs={"scope": "openid email"},
    )
    app.extensions["gtm_oauth"] = oauth

    _register_routes(app, oauth)

    @app.before_request
    def _require_login():
        if request.endpoint in _PUBLIC_ENDPOINTS:
            return None
        if session.get("user"):
            return None
        # Remember where they were headed so the callback can return them.
        session["next"] = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.login"))


def _register_routes(app, oauth) -> None:
    from flask import Blueprint

    bp = Blueprint("auth", __name__)

    @bp.route("/login")
    def login():
        # Cloud Run terminates TLS upstream, so url_for would build an http://
        # redirect URI and Google would reject the mismatch. _scheme forces it.
        redirect_uri = url_for("auth.callback", _external=True, _scheme="https")
        return oauth.google.authorize_redirect(redirect_uri)

    @bp.route("/auth/callback")
    def callback():
        try:
            token = oauth.google.authorize_access_token()
        except Exception as exc:  # noqa: BLE001 — user-visible, never a 500
            log.warning("OAuth callback failed: %s", exc)
            return render_template("denied.html", reason="Sign-in failed. Please try again."), 400

        info = token.get("userinfo") or {}
        email = (info.get("email") or "").strip().lower()
        verified = info.get("email_verified", False)

        if not email or not verified:
            return render_template(
                "denied.html",
                reason="Google did not return a verified email address for that account.",
            ), 403

        if email not in allowed_users():
            # Logged so the operator can see who tried and add them if right.
            log.warning("Denied sign-in for %s — not on ALLOWED_USERS", email)
            return render_template("denied.html", email=email), 403

        session["user"] = email
        target = session.pop("next", None) or url_for("index")
        return redirect(target)

    @bp.route("/logout")
    def logout():
        session.clear()
        return render_template("denied.html", reason="You have been signed out.",
                               signed_out=True)

    app.register_blueprint(bp)


def current_user() -> str:
    """Email of the signed-in operator, or "" when there isn't one.

    Returns "" with AUTH_DISABLED too: the blueprint (and so auth.logout) is
    never registered in that mode, so templates must not render a sign-out link.
    """
    if auth_disabled():
        return ""
    return session.get("user", "")


def login_required(view):
    """Belt-and-braces decorator. The before_request hook already covers every
    endpoint; this is for anything registered outside the normal blueprint."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if auth_disabled() or session.get("user"):
            return view(*args, **kwargs)
        return redirect(url_for("auth.login"))
    return wrapper
