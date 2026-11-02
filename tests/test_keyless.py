"""Keyless Sheets access with the user's own Google access token: errors, expiry, permissions, isolation.

Google's API answers are simulated with a fake HTTP adapter under the REAL gspread/google-auth stack, so the
exceptions tested here are the ones the libraries really raise. No network, no real token.
"""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
import requests  # noqa: E402
from requests.adapters import BaseAdapter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import auth  # noqa: E402
import sheet_store  # noqa: E402
from sheet_store import classify_error, connect  # noqa: E402
from test_app import btn, fresh_caches, make_book, start  # noqa: E402,F401  (fixture re-used)

SHEET_META = {"spreadsheetId": "s", "properties": {"title": "t"},
              "sheets": [{"properties": {"title": "Voorraad", "sheetId": 1, "index": 0,
                                         "gridProperties": {"rowCount": 10, "columnCount": 5}}}]}


class FakeGoogle(BaseAdapter):
    """Answers the first request (open the Sheet) with `first`, later ones with `later`; records auth headers."""

    def __init__(self, first, later=None):
        super().__init__()
        self.first, self.later, self.headers = first, later or first, []

    def send(self, request, **kw):
        self.headers.append(request.headers.get("Authorization"))
        status, body = self.first if len(self.headers) == 1 else self.later
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = status, json.dumps(body).encode(), request.url, request
        r.headers["Content-Type"] = "application/json"
        return r

    def close(self):
        pass


def google_error(status, message, state="X"):
    return status, {"error": {"code": status, "message": message, "status": state}}


def open_with(fake, token="user-token"):
    import gspread
    from google.oauth2.credentials import Credentials
    client = gspread.Client(auth=Credentials(token=token))
    client.http_client.session.mount("https://", fake)
    return client.open_by_key("s")


# --- library behaviour -> classification ------------------------------------------------------------

def test_connect_uses_the_users_bearer_token_and_no_key(monkeypatch):
    import gspread
    fake = FakeGoogle((200, SHEET_META))
    real_client = gspread.Client

    def client_with_fake(auth):
        c = real_client(auth=auth)
        c.http_client.session.mount("https://", fake)
        return c
    monkeypatch.setattr(gspread, "Client", client_with_fake)
    book = connect("user-token-123", "s")
    assert book.id == "s" and fake.headers == ["Bearer user-token-123"]


def test_connect_without_token_fails_closed():
    with pytest.raises(PermissionError):
        connect("", "s")
    with pytest.raises(PermissionError):
        connect(None, "s")


