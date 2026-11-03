"""Pure, testable calculations for the family grocery dashboard.

Missing stock and missing prices are unknown, *not* zero. No service calls here.

GO rule (all must hold, otherwise NO-GO):
- a verified delivery check: supplier, delivery date (today or later), source and
  verification date, with delivery costs of exactly EUR 0.00, plus the basket total
  read from that supplier's checkout (the checkout applies offers and minimum quantities);
- every product to order exists in Voorraad; every Voorraad row of it holds a
  confirmed physical count from the order week (a confirmed 0 is fine;
  blank/unknown/missing/photo never counts), in the same unit as Aanvullijst,
  matching 'Voorraad nu', and 'Te bestellen' covers Weekdoel - count;
- checkout basket total + delivery costs is within min(Weekbudget, EUR 150.00).

Line prices only give the EXPECTED amount and the supplier comparison (indicative): a confirmed price valid on the
delivery date (normal price checked for exactly that date, or an offer running until at least then and whose
minimum quantity is met) first, else a current open-data price (measured within the order week). Outdated or
undated prices are never used.

"Order week" = the 6 days before the delivery date up to today (CHECK_WINDOW_DAYS).
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_BUDGET = 150.00               # household rule: never more than EUR 150 incl. delivery
CHECK_WINDOW_DAYS = 6             # counts, prices and the delivery check must date from the 6 days before delivery
STOCK_WINDOW_DAYS = CHECK_WINDOW_DAYS
CONFIRMED = "Bevestigd"
UNKNOWN = "Onbekend"
APP_COUNT = "Telling app"         # Voorraad 'Bron' for a physical count confirmed in the app
PHYSICAL_SOURCES = {"gebruiker", APP_COUNT.casefold()}   # only these 'Bron' values are real counts
PRICE_NORMAL = "Normaal"
PRICE_OFFER = "Aanbieding"
OPEN_DATA = "Open data"           # Prijssoort of an imported open-data price: indicative, never confirmed

# Sheet schema the app relies on. The app never adds columns or rows itself.
REQUIRED_HEADERS = {
    "Voorraad": ["Product", "Locatie", "Aantal nu", "Eenheid", "Zekerheid", "Laatst gezien", "Bron",
                 "Houdbaar tot", "Status", "EAN"],
    "Aanvullijst": ["Product", "Eenheid", "Weekdoel", "Voorraad nu", "Te bestellen", "In mand"],
    # one row per product x supplier; the only source of prices (Aanvullijst M-P are not used: they carry
    # strict checkbox validation in a typical Sheet layout, which rejects text written through the API)
    "Prijzen": ["Product", "Leverancier", "Prijs verpakking €", "Verpakking", "Prijs/eenheid €", "Prijssoort",
                "Geldig t/m", "Prijsdatum", "Bron"],
    "Leveranciers": ["LEVERANCIERS", "Koopwijze", "Bezorgkosten gecontroleerd €", "Bezorging gecontroleerd op",
                     "Reis- en tijdkosten €"],
    "Bonnen": ["Besteldatum", "Leverancier", "Product", "Aantal", "Eenheid", "Betaald €", "Bestelnummer", "Status",
               "Verwerkt op"],
}
ARCHIVED = "Gearchiveerd"
MIN_TRIP_SAVING = 10.00           # household rule: a separate shop trip only for >= EUR 10 net saving
EXPIRY_WARN_DAYS = 3
DELIVERY_KEYS = {
    "amount": "Werkelijke bezorgkosten",
    "verified": "Bezorgkosten gecontroleerd op",
    "supplier": "Bezorging leverancier",
    "date": "Bezorgdatum",
    "source": "Bezorgkosten bron",
    "basket": "Mandtotaal checkout",
    "basket_list": "Mandtotaal voor lijst",
}
# write_delivery adds these key rows itself when missing. 'Mandtotaal voor lijst' = fingerprint of the order lines the
# checkout total was read for: a changed list makes the stored checkout total stale.
ADDABLE_SETTINGS = {"Mandtotaal checkout", "Mandtotaal voor lijst"}
REQUIRED_SETTINGS = ["Weekbudget"] + [k for k in DELIVERY_KEYS.values() if k not in ADDABLE_SETTINGS]

# Row unit in the Sheet -> (unit the user types a price in, factor).
# price_per_row_unit = price_per_entry_unit * factor  (e.g. €/kg * 0.001 = €/gram).
# Any unit not listed here (stuks, bakjes, blikken, broden, ...) is priced per that unit.
_UNIT_CONVERSIONS = {
    "gram": ("kg", 0.001), "g": ("kg", 0.001), "gr": ("kg", 0.001),
    "kg": ("kg", 1.0), "kilo": ("kg", 1.0),
    "ml": ("liter", 0.001), "cl": ("liter", 0.01),
    "liter": ("liter", 1.0), "l": ("liter", 1.0),
    "stuks": ("stuk", 1.0), "stuk": ("stuk", 1.0), "st": ("stuk", 1.0),
}


def number(value):
    """Parse a numeric cell, returning None for blanks/unreadable values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    raw = str(value).strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        result = float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None
    return result if math.isfinite(result) else None


