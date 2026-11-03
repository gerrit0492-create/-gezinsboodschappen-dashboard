"""Top-level Picker navigation, protected callback and the stock workflow."""

import base64
import hashlib
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import auth  # noqa: E402
import picker  # noqa: E402
import sheet_store  # noqa: E402
from test_app import count_kaas, fresh_caches, make_book, start  # noqa: E402,F401
from test_keyless import Secrets, failing_book, shows_no_data  # noqa: E402

AUTH = {
    "client_id": "123456-example.apps.googleusercontent.com",
    "client_secret": "fixture-client-secret",
    "cookie_secret": "fixture-cookie-secret" * 3,
    "redirect_uri": "https://x.streamlit.app/oauth2callback",
}
TOKEN = "fixture-original-google-token"


def configured_picker():
    return picker.Picker.from_login(AUTH, "fake", TOKEN)


def request_params(at):
    url = at.get("link_button")[0].proto.url
    return parse_qs(urlsplit(url).query)


def test_open_picker_is_top_level_and_has_only_per_file_scope(monkeypatch):
    at, _ = start(monkeypatch, failing_book(PermissionError("")), token=TOKEN, secrets={"auth": AUTH})
    link = at.get("link_button")[0].proto
    url = urlsplit(link.url)
    params = request_params(at)
    assert link.label == "Voorraadbestand kiezen" and shows_no_data(at)
    assert (url.scheme, url.netloc, url.path) == ("https", "accounts.google.com", "/o/oauth2/v2/auth")
    assert params["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    assert params["trigger_onepick"] == ["true"] and params["prompt"] == ["consent"]
    assert params["file_ids"] == ["fake"] and params["redirect_uri"] == ["https://x.streamlit.app/"]
    assert params["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in params and "client_secret" not in params
    assert all(secret not in link.url for secret in (TOKEN, AUTH["client_secret"], AUTH["cookie_secret"]))


@pytest.mark.parametrize("user,allow", [
    ({"is_logged_in": False}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "vreemde@example.com", "email_verified": True}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "ouder@example.com", "email_verified": False}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "ouder@example.com", "email_verified": True}, ()),
])
def test_unauthorised_account_cannot_open_picker_or_use_callback(monkeypatch, user, allow):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Unauthorised OAuth code exchange")

    monkeypatch.setattr(picker.requests, "post", unexpected_request)
    at, calls = start(monkeypatch, failing_book(PermissionError("")), user=user, allow=allow,
                      token=TOKEN, secrets={"auth": AUTH})
    at.query_params.update(callback(configured_picker()))
    at.run()
    assert not at.get("link_button") and calls == [] and shows_no_data(at)


def test_missing_oauth_config_fails_closed(monkeypatch):
    at, _ = start(monkeypatch, failing_book(PermissionError("")), token=TOKEN)
    assert not at.get("link_button") and any("niet ingericht" in e.value for e in at.error)


@pytest.mark.parametrize("failure", ["scope", "expired"])
def test_wrong_scope_or_expired_token_is_not_offered_picker(monkeypatch, failure):
    from test_keyless import api_error, refresh_error
    exc = (api_error(403, "Request had insufficient authentication scopes.")
           if failure == "scope" else refresh_error())
    at, _ = start(monkeypatch, failing_book(exc), token=TOKEN, secrets={"auth": AUTH})
    assert not at.get("link_button") and any("opnieuw" in b.label for b in at.button) and shows_no_data(at)


def callback(flow):
    state = parse_qs(urlsplit(flow.authorization_url("ouder@example.com")).query)["state"]
    return {"state": state, "code": ["fixture-code"], "picked_file_ids": ["fake"]}


@pytest.mark.parametrize("change,expected", [
    ("missing_state", "invalid"), ("tampered", "invalid"), ("expired", "invalid"),
    ("wrong_login", "invalid"), ("duplicate_code", "invalid"), ("different_file", "wrong_file"),
    ("multiple_files", "wrong_file"), ("cancelled", "cancelled"),
])
def test_invalid_callback_is_rejected_before_exchange(monkeypatch, change, expected):
    flow = configured_picker()
    monkeypatch.setattr(picker.time, "time", lambda: 1000)
    params = callback(flow)
    if change == "missing_state":
        del params["state"]
    elif change == "tampered":
        params["state"][0] += "a"
    elif change == "expired":
        monkeypatch.setattr(picker.time, "time", lambda: 1601)
    elif change == "wrong_login":
        flow = replace(flow, login_token="another-login")
    elif change == "duplicate_code":
        params["code"].append("another-code")
    elif change == "different_file":
        params["picked_file_ids"] = ["other"]
    elif change == "multiple_files":
        params["picked_file_ids"] = ["fake,other"]
    elif change == "cancelled":
        params = {"state": params["state"], "error": ["access_denied"]}

    def unexpected_request(*args, **kwargs):
        pytest.fail("Invalid callback triggered an OAuth code exchange")

    monkeypatch.setattr(picker.requests, "post", unexpected_request)
    assert flow.complete(params) == expected


class TokenResponse:
    status_code = 200

    def json(self):
        return {"access_token": "fixture-picker-only-token", "scope": auth.FILE_SCOPE}


