"""v1.0 logic: prices per supplier, comparison, savings, access control, scanning, receipts, week list."""
import io
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scan  # noqa: E402
from core import (compare_suppliers, delivery_check, expiring, go_decision, is_allowed, low_stock,  # noqa: E402
                  match_barcodes, new_receipt_rows, parse_receipt_lines, savings, settings_map, shopping_totals,
                  supplier_rows, weeklijst_rows, effective_budget)

TODAY = date(2026, 11, 5)
DAY = date(2026, 11, 6)


def settings(supplier="Jumbo", amount=0, basket=8.20):
    rows = {"Weekbudget": 150, "Werkelijke bezorgkosten": amount, "Bezorgkosten gecontroleerd op": "2026-11-05",
            "Bezorging leverancier": supplier, "Bezorgdatum": "2026-11-06", "Bezorgkosten bron": "checkout",
            "Mandtotaal checkout": basket}
    return settings_map([["Instelling", "Waarde"]] + [[k, v] for k, v in rows.items()])


def stock(product, count=0, unit="gram", status="Actief", **kw):
    return dict({"Product": product, "Aantal nu": count, "Eenheid": unit, "Zekerheid": "Bevestigd",
                 "Laatst gezien": "2026-11-04", "Bron": "Gebruiker", "Status": status}, **kw)


def line(product, wanted, unit="gram"):
    return {"Product": product, "Eenheid": unit, "Weekdoel": wanted, "Voorraad nu": 0, "Te bestellen": wanted}


def price(product, supplier, per_unit, pack, kind="Normaal", until="2026-11-06", checked="2026-11-05", unit="gram"):
    return {"Product": product, "Leverancier": supplier, "Prijs/eenheid €": per_unit, "Verpakking": f"{pack} {unit}",
            "Prijssoort": kind, "Geldig t/m": until, "Prijsdatum": checked, "Bron": "webshop"}


SUPPLIERS = supplier_rows([
    ["LEVERANCIERS", "Koopwijze", "Bezorgkosten", "Voorwaarden", "Netto beoordeling", "Status",
     "Bezorgkosten gecontroleerd €", "Bezorging gecontroleerd op", "Reis- en tijdkosten €"],
    ["Jumbo", "Thuisbezorgen", "", "", "", "", "", "", ""],
    ["Albert Heijn", "Thuisbezorgen", "", "", "", "", 2.95, "2026-11-04", ""],
    ["Lidl", "Zelf halen", "", "", "", "", "", "", 6.0],
    [],
    ["VOORBEELDVERGELIJKING", "Aantal", "Eenheid", "", "", "", "", "", ""],
])
ROWS = [line("Kaas", 500), line("Melk", 2, "liter")]
INV = [stock("Kaas"), stock("Melk", unit="liter")]
PRICES = [price("Kaas", "Jumbo", 0.0125, 500), price("Melk", "Jumbo", 1.0, 1, unit="liter"),
          price("Kaas", "Albert Heijn", 0.011, 500), price("Melk", "Albert Heijn", 0.95, 1, unit="liter"),
          price("Kaas", "Lidl", 0.008, 500), price("Melk", "Lidl", 0.85, 1, unit="liter")]


def test_supplier_rows_stop_at_first_empty_row():
    assert [s["LEVERANCIERS"] for s in SUPPLIERS] == ["Jumbo", "Albert Heijn", "Lidl"]


def test_prices_come_from_prijzen_for_delivery_supplier():
    spend = shopping_totals(ROWS, INV, delivery_check(settings(), TODAY), TODAY, PRICES)
    assert spend["expected_total"] == 8.25                           # 6.25 + 2 x 1.00 (indicative)
    assert spend["ready"] and spend["grand_total"] == 8.20           # GO total = checkout basket + €0 delivery
    other = shopping_totals(ROWS, INV, delivery_check(settings("Lidl"), TODAY), TODAY, PRICES)
    assert other["expected_total"] == 5.70                           # 4.00 + 2 x 0.85
    none = shopping_totals(ROWS, INV, delivery_check(settings(), TODAY), TODAY, [])
    assert none["expected_total"] is None and set(none["missing_price"]) == {"Kaas", "Melk"}


def test_latest_check_wins_when_supplier_has_several_rows():
    old = price("Kaas", "Jumbo", 0.02, 500, checked="2026-11-01", until="2026-11-01")
    spend = shopping_totals(ROWS, INV, delivery_check(settings(), TODAY), TODAY, PRICES + [old])
    assert spend["expected_total"] == 8.25


def test_comparison_and_savings_same_scope():
    delivery = delivery_check(settings(), TODAY)
    comp = {r["supplier"]: r for r in compare_suppliers(ROWS, INV, PRICES, SUPPLIERS, delivery, TODAY)}
    assert comp["Jumbo"]["total"] == 8.25                             # €0 checkout
    assert comp["Albert Heijn"]["total"] == 10.35                     # 5.50 + 1.90 + 2.95 delivery
    assert comp["Lidl"]["total"] == 11.70                             # 5.70 + 6.00 travel/time
    advice = {s["supplier"]: s for s in savings(list(comp.values()), "Jumbo")}
    assert advice["Albert Heijn"]["difference"] == 2.10 and "Jumbo is €2.10 goedkoper" in advice["Albert Heijn"]["advice"]
    assert advice["Lidl"]["difference"] == 3.45


