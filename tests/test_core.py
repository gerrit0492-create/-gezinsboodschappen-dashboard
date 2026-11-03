import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import (MAX_BUDGET, classify_inventory, column_letter, confirmations, delivery_check, delta_changes,
                  effective_budget, go_decision, is_checked, is_uncertain, line_cost, normalised_price, number,
                  pack_size, pack_to_row_unit_price, parse_date, price_entry_unit, price_status, schema_problems,
                  settings_map, shopping_totals, stock_state, table_rows, to_row_unit_price, valid_amount)

TODAY = date(2026, 11, 5)
DELIVERY_DAY = date(2026, 11, 6)


def settings(**over):
    values = {"Weekbudget": 150, "Werkelijke bezorgkosten": 0, "Bezorgkosten gecontroleerd op": "2026-11-05",
              "Bezorging leverancier": "Jumbo", "Bezorgdatum": "2026-11-06",
              "Bezorgkosten bron": "checkout-scherm", "Mandtotaal checkout": 6.25}
    values.update(over)
    return settings_map([["Instelling", "Waarde"]] + [[k, v] for k, v in values.items()])


def stock(product="Kaas", count=0, status="Bevestigd", seen="2026-11-04", bron="Gebruiker", unit="gram"):
    return {"Product": product, "Aantal nu": count, "Eenheid": unit, "Zekerheid": status, "Laatst gezien": seen,
            "Bron": bron}


def line(product="Kaas", wanted=500, price=0.0125, supplier="Jumbo", kind="Normaal", until="2026-11-06",
         checked="2026-11-05", pack="500 gram", unit="gram", listed=0, goal=None):
    goal = wanted if goal is None else goal   # default: a valid Weekdoel that the order exactly covers
    return {"Product": product, "Eenheid": unit, "Weekdoel": goal, "Voorraad nu": listed, "Te bestellen": wanted,
            "Prijs/eenheid €": price, "Prijs leverancier": supplier, "Prijssoort": kind, "Geldig t/m": until,
            "Prijsdatum": checked, "Verpakking": pack}


def evaluate(rows=None, inventory=None, delivery_settings=None, budget=150):
    rows = [line()] if rows is None else rows
    inventory = [stock()] if inventory is None else inventory
    delivery = delivery_check(delivery_settings or settings(), TODAY)
    spend = shopping_totals(rows, inventory, delivery, TODAY)
    return spend, go_decision(spend, effective_budget(budget))


# --- parsing and units -------------------------------------------------------------------------

def test_number_and_unknown():
    assert number("€ 1.234,56") == 1234.56
    assert number(12) == 12.0
    assert number("") is None and number(None) is None and number("#ERROR!") is None


def test_sheets_date_serial_and_formats():
    assert parse_date(46331) == date(2026, 11, 5)  # Google Sheets serial for 2026-11-05
    assert parse_date("05-11-2026") == date(2026, 11, 5) == parse_date("2026-11-05")
    assert parse_date("") is None and parse_date(True) is None


def test_unit_conversion_gram_kg_ml_liter():
    assert price_entry_unit("gram") == "kg" and price_entry_unit("ml") == "liter"
    assert price_entry_unit("stuks") == "stuk" and price_entry_unit("blikken") == "blikken"
    assert to_row_unit_price("12,50", "gram") == 0.0125      # €12.50/kg -> €/gram
    assert to_row_unit_price(12.5, "kg") == 12.5
    assert to_row_unit_price(1.2, "ml") == 0.0012            # €1.20/liter -> €/ml
    assert to_row_unit_price(1.2, "liter") == 1.2
    assert to_row_unit_price(0.89, "blikken") == 0.89
    assert round(500 * to_row_unit_price(12.5, "gram"), 2) == 6.25   # 500 g at €12.50/kg
    assert round(1500 * to_row_unit_price(1.2, "ml"), 2) == 1.80     # 1.5 l at €1.20/l


@pytest.mark.parametrize("bad", [-1, 0, "", None, "abc", float("nan"), float("inf")])
def test_invalid_price_rejected(bad):
    with pytest.raises(ValueError):
        to_row_unit_price(bad, "gram")


