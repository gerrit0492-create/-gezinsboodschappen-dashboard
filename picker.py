"""Google's top-level, drive.file-only Picker flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import requests

from auth import FILE_SCOPE

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
STATE_TTL = 600
CALLBACK_FIELDS = {"code", "state", "picked_file_ids", "scope", "error", "error_description", "authuser", "prompt"}
_STATE = re.compile(r"picker\.(\d{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})")


@dataclass(frozen=True)
class Picker:
    client_id: str
    client_secret: str = field(repr=False)
    signing_key: str = field(repr=False)
    redirect_uri: str
    sheet_id: str
    login_token: str = field(repr=False)

    @classmethod
    def from_login(cls, config, sheet_id, login_token):
        """Reuse the existing web client. Streamlit's /oauth2callback remains reserved for login."""
        if not config or not sheet_id or not login_token:
            return None
        client_id = config.get("client_id")
        client_secret = config.get("client_secret")
        signing_key = config.get("cookie_secret")
        if not all(isinstance(value, str) and value for value in (client_id, client_secret, signing_key)):
            return None
        if len(signing_key) < 32:
            return None
        try:
            url = urlsplit(str(config.get("redirect_uri", "")))
        except ValueError:
            return None
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path != "/oauth2callback" or url.query or url.fragment):
            return None
        return cls(client_id, client_secret, signing_key, url._replace(path="/").geturl(), sheet_id, login_token)

    def _digest(self, purpose, ticket):
        # Separate MAC and PKCE purposes. Bind the callback to the current browser's login
        # token and this exact configuration, including when the redirect creates a new session.
        context = json.dumps([purpose, ticket, self.login_token, self.client_id, self.redirect_uri, self.sheet_id])
        return hmac.new(self.signing_key.encode(), context.encode(), hashlib.sha256).digest()

    def _verifier(self, ticket):
        return self._digest("pkce", ticket).hex()

    def authorization_url(self, email):
        ticket = f"picker.{int(time.time())}.{secrets.token_hex(16)}"
        challenge = hashlib.sha256(self._verifier(ticket).encode()).digest()
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": FILE_SCOPE,
            "include_granted_scopes": "false",
            "prompt": "consent",
            "trigger_onepick": "true",
            "mimetypes": SPREADSHEET_MIME,
            "file_ids": self.sheet_id,
            "login_hint": email,
            "state": ticket + "." + self._digest("state", ticket).hex(),
            "code_challenge": base64.urlsafe_b64encode(challenge).decode().rstrip("="),
            "code_challenge_method": "S256",
        })

    def complete(self, callback):
        """Validate the return and redeem its code once. The current login remains authoritative."""
        if any(len(values) != 1 for values in callback.values()):
            return "invalid"
        state = callback.get("state", [""])[0]
        match = _STATE.fullmatch(state)
        if not match or not 0 <= time.time() - int(match[1]) < STATE_TTL:
            return "invalid"
        ticket = state.rsplit(".", 1)[0]
        if not hmac.compare_digest(match[3], self._digest("state", ticket).hex()):
            return "invalid"
        if callback.get("error"):
            return "cancelled" if callback["error"][0] == "access_denied" else "invalid"
        code = callback.get("code", [""])[0]
        if not code:
            return "invalid"
        if callback.get("picked_file_ids") != [self.sheet_id]:
            return "wrong_file"
        try:
            response = requests.post(TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "code_verifier": self._verifier(ticket),
            }, timeout=10, allow_redirects=False)
            if response.status_code == 200:
                grant = response.json()
                if (isinstance(grant, dict) and grant.get("access_token")
                        and grant.get("scope", FILE_SCOPE) == FILE_SCOPE):
                    return "ok"
        except (requests.RequestException, ValueError):
            return "failed"  # Never display errors or OAuth responses containing credentials.
        return "failed"
