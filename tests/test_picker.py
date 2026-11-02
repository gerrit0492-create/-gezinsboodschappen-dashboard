"""drive.file + one-time Google Picker: pick interpretation, gating, token exposure and the pick -> access flow."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import auth  # noqa: E402
import picker  # noqa: E402
import sheet_store  # noqa: E402
from picker import SPREADSHEET_MIME, check_pick  # noqa: E402
from test_app import fresh_caches, make_book, start  # noqa: E402,F401  (autouse fixture re-used)
from test_keyless import Secrets, failing_book, shows_no_data  # noqa: E402

PICKER = {"picker": {"api_key": "browser-key-example", "app_id": "123456789012"}}
TOKEN = "ya29.EXAMPLE-TOKEN-VALUE"


def components(at):
    found = []

    def walk(node):
        children = getattr(node, "children", None)
        for ch in (children.values() if isinstance(children, dict) else []):
            if getattr(ch, "type", "") == "bidi_component":
                found.append(ch)
            walk(ch)
    walk(at._tree)
    return found


# --- interpreting the browser's answer -------------------------------------------------------------------

@pytest.mark.parametrize("picked,status", [
    ({"id": "sheet-1", "mime": SPREADSHEET_MIME}, "ok"),
    ({"id": "other-file", "mime": SPREADSHEET_MIME}, "wrong_file"),
    ({"id": "sheet-1", "mime": "application/pdf"}, "wrong_file"),         # same ID claimed, wrong type
    ({"id": "sheet-1"}, "wrong_file"),                                      # manipulated/incomplete answer
    ({"id": " sheet-1", "mime": SPREADSHEET_MIME}, "wrong_file"),          # exact match only
    ({"cancelled": True}, "cancelled"),
    ({"error": "load"}, "error"),
    (None, None), ({}, None), ("sheet-1", None), (["sheet-1"], None),
])
def test_pick_answer_is_only_compared_with_the_configured_sheet(picked, status):
    assert check_pick(picked, "sheet-1")[0] == status


def test_picker_config_requires_key_and_numeric_project_number(monkeypatch):
    import streamlit as st
    for cfg, ok in (({"api_key": "k", "app_id": "123"}, True), ({"api_key": "k", "app_id": "my-project"}, False),
                    ({"api_key": "", "app_id": "123"}, False), ({"app_id": "123"}, False), ({}, False)):
        monkeypatch.setattr(st, "secrets", Secrets(picker=cfg))
        assert (auth.picker_config() != (None, None)) is ok


def test_component_javascript_never_persists_or_logs_the_token():
    js = picker._JS
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB", "console.", "history.",
                      "location.hash", "location.search", "fetch(", "XMLHttpRequest", "postMessage"):
        assert forbidden not in js, forbidden
    assert ".setOAuthToken(token)" in js and ".setAppId(data.appId)" in js and ".setDeveloperKey(data.apiKey)" in js
    assert "return () => { token = null;" in js                             # dropped from memory on unmount
    assert js.count("button.disabled = false") >= 3                         # retry possible after pick/cancel/error
    assert '"https://apis.google.com/js/api.js"' in js                    # only Google's official loader


# --- gating: the token only reaches the browser after every check passed -----------------------------------

@pytest.mark.parametrize("user,allow", [
    ({"is_logged_in": False}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "vreemde@example.com", "email_verified": True}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "ouder@example.com", "email_verified": False}, ("ouder@example.com",)),
    ({"is_logged_in": True, "email": "ouder@example.com", "email_verified": True}, ()),
])
def test_picker_and_token_never_sent_before_authorisation(monkeypatch, user, allow):
    at, calls = start(monkeypatch, failing_book(PermissionError("")), user=user, allow=allow, token=TOKEN,
                      secrets=PICKER)
    assert components(at) == [] and calls == [] and shows_no_data(at)
    assert not any(TOKEN in str(getattr(e, "value", "")) for e in at.markdown)


def test_pick_step_sends_token_and_config_only_to_the_picker_component(monkeypatch):
    at, calls = start(monkeypatch, failing_book(PermissionError("")), token=TOKEN, secrets=PICKER)
    comps = components(at)
    assert len(comps) == 1 and shows_no_data(at)
    proto = str(comps[0].proto)
    assert TOKEN in proto and "browser-key-example" in proto and "123456789012" in proto
    assert '\\"fileId\\": \\"fake\\"' in proto or '"fileId": "fake"' in proto   # restricted to the configured Sheet
    texts = [str(getattr(e, "value", "")) for kind in ("markdown", "caption", "error", "warning", "subheader")
             for e in getattr(at, kind)]
    assert not any(TOKEN in t for t in texts)                                # never as visible text


def test_pick_step_without_picker_config_fails_closed(monkeypatch):
    at, _ = start(monkeypatch, failing_book(PermissionError("")), token=TOKEN)
    assert components(at) == [] and any("niet ingericht" in e.value for e in at.error) and shows_no_data(at)


def test_wrong_scope_token_is_not_offered_the_picker(monkeypatch):
    from test_keyless import api_error
    at, _ = start(monkeypatch, failing_book(api_error(403, "Request had insufficient authentication scopes.")),
                  token=TOKEN, secrets=PICKER)
    assert components(at) == [] and any("opnieuw" in b.label for b in at.button) and shows_no_data(at)


def test_expired_token_is_not_offered_the_picker(monkeypatch):
    from test_keyless import refresh_error
    at, _ = start(monkeypatch, failing_book(refresh_error()), token=TOKEN, secrets=PICKER)
    assert components(at) == [] and any("verlopen" in e.value for e in at.error)


# --- pick -> access flow -----------------------------------------------------------------------------------

def granted_after_pick(monkeypatch):
    """Simulates Google: no access until the configured Sheet is picked; records which ID the server opens."""
    state = {"granted": False, "opened": []}
    book = make_book()

    def connect(token, sheet_id):
        state["opened"].append(sheet_id)
        if not state["granted"]:
            raise PermissionError("")
        return book
    return state, connect


def test_picking_the_configured_sheet_grants_access_and_shows_dashboard(monkeypatch):
    state, connect = granted_after_pick(monkeypatch)
    at, _ = start(monkeypatch, make_book(), token=TOKEN, secrets=PICKER)   # warm start replaced below
    import streamlit as st
    st.cache_data.clear()
    st.cache_resource.clear()
    monkeypatch.setattr(sheet_store, "connect", connect)
    answers = iter([{"id": "fake", "mime": SPREADSHEET_MIME}])

    def picker_step(token, api_key, app_id, sheet_id, key="voorraad_picker"):
        assert token == TOKEN and sheet_id == "fake" and app_id == "123456789012"
        picked = next(answers, None)
        if picked:
            state["granted"] = True                                         # Google records the drive.file grant
        return picked
    monkeypatch.setattr(picker, "picker_step", picker_step)
    at.run()
    assert len(at.tabs) == 7 and not components(at)
    assert set(state["opened"]) == {"fake"}                                 # only SHEET_ID is ever opened


def test_picking_another_file_gives_no_access_and_is_never_opened(monkeypatch):
    state, connect = granted_after_pick(monkeypatch)
    monkeypatch.setattr(sheet_store, "connect", connect)

    def picker_step(token, api_key, app_id, sheet_id, key="voorraad_picker"):
        return {"id": "someone-elses-file", "mime": SPREADSHEET_MIME}
    monkeypatch.setattr(picker, "picker_step", picker_step)
    at, _ = start(monkeypatch, make_book(), token=TOKEN, secrets=PICKER)
    monkeypatch.setattr(sheet_store, "connect", connect)
    import streamlit as st
    st.cache_data.clear()
    st.cache_resource.clear()
    at.run()
    assert any("niet het ingestelde voorraadbestand" in w.value for w in at.warning) and shows_no_data(at)
    assert "someone-elses-file" not in state["opened"]


def test_pick_without_resulting_access_explains_instead_of_looping(monkeypatch):
    calls = {"n": 0}

    def never_granted(token, sheet_id):
        calls["n"] += 1
        raise PermissionError("")

    def picker_step(token, api_key, app_id, sheet_id, key="voorraad_picker"):
        return {"id": "fake", "mime": SPREADSHEET_MIME} if calls["n"] <= 1 else None
    monkeypatch.setattr(picker, "picker_step", picker_step)
    at, _ = start(monkeypatch, make_book(), token=TOKEN, secrets=PICKER)
    monkeypatch.setattr(sheet_store, "connect", never_granted)
    import streamlit as st
    st.cache_data.clear()
    st.cache_resource.clear()
    at.run()
    assert any("nog geen toegang" in w.value for w in at.warning) and shows_no_data(at)
    assert calls["n"] <= 3                                                   # one rerun after the pick, no loop


# --- review M1 / m1 regressions ---------------------------------------------------------------------------

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


def test_project_number_must_be_ascii_digits(monkeypatch):
    import streamlit as st
    monkeypatch.setattr(st, "secrets", Secrets(picker={"api_key": "k", "app_id": "１２３"}))   # full-width digits
    assert auth.picker_config() == (None, None)


def test_wrong_pick_message_is_honest_about_the_google_grant():
    status, message = check_pick({"id": "other", "mime": SPREADSHEET_MIME}, "sheet-1")
    assert status == "wrong_file" and "gebruikt dat bestand niet" in message and "permissions" in message


def test_project_number_is_derived_from_the_oauth_client_id(monkeypatch):
    import streamlit as st
    cases = [
        ({"picker": {"api_key": "k"}, "auth": {"client_id": "123456789012-abc123.apps.googleusercontent.com"}},
         ("k", "123456789012")),
        ({"picker": {"api_key": "k", "app_id": "999"}, "auth": {"client_id": "123-abc.apps.googleusercontent.com"}},
         ("k", "999")),                                                     # explicit app_id wins
        ({"picker": {"api_key": "k"}, "auth": {"client_id": "not-a-number.apps.googleusercontent.com"}},
         (None, None)),
        ({"picker": {"api_key": "k"}, "auth": {"client_id": "nodash"}}, (None, None)),
        ({"picker": {}, "auth": {"client_id": "123-abc.apps.googleusercontent.com"}}, (None, None)),  # no key
    ]
    for secrets, expected in cases:
        monkeypatch.setattr(st, "secrets", Secrets(**secrets))
        assert auth.picker_config() == expected
