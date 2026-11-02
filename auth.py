"""Identity via Streamlit's built-in OIDC login (Google). No shared password, no service-account key.

Configuration lives only in host secrets ([auth] block + ALLOWED_EMAILS); see README.
Access decision itself is core.is_allowed (pure, tested): deny by default.
Sheets access uses the user's own access token (st.user.tokens["access"]) with only the drive.file scope:
the app can reach just the file(s) the user picked for it once in Google Picker. Streamlit keeps that token
in its signed, HttpOnly login cookie and never refreshes it; Google access tokens expire after about an hour,
after which the user logs in again.
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
FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
# The ONLY scopes the app may request: login identity + per-file access. Anything else is refused.
ALLOWED_SCOPES = {"openid", "email", "profile", FILE_SCOPE}


def requested_scopes():
    try:
        auth = st.secrets.get("auth") or {}
        return str((auth.get("client_kwargs") or {}).get("scope") or "").split()
    except Exception:
        return []


def login_configured():
    """[auth] present with a real cookie secret, the access token exposed and EXACTLY the scopes
    openid, email, profile and drive.file (any other scope is treated as a configuration error)."""
    try:
        auth = st.secrets.get("auth")
        if not auth:
            return False
        secret = str(auth.get("cookie_secret") or "")
        exposed = auth.get("expose_tokens") or []
        exposed = [exposed] if isinstance(exposed, str) else [str(x) for x in exposed]
        scope = set(requested_scopes())
        return bool(auth.get("redirect_uri") and auth.get("client_id") and len(secret) >= 32
                    and secret != EXAMPLE_COOKIE_SECRET and "access" in exposed and scope == ALLOWED_SCOPES)
    except Exception:
        return False


def picker_config():
    """(api_key, app_id) for Google Picker from the secrets, or (None, None) when incomplete/invalid.

    app_id is the Google Cloud project NUMBER (ASCII digits); Picker needs it to record the drive.file grant.
    It is taken from [picker].app_id when given, otherwise from the OAuth client ID, which starts with the
    project number ("<number>-....apps.googleusercontent.com"). The API key is a browser key and is visible to
    logged-in users' browsers by design: restrict it in Google Cloud to the Picker API and to this app's URL.
    """
    try:
        cfg = st.secrets.get("picker") or {}
        api_key, app_id = str(cfg.get("api_key") or ""), str(cfg.get("app_id") or "")
        if not app_id:
            client_id = str((st.secrets.get("auth") or {}).get("client_id") or "")
            app_id = client_id.split("-", 1)[0] if "-" in client_id else ""
        if api_key and app_id.isascii() and app_id.isdigit():
            return api_key, app_id
    except Exception:
        pass
    return None, None


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
    """Clear the Streamlit session. The Google token is deliberately NOT revoked: revoking removes the app's
    authorisation including the drive.file grant for the picked file, so every user would have to pick the
    file again after each logout. The token expires by itself after about an hour and only reaches the
    picked file."""
    st.logout()