@pytest.mark.parametrize("granted", [True, False])
def test_callback_checks_actual_sheet_access_and_can_save_stock(monkeypatch, granted):
    book = make_book()
    at, _ = start(monkeypatch, failing_book(PermissionError("")), token=TOKEN, secrets={"auth": AUTH})
    params = request_params(at)
    opened = []
    exchanges = []

    def connect(token, sheet_id):
        opened.append((token, sheet_id))
        if not exchanges or not granted:
            raise PermissionError("")
        return book

    def exchange(url, *, data, timeout, allow_redirects):
        assert url == "https://oauth2.googleapis.com/token" and not allow_redirects and timeout > 0
        assert data["redirect_uri"] == params["redirect_uri"][0]
        assert data["client_secret"] == AUTH["client_secret"] and data["code"] == "fixture-code"
        challenge = hashlib.sha256(data["code_verifier"].encode()).digest()
        assert base64.urlsafe_b64encode(challenge).decode().rstrip("=") == params["code_challenge"][0]
        exchanges.append(url)
        return TokenResponse()

    monkeypatch.setattr(sheet_store, "connect", connect)
    monkeypatch.setattr(picker.requests, "post", exchange)
    # A return in a new tab has the login cookie but a fresh Streamlit session.
    returned = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    returned.secrets.update(at.secrets)
    at = returned
    at.query_params.update({"state": params["state"], "code": ["fixture-code"], "picked_file_ids": ["fake"]})
    at.run()
    assert not at.exception and not at.query_params and len(exchanges) == 1
    if granted:
        assert len(at.tabs) == 7 and not at.get("link_button")
        count_kaas(at, 0.0)
        assert book.tabs["Voorraad"].grid[2][3] == 0.0
        assert set(book.tabs["Voorraad"].writes) == {"D3", "G3", "H3", "I3"}
        assert book.tabs["Voorraad"].formulas[(3, 10)] == '=IF(A3="";"";"x")'
    else:
        assert shows_no_data(at) and any("nog geen toegang" in w.value for w in at.warning)
        assert not book.tabs["Voorraad"].writes
        at.run()
    assert set(opened) == {(TOKEN, "fake")} and len(exchanges) == 1


@pytest.mark.parametrize("scope,ok", [
    ("openid email profile https://www.googleapis.com/auth/drive.file", True),
    ("https://www.googleapis.com/auth/drive.file profile email openid", True),          # order irrelevant
    ("openid email profile https://www.googleapis.com/auth/drive.file https://mail.google.com/", False),
    ("openid email profile https://www.googleapis.com/auth/drive.file "
     "https://www.googleapis.com/auth/drive.metadata.readonly", False),
    ("openid email profile https://www.googleapis.com/auth/drive.file "
     "https://www.googleapis.com/auth/calendar", False),
    ("email profile https://www.googleapis.com/auth/drive.file", False),                # openid missing
    ("openid email profile https://www.googleapis.com/auth/drive.file x", False),
])
def test_scopes_must_be_exactly_login_plus_drive_file(monkeypatch, scope, ok):
    import streamlit as st
    cfg = {"redirect_uri": "https://x.streamlit.app/oauth2callback", "client_id": "id", "cookie_secret": "y" * 40,
           "expose_tokens": "access", "client_kwargs": {"scope": scope}}
    monkeypatch.setattr(st, "secrets", Secrets(auth=cfg))
    assert auth.login_configured() is ok


def test_login_problems_name_what_is_missing_without_values(monkeypatch):
    import streamlit as st
    good = {"redirect_uri": "https://x.streamlit.app/oauth2callback", "client_id": "123-a.apps.googleusercontent.com",
            "cookie_secret": "s3cr3t-" + "y" * 40, "expose_tokens": "access",
            "client_kwargs": {"scope": "openid email profile https://www.googleapis.com/auth/drive.file"}}
    monkeypatch.setattr(st, "secrets", Secrets(auth=good))
    assert auth.login_problems() == [] and auth.login_configured() is True
    bad = dict(good, cookie_secret="short", expose_tokens="id",
               client_kwargs={"scope": "openid email profile https://www.googleapis.com/auth/spreadsheets"})
    monkeypatch.setattr(st, "secrets", Secrets(auth=bad))
    problems = auth.login_problems()
    assert len(problems) == 3 and auth.login_configured() is False
    assert not any("short" in p or "spreadsheets\"" in p or "123-a" in p for p in problems)   # never echoes values
    monkeypatch.setattr(st, "secrets", Secrets())
    assert auth.login_problems() == ["[auth]-blok ontbreekt"]


def test_not_configured_page_lists_problems_and_reads_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(sheet_store, "connect", lambda *a: calls.append(a))
    monkeypatch.setattr(auth, "current_user", lambda: {"is_logged_in": False})
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.secrets["auth"] = {"redirect_uri": "https://x.streamlit.app/oauth2callback", "client_id": "1-a",
                          "cookie_secret": "z" * 40,
                          "client_kwargs": {"scope": "openid email profile https://www.googleapis.com/auth/drive.file"}}
    at.run()
    assert any("niet ingericht" in e.value for e in at.error)
    assert any("expose_tokens" in c.value for c in at.caption) and calls == [] and not at.tabs
    assert not any("z" * 40 in c.value for c in at.caption)