def test_pickup_saving_below_ten_euro_is_not_worth_a_trip():
    delivery = delivery_check(settings(), TODAY)
    cheap = [p if p["Leverancier"] != "Lidl" else dict(p, **{"Prijs/eenheid €": p["Prijs/eenheid €"] / 4})
             for p in PRICES]
    sup = [dict(s, **{"Reis- en tijdkosten €": 0}) if s["LEVERANCIERS"] == "Lidl" else s for s in SUPPLIERS]
    comp = compare_suppliers(ROWS, INV, cheap, sup, delivery, TODAY)
    lidl = next(s for s in savings(comp, "Jumbo") if s["supplier"] == "Lidl")
    assert lidl["difference"] < 0 and "geen aparte rit" in lidl["advice"]


def test_comparison_incomplete_without_proof():
    delivery = delivery_check(settings(), TODAY)
    sup = [dict(s, **{"Bezorging gecontroleerd op": "2026-10-01"}) if s["LEVERANCIERS"] == "Albert Heijn" else
           dict(s, **{"Reis- en tijdkosten €": ""}) if s["LEVERANCIERS"] == "Lidl" else s for s in SUPPLIERS]
    comp = {r["supplier"]: r for r in compare_suppliers(ROWS, INV, PRICES, sup, delivery, TODAY)}
    assert comp["Albert Heijn"]["complete"] is False and "bezorgcontrole te oud" in comp["Albert Heijn"]["problems"]
    assert comp["Lidl"]["complete"] is False and "reis- en tijdkosten onbekend" in comp["Lidl"]["problems"]
    expired = [dict(p, **{"Prijssoort": "Aanbieding", "Geldig t/m": "2026-11-05"}) if p["Leverancier"] == "Lidl"
               else p for p in PRICES]
    lidl = next(r for r in compare_suppliers(ROWS, INV, expired, SUPPLIERS, delivery, TODAY) if r["supplier"] == "Lidl")
    assert lidl["complete"] is False and lidl["total"] is None
    unsure = [stock("Kaas", status="Actief", Zekerheid="Onbekend"), INV[1]]
    assert all(not r["complete"] for r in compare_suppliers(ROWS, unsure, PRICES, SUPPLIERS, delivery, TODAY))
    advice = savings(compare_suppliers(ROWS, unsure, PRICES, SUPPLIERS, delivery, TODAY), "Jumbo")
    assert all(a["difference"] is None for a in advice)


def test_archived_product_is_not_ordered_and_not_blocking():
    inv = [stock("Kaas", status="Gearchiveerd"), INV[1]]
    spend = shopping_totals(ROWS, inv, delivery_check(settings(), TODAY), TODAY, PRICES)
    assert spend["archived"] == ["Kaas"] and spend["missing_inventory"] == [] and spend["expected_total"] == 2.0
    assert spend["ready"]


def test_low_stock_and_expiry():
    inv = [stock("Kaas", 100, Minimum=200), stock("Melk", 3, "liter", Minimum=2),
           stock("Yoghurt", 1, "stuks", **{"Houdbaar tot": "2026-11-07"}),
           stock("Ham", 1, "stuks", **{"Houdbaar tot": "2026-11-20"}),
           stock("Oud", 0, "stuks", Minimum=5, Zekerheid="Onbekend")]
    assert [x["Product"] for x in low_stock(inv, DAY, TODAY)] == ["Kaas"]        # unknown count is never "bijna op"
    assert [x["Product"] for x, _ in expiring(inv, TODAY)] == ["Yoghurt"]


@pytest.mark.parametrize("user,ok", [
    ({"is_logged_in": True, "email": "A@example.org", "email_verified": True}, True),
    ({"is_logged_in": True, "email": "b@example.org", "email_verified": True}, False),     # not on the list
    ({"is_logged_in": True, "email": "a@example.org", "email_verified": False}, False),    # unverified e-mail
    ({"is_logged_in": True, "email": "a@example.org"}, False),                             # claim missing
    ({"is_logged_in": False, "email": "a@example.org", "email_verified": True}, False),
    ({}, False), (None, False),
])
def test_allowlist_is_deny_by_default(user, ok):
    assert is_allowed(user, ["a@example.org"]) is ok


def test_empty_allowlist_denies_everyone():
    assert is_allowed({"is_logged_in": True, "email": "a@example.org", "email_verified": True}, []) is False


def test_barcode_matching():
    inv = [stock("Kaas", EAN="8710398000002"), stock("Oud", EAN="4006381333931", status="Gearchiveerd")]
    known, unknown = match_barcodes(["8710398000002", "4006381333931", "8710398000002", ""], inv)
    assert [x["Product"] for x in known] == ["Kaas"] and unknown == ["4006381333931"]


