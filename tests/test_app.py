"""Headless Streamlit AppTest of app.py against an in-memory fake Google Sheet (no network, no secrets).

Login is simulated by replacing auth.current_user; the real Google OIDC flow can only be tested live.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import auth  # noqa: E402
import sheet_store  # noqa: E402
from core import column_letter  # noqa: E402

APP = str(ROOT / "app.py")
TODAY = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
TOMORROW = TODAY + timedelta(days=1)
ALLOWED = {"is_logged_in": True, "email": "ouder@example.com", "email_verified": True, "name": "Ouder"}


class LiveFakeSheet:
    """Applies batch_update ranges to its grid, like the real Sheet, so reruns read back the writes."""

    def __init__(self, grid, formulas=None):
        self.grid = [list(r) for r in grid]
        self.formulas = formulas or {}          # {(row, col): formula} shown in FORMULA rendering
        self.writes = []

    def get_all_values(self, value_render_option=None):
        out = [list(r) for r in self.grid]
        if value_render_option == "FORMULA":
            for (r, c), f in self.formulas.items():
                if r <= len(out) and c < len(out[r - 1]):
                    out[r - 1][c] = f
        return out

    def batch_update(self, req, value_input_option=None):
        assert value_input_option in ("RAW", "USER_ENTERED")
        for u in req:
            ref = u["range"].split(":")[0]
            letters = "".join(c for c in ref if c.isalpha())
            row = int(ref[len(letters):])
            col = next(i for i in range(200) if column_letter(i) == letters)
            for j, v in enumerate(u["values"][0]):
                if value_input_option == "USER_ENTERED" and isinstance(v, str) and v:
                    assert v[0] in "'=", f"unescaped text under USER_ENTERED: {u}"
                    v = v[1:] if v.startswith("'") else v
                if col + j >= len(self.grid[0]):
                    raise RuntimeError("exceeds grid limits")  # real Sheets refuses writes outside the grid
                while len(self.grid) < row:
                    self.grid.append([""] * len(self.grid[0]))
                r = self.grid[row - 1]
                r += [""] * (col + j + 1 - len(r))
                if value_input_option == "USER_ENTERED" and isinstance(u["values"][0][j], str) \
                        and u["values"][0][j].startswith("="):
                    self.formulas[(row, col + j)] = v
                    v = "(formule)"
                r[col + j] = v
            self.writes.append(u["range"])


class FakeBook:
    def __init__(self, tabs):
        self.tabs = tabs
        self.opened = True

    def worksheet(self, name):
        return self.tabs[name]


def make_book(new_schema=True):
    voorraad = [["Product", "Categorie", "Locatie", "Aantal nu", "Eenheid", "Minimum", "Zekerheid", "Laatst gezien",
                 "Bron", "Notities", "Actie", "Houdbaar tot", "Status", "EAN"],
                ["Eieren", "Zuivel", "Koelkast", 12, "stuks", 6, "Bevestigd", TODAY.isoformat(), "Gebruiker", "", "",
                 "", "Actief", ""],
                ["Kaas", "Zuivel", "Koelkast", "", "gram", 200, "Onbekend", "2026-11-01", "Foto koelkast", "", "",
                 "", "Actief", ""]]
    aanvul = [["Product", "Categorie", "Weekdoel", "Eenheid", "Voorraad nu", "Te bestellen", "Prijs/eenheid €",
               "Totaal €", "Controle", "Opmerking", "Prijsdatum", "In mand"],
              ["Kaas", "Zuivel", 500, "gram", 0, 500, "", "", "", "notitie", "", False]]
    inst = [["Instelling", "Waarde", "Toelichting"], ["Weekbudget", 150, ""], ["Werkelijke bezorgkosten", "", ""],
            ["Voorraadfoto's van", "2026-11-01", ""]]
    inst += [[k, "", ""] for k in ("Bezorgkosten gecontroleerd op", "Bezorging leverancier", "Bezorgdatum",
                                   "Bezorgmoment", "Bezorgkosten bron")]
    lev = [["LEVERANCIERS", "Koopwijze", "Bezorgkosten", "Voorwaarden", "Netto beoordeling", "Status",
            "Bezorgkosten gecontroleerd €", "Bezorging gecontroleerd op", "Reis- en tijdkosten €"],
           ["Jumbo", "Thuisbezorgen", "?", "", "goedkoopst?", "", "", "", ""],
           ["Lidl", "Zelf halen", "", "", "", "", "", "", ""]]
    prijzen = [["Product", "Leverancier", "Prijs verpakking €", "Verpakking", "Prijs/eenheid €", "Prijssoort",
                "Geldig t/m", "Prijsdatum", "Bron"]]
    bonnen = [["Besteldatum", "Leverancier", "Product", "Aantal", "Eenheid", "Betaald €", "Bestelnummer", "Status",
               "Verwerkt op"]]
    week = [["Onderdeel", "Item", "Aantal", "Eenheid", "Bedrag €", "Opmerking"]]
    if not new_schema:
        voorraad = [r[:11] for r in voorraad]
        lev = [r[:6] for r in lev]
    tabs = {"Voorraad": voorraad, "Aanvullijst": aanvul, "Instellingen": inst, "Leveranciers": lev,
            "Weekmenu": [["Dag", "Ritme", "Diner", "Gebruik voorraad eerst"], ["Zondag", "Koken", "Pasta", ""]],
            "Prijzen": prijzen, "Bonnen": bonnen, "Weeklijst": week, "Overzicht": []}
    if not new_schema:
        tabs.pop("Prijzen")
        tabs.pop("Weeklijst")
    sheets = {k: LiveFakeSheet(v) for k, v in tabs.items()}
    sheets["Voorraad"].formulas = {(2, 10): '=IF(A2="";"";"x")', (3, 10): '=IF(A3="";"";"x")'}
    sheets["Aanvullijst"].formulas = {(2, 4): "=VLOOKUP(A2;Voorraad!$A$2:$D$200;4;FALSE)",
                                      (2, 5): "=MAX(0;C2-E2)", (2, 7): "=F2*G2"}
    return FakeBook(sheets)


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    import gspread
    import streamlit as st
    st.cache_data.clear()
    st.cache_resource.clear()
    real = FakeBook.worksheet

    def worksheet(self, name):
        if name not in self.tabs:
            raise gspread.WorksheetNotFound(name)
        return real(self, name)
    monkeypatch.setattr(FakeBook, "worksheet", worksheet)
    yield


def start(monkeypatch, book, user=ALLOWED, configured=True, allow=("ouder@example.com",), token="tok-ouder",
          secrets=None):
    calls = []

    def connect(access_token, sid):
        calls.append(sid)
        return book
    monkeypatch.setattr(sheet_store, "connect", connect)
    monkeypatch.setattr(auth, "current_user", lambda: dict(user))
    monkeypatch.setattr(auth, "login_configured", lambda: configured)
    monkeypatch.setattr(auth, "access_token", token if callable(token) else (lambda: token))
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["ALLOWED_EMAILS"] = list(allow)
    at.secrets["SHEET_ID"] = "fake"
    for k, v in (secrets or {}).items():
        at.secrets[k] = v
    at.run()
    assert not at.exception, at.exception
    return at, calls


def btn(at, text):
    return next(b for b in at.button if text in b.label)


def go_shown(at):
    return any(s.value.startswith("GO") for s in at.success)


def num(at, prefix):
    return next(n for n in at.number_input if n.label.startswith(prefix))


def count_kaas(at, value=0.0):
    num(at, "Kaas").set_value(value)
    btn(at, "Tellingen bevestigen").click().run()
    assert not at.exception, at.exception


def save_delivery(at, amount=0.0, day=TOMORROW):
    next(d for d in at.date_input if d.label.startswith("Bezorgdatum")).set_value(day)
    next(t for t in at.text_input if t.label == "Bezorgmoment").input("18:00-20:00")
    num(at, "Bezorgkosten € (checkout)").set_value(amount)
    next(t for t in at.text_input if t.label == "Bron van de controle").input("checkout-scherm")
    next(c for c in at.checkbox if c.label.startswith("Ik heb dit bedrag")).check()
    btn(at, "Bezorgcontrole opslaan").click().run()
    assert not at.exception, at.exception


def save_price(at, pack_price=6.25, content=500.0, kind="Normaal", offer_end=None):
    num(at, "Prijs per verpakking").set_value(pack_price)
    num(at, "Inhoud verpakking").set_value(content)
    next(r for r in at.radio if r.label == "Soort prijs").set_value(kind)
    if offer_end:
        next(d for d in at.date_input if d.label.startswith("Aanbieding")).set_value(offer_end)
    next(t for t in at.text_input if t.label == "Bron").input("webshop")
    next(c for c in at.checkbox if c.label.startswith("Ik zie deze prijs")).check()
    btn(at, "Prijs opslaan").click().run()
    assert not at.exception, at.exception


# --- access control ---------------------------------------------------------------------------------

def test_logged_out_sees_only_login_and_no_data_is_read(monkeypatch):
    at, calls = start(monkeypatch, make_book(), user={"is_logged_in": False})
    assert any("Inloggen met Google" in b.label for b in at.button)
    assert calls == [] and not at.tabs and not at.metric


def test_login_not_configured_shows_error_and_reads_nothing(monkeypatch):
    at, calls = start(monkeypatch, make_book(), user={"is_logged_in": False}, configured=False)
    assert any("niet ingericht" in e.value for e in at.error) and calls == [] and not at.tabs


@pytest.mark.parametrize("user", [
    {"is_logged_in": True, "email": "vreemde@example.com", "email_verified": True},
    {"is_logged_in": True, "email": "ouder@example.com", "email_verified": False},
])
def test_not_allowed_account_is_denied_before_any_data(monkeypatch, user):
    at, calls = start(monkeypatch, make_book(), user=user)
    assert any("Geen toegang" in e.value for e in at.error)
    assert calls == [] and not at.tabs and not at.metric


def test_empty_allowlist_denies_everyone(monkeypatch):
    at, calls = start(monkeypatch, make_book(), allow=())
    assert any("Geen toegang" in e.value for e in at.error) and calls == []


def test_allowed_account_sees_dashboard_with_no_go(monkeypatch):
    at, calls = start(monkeypatch, make_book())
    assert calls == ["fake"] and len(at.tabs) == 7 and not go_shown(at)
    assert any("NO-GO" in w.value for w in at.warning)


# --- order flow -------------------------------------------------------------------------------------

def test_full_flow_reaches_go_and_writes_only_target_cells(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    count_kaas(at, 0.0)                       # a counted 0 is valid stock
    save_delivery(at, 0.0)
    save_price(at, 6.25, 500.0)
    assert go_shown(at), [w.value for w in at.warning]
    assert any(m.value == "€6.25" for m in at.metric if m.label.startswith("Totaal"))
    p = book.tabs["Prijzen"].grid[1]
    assert p[:6] == ["Kaas", "Jumbo", 6.25, "500 gram", 0.0125, "Normaal"] and p[6] == TOMORROW.isoformat()
    assert set(book.tabs["Voorraad"].writes) == {"D3", "G3", "H3", "I3"}
    assert book.tabs["Aanvullijst"].writes == []                         # formulas E/F/H untouched
    assert all(r.startswith("B") for r in book.tabs["Instellingen"].writes)


def test_paid_delivery_stays_no_go(monkeypatch):
    at, _ = start(monkeypatch, make_book())
    count_kaas(at)
    save_delivery(at, 0.01)
    save_price(at)
    assert not go_shown(at) and any("exact €0,00" in w.value for w in at.warning)


def test_expired_offer_is_refused(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    count_kaas(at)
    save_delivery(at, 0.0, day=TODAY + timedelta(days=2))
    save_price(at, kind="Aanbieding", offer_end=TOMORROW)
    assert any("minstens tot de bezorgdatum" in e.value for e in at.error)
    assert len(book.tabs["Prijzen"].grid) == 1 and not go_shown(at)


def test_unknown_stock_blocks_go(monkeypatch):
    at, _ = start(monkeypatch, make_book())
    save_delivery(at, 0.0)
    save_price(at)
    assert not go_shown(at) and any("voorraadtelling" in w.value for w in at.warning)


def test_concurrent_change_is_refused_in_ui(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    book.tabs["Voorraad"].grid[2][3] = 7          # someone else counts Kaas after our screen loaded
    count_kaas(at, 0.0)
    assert any("intussen gewijzigd" in e.value for e in at.error) and book.tabs["Voorraad"].writes == []


def test_old_schema_reports_setup_and_never_adds_tabs(monkeypatch):
    book = make_book(new_schema=False)
    at, _ = start(monkeypatch, book)
    assert any("Sheet-inrichting nodig" in e.value for e in at.error)
    assert "Prijzen" not in book.tabs and not go_shown(at)


def test_basket_ticks_are_saved(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    next(c for c in at.checkbox if c.label.startswith("Kaas —")).check()
    btn(at, "Mandje bewaren").click().run()
    assert book.tabs["Aanvullijst"].grid[1][11] is True and book.tabs["Aanvullijst"].writes == ["L2"]


# --- stock management -------------------------------------------------------------------------------

def test_add_product_appends_row_and_copies_formula(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    next(t for t in at.text_input if t.label == "Productnaam").input("Rijst")
    next(s for s in at.selectbox if s.label == "Eenheid").set_value("gram")
    btn(at, "Toevoegen").click().run()
    assert not at.exception
    row = book.tabs["Voorraad"].grid[3]
    assert row[0] == "Rijst" and row[6] == "Onbekend" and row[12] == "Actief"
    assert book.tabs["Voorraad"].formulas[(4, 10)] == '=IF(A4="";"";"x")'


def test_archive_needs_confirmation(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    assert next(b for b in at.button if b.label == "Archiveren").disabled
    next(c for c in at.checkbox if c.label == "Zeker archiveren").check().run()
    btn(at, "Archiveren").click().run()
    assert book.tabs["Voorraad"].grid[1][12] == "Gearchiveerd" and book.tabs["Voorraad"].writes == ["M2"]


def test_used_up_sets_confirmed_zero(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    btn(at, "Opgebruikt").click().run()
    assert book.tabs["Voorraad"].grid[1][3] == 0.0 and book.tabs["Voorraad"].grid[1][6] == "Bevestigd"


# --- other tabs -------------------------------------------------------------------------------------

def test_receipt_import_skips_lines_already_in_sheet(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)

    def submit():
        next(t for t in at.text_input if t.label == "Bestelnummer").input("J-1")
        next(t for t in at.text_area if t.label.startswith("Regels")).input("Kaas; 1; stuks; 4,99\nMelk; 2; liter; 2,38")
        btn(at, "Bon opslaan").click().run()
    submit()
    assert any("2 regel(s) opgeslagen, 0 dubbel" in s.value for s in at.success)
    submit()                                   # same order again: nothing is counted twice
    assert any("0 regel(s) opgeslagen, 2 dubbel" in s.value for s in at.success)
    assert len(book.tabs["Bonnen"].grid) == 3


def test_weeklijst_button_writes_tab(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    btn(at, "Weeklijst bijwerken").click().run()
    grid = book.tabs["Weeklijst"].grid
    assert grid[1][:2] == ["Status", "NO-GO"] and any(r[0] == "Totaal" for r in grid)


def test_menu_edit(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    next(t for t in at.text_input if t.label == "Nieuw diner").input("Stamppot")
    next(b for b in at.button if b.label == "💾 Opslaan" and "menu" in str(b.form_id)).click().run()
    assert book.tabs["Weekmenu"].grid[1][2] == "Stamppot"


def test_stores_tab_makes_no_cheapest_claim(monkeypatch):
    at, _ = start(monkeypatch, make_book())
    texts = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "goedkoopst" not in texts.lower() and "winnaar" not in texts.lower()   # Sheet note is not shown as a claim
    assert "onvolledig" in texts and "geen live koppeling" in texts


def test_scan_tab_claims_no_ai(monkeypatch):
    at, _ = start(monkeypatch, make_book())
    captions = " ".join(c.value for c in at.caption)
    assert "schat geen hoeveelheden" in captions and "herkent geen producten zonder barcode" in captions


# --- second-session changes are never silently overwritten (stale baseline regression) -------------

def other_session_changes(book, tab, row, col, value):
    import streamlit as st
    book.tabs[tab].grid[row - 1][col] = value
    st.cache_data.clear()                      # what another session's save does to the shared cache


def test_basket_keeps_tick_made_in_other_session(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    other_session_changes(book, "Aanvullijst", 2, 11, True)       # B ticks Kaas
    btn(at, "Mandje bewaren").click().run()                        # A saves without changing anything
    assert book.tabs["Aanvullijst"].grid[1][11] is True and book.tabs["Aanvullijst"].writes == []


def test_delivery_check_of_other_session_is_not_overwritten(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    other_session_changes(book, "Instellingen", 3, 1, 4.95)       # B stores €4.95 meanwhile
    save_delivery(at, 0.0)                                         # A submits a form opened earlier
    assert any("intussen gewijzigd" in e.value for e in at.error)
    assert book.tabs["Instellingen"].grid[2][1] == 4.95 and book.tabs["Instellingen"].writes == []


def test_recount_of_other_session_is_not_overwritten(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    other_session_changes(book, "Voorraad", 3, 3, 300)
    count_kaas(at, 0.0)
    assert any("intussen gewijzigd" in e.value or "veranderd" in e.value for e in at.error)
    assert book.tabs["Voorraad"].grid[2][3] == 300 and book.tabs["Voorraad"].writes == []


def test_price_of_other_session_is_not_overwritten(monkeypatch):
    book = make_book()
    at, _ = start(monkeypatch, book)
    count_kaas(at)
    save_delivery(at, 0.0)
    row = ["Kaas", "Jumbo", 5.0, "500 gram", 0.01, "Normaal", TOMORROW.isoformat(), TODAY.isoformat(), "B"]
    import streamlit as st
    book.tabs["Prijzen"].grid.append(row)
    st.cache_data.clear()
    save_price(at)
    assert any("intussen gewijzigd" in e.value for e in at.error)
    assert book.tabs["Prijzen"].grid[1] == row and len(book.tabs["Prijzen"].grid) == 2


def test_every_tab_save_path_runs(monkeypatch):
    """Regression: a variable named like the baseline helper broke every save after the delivery form."""
    book = make_book()
    at, _ = start(monkeypatch, book)
    save_delivery(at, 0.0)
    num(at, "Reis- en tijdkosten").set_value(5.0)
    next(b for b in at.button if b.label == "💾 Opslaan" and "supplier" in str(b.form_id)).click().run()
    assert any("bijgewerkt" in s.value for s in at.success), [e.value for e in at.error]
    next(t for t in at.text_input if t.label == "Nieuw diner").input("Soep")
    next(b for b in at.button if b.label == "💾 Opslaan" and "menu" in str(b.form_id)).click().run()
    assert book.tabs["Weekmenu"].grid[1][2] == "Soep", [e.value for e in at.error]


def test_add_product_into_row_with_prefilled_formulas(monkeypatch):
    book = make_book()
    v = book.tabs["Voorraad"]
    v.grid.append([""] * 14)
    v.formulas[(4, 10)] = '=IF(A4="";"";"x")'                       # formulas pre-filled below the data
    at, _ = start(monkeypatch, book)
    next(t for t in at.text_input if t.label == "Productnaam").input("=HYPERLINK(\"http://x\")")
    next(s for s in at.selectbox if s.label == "Eenheid").set_value("gram")
    btn(at, "Toevoegen").click().run()
    assert v.grid[3][0] == '=HYPERLINK("http://x")'                # stored as text, not as a formula
    assert (4, 0) not in v.formulas and v.formulas[(4, 10)] == '=IF(A4="";"";"x")'
    assert "K4" not in v.writes                                      # existing formula left alone


def test_basket_after_refresh_cannot_undo_other_sessions_tick(monkeypatch):
    """Regression (final review P1b): a rerun between B's tick and A's save must not let A's stale box win."""
    book = make_book()
    at, _ = start(monkeypatch, book)
    other_session_changes(book, "Aanvullijst", 2, 11, True)       # B ticks Kaas
    at.run()                                                       # A taps something -> rerun with fresh data
    box = next(c for c in at.checkbox if c.label.startswith("Kaas —"))
    assert box.value is True                                       # A now sees B's tick
    btn(at, "Mandje bewaren").click().run()                        # A saves without changing anything
    assert book.tabs["Aanvullijst"].grid[1][11] is True and book.tabs["Aanvullijst"].writes == []


def test_product_form_reports_partial_save(monkeypatch):
    """Regression (final review Q1): count saved but details refused -> 'Deels opgeslagen', not 'Niet opgeslagen'."""
    book = make_book()
    at, _ = start(monkeypatch, book)
    num(at, "Nieuwe telling").set_value(20.0)
    next(s for s in at.selectbox if s.label == "Locatie").set_value("Vriezer")
    other_session_changes(book, "Voorraad", 2, 2, "Voorraadkast")  # B moved Eieren meanwhile
    next(b for b in at.button if b.label == "💾 Opslaan" and "product" in str(b.form_id)).click().run()
    assert any(e.value.startswith("Deels opgeslagen") and "telling 20" in e.value for e in at.error)
    assert book.tabs["Voorraad"].grid[1][3] == 20.0 and book.tabs["Voorraad"].grid[1][2] == "Voorraadkast"
