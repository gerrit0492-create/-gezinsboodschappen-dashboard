"""Sheets writeback against a fake gspread workbook: targeted cells only, guarded against stale data."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import table_rows  # noqa: E402
from sheet_store import (ConflictError, add_product, archive_product, shift_formula, write_basket,
                         write_delivery, write_details, write_menu, write_price, write_receipts, write_stock,
                         write_supplier_check, write_weeklijst)

TODAY = date(2026, 11, 5)
VOORRAAD = [
    ["Product", "Categorie", "Locatie", "Aantal nu", "Eenheid", "Minimum", "Zekerheid", "Laatst gezien", "Bron", "Notities", "Actie"],
    ["Eieren", "Zuivel", "Koelkast", 12, "stuks", 6, "Bevestigd", "2026-11-01", "Foto", "", "=IF(D2<F2;\"Kopen\";\"\")"],
    ["Melk", "Zuivel", "Koelkast", "", "liter", 4, "Onbekend", "", "", "", "=IF(D3<F3;\"Kopen\";\"\")"],
]
AANVUL = [
    ["Product", "Categorie", "Weekdoel", "Eenheid", "Voorraad nu", "Te bestellen", "Prijs/eenheid €", "Totaal €", "Controle", "Opmerking", "Prijsdatum", "In mand", "Prijs leverancier", "Prijssoort", "Geldig t/m", "Verpakking"],
    ["Kaas", "Zuivel", 500, "gram", 0, 500, "", "=F2*G2", "", "eigen notitie", "", False, "", "", "", ""],
]
INSTELLINGEN = [
    ["Instelling", "Waarde", "Toelichting"],
    ["Weekbudget", 150, "Inclusief bezorging"],
    ["Werkelijke bezorgkosten", "", "In te vullen"],
    ["Bezorgkosten gecontroleerd op", "", ""],
    ["Bezorging leverancier", "", ""],
    ["Bezorgdatum", "", ""],
    ["Bezorgmoment", "", ""],
    ["Bezorgkosten bron", "", ""],
]
PRIJZEN = [["Product", "Leverancier", "Prijs verpakking €", "Verpakking", "Prijs/eenheid €", "Prijssoort",
            "Geldig t/m", "Prijsdatum", "Bron"]]
EMPTY_OLD = {"amount": "", "verified": "", "supplier": "", "date": "", "source": "", "basket": "", "basket_list": ""}
RECORD = {"amount": 0.0, "verified": TODAY, "supplier": "Jumbo", "date": date(2026, 11, 6),
          "source": "checkout-scherm", "basket": 123.456, "basket_list": "abc123def456"}


class FakeSheet:
    """UNFORMATTED reads return `grid`; FORMULA reads return `formulas` where given (else the same grid)."""

    def __init__(self, grid, fail=None, formulas=None):
        self.grid = [list(r) for r in grid]
        self.formulas = formulas
        self.updates = []
        self.input_options = []
        self.fail = fail

    def get_all_values(self, value_render_option=None):
        if value_render_option == "FORMULA" and self.formulas is not None:
            return [list(r) for r in self.formulas]
        return [list(r) for r in self.grid]

    def batch_update(self, req, value_input_option=None):
        if self.fail:
            raise self.fail
        assert value_input_option in ("RAW", "USER_ENTERED")
        stored = []
        for u in req:
            v = u["values"][0][0]
            if value_input_option == "USER_ENTERED" and isinstance(v, str) and v:
                # like Sheets: a leading apostrophe stores literal text; anything else must be a copied formula
                assert v.startswith("'") or v.startswith("="), f"unescaped text under USER_ENTERED: {u}"
                v = v[1:] if v.startswith("'") else v
            stored.append({"range": u["range"], "values": [[v]]})
        self.input_options.append(value_input_option)
        self.updates.extend(stored)


class FakeBook:
    def __init__(self, **tabs):
        self.tabs = tabs

    def worksheet(self, name):
        return self.tabs[name]


def ranges(sheet):
    return {u["range"]: u["values"][0] for u in sheet.updates}


def test_stock_write_targets_only_count_status_date():
    ws = FakeSheet(VOORRAAD)
    write_stock(FakeBook(Voorraad=ws), [(3, "Melk", None, 6.0)], TODAY)
    assert ranges(ws) == {"D3": [6.0], "G3": ["Bevestigd"], "H3": ["2026-11-05"], "I3": ["Telling app"]}
    assert all(not r.startswith("K") for r in ranges(ws))  # formula column untouched


def test_stock_clear_to_unknown_never_zero():
    ws = FakeSheet(VOORRAAD)
    write_stock(FakeBook(Voorraad=ws), [(2, "Eieren", 12.0, None)], TODAY)
    assert ranges(ws)["D2"] == [""] and ranges(ws)["G2"] == ["Onbekend"]
    assert "I2" not in ranges(ws)  # source untouched when clearing


def test_stock_conflict_when_changed_or_rows_shifted():
    ws = FakeSheet(VOORRAAD)
    with pytest.raises(ConflictError):  # someone else counted 10 eggs meanwhile
        write_stock(FakeBook(Voorraad=ws), [(2, "Eieren", 10.0, 11.0)], TODAY)
    with pytest.raises(ConflictError):  # a row was inserted: row 3 is no longer Kaas
        write_stock(FakeBook(Voorraad=ws), [(3, "Kaas", None, 1.0)], TODAY)
    assert ws.updates == []


def test_stock_refuses_when_columns_moved():
    moved = [list(VOORRAAD[0])] + [list(r) for r in VOORRAAD[1:]]
    moved[0][6] = "Status"
    ws = FakeSheet(moved)
    with pytest.raises(ConflictError):
        write_stock(FakeBook(Voorraad=ws), [(3, "Melk", None, 6.0)], TODAY)
    assert ws.updates == []


def test_columns_are_resolved_from_headers():
    shifted = [["Extra"] + r for r in VOORRAAD]  # every column one to the right
    ws = FakeSheet(shifted)
    write_stock(FakeBook(Voorraad=ws), [(3, "Melk", None, 2.0)], TODAY)
    assert set(ranges(ws)) == {"E3", "H3", "I3", "J3"}


def test_basket_write_and_conflict():
    ws = FakeSheet(AANVUL)
    write_basket(FakeBook(Aanvullijst=ws), [(2, "Kaas", False, True)])
    assert ranges(ws) == {"L2": [True]}
    ws2 = FakeSheet(AANVUL)
    with pytest.raises(ConflictError):  # snapshot thought it was already checked
        write_basket(FakeBook(Aanvullijst=ws2), [(2, "Kaas", True, False)])


def test_delivery_write_fills_only_value_cells():
    ws = FakeSheet(INSTELLINGEN)
    write_delivery(FakeBook(Instellingen=ws), RECORD, EMPTY_OLD)
    got = ranges(ws)
    # the old 'Bezorgmoment' row (B7) is no longer read or written; the missing 'Mandtotaal checkout' key is added
    # once in the first empty row below the data (A9 + note C9), its value on 2 decimals
    assert got == {"B3": [0.0], "B4": ["2026-11-05"], "B5": ["Jumbo"], "B6": ["2026-11-06"],
                   "B8": ["checkout-scherm"], "A9": ["Mandtotaal checkout"], "B9": [123.46],
                   "C9": [got["C9"][0]], "A10": ["Mandtotaal voor lijst"], "B10": ["abc123def456"],
                   "C10": [got["C10"][0]]}
    assert "zonder bezorgkosten" in got["C9"][0] and "niet wijzigen" in got["C10"][0]


def test_delivery_updates_existing_basket_row_in_place():
    grid = [list(r) for r in INSTELLINGEN] + [["Mandtotaal checkout", 99.0, ""], ["Mandtotaal voor lijst", "old", ""]]
    ws = FakeSheet(grid)
    write_delivery(FakeBook(Instellingen=ws), RECORD, dict(EMPTY_OLD, basket=99.0, basket_list="old"))
    got = ranges(ws)
    assert got["B9"] == [123.46] and got["B10"] == ["abc123def456"] and not any(r.startswith(("A", "C")) for r in got)


def test_delivery_refuses_when_basket_row_appeared_or_vanished():
    grid = [list(r) for r in INSTELLINGEN] + [["Mandtotaal checkout", 80.0, ""]]
    ws = FakeSheet(grid)                      # another session added the row with a value meanwhile
    with pytest.raises(ConflictError, match="Mandtotaal checkout"):
        write_delivery(FakeBook(Instellingen=ws), RECORD, EMPTY_OLD)
    ws2 = FakeSheet(INSTELLINGEN)             # the row the user saw (with a value) was removed
    with pytest.raises(ConflictError, match="verdwenen"):
        write_delivery(FakeBook(Instellingen=ws2), RECORD, dict(EMPTY_OLD, basket=50.0))
    assert ws.updates == [] and ws2.updates == []


def test_delivery_never_adds_required_rows():
    ws = FakeSheet(INSTELLINGEN[:3])  # date/supplier rows not created yet: only Mandtotaal may be added
    with pytest.raises(ConflictError, match="Voeg eerst"):
        write_delivery(FakeBook(Instellingen=ws), RECORD, EMPTY_OLD)
    assert ws.updates == []


@pytest.mark.parametrize("over", [{"amount": -1}, {"amount": None}, {"supplier": ""}, {"basket": None}, {"basket_list": ""},
                                  {"basket": -1}, {"source": ""},
                                  {"date": None}, {"verified": date(2026, 11, 7)}])
def test_delivery_rejects_invalid_input(over):
    ws = FakeSheet(INSTELLINGEN)
    with pytest.raises(ValueError):
        write_delivery(FakeBook(Instellingen=ws), dict(RECORD, **over), EMPTY_OLD)
    assert ws.updates == []


def test_delivery_refused_on_concurrent_change_or_layout():
    changed = [list(r) for r in INSTELLINGEN]
    changed[4][1] = "AH"  # someone else stored another supplier meanwhile
    ws = FakeSheet(changed)
    with pytest.raises(ConflictError, match="Bezorging leverancier"):
        write_delivery(FakeBook(Instellingen=ws), RECORD, EMPTY_OLD)
    bad = [["Sleutel", "X"]] + INSTELLINGEN[1:]
    with pytest.raises(ConflictError):
        write_delivery(FakeBook(Instellingen=FakeSheet(bad)), RECORD, EMPTY_OLD)
    assert ws.updates == []


def test_readonly_or_dropped_connection_surfaces_error_and_writes_nothing():
    ws = FakeSheet(VOORRAAD, fail=PermissionError("403 read-only"))
    with pytest.raises(PermissionError):
        write_stock(FakeBook(Voorraad=ws), [(3, "Melk", None, 6.0)], TODAY)
    assert ws.updates == []
    ws2 = FakeSheet(PRIJZEN, fail=ConnectionError("reset"))
    with pytest.raises(ConnectionError):
        write_price(FakeBook(Prijzen=ws2), "Kaas", "Jumbo", 4.99, 400, "gram", "Normaal", date(2026, 11, 6),
                    TODAY, "webshop")
    assert ws2.updates == []


def test_empty_change_lists_do_not_touch_sheet():
    assert write_stock(None, [], TODAY) == 0
    assert write_basket(None, []) == 0


def test_stock_refused_when_status_source_or_date_changed_meanwhile():
    snap = {3: {"Zekerheid": "Onbekend", "Bron": "", "Laatst gezien": ""}}
    for col, value in ((6, "Bevestigd"), (8, "Gebruiker"), (7, "2026-11-04")):
        grid = [list(r) for r in VOORRAAD]
        grid[2][col] = value
        ws = FakeSheet(grid)
        with pytest.raises(ConflictError):
            write_stock(FakeBook(Voorraad=ws), [(3, "Melk", None, 6.0)], TODAY, snap)
        assert ws.updates == []
    ok = FakeSheet(VOORRAAD)
    write_stock(FakeBook(Voorraad=ok), [(3, "Melk", None, 6.0)], TODAY, snap)
    assert ranges(ok)["D3"] == [6.0]


def with_formula(grid, row, col, formula):
    formulas = [list(r) for r in grid]
    formulas[row - 1][col] = formula
    return formulas


def test_formula_in_target_cell_is_never_overwritten():
    # computed value equals the snapshot, but the cell is a formula -> refuse
    ws = FakeSheet(VOORRAAD, formulas=with_formula(VOORRAAD, 2, 3, "=Bonnen!C2"))
    with pytest.raises(ConflictError, match="formule"):
        write_stock(FakeBook(Voorraad=ws), [(2, "Eieren", 12.0, 11.0)], TODAY)
    existing = PRIJZEN + [["Kaas", "Jumbo", 4.0, "400 gram", 0.01, "Normaal", "2026-11-06", "2026-11-04", "x"]]
    ws = FakeSheet(existing, formulas=with_formula(existing, 2, 4, "=C2/400"))
    with pytest.raises(ConflictError, match="formule"):
        write_price(FakeBook(Prijzen=ws), "Kaas", "Jumbo", 4.99, 400, "gram", "Normaal", date(2026, 11, 6), TODAY,
                    "webshop", table_rows(existing)[0])
    ws = FakeSheet(AANVUL, formulas=with_formula(AANVUL, 2, 11, "=F2>0"))
    with pytest.raises(ConflictError, match="formule"):
        write_basket(FakeBook(Aanvullijst=ws), [(2, "Kaas", False, True)])
    ws = FakeSheet(INSTELLINGEN, formulas=with_formula(INSTELLINGEN, 3, 1, "=Leveranciers!C2"))
    with pytest.raises(ConflictError, match="formule"):
        write_delivery(FakeBook(Instellingen=ws), RECORD, EMPTY_OLD)
    assert ws.updates == []




# --- v1.0 write paths --------------------------------------------------------------------------

def test_price_added_as_new_row_with_unit_price():
    ws = FakeSheet(PRIJZEN)
    row = write_price(FakeBook(Prijzen=ws), "Kaas", "Jumbo", 4.99, 400, "gram", "Normaal", date(2026, 11, 6),
                      TODAY, "webshop Jumbo")
    assert row == 2
    assert ranges(ws) == {"A2": ["Kaas"], "B2": ["Jumbo"], "C2": [4.99], "D2": ["400 gram"], "E2": [0.012475],
                          "F2": ["Normaal"], "G2": ["2026-11-06"], "H2": ["2026-11-05"], "I2": ["webshop Jumbo"]}


def test_price_updates_existing_supplier_row_and_checks_snapshot():
    existing = PRIJZEN + [["Kaas", "AH", 5.0, "400 gram", 0.0125, "Normaal", "2026-11-06", "2026-11-04", "x"],
                          ["Kaas", "Jumbo", 4.0, "400 gram", 0.01, "Normaal", "2026-11-06", "2026-11-04", "x"]]
    snap = table_rows(existing)[1]
    ws = FakeSheet(existing)
    write_price(FakeBook(Prijzen=ws), "Kaas", "jumbo", 4.99, 400, "gram", "Aanbieding", date(2026, 11, 9), TODAY,
                "folder", snap)
    assert set(ranges(ws)) == {"C3", "D3", "E3", "F3", "G3", "H3", "I3"}      # only Jumbo's row, not AH's
    stale = dict(snap, **{"Prijsdatum": "2026-11-01"})
    with pytest.raises(ConflictError):
        write_price(FakeBook(Prijzen=FakeSheet(existing)), "Kaas", "Jumbo", 4.99, 400, "gram", "Normaal",
                    date(2026, 11, 6), TODAY, "x", stale)
    with pytest.raises(ConflictError):        # app saw no row, but one exists now
        write_price(FakeBook(Prijzen=FakeSheet(existing)), "Kaas", "Jumbo", 4.99, 400, "gram", "Normaal",
                    date(2026, 11, 6), TODAY, "x", None)


@pytest.mark.parametrize("over", [{"pack_price": -1}, {"pack_price": 0}, {"pack": 0}, {"pack": None},
                                  {"supplier": ""}, {"kind": "Bonus"}, {"until": None},
                                  {"until": date(2026, 11, 4)}, {"source": ""}])
def test_price_rejects_invalid(over):
    args = dict(product="Kaas", supplier="Jumbo", pack_price=4.99, pack=400, unit="gram", kind="Normaal",
                until=date(2026, 11, 6), source="webshop")
    args.update(over)
    ws = FakeSheet(PRIJZEN)
    with pytest.raises(ValueError):
        write_price(FakeBook(Prijzen=ws), args["product"], args["supplier"], args["pack_price"], args["pack"],
                    args["unit"], args["kind"], args["until"], TODAY, args["source"])
    assert ws.updates == []


def test_price_pack_written_as_plain_number():
    ws = FakeSheet(PRIJZEN)
    write_price(FakeBook(Prijzen=ws), "Rijst", "Lidl", 12.0, 1234567.0, "gram", "Normaal", date(2026, 11, 6),
                TODAY, "x")
    assert ranges(ws)["D2"] == ["1234567 gram"]


def test_shift_formula_keeps_absolute_and_quoted_parts():
    f = '=IF(A28="";"";IFERROR(IF(VLOOKUP(A28;Voorraad!$A$2:$K$200;11;FALSE)="CONTROLEREN";"";1);""))'
    assert shift_formula(f, 28, 29) == f.replace("A28", "A29")
    assert shift_formula('=IF(D2="A2";D2;$D$2)', 2, 3) == '=IF(D3="A2";D3;$D$2)'
    assert shift_formula("=LOG10(A5)", 5, 6) == "=LOG10(A6)"
    assert shift_formula("=SUM(B5:B9)+A4", 5, 6) == "=SUM(B6:B10)+A5"   # every relative row moves by the same delta


VOORRAAD_V1 = [VOORRAAD[0] + ["Houdbaar tot", "Status", "EAN"]] + [r + ["", "Actief", ""] for r in VOORRAAD[1:]]
VOORRAAD_V1_F = [list(r) for r in VOORRAAD_V1]
VOORRAAD_V1_F[2][10] = '=IF(A3="";"";IF(D3="";"CONTROLEREN";"OK"))'


def test_add_product_appends_row_copies_formula_and_marks_unknown():
    ws = FakeSheet(VOORRAAD_V1, formulas=VOORRAAD_V1_F)
    add_product(FakeBook(Voorraad=ws), "Rijst", TODAY, location="Voorraadkast", unit="gram")
    r = ranges(ws)
    assert r["A4"] == ["Rijst"] and r["D4"] == [""] and r["G4"] == ["Onbekend"] and r["M4"] == ["Actief"]
    assert r["K4"] == ['=IF(A4="";"";IF(D4="";"CONTROLEREN";"OK"))']            # formula copied, row-shifted
    assert ws.input_options == ["USER_ENTERED"]                                   # one request, no half rows


def test_add_product_with_count_is_confirmed_and_duplicates_refused():
    ws = FakeSheet(VOORRAAD_V1)
    add_product(FakeBook(Voorraad=ws), "Rijst", TODAY, unit="gram", count=0)
    r = ranges(ws)
    assert r["D4"] == [0.0] and r["G4"] == ["Bevestigd"] and r["I4"] == ["Telling app"]
    with pytest.raises(ConflictError):
        add_product(FakeBook(Voorraad=FakeSheet(VOORRAAD_V1)), "eieren", TODAY, unit="stuks")


def test_add_product_never_overwrites_a_non_empty_row():
    grid = VOORRAAD_V1 + [["", "", "", "", "", "", "", "", "", "losse notitie", "", "", "", ""]]
    ws = FakeSheet(grid)
    with pytest.raises(ConflictError, match="niet leeg"):
        add_product(FakeBook(Voorraad=ws), "Rijst", TODAY, unit="gram")
    assert ws.updates == []
    unticked = VOORRAAD_V1 + [["", "", "", "", "", "", "", "", "", "", "", "", "", False]]
    ok = FakeSheet(unticked)
    add_product(FakeBook(Voorraad=ok), "Rijst", TODAY, unit="gram")          # an unticked checkbox counts as empty
    assert ranges(ok)["A4"] == ["Rijst"]


def test_add_product_rejects_invalid():
    for kwargs in ({"unit": ""}, {"unit": "gram", "count": -1}):
        ws = FakeSheet(VOORRAAD_V1)
        with pytest.raises(ValueError):
            add_product(FakeBook(Voorraad=ws), "Rijst", TODAY, **kwargs)
        assert ws.updates == []


def test_archive_and_restore_only_status_cell():
    ws = FakeSheet(VOORRAAD_V1)
    snap = table_rows(VOORRAAD_V1)[1]
    archive_product(FakeBook(Voorraad=ws), 3, "Melk", snap)
    assert ranges(ws) == {"M3": ["Gearchiveerd"]}
    changed = [list(r) for r in VOORRAAD_V1]
    changed[2][12] = "Gearchiveerd"
    with pytest.raises(ConflictError):        # someone else archived it already
        archive_product(FakeBook(Voorraad=FakeSheet(changed)), 3, "Melk", snap)


def test_details_write_location_expiry_ean():
    ws = FakeSheet(VOORRAAD_V1)
    snap = table_rows(VOORRAAD_V1)[0]
    write_details(FakeBook(Voorraad=ws), 2, "Eieren", {"Locatie": "Koelkast", "Houdbaar tot": date(2026, 11, 20),
                                                        "EAN": "8710398000002"}, snap)
    assert ranges(ws) == {"C2": ["Koelkast"], "L2": ["2026-11-20"], "N2": ["8710398000002"]}
    with pytest.raises(ValueError):
        write_details(FakeBook(Voorraad=FakeSheet(VOORRAAD_V1)), 2, "Eieren", {"Aantal nu": 3}, snap)


BONNEN = [["Besteldatum", "Leverancier", "Product", "Aantal", "Eenheid", "Betaald €", "Bestelnummer", "Status",
           "Verwerkt op"],
          ["2026-11-01", "Jumbo", "Kaas", 1, "stuks", 4.99, "J-1", "Ingevoerd", "2026-11-01"]]


def test_receipts_append_and_skip_duplicates_on_fresh_read():
    ws = FakeSheet(BONNEN)
    entries = [{"Besteldatum": "2026-11-01", "Leverancier": "Jumbo", "Bestelnummer": "J-1", "Product": "kaas",
                "Aantal": 1, "Eenheid": "stuks", "Betaald €": 4.99},
               {"Besteldatum": "2026-11-01", "Leverancier": "Jumbo", "Bestelnummer": "J-1", "Product": "Melk",
                "Aantal": 2, "Eenheid": "liter", "Betaald €": "2,38"}]
    assert write_receipts(FakeBook(Bonnen=ws), entries, TODAY) == (1, 1)
    assert ranges(ws)["C3"] == ["Melk"] and ranges(ws)["F3"] == [2.38] and ranges(ws)["G3"] == ["J-1"]


WEEK = [["Onderdeel", "Item", "Aantal", "Eenheid", "Bedrag €", "Opmerking"],
        ["Oud", "a", "", "", "", ""], ["Oud", "b", "", "", "", ""], ["Oud", "c", "", "", "", ""]]


def test_weeklijst_replaces_content_and_clears_leftovers():
    ws = FakeSheet(WEEK)
    write_weeklijst(FakeBook(Weeklijst=ws), [["Status", "GO", "", "", "", ""]])
    r = ranges(ws)
    assert r["A2"] == ["Status"] and r["B2"] == ["GO"] and r["A3"] == [""] and r["A4"] == [""]
    assert "A1" not in r


SUPPLIERS = [["LEVERANCIERS", "Koopwijze", "Bezorgkosten", "Voorwaarden", "Netto beoordeling", "Status",
              "Bezorgkosten gecontroleerd €", "Bezorging gecontroleerd op", "Reis- en tijdkosten €"],
             ["Jumbo", "Thuisbezorgen", "", "", "", "", "", "", ""],
             ["Lidl", "Zelf halen", "", "", "", "", "", "", ""]]


def test_supplier_check_writes_only_its_row():
    ws = FakeSheet(SUPPLIERS)
    snap = {"Bezorgkosten gecontroleerd €": "", "Bezorging gecontroleerd op": "", "Reis- en tijdkosten €": ""}
    write_supplier_check(FakeBook(Leveranciers=ws), "Lidl", None, TODAY, 6.5, snap)
    assert ranges(ws) == {"I3": [6.5]}
    with pytest.raises(ValueError):
        write_supplier_check(FakeBook(Leveranciers=FakeSheet(SUPPLIERS)), "Lidl", -1, TODAY, None, snap)


def test_menu_write_checks_day_and_old_value():
    grid = [["Dag", "Ritme", "Diner"], ["Zondag", "Koken", "Nasi"]]
    ws = FakeSheet(grid)
    write_menu(FakeBook(Weekmenu=ws), 2, "Zondag", "Stamppot", "Nasi")
    assert ranges(ws) == {"C2": ["Stamppot"]}
    with pytest.raises(ConflictError):
        write_menu(FakeBook(Weekmenu=FakeSheet(grid)), 2, "Zondag", "Soep", "Pasta")


def test_append_uses_prefilled_formula_row_without_touching_formulas():
    grid = VOORRAAD_V1 + [[""] * 14]
    formulas = [list(r) for r in grid]
    formulas[3][10] = '=IF(A4="";"";"x")'
    ws = FakeSheet(grid, formulas=formulas)
    add_product(FakeBook(Voorraad=ws), "Rijst", TODAY, unit="gram")
    assert ranges(ws)["A4"] == ["Rijst"] and "K4" not in ranges(ws) and ws.input_options == ["USER_ENTERED"]


def test_user_text_is_escaped_never_a_formula():
    ws = FakeSheet(VOORRAAD_V1)
    add_product(FakeBook(Voorraad=ws), "=IMPORTXML(\"http://x\")", TODAY, unit="gram", location="+cmd")
    # the fake asserts every USER_ENTERED string is apostrophe-escaped or a copied formula; values arrive literally
    assert ranges(ws)["A4"] == ['=IMPORTXML("http://x")'] and ranges(ws)["C4"] == ["+cmd"]


def test_receipts_are_written_in_one_request():
    ws = FakeSheet(BONNEN)
    entries = [{"Besteldatum": "2026-11-02", "Leverancier": "AH", "Bestelnummer": "A-7", "Product": f"P{i}",
                "Aantal": 1, "Eenheid": "stuks", "Betaald €": 1} for i in range(30)]
    assert write_receipts(FakeBook(Bonnen=ws), entries, TODAY) == (30, 0)
    assert ws.input_options == ["USER_ENTERED"] and ranges(ws)["C32"] == ["P29"]


def test_partial_add_is_reported_and_retry_adds_only_the_list_row():
    from sheet_store import PartialSaveError
    aan = [["Product", "Eenheid", "Weekdoel", "In mand"]]

    class FlakyList(FakeSheet):
        calls = 0

        def batch_update(self, req, value_input_option=None):
            FlakyList.calls += 1
            if FlakyList.calls == 1:
                raise ConnectionError("reset")
            return super().batch_update(req, value_input_option)
    stock_ws, list_ws = FakeSheet(VOORRAAD_V1), FlakyList(aan)
    book = FakeBook(Voorraad=stock_ws, Aanvullijst=list_ws)
    with pytest.raises(PartialSaveError, match="wel aan Voorraad"):
        add_product(book, "Rijst", TODAY, unit="gram", weekdoel=500)
    stock_ws.grid = [list(r) for r in VOORRAAD_V1] + [["Rijst"] + [""] * 13]   # Voorraad row did arrive
    assert add_product(book, "Rijst", TODAY, unit="gram", weekdoel=500) is None  # retry: list row only
    assert ranges(list_ws)["A2"] == ["Rijst"] and ranges(list_ws)["C2"] == [500.0]


def test_add_refused_before_any_write_when_list_row_is_blocked():
    aan = [["Product", "Eenheid", "Weekdoel", "In mand"], ["Melk", "liter", 2, False], ["", "", "notitie", ""]]
    stock_ws, list_ws = FakeSheet(VOORRAAD_V1), FakeSheet(aan)
    with pytest.raises(ConflictError, match="niet leeg"):
        add_product(FakeBook(Voorraad=stock_ws, Aanvullijst=list_ws), "Rijst", TODAY, unit="gram", weekdoel=1)
    assert stock_ws.updates == [] and list_ws.updates == []