def test_amount_validation():
    assert valid_amount("€ 4,951") == 4.95 and valid_amount(0) == 0.0
    for bad in (-0.01, "", None, "x", float("nan")):
        with pytest.raises(ValueError):
            valid_amount(bad)


# --- stock -------------------------------------------------------------------------------------

def test_worksheet_rows_preserve_row_addresses():
    rows = table_rows([["Product", "Aantal nu", "Zekerheid"], ["Eieren", 12, "Bevestigd"], ["", "", ""], ["Kaas", "", "Onbekend"]])
    assert [(i["Product"], i["_row"]) for i in rows] == [("Eieren", 2), ("Kaas", 4)]


def test_twelve_confirmed_eggs_and_uncertain_stock():
    rows = [stock("Eieren", 12, seen="2026-11-01"), stock("Melk", "", "Onbekend"),
            stock("Kaas", 300, "Onbekend", bron="Foto koelkast"), stock("Pasta", "", "Bevestigd")]
    assert [is_uncertain(r) for r in rows] == [False, True, True, True]
    assert stock_state(rows[0], DELIVERY_DAY, TODAY) == "ok"   # told by user on 1 Nov, delivery 6 Nov
    assert classify_inventory(rows, DELIVERY_DAY, TODAY)["known"] == 1


def test_missing_zekerheid_column_is_not_a_confirmation():
    assert is_uncertain({"Product": "Melk", "Aantal nu": 4})


def test_confirmed_zero_is_valid_stock():
    spend, (go, _) = evaluate(inventory=[stock(count=0)])
    assert spend["missing_stock"] == [] and go is True


def test_old_confirmation_and_photo_snapshot_are_not_current():
    assert stock_state(stock(seen="2026-10-31"), DELIVERY_DAY, TODAY) == "ok"         # 6 days before delivery: edge
    assert stock_state(stock(seen="2026-10-30"), DELIVERY_DAY, TODAY) == "verouderd"
    assert stock_state(stock(seen=""), DELIVERY_DAY, TODAY) == "verouderd"
    assert stock_state(stock(seen="2026-11-06"), DELIVERY_DAY, TODAY) == "verouderd"   # future-dated
    photo = stock(seen="2026-11-01", bron="Foto koelkast")
    assert stock_state(photo, DELIVERY_DAY, TODAY) == "geen telling"
    # even marked 'Bevestigd' and dated in the order week, a photo/recognition source is not a count
    for bron in ("Foto vriezer", "Fruitfoto", "Beeldherkenning", "Niet gefotografeerd", ""):
        assert stock_state(stock(seen="2026-11-05", bron=bron), DELIVERY_DAY, TODAY) == "geen telling"
    assert stock_state(stock(seen="2026-11-05", bron="Telling app"), DELIVERY_DAY, TODAY) == "ok"
    assert stock_state(stock(seen="2026-11-05", status="bevestigd"), DELIVERY_DAY, TODAY) == "ok"
    spend, (go, reasons) = evaluate(inventory=[photo])
    assert go is False and spend["missing_stock"] == ["Kaas"]


def test_product_missing_from_voorraad_blocks_go():
    spend, (go, reasons) = evaluate(inventory=[stock("Melk")])
    assert go is False and spend["missing_inventory"] == ["Kaas"] and spend["grand_total"] is None
    assert any("niet in Voorraad" in r for r in reasons)


def test_missing_voorraad_tab_blocks_go():
    spend, (go, _) = evaluate(inventory=[])
    assert go is False and spend["missing_inventory"] == ["Kaas"]


def test_unknown_stock_blocks_even_if_formula_gives_number():
    spend, (go, _) = evaluate(inventory=[stock(count=0, status="Onbekend")])
    assert go is False and spend["missing_stock"] == ["Kaas"]
    blank = [line(listed="")]
    assert evaluate(rows=blank)[0]["missing_stock"] == ["Kaas"]


def test_negative_stock_edit_rejected():
    with pytest.raises(ValueError):
        delta_changes([{"_row": 2, "Aantal nu": 3}], [{"_row": 2, "Aantal nu": -1}], "Aantal nu")
    assert delta_changes([{"_row": 2, "Aantal nu": 3}], [{"_row": 2, "Aantal nu": float("nan")}], "Aantal nu") == [(2, None)]


