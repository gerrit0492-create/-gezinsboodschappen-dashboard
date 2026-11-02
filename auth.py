"""Identity via Streamlit's built-in OIDC login (Google). No shared password, no service-account key.

Configuration lives only in host secrets ([auth] block + ALLOWED_EMAILS); see README.
Access decision itself is core.is_allowed (pure, tested): deny by default.
Sheets access uses the user's own access token (st.user.tokens["access"]), requested with the
spreadsheets scope at login. Streamlit stores that token at login and never refreshes it; Google
access tokens expire after about an hour, after which the user logs in again.
"""
from __future__ import annotations

import streamlit as st


def current_user():
    """Plain dict with the identity claims we use; never raises (logged-out on any error)."""
    try:
        user = st.user
        if not getattr(user, "is_logged_in", False):
            return {"is_logged_in": False}
        return {"is_logged_in": True, "email": user.get("email"), "email_verified": user.get("email_verified"),
                "name": user.get("name")}
    except Exception:
        return {"is_logged_in": False}


EXAMPLE_COOKIE_SECRET = "a-long-random-string-of-at-least-32-characters"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def login_configured():
    """[auth] present with a real cookie secret, the access token exposed and the Sheets scope requested."""
    try:
        auth = st.secrets.get("auth")
        if not auth:
            return False
        secret = str(auth.get("cookie_secret") or "")
        exposed = auth.get("expose_tokens") or []
        exposed = [exposed] if isinstance(exposed, str) else [str(x) for x in exposed]
        scope = str((auth.get("client_kwargs") or {}).get("scope") or "").split()
        return bool(auth.get("redirect_uri") and auth.get("client_id") and len(secret) >= 32
                    and secret != EXAMPLE_COOKIE_SECRET and "access" in exposed and SHEETS_SCOPE in scope)
    except Exception:
        return False


def access_token():
    """The logged-in user's Google access token, or None. Never display, log or store it."""
    try:
        token = st.user.tokens.get("access")
        return token if isinstance(token, str) and token else None
    except Exception:
        return None


def login():
    st.login()


def logout():
    st.logout()
