"""Identity via Streamlit's built-in OIDC login (Google). No shared password.

Configuration lives only in host secrets ([auth] block + ALLOWED_EMAILS); see README.
Access decision itself is core.is_allowed (pure, tested): deny by default.
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


def login_configured():
    """[auth] present with a real cookie secret (not the example, at least 32 characters)."""
    try:
        auth = st.secrets.get("auth")
        secret = str((auth or {}).get("cookie_secret") or "")
        return bool(auth and auth.get("redirect_uri") and auth.get("client_id") and len(secret) >= 32
                    and secret != EXAMPLE_COOKIE_SECRET)
    except Exception:
        return False


def login():
    st.login()


def logout():
    st.logout()