def test_confirmations_write_even_unchanged_counts():
    rows = [{"_row": 3, "Product": "Kaas", "Aantal nu": 300}, {"_row": 4, "Product": "Melk", "Aantal nu": ""}]
    assert confirmations(rows, {3: 300, 4: None}) == [(3, "Kaas", 300.0, 300.0)]
    with pytest.raises(ValueError):
        confirmations(rows, {4: -2})


# --- delivery ----------------------------------------------------------------------------------

def test_verified_free_delivery_gives_go():
    spend, (go, reasons) = evaluate()
    assert go is True and spend["grand_total"] == 6.25 and "checkout €6.25 + €0.00 bezorging" in reasons[0]
    assert spend["expected_total"] == 6.25 and spend["lines"][0]["opmerking"] == "verwacht (bevestigd)"


@pytest.mark.parametrize("basket,go_expected", [("", False), ("abc", False), (-1, False), (0, True), ("€ 143,75", True)])
def test_go_needs_checkout_basket_total(basket, go_expected):
    spend, (go, reasons) = evaluate(delivery_settings=settings(**{"Mandtotaal checkout": basket}))
    assert go is go_expected
    if not go_expected:
        assert spend["grand_total"] is None and any("Mandtotaal" in r for r in reasons)


def test_slot_is_no_longer_required():
    spend, (go, _) = evaluate(delivery_settings=settings(Bezorgmoment=""))
    assert go is True and "slot" not in delivery_check(settings(), TODAY)


@pytest.mark.parametrize("amount", [0.01, 4.95, "€ 1,00"])
def test_paid_delivery_is_no_go(amount):
    spend, (go, reasons) = evaluate(delivery_settings=settings(**{"Werkelijke bezorgkosten": amount}))
    assert go is False and spend["grand_total"] is None
    assert any("exact €0,00" in r for r in reasons)


@pytest.mark.parametrize("over", [
    {"Bezorgdatum": "2026-11-04"},                      # stale €0: slot already passed
    {"Bezorgkosten gecontroleerd op": ""},              # unverified €0
    {"Bezorgkosten gecontroleerd op": "2026-11-07"},    # verified after delivery / in the future
    {"Bezorging leverancier": ""},
    {"Mandtotaal checkout": ""},
    {"Bezorgkosten bron": ""},
    {"Werkelijke bezorgkosten": ""},
])
def test_stale_or_incomplete_free_delivery_is_no_go(over):
    spend, (go, _) = evaluate(delivery_settings=settings(**over))
    assert go is False and spend["grand_total"] is None


def test_free_delivery_of_another_supplier_invalidates_prices():
    spend, (go, _) = evaluate(delivery_settings=settings(**{"Bezorging leverancier": "AH"}))
    assert spend["invalid_price"] == [("Kaas", "andere leverancier")] and spend["expected_total"] is None
    assert go is True            # GO rests on the AH checkout total, never on another supplier's line prices


def test_new_date_invalidates_normal_price_checked_for_other_date():
    spend, (go, _) = evaluate(delivery_settings=settings(Bezorgdatum="2026-11-07"))
    assert spend["invalid_price"] == [("Kaas", "verlopen")] and spend["expected_total"] is None


# --- prices ------------------------------------------------------------------------------------

def test_offer_valid_through_delivery_date():
    spend = evaluate(rows=[line(kind="Aanbieding", until="2026-11-10")])[0]
    assert spend["expected_total"] == 6.25 and spend["lines"][0]["opmerking"] == "verwacht (aanbieding)"


def test_expired_offer_before_delivery_is_no_go():
    spend, (go, _) = evaluate(rows=[line(kind="Aanbieding", until="2026-11-05")])
    assert spend["invalid_price"] == [("Kaas", "verlopen")] and spend["expected_total"] is None


