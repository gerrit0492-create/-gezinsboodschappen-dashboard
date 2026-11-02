"""One-time Google Picker step for the drive.file scope.

With drive.file the app may only use files the user explicitly opened with it. The user picks the existing
stock Sheet once in Google Picker; Google then records a per-user, per-file grant for this app (OAuth client /
Cloud project), which persists across logins until the user removes the app's access in their Google account.

Security model:
- The Picker runs in the user's own browser with the user's own access token (needed by Picker to show the
  user's files). The token is only held in JavaScript memory for that moment: nothing is written to
  localStorage, sessionStorage, cookies or the URL by this code.
- The picked file ID is NEVER used for access. The server only ever opens SHEET_ID from the secrets; the pick
  result is only compared with it, so a wrong or manipulated pick cannot grant access to anything else.
"""
from __future__ import annotations

import streamlit as st

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"

_JS = r"""
export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const button = parentElement.querySelector("button");
  const status = parentElement.querySelector(".status");
  let token = data && data.token;              // memory only; never persisted

  function loadGapi() {
    return new Promise((resolve, reject) => {
      if (window.gapi && window.gapi.load) { resolve(); return; }
      const s = document.createElement("script");
      s.src = "https://apis.google.com/js/api.js";
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error("load"));
      document.head.appendChild(s);
    });
  }

  function topOrigin() {
    try { return window.top.location.origin; } catch (e) { return window.location.origin; }
  }

  async function openPicker() {
    button.disabled = true;
    status.textContent = "Google Drive openen…";
    try {
      await loadGapi();
      await new Promise((resolve) => window.gapi.load("picker", { callback: resolve }));
      const view = new google.picker.DocsView(google.picker.ViewId.SPREADSHEETS)
        .setMimeTypes(data.mime)
        .setMode(google.picker.DocsViewMode.LIST);
      try { if (data.fileId && view.setFileIds) { view.setFileIds(data.fileId); } } catch (e) { /* older Picker */ }
      const picker = new google.picker.PickerBuilder()
        .addView(view)
        .setOAuthToken(token)
        .setDeveloperKey(data.apiKey)
        .setAppId(data.appId)
        .setOrigin(topOrigin())
        .setTitle("Kies het voorraadbestand")
        .setCallback((res) => {
          const action = res[google.picker.Response.ACTION];
          if (action === google.picker.Action.PICKED) {
            const doc = res[google.picker.Response.DOCUMENTS][0];
            button.disabled = false;                 // a wrong pick can be corrected right away
            status.textContent = "Gekozen; de app controleert de toegang…";
            setTriggerValue("picked", { id: String(doc[google.picker.Document.ID] || ""),
                                         mime: String(doc[google.picker.Document.MIME_TYPE] || "") });
          } else if (action === google.picker.Action.CANCEL) {
            button.disabled = false;
            status.textContent = "Geannuleerd.";
            setTriggerValue("picked", { cancelled: true });
          }
        })
        .build();
      picker.setVisible(true);
      status.textContent = "";
    } catch (e) {
      button.disabled = false;
      status.textContent = "Google Drive kon niet worden geopend. Probeer het opnieuw of gebruik een computer.";
      setTriggerValue("picked", { error: "load" });
    }
  }

  button.onclick = openPicker;
  return () => { token = null; button.onclick = null; };   // cleanup on unmount: token dropped from memory
}
"""

_HTML = """
<div class="picker-step">
  <button type="button">📄 Voorraadbestand kiezen in Google Drive</button>
  <div class="status" aria-live="polite"></div>
</div>
"""

_CSS = """
button { width: 100%; padding: 0.7rem; border-radius: 0.5rem; border: 1px solid var(--st-primary-color);
         background: var(--st-primary-color); color: white; font-size: 1rem; }
button:disabled { opacity: 0.6; }
.status { margin-top: 0.4rem; font-size: 0.9rem; }
"""

_component = None


def _get_component():
    global _component
    if _component is None:
        _component = st.components.v2.component("voorraad_picker", html=_HTML, css=_CSS, js=_JS)
    return _component


def picker_step(token, api_key, app_id, sheet_id, key="voorraad_picker"):
    """Render the Picker button; returns the trigger value dict from the browser, or None."""
    result = _get_component()(
        key=key,
        data={"token": token, "apiKey": api_key, "appId": app_id, "fileId": sheet_id, "mime": SPREADSHEET_MIME},
        on_picked_change=lambda: None,
    )
    return getattr(result, "picked", None)


def check_pick(picked, sheet_id):
    """Interpret the browser's answer. Returns (status, message); the picked ID is only compared, never used.

    status: 'ok' (the configured Sheet was picked), 'wrong_file', 'cancelled', 'error' or None (nothing yet).
    """
    if not isinstance(picked, dict) or not picked:
        return None, ""
    if picked.get("cancelled"):
        return "cancelled", "Je hebt niets gekozen. Kies het voorraadbestand om verder te gaan."
    if picked.get("error"):
        return "error", "Google Drive kon niet worden geopend. Probeer het opnieuw, of kies het bestand eenmalig op " \
                        "een computer; daarna werkt de app ook op je iPhone."
    if str(picked.get("id") or "") == str(sheet_id) and picked.get("mime") == SPREADSHEET_MIME:
        return "ok", "Voorraadbestand gekozen; de app controleert nu de toegang."
    return "wrong_file", "Dat is niet het ingestelde voorraadbestand. Er is niets met dat bestand gedaan; kies het " \
                         "juiste bestand."