@pytest.mark.parametrize("first,later,action,kind", [
    (google_error(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED"), None, "open", "expired"),
    (google_error(403, "The caller does not have permission", "PERMISSION_DENIED"), None, "open", "forbidden"),
    (google_error(404, "Requested entity was not found.", "NOT_FOUND"), None, "open", "not_found"),
    ((200, SHEET_META), google_error(401, "x", "UNAUTHENTICATED"), "read", "expired"),
    ((200, SHEET_META), google_error(403, "Request had insufficient authentication scopes.", "PERMISSION_DENIED"),
     "read", "scope"),
    ((200, SHEET_META), google_error(403, "The caller does not have permission", "PERMISSION_DENIED"), "write",
     "forbidden"),
    ((200, SHEET_META), google_error(429, "Quota exceeded", "RESOURCE_EXHAUSTED"), "write", "rate_limited"),
    ((200, SHEET_META), google_error(500, "Internal error", "INTERNAL"), "read", "other"),
])
def test_real_library_errors_are_classified(first, later, action, kind):
    fake = FakeGoogle(first, later)
    with pytest.raises(Exception) as info:
        ws = open_with(fake).worksheet("Voorraad")
        if action == "read":
            ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
        elif action == "write":
            ws.batch_update([{"range": "A2", "values": [["x"]]}], value_input_option="RAW")
    assert classify_error(info.value) == kind
    assert all(h == "Bearer user-token" for h in fake.headers)       # never another credential, never a refresh


def test_expired_token_is_never_refreshed_or_retried_with_other_credentials():
    fake = FakeGoogle(google_error(401, "expired", "UNAUTHENTICATED"))
    with pytest.raises(Exception) as info:
        open_with(fake)
    assert type(info.value).__name__ == "RefreshError" and len(fake.headers) == 1


def test_error_text_never_contains_the_token():
    fake = FakeGoogle((200, SHEET_META), google_error(403, "Request had insufficient authentication scopes."))
    with pytest.raises(Exception) as info:
        open_with(fake, token="ya29.SECRET-TOKEN").worksheet("Voorraad").get_all_values()
    assert "SECRET-TOKEN" not in str(info.value) and "SECRET-TOKEN" not in repr(info.value)


# --- configuration -----------------------------------------------------------------------------------

class Secrets(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


BASE = {"redirect_uri": "https://x.streamlit.app/oauth2callback", "client_id": "id", "cookie_secret": "y" * 40,
        "expose_tokens": "access", "client_kwargs": {"scope": "openid email profile " + auth.FILE_SCOPE}}


@pytest.mark.parametrize("change,ok", [
    ({}, True),
    ({"expose_tokens": ["id", "access"]}, True),
    ({"expose_tokens": "id"}, False),                                        # access token not exposed
    ({"expose_tokens": None}, False),
    ({"client_kwargs": {"scope": "openid email profile"}}, False),           # drive.file not requested
    ({"client_kwargs": {"scope": "openid email profile "
                                 "https://www.googleapis.com/auth/spreadsheets.readonly"}}, False),
    # least privilege: any broad Sheets/Drive scope next to drive.file is a configuration error
    ({"client_kwargs": {"scope": "openid email profile https://www.googleapis.com/auth/drive.file "
                                 "https://www.googleapis.com/auth/spreadsheets"}}, False),
    ({"client_kwargs": {"scope": "openid email profile https://www.googleapis.com/auth/drive.file "
                                 "https://www.googleapis.com/auth/drive"}}, False),
    ({"client_kwargs": {"scope": "openid email profile https://www.googleapis.com/auth/drive.file "
                                 "https://www.googleapis.com/auth/drive.readonly"}}, False),
    ({"client_kwargs": None}, False),
])
def test_login_configured_requires_access_token_and_sheets_scope(monkeypatch, change, ok):
    import streamlit as st
    cfg = {k: v for k, v in dict(BASE, **change).items() if v is not None}
    monkeypatch.setattr(st, "secrets", Secrets(auth=cfg))
    assert auth.login_configured() is ok


def test_access_token_reader_fails_closed(monkeypatch):
    import streamlit as st

    class User:
        def __init__(self, tokens):
            self.tokens = tokens
    for tokens, expected in (({"access": "abc"}, "abc"), ({}, None), ({"access": ""}, None), ({"access": 5}, None)):
        monkeypatch.setattr(st, "user", User(tokens))
        assert auth.access_token() == expected

    class Broken:
        @property
        def tokens(self):
            raise RuntimeError("no auth configured")
    monkeypatch.setattr(st, "user", Broken())
    assert auth.access_token() is None


# --- app behaviour (AppTest) -----------------------------------------------------------------------------

def failing_book(exc):
    book = make_book()

    class Failing:
        def get_all_values(self, value_render_option=None):
            raise exc
    for name in list(book.tabs):
        book.tabs[name] = Failing()
    return book


def refresh_error():
    from google.auth.exceptions import RefreshError
    return RefreshError("The credentials do not contain the necessary fields need to refresh the access token.")


def api_error(status, message):
    from gspread.exceptions import APIError
    r = requests.Response()
    r.status_code, r._content = status, json.dumps({"error": {"code": status, "message": message}}).encode()
    return APIError(r)


def shows_no_data(at):
    return not at.tabs and not at.metric


def test_no_access_token_shows_relogin_and_reads_nothing(monkeypatch):
    at, calls = start(monkeypatch, make_book(), token=None)
    assert any("geen toegang tot Google Drive-bestanden" in e.value for e in at.error)
    assert any("opnieuw inloggen" in b.label for b in at.button)
    assert calls == [] and shows_no_data(at)


@pytest.mark.parametrize("exc,text", [
    (refresh_error(), "verlopen"),
    (api_error(403, "Request had insufficient authentication scopes."), "geen toestemming gegeven"),
    (api_error(429, "Quota exceeded"), "te druk"),
    (api_error(500, "Internal error"), "kon niet worden geladen"),
])
def test_read_failures_fail_closed_with_clear_message(monkeypatch, exc, text):
    at, _ = start(monkeypatch, failing_book(exc))
    assert any(text in e.value for e in at.error), [e.value for e in at.error]
    assert shows_no_data(at)


def picks_shown(at):
    return any("kies het voorraadbestand" in h.value for h in at.subheader)


@pytest.mark.parametrize("exc", [PermissionError(""), api_error(403, "The caller does not have permission"),
                                 api_error(404, "Requested entity was not found.")])
def test_no_file_grant_leads_to_one_time_pick_step_and_no_data(monkeypatch, exc):
    at, _ = start(monkeypatch, failing_book(exc))
    assert picks_shown(at) and shows_no_data(at)


def test_permission_error_while_opening_fails_closed(monkeypatch):
    at, _ = start(monkeypatch, make_book())
    import streamlit as st
    st.cache_data.clear()
    st.cache_resource.clear()

    def no_access(token, sid):
        raise PermissionError("")
    monkeypatch.setattr(sheet_store, "connect", no_access)
    at.run()
    assert picks_shown(at) and shows_no_data(at)


def test_token_expiring_during_a_save_is_reported_as_not_saved(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    voorraad = book.tabs["Voorraad"]

    def expired(*a, **k):
        raise refresh_error()
    voorraad.batch_update = expired
    btn(at, "Opgebruikt").click().run()
    assert any(e.value.startswith("Niet opgeslagen") and "verlopen" in e.value for e in at.error)
    assert voorraad.grid[1][3] == 12                                   # unchanged


def test_cached_sheet_data_is_never_served_to_another_user(monkeypatch):
    """User A loads data; user B (allowlisted, but WITHOUT Sheet access) must not get A's cached data."""
    allow = ("a@example.org", "b@example.org")
    user_a = {"is_logged_in": True, "email": "a@example.org", "email_verified": True}
    user_b = {"is_logged_in": True, "email": "b@example.org", "email_verified": True}
    at_a, _ = start(monkeypatch, make_book(), user=user_a, allow=allow, token="tok-a")
    assert len(at_a.tabs) == 7                                         # A sees the dashboard; cache is warm

    def b_has_no_access(token, sid):
        assert token == "tok-b"                                        # B's own token, never A's
        raise PermissionError("")
    monkeypatch.setattr(sheet_store, "connect", b_has_no_access)
    monkeypatch.setattr(auth, "current_user", lambda: dict(user_b))
    monkeypatch.setattr(auth, "access_token", lambda: "tok-b")
    from streamlit.testing.v1 import AppTest
    at_b = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at_b.secrets["ALLOWED_EMAILS"] = list(allow)
    at_b.secrets["SHEET_ID"] = "fake"
    at_b.run()
    assert picks_shown(at_b) and shows_no_data(at_b)


def test_new_token_after_relogin_reads_fresh_not_from_old_token_cache(monkeypatch):
    seen_tokens = []
    book = make_book()

    def connect(token, sid):
        seen_tokens.append(token)
        return book
    at, _ = start(monkeypatch, book, token="tok-old")
    monkeypatch.setattr(sheet_store, "connect", connect)
    monkeypatch.setattr(auth, "access_token", lambda: "tok-new")
    at.run()
    assert seen_tokens == ["tok-new"] and len(at.tabs) == 7


def test_token_never_rendered_anywhere(monkeypatch):
    at, _ = start(monkeypatch, make_book(), token="ya29.SECRET-TOKEN-VALUE")
    rendered = []
    for kind in ("markdown", "caption", "error", "warning", "info", "success", "text", "metric", "button",
                 "checkbox", "text_input", "number_input", "selectbox", "title", "subheader", "expander"):
        for el in getattr(at, kind, []):
            for attr in ("value", "label", "body"):
                v = getattr(el, attr, None)
                if isinstance(v, str):
                    rendered.append(v)
    assert rendered and not any("SECRET-TOKEN" in r for r in rendered)
    assert "SECRET-TOKEN" not in str(at.session_state["seen_data"])   # nothing token-like kept in session data


@pytest.mark.parametrize("user,allow", [
    ({"is_logged_in": False}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "vreemde@example.com", "email_verified": True}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "ouder@example.com", "email_verified": False}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "ouder@example.com", "email_verified": True}, ()),
])
def test_auth_negatives_never_touch_the_token_or_sheet(monkeypatch, user, allow):
    used = []
    at, calls = start(monkeypatch, make_book(), user=user, allow=allow, token=lambda: used.append(1) or "tok")
    assert calls == [] and used == [] and shows_no_data(at)          # token not even read before access


# --- review hardening ----------------------------------------------------------------------------------

def test_missing_scope_is_recognised_even_when_opening_the_sheet():
    fake = FakeGoogle(google_error(403, "Request had insufficient authentication scopes.", "PERMISSION_DENIED"))
    with pytest.raises(PermissionError) as info:
        open_with(fake)
    assert classify_error(info.value) == "scope"


def test_logout_does_not_revoke_so_the_file_grant_survives(monkeypatch):
    """Revoking would withdraw the app's authorisation incl. the drive.file grant (pick again after every logout)."""
    import streamlit as st
    calls = []
    monkeypatch.setattr(auth, "access_token", lambda: calls.append("token-read") or "tok-x")
    monkeypatch.setattr(st, "logout", lambda: calls.append("logout"))
    auth.logout()
    assert calls == ["logout"] and not hasattr(auth, "revoke")


def test_cached_sheet_connections_expire_with_the_token():
    src = (ROOT / "app.py").read_text()
    assert "@st.cache_resource(ttl=3600" in src