@pytest.mark.parametrize("over,status", [
    ({"price": ""}, "ontbreekt"),
    ({"checked": ""}, "ongedateerd"),
    ({"checked": "2026-11-07"}, "ongedateerd"),
    ({"kind": ""}, "geldigheid onbekend"),
    ({"until": ""}, "geldigheid onbekend"),
    ({"supplier": "Lidl"}, "andere leverancier"),
])
def test_price_without_proof_is_not_trusted(over, status):
    item = line(**over)
    delivery = dict(delivery_check(settings(), TODAY), today=TODAY)
    assert price_status(item, delivery) == status
    assert evaluate(rows=[item])[0]["expected_total"] is None


def test_week_old_price_alone_is_not_enough():
    # checked 7 days before delivery for an earlier date -> too old and not for this delivery day
    spend, (go, _) = evaluate(rows=[line(checked="2026-10-30", until="2026-10-31")])
    assert spend["invalid_price"] == [("Kaas", "verouderd")] and spend["expected_total"] is None


def test_price_without_delivery_check():
    assert price_status(line(), {}) == "geen bezorgcontrole"


# --- budget / GO -------------------------------------------------------------------------------

def test_budget_hard_cap_and_limits():
    assert effective_budget(200) == MAX_BUDGET == 150.0
    assert effective_budget("€ 120,00") == 120.0
    assert effective_budget(None) is None and effective_budget(0) is None and effective_budget(-5) is None
    at = lambda v: settings(**{"Mandtotaal checkout": v})
    assert evaluate(delivery_settings=at(150.0), budget=200)[1][0] is True      # exactly 150.00 incl. €0 delivery
    spend, (go, reasons) = evaluate(delivery_settings=at(150.01), budget=200)
    assert go is False and "Budget overschreden" in reasons[0]
    spend, (go, reasons) = evaluate(delivery_settings=at(120.01), budget="€ 120,00")
    assert go is False and "€120.01 op €120.00" in reasons[0]
    assert evaluate(budget=None)[1][0] is False


@pytest.mark.parametrize("rows", [[], [line(wanted=0)]])
def test_empty_or_nothing_needed_never_gives_go(rows):
    spend, (go, reasons) = evaluate(rows=rows)
    assert go is False and spend["grand_total"] is None


def test_schema_problems_block_go():
    data = {"Voorraad": [["Product", "Aantal nu"]], "Aanvullijst": [], "Instellingen": [["Instelling", "Waarde"]]}
    problems = schema_problems(data)
    assert any("Voorraad" in p and "Zekerheid" in p for p in problems)
    assert any("Aanvullijst" in p for p in problems)
    assert any("Bezorgdatum" in p for p in problems)
    spend = evaluate()[0]
    assert go_decision(spend, 150.0, problems)[0] is False


def test_settings_map_and_column_letters():
    s = settings_map([["Instelling", "Waarde"], ["Weekbudget", 150], [], ["Werkelijke bezorgkosten", ""]])
    assert s["Weekbudget"] == {"value": 150, "row": 2} and s["Werkelijke bezorgkosten"]["row"] == 4
    assert [column_letter(i) for i in (0, 3, 11, 25, 26)] == ["A", "D", "L", "Z", "AA"]
    assert is_checked(True) and is_checked("TRUE") and not is_checked("")


# --- second-review regressions ---------------------------------------------------------------

def test_duplicate_voorraad_rows_must_all_be_current_and_are_summed():
    rows = [line(listed=300, goal=800)]
    two_ok = [stock(count=100), stock(count=200)]
    assert evaluate(rows=rows, inventory=two_ok)[1][0] is True
    one_photo = [stock(count=100), stock(count=200, status="Onbekend", bron="Foto koelkast")]
    spend, (go, _) = evaluate(rows=rows, inventory=one_photo)
    assert go is False and spend["missing_stock"] == ["Kaas"]


def test_unit_change_after_price_save_invalidates_price():
    item = line(unit="kg", pack="500 gram", wanted=1, listed=0)   # price saved as €/gram, row now in kg
    spend, (go, _) = evaluate(rows=[item], inventory=[stock(unit="kg")])
    assert spend["invalid_price"] == [("Kaas", "verpakking/eenheid onbekend")] and spend["expected_total"] is None