def _barcode_png(codes):
    import numpy as np
    import zxingcpp
    from PIL import Image
    ims = [Image.fromarray(np.array(zxingcpp.write_barcode_to_image(
        zxingcpp.create_barcode(c, zxingcpp.BarcodeFormat.EAN13), scale=4))).convert("L") for c in codes]
    canvas = Image.new("L", (sum(i.width for i in ims) + 20 * (len(ims) + 1), max(i.height for i in ims) + 40), 255)
    x = 20
    for i in ims:
        canvas.paste(i, (x, 20))
        x += i.width + 20
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def test_decode_several_barcodes_in_one_photo():
    pytest.importorskip("zxingcpp")
    codes = scan.decode_barcodes(_barcode_png(["400638133393", "871039800000"]))
    assert sorted(codes) == ["4006381333931", "8710398000002"]


def test_decode_photo_without_barcode():
    pytest.importorskip("zxingcpp")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (200, 100), 255).save(buf, format="PNG")
    assert scan.decode_barcodes(buf.getvalue()) == []


def test_product_lookup_is_a_suggestion_and_fails_safe():
    found = lambda url, timeout: {"status": 1, "product": {"product_name": "Halfvolle melk", "quantity": "1 L"}}
    assert scan.lookup_product("8710398000002", fetch=found)["name"] == "Halfvolle melk"
    assert scan.lookup_product("8710398000002", fetch=lambda u, t: {"status": 0}) is None

    def offline(url, timeout):
        raise OSError("no network")
    with pytest.raises(scan.LookupUnavailable):          # not "unknown": must not be cached as a result
        scan.lookup_product("8710398000002", fetch=offline)
    assert scan.lookup_product("abc", fetch=found) is None


def test_receipt_parsing_validation_and_dedupe():
    entries = parse_receipt_lines("Kaas; 1; stuks; 4,99\n\nMelk; 2; liter; 2,38", TODAY, "Jumbo", "J-9")
    assert [e["Product"] for e in entries] == ["Kaas", "Melk"]
    existing = [{"Leverancier": "Jumbo", "Bestelnummer": "J-9", "Product": "kaas"}]
    new, skipped = new_receipt_rows(existing, entries + [dict(entries[1])])
    # already in the Sheet -> skipped; two Melk lines within one pasted order are both kept
    assert [e["Product"] for e in new] == ["Melk", "Melk"] and len(skipped) == 1
    for bad in ("Kaas; 1; stuks", "Kaas; -1; stuks; 1", "Kaas; 1; stuks; -2"):
        with pytest.raises(ValueError):
            new_receipt_rows([], parse_receipt_lines(bad, TODAY, "Jumbo", "J-9"))
    with pytest.raises(ValueError):
        new_receipt_rows([], parse_receipt_lines("Kaas; 1; stuks; 1", TODAY, "Jumbo", ""))


def test_weeklijst_contains_all_sections():
    delivery = delivery_check(settings(), TODAY)
    spend = shopping_totals(ROWS, INV, delivery, TODAY, PRICES)
    comp = compare_suppliers(ROWS, INV, PRICES, SUPPLIERS, delivery, TODAY)
    go, reasons = go_decision(spend, effective_budget(150))
    menu = [{"Dag": "Zondag", "Ritme": "Koken", "Diner": "Nasi"}]
    rows = weeklijst_rows(menu, spend, comp, savings(comp, "Jumbo"), go, reasons, delivery, 150.0,
                          [("Ham", "telling onbekend")], TODAY)
    sections = {r[0] for r in rows}
    assert {"Status", "Weekmenu", "Boodschappen", "Voorraadcorrectie", "Supermarktvergelijking", "Besparing",
            "Bezorgkosten", "Totaal"} <= sections
    assert rows[0][1] == "GO" and all(len(r) == 6 for r in rows)
    total = next(r for r in rows if r[0] == "Totaal")
    assert total[4] == 8.20 and "141.80" in total[5]
    expected = next(r for r in rows if r[0] == "Verwacht")
    assert expected[4] == 8.25 and "geen bevestigd bedrag" in expected[5]


def test_ean_normalisation_matches_upc_and_numeric_cells():
    inv = [stock("Melk", EAN="0012345678905"), stock("Kaas", EAN=8710398000002.0)]
    known, unknown = match_barcodes(["012345678905", "8710398000002"], inv)
    assert [x["Product"] for x in known] == ["Melk", "Kaas"] and unknown == []


def test_example_cookie_secret_is_rejected(monkeypatch):
    import auth
    import streamlit as st

    class Secrets(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)
    base = {"redirect_uri": "https://x.streamlit.app/oauth2callback", "client_id": "id", "expose_tokens": "access",
            "client_kwargs": {"scope": "openid email profile " + auth.FILE_SCOPE}}
    for secret, ok in ((auth.EXAMPLE_COOKIE_SECRET, False), ("short", False), ("x" * 40, True)):
        monkeypatch.setattr(st, "secrets", Secrets(auth=dict(base, cookie_secret=secret)))
        assert auth.login_configured() is ok