def cents(value):
    """Round money half-up to 2 decimals (float round() turns 12.475 into 12.47)."""
    return float(Decimal(repr(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_date(value):
    """Parse an ISO/Dutch date string, a date, or a Google Sheets serial number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        return date(1899, 12, 30) + timedelta(days=int(value))
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def text(value):
    return str(value if value is not None else "").strip()


def same_name(a, b):
    return text(a).casefold() == text(b).casefold() and text(a) != ""


def price_entry_unit(row_unit):
    """The unit a person types a price in for this Sheet row unit (€/kg, €/liter, €/stuk, ...)."""
    unit = text(row_unit).lower()
    return _UNIT_CONVERSIONS.get(unit, (unit, 1.0))[0] or None


def to_row_unit_price(entered_price, row_unit):
    """Convert a price per entry unit (€/kg, €/liter, €/stuk) to €/row unit for Aanvullijst G."""
    price = number(entered_price)
    if price is None or price <= 0:
        raise ValueError("Prijs moet een positief bedrag zijn")
    unit = text(row_unit).lower()
    if not unit:
        raise ValueError("Eenheid ontbreekt in de Sheet; prijs kan niet worden omgerekend")
    factor = _UNIT_CONVERSIONS.get(unit, (unit, 1.0))[1]
    return round(price * factor, 6)


def pack_size(value, row_unit):
    """'400 gram' -> 400.0 when the unit matches the row unit; None if missing/other unit/invalid."""
    parts = text(value).split(None, 1)
    if len(parts) != 2:
        return None
    size = number(parts[0])
    if size is None or size <= 0 or parts[1].strip().lower() != text(row_unit).lower():
        return None
    return size


def pack_to_row_unit_price(pack_price, size):
    """Price per pack and pack content (in the row unit) -> € per row unit for Aanvullijst G."""
    price, content = number(pack_price), number(size)
    if price is None or price <= 0:
        raise ValueError("Prijs moet een positief bedrag zijn")
    if content is None or content <= 0:
        raise ValueError("Inhoud van de verpakking moet groter dan nul zijn")
    return round(price / content, 10)


def normalised_price(price_per_row_unit, row_unit):
    """€ per row unit -> (price, unit) in €/kg, €/liter or €/stuk for comparing; None if unknown."""
    value = number(price_per_row_unit)
    unit = text(row_unit).lower()
    if value is None or not unit:
        return None
    entry, factor = _UNIT_CONVERSIONS.get(unit, (unit, 1.0))
    return float((Decimal(repr(value)) / Decimal(repr(factor))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), entry


def valid_amount(value, allow_zero=True):
    """Validate a money amount entered in the app; returns it on 2 decimals or raises ValueError."""
    amount = number(value)
    if amount is None:
        raise ValueError("Bedrag ontbreekt of is ongeldig")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError("Bedrag mag niet negatief zijn" if amount < 0 else "Bedrag moet groter dan nul zijn")
    return round(amount, 2)


def table_rows(grid):
    """Return dicts with stable _row indices for writing back into Sheets."""
    if not grid:
        return []
    headers = [str(c).strip() for c in grid[0]]
    out = []
    for row_num, values in enumerate(grid[1:], start=2):
        padded = list(values) + [None] * max(0, len(headers) - len(values))
        item = {h: padded[i] for i, h in enumerate(headers) if h}
        if str(item.get(headers[0], "") or "").strip():
            item["_row"] = row_num
            out.append(item)
    return out


def settings_map(grid):
    """Instellingen as {key: {"value": B, "row": n}}; keys are column A."""
    out = {}
    for row_num, values in enumerate((grid or [])[1:], start=2):
        if values and str(values[0]).strip():
            out[str(values[0]).strip()] = {"value": values[1] if len(values) > 1 else None, "row": row_num}
    return out


def schema_problems(data):
    """Human-readable list of Sheet set-up gaps that block GO (never fixed automatically)."""
    problems = []
    for tab, needed in REQUIRED_HEADERS.items():
        grid = data.get(tab) or []
        if not grid:
            problems.append(f"Tab '{tab}' ontbreekt of is leeg")
            continue
        headers = [text(h) for h in grid[0]]
        missing = [h for h in needed if h not in headers]
        if missing:
            problems.append(f"Tab '{tab}' mist kolom(men): {', '.join(missing)}")
    keys = settings_map(data.get("Instellingen"))
    missing_keys = [k for k in REQUIRED_SETTINGS if k not in keys]
    if missing_keys:
        problems.append(f"Tab 'Instellingen' mist regel(s) in kolom A: {', '.join(missing_keys)}")
    return problems


def is_checked(value):
    return value is True or str(value).strip().lower() in {"true", "waar", "1"}


def is_uncertain(item):
    """Stock is uncertain when the count is blank or not explicitly confirmed."""
    if number(item.get("Aantal nu")) is None:
        return True
    return text(item.get("Zekerheid")).casefold() != CONFIRMED.casefold()  # only an explicit confirmation counts


def stock_state(item, reference, today):
    """'ok' only for a confirmed count seen in the order week; otherwise why it needs a recount.

    reference = delivery date (or today without a delivery check). Only a 'Bron' from
    PHYSICAL_SOURCES is a physical count; photo/recognition/other sources never are.
    """
    if is_uncertain(item):
        return "onbekend"
    seen = parse_date(item.get("Laatst gezien"))
    if seen is None or seen > today or seen < reference - timedelta(days=STOCK_WINDOW_DAYS):
        return "verouderd"
    if text(item.get("Bron")).casefold() not in PHYSICAL_SOURCES:
        return "geen telling"
    return "ok"


def classify_inventory(rows, reference=None, today=None):
    today = today or date.today()
    reference = reference or today
    known = unknown = low = 0
    for item in rows:
        if stock_state(item, reference, today) != "ok":
            unknown += 1
            continue
        known += 1
        minimum = number(item.get("Minimum"))
        if minimum is not None and number(item.get("Aantal nu")) < minimum:
            low += 1
    return {"known": known, "unknown": unknown, "low": low, "total": len(rows)}


def delivery_check(settings, today):
    """Validate the stored delivery check. Returns dict with ok, problems and the parsed fields."""
    get = lambda k: (settings.get(DELIVERY_KEYS[k]) or {}).get("value")
    amount, verified = number(get("amount")), parse_date(get("verified"))
    when, supplier, source = parse_date(get("date")), text(get("supplier")), text(get("source"))
    basket, basket_list = number(get("basket")), text(get("basket_list"))
    problems = []
    if not supplier:
        problems.append("leverancier ontbreekt")
    if when is None:
        problems.append("bezorgdatum ontbreekt")
    elif when < today:
        problems.append(f"bezorgdatum {when:%d-%m-%Y} is voorbij")
    if not source:
        problems.append("bron van de controle ontbreekt")
    if verified is None:
        problems.append("controledatum ontbreekt")
    elif verified > today or (when is not None and verified > when):
        problems.append("controledatum ligt in de toekomst of na de bezorgdatum")
    elif when is not None and verified < when - timedelta(days=CHECK_WINDOW_DAYS):
        problems.append(f"controle is ouder dan {CHECK_WINDOW_DAYS} dagen vóór de bezorgdatum; opnieuw controleren")
    if amount is None or amount < 0:
        problems.append("bezorgkosten ontbreken")
    elif round(amount, 2) != 0:
        problems.append(f"bezorgkosten €{amount:.2f}; GO vereist exact €0,00")
    basket_problem = None if basket is not None and basket >= 0 else "mandtotaal uit de checkout ontbreekt"
    return {"ok": not problems, "problems": problems, "amount": None if amount is None else round(amount, 2),
            "verified": verified, "date": when, "supplier": supplier, "source": source,
            "basket": None if basket_problem else round(basket, 2), "basket_problem": basket_problem,
            "basket_list": basket_list}


def price_status(item, delivery):
    """'ok' only for a price from the delivery supplier, for the current unit/pack, valid on the delivery date."""
    price = number(item.get("Prijs/eenheid €"))
    if price is None or price <= 0:
        return "ontbreekt"
    if not delivery or not delivery.get("supplier") or not delivery.get("date"):
        return "geen bezorgcontrole"
    if not same_name(item.get("Prijs leverancier"), delivery["supplier"]):
        return "andere leverancier"
    if pack_size(item.get("Verpakking"), item.get("Eenheid")) is None:
        return "verpakking/eenheid onbekend"
    checked, until = parse_date(item.get("Prijsdatum")), parse_date(item.get("Geldig t/m"))
    if checked is None or checked > delivery["date"] or (delivery.get("today") and checked > delivery["today"]):
        return "ongedateerd"
    if checked < delivery["date"] - timedelta(days=CHECK_WINDOW_DAYS):
        return "verouderd"
    kind = text(item.get("Prijssoort"))
    if kind not in (PRICE_NORMAL, PRICE_OFFER) or until is None:
        return "geldigheid onbekend"
    if until < delivery["date"] or (kind == PRICE_NORMAL and until != delivery["date"]):
        return "verlopen"   # a normal price is only proven for the exact delivery date it was checked for
    return "ok"


def line_cost(item):
    """Cost of a line in whole packs, rounded to cents: ceil(F / pack) * round(pack * G, 2)."""
    wanted, size = number(item.get("Te bestellen")), pack_size(item.get("Verpakking"), item.get("Eenheid"))
    per_unit = number(item.get("Prijs/eenheid €"))
    if wanted is None or size is None or per_unit is None:
        return None
    packs = math.ceil(round(wanted / size, 9))
    return cents(packs * cents(Decimal(repr(per_unit)) * Decimal(repr(size))))


def stock_problem(item, rows, reference, today):
    """Why the Aanvullijst quantity for `item` cannot be trusted, or None.

    rows = all Voorraad rows for this product (several Locaties are summed; each must be current).
    """
    if not rows:
        return "staat niet in Voorraad"
    states = {stock_state(r, reference, today) for r in rows}
    if states != {"ok"}:
        return "telling " + "/".join(sorted(states - {"ok"}))
    unit = text(item.get("Eenheid")).lower()
    if any(text(r.get("Eenheid")).lower() != unit for r in rows):
        return "eenheid in Voorraad wijkt af van Aanvullijst"
    counted = sum(number(r.get("Aantal nu")) for r in rows)
    wanted = number(item.get("Te bestellen"))
    if wanted is None or wanted < 0:
        return "te bestellen onbekend"
    if "Voorraad nu" in item:
        listed = number(item.get("Voorraad nu"))
        if listed is None or abs(listed - counted) > 1e-9:
            return "'Voorraad nu' loopt niet gelijk met de telling"
    goal = number(item.get("Weekdoel"))
    if "Weekdoel" in item and goal is None:
        return "weekdoel onbekend"
    if goal is not None and wanted + 1e-9 < max(goal - counted, 0):
        return f"te weinig besteld: weekdoel {goal:g} - telling {counted:g} > {wanted:g}"
    return None


def is_active(item):
    return text(item.get("Status")).casefold() != ARCHIVED.casefold()


def price_kind(row):
    """Canonical Prijssoort ('Normaal', 'Aanbieding', 'Open data') compared case-insensitively; else the raw text."""
    raw = text((row or {}).get("Prijssoort"))
    return next((k for k in (PRICE_NORMAL, PRICE_OFFER, OPEN_DATA) if k.casefold() == raw.casefold()), raw)


def is_open_data(row):
    return price_kind(row) == OPEN_DATA


def confirmed_row(prices, product, supplier):
    """The single non-open-data Prijzen row for product + supplier as the writer finds it (None if none/several)."""
    rows = [p for p in prices or [] if not is_open_data(p)
            and same_name(p.get("Product"), product) and same_name(p.get("Leverancier"), supplier)]
    return rows[0] if len(rows) == 1 else None


def order_fingerprint(rows):
    """Short fingerprint of the order lines (product + Te bestellen > 0) a checkout total was read for."""
    lines = sorted((text(r.get("Product")).casefold(), round(number(r.get("Te bestellen")) or 0, 6)) for r in rows or []
                   if text(r.get("Product")) and (number(r.get("Te bestellen")) or 0) > 0)
    return hashlib.sha256(repr(lines).encode("utf-8")).hexdigest()[:12]


def latest_price(prices, product, supplier, kinds=(PRICE_NORMAL, PRICE_OFFER)):
    """Most recently checked Prijzen row of one of `kinds` for product + supplier, mapped to the keys price_status
    uses. Default: confirmed kinds only, so an imported open-data row never shadows a confirmed price or offer."""
    best = None
    for p in prices or []:
        if price_kind(p) not in kinds:
            continue
        if same_name(p.get("Product"), product) and same_name(p.get("Leverancier"), supplier):
            if best is None or (parse_date(p.get("Prijsdatum")) or date.min) > (parse_date(best.get("Prijsdatum")) or date.min):
                best = p
    if best is None:
        return {}
    return {"Prijs/eenheid €": best.get("Prijs/eenheid €"), "Prijs leverancier": best.get("Leverancier"),
            "Prijssoort": price_kind(best), "Geldig t/m": best.get("Geldig t/m"),
            "Prijsdatum": best.get("Prijsdatum"), "Verpakking": best.get("Verpakking"),
            "Bron": best.get("Bron"), "Prijs verpakking €": best.get("Prijs verpakking €"),
            "Min. aantal": best.get("Min. aantal"), "_price_row": best.get("_row")}


_PRICE_FIELDS = ("Prijs/eenheid €", "Prijs leverancier", "Prijssoort", "Geldig t/m", "Prijsdatum", "Verpakking",
                 "Bron", "Prijs verpakking €", "Min. aantal")


def priced_line(item, prices, supplier, kinds=(PRICE_NORMAL, PRICE_OFFER)):
    """Aanvullijst line with the price fields of `supplier` from the Prijzen tab (price fields cleared if none)."""
    merged = {k: v for k, v in item.items() if k not in _PRICE_FIELDS}
    merged.update(latest_price(prices, item.get("Product"), supplier, kinds))
    return merged


def packs_needed(item):
    wanted, size = number(item.get("Te bestellen")), pack_size(item.get("Verpakking"), item.get("Eenheid"))
    if wanted is None or size is None:
        return None
    return math.ceil(round(wanted / size, 9))


def open_status(item, reference, today):
    """'actueel' only for an open-data price with an exact measurement date ('Geldig t/m' == 'Prijsdatum') within
    CHECK_WINDOW_DAYS before `reference` (the delivery date) and a known pack content; else why not."""
    if not item or not text(item.get("Prijssoort")):
        return "niet gekoppeld"
    measured, exact = parse_date(item.get("Prijsdatum")), parse_date(item.get("Geldig t/m"))
    if measured is None or exact != measured or measured > today or measured < reference - timedelta(days=CHECK_WINDOW_DAYS):
        return "verouderd"
    if pack_size(item.get("Verpakking"), item.get("Eenheid")) is None or not number(item.get("Prijs/eenheid €")):
        return "inhoud onbekend"
    return "actueel"


def line_price(item, prices, supplier, delivery, today):
    """(priced item, basis, note) for one line at `supplier`. basis: 'bevestigd', 'aanbieding' or 'open data';
    (None, None, why) when no usable price. prices=None: use the price fields already on the line."""
    d = dict(delivery or {}, supplier=supplier, today=today)
    confirmed = item if prices is None else priced_line(item, prices, supplier)
    status = price_status(confirmed, d)
    note = status
    if status == "ok":
        minimum, packs = number(confirmed.get("Min. aantal")), packs_needed(confirmed)
        offer = text(confirmed.get("Prijssoort")) == PRICE_OFFER
        if not (offer and minimum and (packs is None or packs < minimum)):
            return confirmed, "aanbieding" if offer else "bevestigd", ""
        note = f"aanbieding pas vanaf {minimum:g} verpakkingen"
    if prices is None:
        return None, None, note
    reference = d.get("date") or today
    open_item = priced_line(item, prices, supplier, kinds=(OPEN_DATA,))
    state = open_status(open_item, reference, today)
    if state == "actueel":
        return open_item, "open data", ""
    if status == "ontbreekt":
        note = "geen prijs" if state == "niet gekoppeld" else f"open data {state}"
    elif state != "niet gekoppeld":
        note += f"; open data {state}"
    return None, None, note


def shopping_totals(rows, inventory, delivery, today, prices=None):
    """Stock readiness, the EXPECTED amount (indicative line prices) and the checkout-based GO total.

    F = quantity to buy (row unit), G = price per *row unit*. H is a Sheet formula; we recalculate (in whole
    packs) rather than trust it. A number in F is not enough: F may be computed from unknown or old stock.
    prices: Prijzen rows; when given, each line is priced via line_price for the delivery supplier.
    The GO total is the checkout basket total + the confirmed delivery costs, never a sum of line prices.
    Archived Voorraad products are not ordered and are listed in 'archived'.
    """
    delivery = dict(delivery or {}, today=today)
    reference = delivery.get("date") or today
    stock, archived_names = {}, set()
    for x in inventory or []:
        if is_active(x):
            stock.setdefault(text(x.get("Product")).casefold(), []).append(x)
        else:
            archived_names.add(text(x.get("Product")).casefold())
    archived, lines = [], []
    missing_stock, missing_inventory, missing_price, invalid_price = [], [], [], []
    stock_reasons = {}
    total, open_lines = 0.0, 0
    ready_count = 0
    for item in rows:
        name = text(item.get("Product"))
        if not name:
            continue
        if name.casefold() in archived_names and name.casefold() not in stock:
            archived.append(name)
            continue
        problem = stock_problem(item, stock.get(name.casefold(), []), reference, today)
        line = {"product": name, "aantal": number(item.get("Te bestellen")), "eenheid": text(item.get("Eenheid")),
                "bedrag": None, "opmerking": problem or ""}
        if problem == "staat niet in Voorraad":
            missing_inventory.append(name)
            lines.append(line)
            continue
        if problem:
            missing_stock.append(name)
            stock_reasons[name] = problem
            lines.append(line)
            continue
        if number(item.get("Te bestellen")) == 0:
            continue
        ready_count += 1
        priced, basis, note = line_price(item, prices, delivery.get("supplier"), delivery, today)
        lines.append(line)
        if priced is not None:
            line["bedrag"] = line_cost(priced)
            line["opmerking"] = f"verwacht ({basis})"
            total += line["bedrag"]
            open_lines += basis == "open data"
        elif note in ("geen prijs", "ontbreekt"):
            missing_price.append(name)
            line["opmerking"] = "geen prijs"
        else:
            invalid_price.append((name, note))
            line["opmerking"] = note
    stock_ok = bool(rows) and not (missing_stock or missing_inventory)
    delivery_ok = bool(delivery.get("ok"))
    basket = delivery.get("basket") if delivery_ok else None
    basket_problem = delivery.get("basket_problem") if delivery_ok else None
    if basket is not None and ready_count > 0 and basket <= 0:
        basket, basket_problem = None, "mandtotaal €0,00 terwijl er te bestellen regels zijn"
    elif basket is not None and delivery.get("basket_list") != order_fingerprint(rows):
        basket, basket_problem = None, "aanvullijst gewijzigd na de checkoutcontrole; mandtotaal opnieuw invullen"
    ready = stock_ok and ready_count > 0 and delivery_ok and basket is not None
    priced_all = stock_ok and not (missing_price or invalid_price)
    return {
        "ready": ready,
        "expected_total": cents(total) if priced_all else None,
        "expected_open_lines": open_lines,
        "basket_total": basket,
        "delivery_total": delivery.get("amount") if delivery_ok else None,
        "grand_total": cents(basket + delivery["amount"]) if ready else None,
        "missing_stock": missing_stock,
        "stock_reasons": stock_reasons,
        "missing_inventory": missing_inventory,
        "missing_price": missing_price,
        "invalid_price": invalid_price,
        "delivery_problems": list(delivery.get("problems") or []),
        "basket_problem": basket_problem,
        "needed_lines": ready_count,
        "archived": archived,
        "lines": lines,
    }


def effective_budget(weekbudget):
    """min(Weekbudget from the Sheet, EUR 150); None when the Sheet value is missing/invalid."""
    value = number(weekbudget)
    if value is None or value <= 0:
        return None
    return round(min(value, MAX_BUDGET), 2)


def go_decision(spend, budget, schema=None):
    """(go, reasons). GO only with a complete, verified total within budget; reasons explain any NO-GO."""
    reasons = list(schema or [])
    if budget is None:
        reasons.append("Weekbudget ontbreekt of is ongeldig in Instellingen")
    if spend["delivery_problems"]:
        reasons.append("Bezorgcontrole: " + "; ".join(spend["delivery_problems"]))
    if spend["missing_inventory"]:
        reasons.append(f"{len(spend['missing_inventory'])} product(en) staan niet in Voorraad: "
                       + ", ".join(spend["missing_inventory"]))
    if spend["missing_stock"]:
        reasons.append(f"{len(spend['missing_stock'])} voorraadtelling(en) niet bruikbaar: " + "; ".join(
            f"{n} ({spend['stock_reasons'].get(n, 'onbekend')})" for n in spend["missing_stock"]))
    if spend.get("basket_problem"):
        reasons.append(f"Checkout: {spend['basket_problem']} (Bestellen → Bezorging en checkout)")
    if not reasons and spend["needed_lines"] == 0:
        reasons.append("Geen te bestellen regels gevonden (lege of ontbrekende Aanvullijst)")
    if not reasons and spend["grand_total"] is not None and spend["grand_total"] > budget:
        reasons.append(f"Budget overschreden: €{spend['grand_total']:.2f} op €{budget:.2f}")
    if reasons or not spend["ready"]:
        return False, reasons or ["Controles nog open"]
    return True, [f"Binnen budget: €{spend['grand_total']:.2f} van €{budget:.2f} (checkout €{spend['basket_total']:.2f}"
                  f" + €{spend['delivery_total']:.2f} bezorging)"]


def delta_changes(old, new, key):
    """Detect edits in an indexed table; avoid writing unmodified cells."""
    previous = {int(x["_row"]): x for x in old}
    changes = []
    for item in new:
        row_id = int(item["_row"])
        if row_id not in previous:
            continue
        before = previous[row_id].get(key)
        after = item.get(key)
        if number(before) == number(after):
            continue
        if number(after) is not None and number(after) < 0:
            raise ValueError("Voorraad mag niet negatief zijn")
        changes.append((row_id, number(after)))
    return changes


def confirmations(rows, entered):
    """Recount flow: every filled-in count is a confirmation, even if equal to the old value.

    `entered` maps sheet row -> number or None (blank = still unknown, nothing written).
    Returns [(row, product, old_count, new_count)] for a guarded write.
    """
    by_row = {int(x["_row"]): x for x in rows}
    out = []
    for row_id, value in entered.items():
        count = number(value)
        if count is None or int(row_id) not in by_row:
            continue
        if count < 0:
            raise ValueError("Voorraad mag niet negatief zijn")
        item = by_row[int(row_id)]
        out.append((int(row_id), str(item["Product"]), number(item.get("Aantal nu")), count))
    return out


def column_letter(index):
    """0-based column index -> A1 letter (0 -> A, 25 -> Z, 26 -> AA)."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# --- dashboard helpers --------------------------------------------------------------------------

def low_stock(inventory, reference, today):
    """Products with a current confirmed count below 'Minimum' (bijna op), active rows only."""
    out = []
    for x in inventory or []:
        if not is_active(x) or stock_state(x, reference, today) != "ok":
            continue
        minimum, count = number(x.get("Minimum")), number(x.get("Aantal nu"))
        if minimum is not None and count is not None and count < minimum:
            out.append(x)
    return out


def expiring(inventory, today, days=EXPIRY_WARN_DAYS):
    """(item, date) for active products whose 'Houdbaar tot' is past or within `days`, soonest first."""
    out = []
    for x in inventory or []:
        until = parse_date(x.get("Houdbaar tot"))
        if is_active(x) and until is not None and until <= today + timedelta(days=days):
            out.append((x, until))
    return sorted(out, key=lambda t: t[1])


def supplier_delivery(supplier_row, delivery, target, today):
    """(amount, why) delivery cost for a supplier: the verified checkout for the chosen supplier, the
    Leveranciers check (in the order week) for others, 0 for pick-up; None when not proven."""
    name = text(supplier_row.get("LEVERANCIERS"))
    if "zelf" in text(supplier_row.get("Koopwijze")).lower():
        return 0.0, "zelf halen"
    if delivery and same_name(delivery.get("supplier"), name):
        return (delivery["amount"], "checkout") if delivery.get("ok") else (None, "bezorgcontrole ongeldig")
    amount = number(supplier_row.get("Bezorgkosten gecontroleerd €"))
    checked = parse_date(supplier_row.get("Bezorging gecontroleerd op"))
    if amount is None or amount < 0 or checked is None or target is None:
        return None, "bezorgkosten niet gecontroleerd"
    if checked > today or checked < target - timedelta(days=CHECK_WINDOW_DAYS):
        return None, "bezorgcontrole te oud"
    return round(amount, 2), "gecontroleerd"


def compare_suppliers(rows, inventory, prices, suppliers, delivery, today):
    """Total cost per supplier for the same needed lines (whole packs), only when complete and proven.

    total = groceries + delivery + travel/time ('Reis- en tijdkosten €', required for pick-up suppliers).
    Lines whose stock is not reliable make every comparison incomplete (same scope for everyone).
    """
    delivery = dict(delivery or {}, today=today)
    target = delivery.get("date")
    base = shopping_totals(rows, inventory, dict(delivery, ok=True, amount=0.0, problems=[]), today, prices=[])
    stock_blocked = bool(base["missing_stock"] or base["missing_inventory"])
    needed = [x for x in rows if text(x.get("Product")) and number(x.get("Te bestellen"))
              and text(x.get("Product")) not in base["archived"]]
    results = []
    for sup in suppliers or []:
        name = text(sup.get("LEVERANCIERS"))
        if not name:
            continue
        missing, groceries, open_lines = [], 0.0, 0
        for item in needed:
            line, basis, note = line_price(item, prices, name, dict(delivery, date=target), today)
            if line is not None:
                groceries += line_cost(line)
                open_lines += basis == "open data"
            else:
                missing.append((text(item.get("Product")), note))
        ship, ship_why = supplier_delivery(sup, delivery, target, today)
        extra = number(sup.get("Reis- en tijdkosten €"))
        pickup = "zelf" in text(sup.get("Koopwijze")).lower()
        problems = [f"{len(missing)} prijs/prijzen ontbreken of zijn verouderd"] if missing else []
        if stock_blocked:
            problems.append("voorraad nog niet betrouwbaar")
        if not needed:
            problems.append("geen te bestellen regels")
        if ship is None:
            problems.append(ship_why)
        if pickup and extra is None:
            problems.append("reis- en tijdkosten onbekend")
        complete = not problems
        total = cents(groceries + (ship or 0) + (extra or 0)) if complete else None
        results.append({"supplier": name, "pickup": pickup, "groceries": cents(groceries) if not missing else None,
                        "delivery": ship, "extra": extra or 0.0, "total": total, "complete": complete,
                        "problems": problems, "missing": missing, "open_lines": open_lines})
    return results


def savings(comparison, chosen):
    """Saving of `chosen` versus every other complete alternative; pick-up only counts from EUR 10 net."""
    mine = next((r for r in comparison if same_name(r["supplier"], chosen)), None)
    out = []
    for r in comparison:
        if mine is None or r is mine:
            continue
        if not (mine["complete"] and r["complete"]):
            out.append({"supplier": r["supplier"], "difference": None, "advice": "niet vergelijkbaar (onvolledig)"})
            continue
        diff = cents(r["total"] - mine["total"])          # > 0: chosen is cheaper
        if diff >= 0:
            advice = f"{chosen} is €{diff:.2f} goedkoper"
        elif r["pickup"] and -diff < MIN_TRIP_SAVING:
            advice = f"{r['supplier']} €{-diff:.2f} goedkoper, onder de €{MIN_TRIP_SAVING:.0f} drempel: geen aparte rit"
        else:
            advice = f"{r['supplier']} is €{-diff:.2f} goedkoper"
        out.append({"supplier": r["supplier"], "difference": diff, "advice": advice})
    return out


def is_allowed(user, allowlist):
    """Deny by default: only a logged-in, verified e-mail on the explicit allowlist gets access."""
    if not user or not user.get("is_logged_in"):
        return False
    email = text(user.get("email")).casefold()
    allowed = {text(e).casefold() for e in (allowlist or []) if text(e)}
    return bool(email) and user.get("email_verified") is True and email in allowed


def normal_ean(code):
    """UPC-A (12 digits) and EAN-13 with the leading zero dropped are the same product as the 13-digit form."""
    code = text(code)
    if code.endswith(".0"):
        code = code[:-2]                       # number cells come back as floats
    return "0" + code if code.isdigit() and len(code) == 12 else code


def match_barcodes(codes, inventory):
    """Split decoded EAN codes into (known Voorraad rows, unknown codes); duplicates removed, order kept."""
    by_ean = {}
    for x in inventory or []:
        ean = normal_ean(x.get("EAN"))
        if ean and is_active(x):
            by_ean.setdefault(ean, x)
    known, unknown, seen = [], [], set()
    for code in codes:
        code = normal_ean(code)
        if not code or code in seen:
            continue
        seen.add(code)
        (known.append(by_ean[code]) if code in by_ean else unknown.append(code))
    return known, unknown


def receipt_key(entry):
    return (text(entry.get("Leverancier")).casefold(), text(entry.get("Bestelnummer")).casefold(),
            text(entry.get("Product")).casefold())


def new_receipt_rows(existing, entries):
    """Validate receipt lines and drop ones already in Bonnen (same supplier + order number + product).

    Two lines for the same product within one pasted order are both kept (e.g. two different packs)."""
    seen = {receipt_key(x) for x in existing or []}
    out, skipped = [], []
    for e in entries:
        if not (text(e.get("Leverancier")) and text(e.get("Bestelnummer")) and text(e.get("Product"))):
            raise ValueError("Leverancier, bestelnummer en product zijn verplicht")
        if parse_date(e.get("Besteldatum")) is None:
            raise ValueError("Besteldatum ontbreekt of is ongeldig")
        qty, paid = number(e.get("Aantal")), number(e.get("Betaald €"))
        if qty is None or qty <= 0 or paid is None or paid < 0:
            raise ValueError("Aantal moet positief zijn en bedrag mag niet negatief zijn")
        if receipt_key(e) in seen:
            skipped.append(e)
            continue
        out.append(e)
    return out, skipped


def weeklijst_rows(menu, spend, comparison, saving, go, reasons, delivery, budget, inventory_issues, today):
    """Rows for the 'Weeklijst' tab that an external e-mail task can read: Onderdeel | Item | Aantal | Eenheid | Bedrag € | Opmerking."""
    rows = [["Status", "GO" if go else "NO-GO", "", "", "", "; ".join(reasons)],
            ["Bijgewerkt", today.isoformat(), "", "", "", "Lijst is een momentopname van het dashboard"]]
    for m in menu:
        rows.append(["Weekmenu", text(m.get("Dag")), "", "", "", f"{text(m.get('Ritme'))}: {text(m.get('Diner'))}"])
    for line in spend.get("lines", []):
        rows.append(["Boodschappen", line["product"], line["aantal"], line["eenheid"],
                     "" if line["bedrag"] is None else line["bedrag"], line["opmerking"]])
    for issue in inventory_issues:
        rows.append(["Voorraadcorrectie", issue[0], "", "", "", issue[1]])
    for r in comparison:
        basis = "indicatief" + (f"; {r['open_lines']} regel(s) open data" if r.get("open_lines") else "")
        rows.append(["Supermarktvergelijking", r["supplier"], "", "", "" if r["total"] is None else r["total"],
                     basis if r["complete"] else "; ".join(r["problems"])])
    for s in saving:
        rows.append(["Besparing", s["supplier"], "", "", "" if s["difference"] is None else s["difference"],
                     f"{s['advice']} (indicatief)"])
    rows.append(["Bezorgkosten", delivery.get("supplier") or "", "", "",
                 "" if delivery.get("amount") is None else delivery["amount"],
                 "geldig" if delivery.get("ok") else "; ".join(delivery.get("problems") or [])])
    expected = spend.get("expected_total")
    rows.append(["Verwacht", "boodschappen (indicatief)", "", "", "" if expected is None else expected,
                 "som van regelprijzen; geen bevestigd bedrag"])
    total = spend.get("grand_total")
    rows.append(["Totaal", "checkout incl. bezorging", "", "", "" if total is None else total,
                 "" if total is None or budget is None else f"budgetverschil €{cents(budget - total):.2f}"])
    return rows


def supplier_rows(grid):
    """Leveranciers rows from row 2 up to the first empty row (a second table below it is ignored)."""
    if not grid:
        return []
    headers = [text(h) for h in grid[0]]
    out = []
    for row_num, values in enumerate(grid[1:], start=2):
        if not any(text(c) for c in values):
            break
        padded = list(values) + [None] * max(0, len(headers) - len(values))
        item = {h: padded[i] for i, h in enumerate(headers) if h}
        if text(item.get("LEVERANCIERS")):
            item["_row"] = row_num
            out.append(item)
    return out


def parse_receipt_lines(raw, order_date, supplier, order_number):
    """'product; aantal; eenheid; bedrag' per line -> receipt entries (validated by new_receipt_rows)."""
    entries = []
    for n, line in enumerate(str(raw or "").splitlines(), start=1):
        if not line.strip():
            continue
        parts = [x.strip() for x in line.split(";")]
        if len(parts) != 4:
            raise ValueError(f"Regel {n}: gebruik 'product; aantal; eenheid; bedrag'")
        entries.append({"Besteldatum": order_date, "Leverancier": supplier, "Bestelnummer": order_number,
                        "Product": parts[0], "Aantal": parts[1], "Eenheid": parts[2], "Betaald €": parts[3]})
    if not entries:
        raise ValueError("Geen regels ingevuld")
    return entries