def test_normal_price_must_be_checked_for_exact_delivery_date_in_order_week():
    spend, (go, _) = evaluate(rows=[line(checked="2026-01-05", until="2026-12-31")])
    assert spend["invalid_price"] == [("Kaas", "verouderd")]
    spend, (go, _) = evaluate(rows=[line(checked="2026-11-05", until="2026-12-31")])   # normal, but not for 6 Nov
    assert spend["invalid_price"] == [("Kaas", "verlopen")]
    assert evaluate(rows=[line(kind="Aanbieding", checked="2026-11-05", until="2026-12-31")])[0]["expected_total"] == 6.25


def test_old_delivery_check_is_no_go_even_if_date_moved():
    spend, (go, reasons) = evaluate(delivery_settings=settings(**{"Bezorgkosten gecontroleerd op": "2026-01-02"}))
    assert go is False and any("ouder dan" in r for r in reasons)


@pytest.mark.parametrize("over,reason", [
    ({"listed": 400}, "loopt niet gelijk"),                     # stale E while count is 0
    ({"goal": 800, "wanted": 100}, "te weinig besteld"),
    ({"unit": "gram"}, None),
])
def test_order_quantity_checked_against_count(over, reason):
    spend, (go, _) = evaluate(rows=[line(**over)])
    if reason:
        assert go is False and reason in spend["stock_reasons"]["Kaas"]
    else:
        assert go is True


def test_unit_mismatch_between_voorraad_and_aanvullijst_is_no_go():
    spend, (go, _) = evaluate(inventory=[stock(unit="kg")])
    assert go is False and "eenheid" in spend["stock_reasons"]["Kaas"]


def test_whole_packs_and_cent_rounding():
    assert line_cost(line(wanted=750, pack="500 gram", price=0.0125)) == 12.50     # 2 packs of €6.25
    assert line_cost(line(wanted=500, pack="500 gram", price=0.0125)) == 6.25
    assert line_cost(line(wanted=1, pack="1 stuks", unit="stuks", price=75.002)) == 75.0
    spend, (go, _) = evaluate(rows=[line(wanted=750)])
    assert go is True and spend["expected_total"] == 12.50 and spend["grand_total"] == 6.25   # GO = checkout


def test_pack_parsing_and_normalisation():
    assert pack_size("400 gram", "gram") == 400 and pack_size("400 gram", "kg") is None
    assert pack_size("", "gram") is None and pack_size("0 gram", "gram") is None
    assert pack_to_row_unit_price(4.99, 400) == 0.012475
    assert normalised_price(0.012475, "gram") == (12.48, "kg")
    assert normalised_price(0.0012, "ml") == (1.2, "liter")
    assert normalised_price(0.89, "blikken") == (0.89, "blikken")
    with pytest.raises(ValueError):
        pack_to_row_unit_price(4.99, 0)


# --- verification-review regressions ---------------------------------------------------------

@pytest.mark.parametrize("goal", ["", "#REF!"])
def test_blank_or_error_weekdoel_is_no_go(goal):
    spend, (go, _) = evaluate(rows=[line(wanted=0, goal=goal)])
    assert go is False and spend["stock_reasons"]["Kaas"] == "weekdoel onbekend"


def test_large_pack_price_keeps_cents():
    per_unit = pack_to_row_unit_price(7.77, 25000)
    assert line_cost(line(wanted=25000, pack="25000 gram", price=per_unit)) == 7.77


def test_renamed_weekdoel_column_is_reported():
    data = {"Voorraad": [["Product", "Aantal nu", "Eenheid", "Zekerheid", "Laatst gezien", "Bron"]],
            "Aanvullijst": [["Product", "Eenheid", "Doel", "Voorraad nu", "Te bestellen", "Prijs/eenheid €",
                             "Prijsdatum", "In mand", "Prijs leverancier", "Prijssoort", "Geldig t/m", "Verpakking"]],
            "Instellingen": settings_rows()}
    assert any("Weekdoel" in p for p in schema_problems(data))


def settings_rows():
    return [["Instelling", "Waarde"]] + [[k, ""] for k in ("Weekbudget", "Werkelijke bezorgkosten",
            "Bezorgkosten gecontroleerd op", "Bezorging leverancier", "Bezorgdatum", "Bezorgkosten bron")]
