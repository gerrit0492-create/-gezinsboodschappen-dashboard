"""Open-data prices (Checkjebon): matching, sizes, measurement dates, indicative pricing and safe Sheet writes."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import open_prices as op  # noqa: E402
from core import (OPEN_DATA, compare_suppliers, delivery_check, latest_price, line_price, open_status,  # noqa: E402
                  settings_map, shopping_totals, supplier_rows, table_rows)
from sheet_store import ConflictError, write_open_prices, write_price  # noqa: E402
from test_sheet_store import PRIJZEN, FakeBook, FakeSheet, ranges, with_formula  # noqa: E402

TODAY = date(2026, 11, 5)
DAY = date(2026, 11, 6)
JUMBO = "https://www.jumbo.com/producten/"
AH = "https://www.ah.nl/producten/product/"
DATA = [
    {"n": "jumbo", "u": JUMBO, "d": [
        {"n": "Jumbo Jong Belegen Kaas Plakken", "l": "kaas-1", "p": 3.49, "s": "250 g"},
        {"n": "Old Amsterdam Kaas Plakken", "l": "kaas-2", "p": 4.29, "s": "200 g"},
        {"n": "Jumbo Kaas Plakken Jong", "l": "kaas-3", "p": 2.99, "s": "300 g"},
        {"n": "Jumbo Halfvolle Melk", "l": "melk-1", "p": 1.19, "s": "1 l"},
        {"n": "Jumbo Kidneybonen", "l": "bonen-1", "p": 0.79, "s": "400 g"},
        {"n": "Jumbo Tomatenblokjes 3 x 400 g", "l": "tom-3", "p": 1.89, "s": ""},
    ]},
    {"n": "ah", "u": AH, "d": [{"n": "AH Kaas plakken jong", "l": "wi1/ah-kaas", "p": 3.19, "s": "250 g"}]},
    {"n": "lidl", "u": "https://boodschaapje.nl/product/", "d": [{"n": "Kaas plakken", "l": "77", "p": 2.49, "s": ""}]},
    {"n": "spar", "u": "x", "d": []},
]


# --- matching and sizes -------------------------------------------------------------------------------

@pytest.mark.parametrize("size,name,expected", [
    ("250 g", "", (250, "g")), ("0,75 l", "", (750, "ml")), ("1 kg", "", (1000, "g")), ("6 stuks", "", (6, "st")),
    ("", "Tomatenblokjes 3 x 400 g", (1200, "g")), ("", "Kaas plakken", None), ("50 cl", "", (500, "ml")),
])
def test_parse_size(size, name, expected):
    assert op.parse_size(size, name) == expected


@pytest.mark.parametrize("size,name,unit,expected", [
    ("250 g", "", "gram", 250), ("250 g", "", "kg", 0.25), ("1 l", "", "liter", 1), ("1 l", "", "gram", None),
    ("6 stuks", "", "stuks", 6), ("400 g", "Kidneybonen", "blikken", 1), ("", "Tomatenblokjes 3 x 400 g", "blikken", 3),
    ("", "Kaas plakken", "gram", None), ("400 g", "", "", None),
])
def test_content_in_row_unit(size, name, unit, expected):
    assert op.content_in_row_unit(size, name, unit) == expected


def test_candidates_rank_house_brand_first_and_handle_alternatives():
    items = op.chain_items(DATA, "jumbo")
    found = op.candidates(items, "Jonge kaas plakken / smeerkaas")
    assert [a["l"] for a, _ in found] == ["kaas-3", "kaas-1"]          # 'jonge' needs all words of one alternative
    found = op.candidates(items, "kaas plakken")
    assert [h for _, h in found] == [True, True, False] and found[-1][0]["l"] == "kaas-2"
    assert op.candidates(items, "Griekse-stijlyoghurt") == [] and op.candidates(items, "") == []
    assert op.is_house_brand("lidl", "Kaas plakken") is None and op.chain_of("Albert Heijn") == "ah"


def test_house_alternatives_cheapest_per_unit_and_unknown_for_lidl():
    alts = op.house_alternatives(DATA, "jumbo", "kaas plakken", "gram")
    assert [a["url"] for a in alts] == [JUMBO + "kaas-3", JUMBO + "kaas-1"]   # €9.97/kg before €13.96/kg
    assert op.house_alternatives(DATA, "lidl", "kaas plakken", "gram") == []


# --- measurement date per chain -----------------------------------------------------------------------

def test_measured_dates_find_the_real_change_and_flag_unknown():
    commits = [(f"c{i}", date(2026, 11, 3).fromordinal(date(2026, 11, 3).toordinal() - i)) for i in range(40)]
    jumbo_changed_at, lidl_changed_at = 23, 1          # identical data in commits 0..22 / 0..0
    calls = []

    def fp(sha):
        i = int(sha[1:])
        calls.append(i)
        return {"jumbo": "J0" if i < jumbo_changed_at else f"J{i}", "lidl": "L0" if i < lidl_changed_at else f"L{i}",
                "ah": "A0"}
    dates = op.measured_dates(commits, fp)
    assert dates["jumbo"] == (commits[22][1], True)
    assert dates["lidl"] == (commits[0][1], True)
    assert dates["ah"] == (commits[39][1], False)      # identical in every listed commit: real date older/unknown
    assert len(set(calls)) < 20 and len(calls) == len(set(calls))   # galloping + bisect, each commit fetched once


def test_fingerprint_ignores_other_chains():
    a = op.fingerprint(DATA)
    changed = [dict(m, d=[]) if m["n"] == "spar" else m for m in DATA]
    assert op.fingerprint(changed) == a and set(a) == {"jumbo", "ah", "lidl"}


def test_fetch_commits_parses_amsterdam_dates_and_pages():
    pages = {1: [{"sha": f"s{i}", "commit": {"committer": {"date": "2026-10-10T23:30:00Z"}}} for i in range(100)],
             2: [{"sha": "last", "commit": {"committer": {"date": "2026-10-09T05:00:00Z"}}}]}

    def get(url, timeout=60):
        import json
        return json.dumps(pages[int(url.rsplit("=", 1)[1])]).encode()
    commits = op.fetch_commits(get)
    assert len(commits) == 101 and commits[0] == ("s0", date(2026, 10, 11)) and commits[-1][1] == date(2026, 10, 9)


# --- indicative pricing in the dashboard --------------------------------------------------------------

def open_row(product="Kaas", supplier="Jumbo", measured="2026-11-03", exact=True, pack="300 gram", per_unit=2.99 / 300,
             url=JUMBO + "kaas-3", row=2):
    return {"Product": product, "Leverancier": supplier, "Prijs verpakking €": 2.99, "Verpakking": pack,
            "Prijs/eenheid €": per_unit, "Prijssoort": OPEN_DATA, "Geldig t/m": measured if exact else "",
            "Prijsdatum": measured, "Bron": url, "_row": row}


def confirmed(kind="Normaal", until="2026-11-06", minimum=None, per_unit=0.0125):
    return {"Product": "Kaas", "Leverancier": "Jumbo", "Prijs/eenheid €": per_unit, "Verpakking": "500 gram",
            "Prijssoort": kind, "Geldig t/m": until, "Prijsdatum": "2026-11-05", "Bron": "webshop",
            "Min. aantal": minimum, "_row": 3}


ITEM = {"Product": "Kaas", "Eenheid": "gram", "Weekdoel": 500, "Voorraad nu": 0, "Te bestellen": 500}
DELIVERY = {"supplier": "Jumbo", "date": DAY, "ok": True, "amount": 0.0}


@pytest.mark.parametrize("over,state", [
    ({}, "actueel"), ({"measured": "2026-10-31"}, "actueel"), ({"measured": "2026-10-30"}, "verouderd"),
    ({"exact": False}, "verouderd"), ({"measured": "2026-11-06"}, "verouderd"), ({"pack": ""}, "inhoud onbekend"),
    ({"measured": "2026-10-11"}, "verouderd"),
])
def test_open_status_is_current_only_within_order_week_with_exact_date(over, state):
    assert open_status(dict(open_row(**over), Eenheid="gram"), DAY, TODAY) == state


def test_open_data_never_shadows_confirmed_prices():
    prices = [confirmed(), open_row()]                 # open row is newer but must not replace the confirmed one
    assert latest_price(prices, "Kaas", "Jumbo")["Prijssoort"] == "Normaal"
    assert latest_price([open_row()], "Kaas", "Jumbo") == {}


def test_line_price_precedence_offer_minimum_open_data():
    _, basis, _ = line_price(ITEM, [confirmed(), open_row()], "Jumbo", DELIVERY, TODAY)
    assert basis == "bevestigd"
    _, basis, _ = line_price(ITEM, [confirmed("Aanbieding", "2026-11-10"), open_row()], "Jumbo", DELIVERY, TODAY)
    assert basis == "aanbieding"
    item, basis, _ = line_price(ITEM, [confirmed("Aanbieding", "2026-11-10", minimum=2), open_row()], "Jumbo",
                                DELIVERY, TODAY)                # 1 pack needed, offer needs 2: offer does not apply
    assert basis == "open data" and item["Bron"] == JUMBO + "kaas-3"
    item, basis, note = line_price(ITEM, [confirmed("Aanbieding", "2026-11-10", minimum=2)], "Jumbo", DELIVERY, TODAY)
    assert item is None and note == "aanbieding pas vanaf 2 verpakkingen"
    item, basis, note = line_price(ITEM, [open_row(measured="2026-10-11")], "Jumbo", DELIVERY, TODAY)
    assert item is None and note == "open data verouderd"     # outdated open data is never used
    assert line_price(ITEM, [], "Jumbo", DELIVERY, TODAY)[2] == "geen prijs"


def test_expected_total_and_comparison_use_current_open_data_but_never_go():
    settings = settings_map([["Instelling", "Waarde"]] + [[k, v] for k, v in {
        "Weekbudget": 150, "Werkelijke bezorgkosten": 0, "Bezorgkosten gecontroleerd op": "2026-11-05",
        "Bezorging leverancier": "Jumbo", "Bezorgdatum": "2026-11-06", "Bezorgkosten bron": "checkout"}.items()])
    delivery = delivery_check(settings, TODAY)
    inv = [{"Product": "Kaas", "Aantal nu": 0, "Eenheid": "gram", "Zekerheid": "Bevestigd",
            "Laatst gezien": "2026-11-04", "Bron": "Gebruiker"}]
    spend = shopping_totals([ITEM], inv, delivery, TODAY, [open_row()])
    assert spend["expected_total"] == 5.98 and spend["expected_open_lines"] == 1     # 2 packs of 300 g
    assert spend["ready"] is False and spend["grand_total"] is None                 # no checkout total -> no GO
    sup = supplier_rows([["LEVERANCIERS", "Koopwijze", "Bezorgkosten gecontroleerd €", "Bezorging gecontroleerd op",
                          "Reis- en tijdkosten €"], ["Jumbo", "Thuisbezorgen", "", "", ""]])
    comp = compare_suppliers([ITEM], inv, [open_row()], sup, delivery, TODAY)
    assert comp[0]["complete"] and comp[0]["total"] == 5.98 and comp[0]["open_lines"] == 1
    old = compare_suppliers([ITEM], inv, [open_row(measured="2026-10-11")], sup, delivery, TODAY)
    assert old[0]["complete"] is False and old[0]["missing"] == [("Kaas", "open data verouderd")]


# --- refresh ------------------------------------------------------------------------------------------

def test_refresh_changes_updates_exactly_the_linked_article():
    links = [open_row(measured="2026-10-01"), open_row("Melk", url=JUMBO + "weg", row=3),
             open_row("Brood", "Plus", row=4)]
    shopping = [ITEM, {"Product": "Melk", "Eenheid": "liter"}]
    dates = {"jumbo": (date(2026, 10, 11), True)}
    changes, report = op.refresh_changes(links, DATA, dates, shopping, [])
    assert len(changes) == 1 and changes[0]["seen"] is links[0]
    v = changes[0]["values"]
    assert v["Prijs verpakking €"] == 2.99 and v["Verpakking"] == "300 gram" and v["Prijsdatum"] == "2026-10-11"
    assert v["Geldig t/m"] == "2026-10-11" and v["Bron"] == JUMBO + "kaas-3" and v["Prijssoort"] == OPEN_DATA
    assert any("Melk" in r and "niet meer" in r for r in report) and any("Brood" in r for r in report)
    same = dict(links[0], **{k: v[k] for k in v})
    assert op.refresh_changes([same], DATA, dates, shopping, [])[0] == []          # unchanged -> no write


def test_inexact_date_is_stored_without_valid_through():
    v = op.price_values(DATA[0]["d"][2], JUMBO + "kaas-3", "gram", date(2025, 6, 1), exact=False)
    assert v["Geldig t/m"] == "" and v["Prijsdatum"] == "2025-06-01"
    lidl = op.price_values(DATA[2]["d"][0], "u", "gram", date(2026, 11, 3))
    assert lidl["Verpakking"] == "" and lidl["Prijs/eenheid €"] == "" and lidl["Prijs verpakking €"] == 2.49


# --- Sheet writes ------------------------------------------------------------------------------------

GRID = PRIJZEN + [["Kaas", "Jumbo", 6.25, "500 gram", 0.0125, "Normaal", "2026-11-06", "2026-11-05", "webshop"],
                  ["Kaas", "Jumbo", 2.79, "300 gram", 0.0093, OPEN_DATA, "2026-10-01", "2026-10-01", JUMBO + "kaas-3"]]


def change(seen, **values):
    base = {"Prijs verpakking €": 2.99, "Verpakking": "300 gram", "Prijs/eenheid €": 0.00996, "Geldig t/m": "2026-10-11",
            "Prijsdatum": "2026-10-11", "Bron": JUMBO + "kaas-3"}
    return {"product": "Kaas", "supplier": "Jumbo", "values": dict(base, **values), "seen": seen}


def test_write_open_prices_updates_only_the_open_row():
    ws = FakeSheet(GRID)
    seen = table_rows(GRID)[1]
    assert write_open_prices(FakeBook(Prijzen=ws), [change(seen)]) == (1, 0)
    got = ranges(ws)
    assert all(r.endswith("3") for r in got) and got["C3"] == [2.99] and got["H3"] == ["2026-10-11"]
    assert not any(r.endswith("2") for r in got) and "A3" not in got and "B3" not in got
    assert ws.input_options == ["USER_ENTERED"]                     # one request; text stored literally


def test_write_open_prices_appends_new_link_and_refuses_stale_screen():
    ws = FakeSheet(GRID[:2])
    assert write_open_prices(FakeBook(Prijzen=ws), [change(None)]) == (0, 1)
    assert ranges(ws)["F3"] == [OPEN_DATA] and ranges(ws)["I3"] == [JUMBO + "kaas-3"]
    ws = FakeSheet(GRID)
    stale = dict(table_rows(GRID)[1], Bron=JUMBO + "kaas-1")      # another session re-linked meanwhile
    with pytest.raises(ConflictError):
        write_open_prices(FakeBook(Prijzen=ws), [change(stale)])
    with pytest.raises(ConflictError):                             # user saw no row, but one exists now
        write_open_prices(FakeBook(Prijzen=ws), [change(None)])
    with pytest.raises(ValueError):
        write_open_prices(FakeBook(Prijzen=ws), [change(table_rows(GRID)[1], Bron="")])
    assert ws.updates == [] and write_open_prices(FakeBook(), []) == (0, 0)


def test_write_open_prices_never_overwrites_formulas():
    ws = FakeSheet(GRID, formulas=with_formula(GRID, 3, 4, "=C3/300"))
    with pytest.raises(ConflictError, match="formule"):
        write_open_prices(FakeBook(Prijzen=ws), [change(table_rows(GRID)[1])])
    assert ws.updates == []


def test_manual_price_ignores_open_row_and_writes_min_quantity_only_when_column_exists():
    ws = FakeSheet(GRID)
    write_price(FakeBook(Prijzen=ws), "Kaas", "Jumbo", 5.99, 500, "gram", "Aanbieding", DAY, TODAY, "webshop",
                table_rows(GRID)[0])
    assert all(r.endswith("2") for r in ranges(ws))                 # confirmed row 2 only, open row 3 untouched
    with_min = [GRID[0] + ["Min. aantal"]] + [r + [""] for r in GRID[1:]]
    ws = FakeSheet(with_min)
    write_price(FakeBook(Prijzen=ws), "Kaas", "Jumbo", 5.99, 500, "gram", "Aanbieding", DAY, TODAY, "webshop",
                table_rows(with_min)[0], minimum=2)
    assert ranges(ws)["J2"] == [2]
    with pytest.raises(ValueError):
        write_price(FakeBook(Prijzen=FakeSheet(with_min)), "Kaas", "Jumbo", 5.99, 500, "gram", "Aanbieding", DAY,
                    TODAY, "webshop", table_rows(with_min)[0], minimum=0)
