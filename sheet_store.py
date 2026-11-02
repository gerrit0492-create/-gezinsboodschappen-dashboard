"""Google Sheets adapter. Access uses the logged-in user's own OAuth token; no keys are stored.

Every write re-reads the target tab first and refuses to write when the header
layout changed, the row now holds another product, or the cell was changed by
someone else since the snapshot (optimistic concurrency). Only the target cells
are sent, so formulas in other columns stay intact. The app never adds columns
or tabs (schema); missing schema is reported and the write is refused. New data
rows (a product, a price, a receipt line) are only written into a row that is
completely empty in a fresh read, directly below the last used row.

LIMITATION: this is check-then-write, not an atomic compare-and-swap. The Sheets
API offers no conditional write, so a change made by someone else in the short
window between the re-read and the batch_update (typically well under a second)
can still be overwritten. The re-read narrows that window and catches stale
screens, shifted rows and moved columns; it does not make concurrent editing safe.
"""
from __future__ import annotations

import re
from decimal import Decimal

from core import (APP_COUNT, ARCHIVED, DELIVERY_KEYS, PRICE_NORMAL, PRICE_OFFER, column_letter, is_checked, number,
                  pack_size, pack_to_row_unit_price, parse_date, receipt_key, text)

TAB_NAMES = ["Overzicht", "Voorraad", "Aanvullijst", "Weekmenu", "Instellingen", "Leveranciers", "Bonnen", "Prijzen",
             "Weeklijst"]


def _plain(value):
    """Number as plain text without exponent or float noise: 1234567.0 -> '1234567', 0.4 -> '0.4'."""
    return format(Decimal(repr(value)).normalize(), "f")


class ConflictError(RuntimeError):
    """The Sheet changed since it was loaded; nothing was written."""


def connect(access_token: str, sheet_id: str):
    """Open the Sheet with the logged-in user's own Google access token (keyless; no service account).

    The token comes from the user's Google login and is never refreshed here: when it expires, Google
    answers 401 and google-auth raises RefreshError, which classify_error reports as 'expired'.
    """
    import gspread
    from google.oauth2.credentials import Credentials

    if not access_token:
        raise PermissionError("no access token")
    client = gspread.Client(auth=Credentials(token=access_token))
    return client.open_by_key(sheet_id)


def classify_error(exc):
    """Map a Google/gspread failure to: expired, scope, forbidden, not_found, rate_limited or other.

    Measured behaviour with a user token: an expired token raises google.auth RefreshError; a 403 while
    opening the Sheet is a bare PermissionError (scope and sharing problems look the same there); later
    calls raise gspread APIError with the HTTP status and Google's message.
    """
    name = type(exc).__name__
    if name == "RefreshError":
        return "expired"
    if name == "SpreadsheetNotFound":
        return "not_found"
    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "expired"
    if status == 403 or isinstance(exc, PermissionError):
        return "scope" if "scope" in str(exc).lower() else "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    return "other"


def read_all(book):
    from gspread import WorksheetNotFound

    data = {}
    for name in TAB_NAMES:
        try:
            data[name] = book.worksheet(name).get_all_values(value_render_option="UNFORMATTED_VALUE")
        except WorksheetNotFound:
            data[name] = []
    return data


def _fresh(ws, required):
    """Re-read a tab; return (grid, {header: A1 letter}) or raise ConflictError."""
    grid = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    headers = [str(h).strip() for h in (grid[0] if grid else [])]
    missing = [h for h in required if h not in headers]
    if missing:
        raise ConflictError(f"Kolom(men) niet gevonden in de Sheet: {', '.join(missing)}")
    return grid, {h: column_letter(headers.index(h)) for h in required}, headers


def _a1(ref):
    """'M12' -> (row 12, 0-based column 12)."""
    letters = "".join(c for c in ref if c.isalpha())
    index = 0
    for c in letters.upper():
        index = index * 26 + ord(c) - 64
    return int(ref[len(letters):]), index - 1


def _safe_update(ws, req, value_input_option="RAW"):
    """Send the batch only if none of the target cells holds a formula (never overwrite formulas).

    Values are compared elsewhere; a formula's computed value can match the snapshot, so the
    target cells are re-read in FORMULA rendering here, immediately before writing.
    """
    formulas = ws.get_all_values(value_render_option="FORMULA")
    for u in req:
        row, col = _a1(u["range"])
        cells = formulas[row - 1] if 0 < row <= len(formulas) else []
        current = cells[col] if col < len(cells) else ""
        if isinstance(current, str) and current.startswith("="):
            raise ConflictError(f"Cel {u['range']} bevat een formule; de app overschrijft geen formules. "
                                "Pas de Sheet aan of vul de waarde daar zelf in.")
    ws.batch_update(req, value_input_option=value_input_option)


def _cell(grid, headers, row, header):
    values = grid[row - 1] if 0 < row <= len(grid) else []
    i = headers.index(header)
    return values[i] if i < len(values) else None


def _check_product(grid, headers, row, product):
    found = str(_cell(grid, headers, row, "Product") or "").strip()
    if found != str(product).strip():
        raise ConflictError(f"Rij {row} bevat nu '{found or 'leeg'}' in plaats van '{product}'. Vernieuw de data.")


def write_stock(book, changes, today, snapshot=None):
    """changes: [(row, product, old_count, new_count)]. Writes Aantal nu, Zekerheid, Laatst gezien, Bron.

    Bron becomes 'Telling app' for a confirmed count (the number now comes from a physical count,
    not from a photo); clearing a count to unknown leaves Bron untouched.
    snapshot: optional {row: Voorraad item as loaded}; Zekerheid/Laatst gezien/Bron must still match it.
    """
    if not changes:
        return 0
    ws = book.worksheet("Voorraad")
    grid, col, headers = _fresh(ws, ["Product", "Aantal nu", "Zekerheid", "Laatst gezien", "Bron"])
    req = []
    for row, product, old, new in changes:
        _check_product(grid, headers, row, product)
        current = number(_cell(grid, headers, row, "Aantal nu"))
        if current != (None if old is None else float(old)):
            raise ConflictError(f"'{product}' is intussen gewijzigd in de Sheet. Vernieuw de data.")
        before = (snapshot or {}).get(row)
        if before is not None:
            for h in ("Zekerheid", "Bron"):
                if text(_cell(grid, headers, row, h)) != text(before.get(h)):
                    raise ConflictError(f"'{h}' van '{product}' is intussen gewijzigd. Vernieuw de data.")
            if parse_date(_cell(grid, headers, row, "Laatst gezien")) != parse_date(before.get("Laatst gezien")):
                raise ConflictError(f"'Laatst gezien' van '{product}' is intussen gewijzigd. Vernieuw de data.")
        if new is not None and new < 0:
            raise ValueError("Voorraad mag niet negatief zijn")
        req.extend([
            {"range": f"{col['Aantal nu']}{row}", "values": [[new if new is not None else ""]]},
            {"range": f"{col['Zekerheid']}{row}", "values": [["Bevestigd" if new is not None else "Onbekend"]]},
            {"range": f"{col['Laatst gezien']}{row}", "values": [[today.isoformat()]]},
        ])
        if new is not None:
            req.append({"range": f"{col['Bron']}{row}", "values": [[APP_COUNT]]})
    _safe_update(ws, req)
    return len(changes)


def write_basket(book, changes):
    """changes: [(row, product, old_checked, new_checked)] persisted in Aanvullijst 'In mand'."""
    if not changes:
        return 0
    ws = book.worksheet("Aanvullijst")
    grid, col, headers = _fresh(ws, ["Product", "In mand"])
    req = []
    for row, product, old, new in changes:
        _check_product(grid, headers, row, product)
        if is_checked(_cell(grid, headers, row, "In mand")) != bool(old):
            raise ConflictError(f"Mandstatus van '{product}' is intussen gewijzigd. Vernieuw de data.")
        req.append({"range": f"{col['In mand']}{row}", "values": [[bool(new)]]})
    _safe_update(ws, req)
    return len(changes)


def write_delivery(book, record, old):
    """Store a delivery check in Instellingen column B.

    record/old: {field: value} for the fields in core.DELIVERY_KEYS. All key rows must already
    exist in column A (the app never guesses where to add rows); every current value must still
    equal the snapshot (`old`), otherwise nothing is written.
    """
    amount = number(record.get("amount"))
    if amount is None or amount < 0:
        raise ValueError("Bezorgkosten mogen niet negatief zijn")
    for field in ("supplier", "slot", "source"):
        if not text(record.get(field)):
            raise ValueError(f"Veld '{DELIVERY_KEYS[field]}' is verplicht")
    when, verified = parse_date(record.get("date")), parse_date(record.get("verified"))
    if when is None or verified is None or verified > when:
        raise ValueError("Bezorgdatum en controledatum zijn verplicht; controle kan niet ná de bezorgdatum liggen")
    ws = book.worksheet("Instellingen")
    grid = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    head = [text(h) for h in (grid[0] if grid else [])][:2]
    if head != ["Instelling", "Waarde"]:
        raise ConflictError("Tab Instellingen heeft niet de kolommen 'Instelling' en 'Waarde' in A/B")
    keys = {text(r[0]): i + 1 for i, r in enumerate(grid) if r and text(r[0])}
    missing = [k for k in DELIVERY_KEYS.values() if k not in keys]
    if missing:
        raise ConflictError("Voeg eerst deze regel(s) toe in kolom A van Instellingen: " + ", ".join(missing))
    req = []
    for field, key in DELIVERY_KEYS.items():
        current = grid[keys[key] - 1]
        now = current[1] if len(current) > 1 else None
        before = old.get(field)
        same = (number(now) == number(before)) if field == "amount" else (
            parse_date(now) == parse_date(before) if field in ("date", "verified") else text(now) == text(before))
        if not same:
            raise ConflictError(f"'{key}' is intussen gewijzigd in de Sheet. Vernieuw de data.")
        value = record[field]
        if field == "amount":
            value = round(amount, 2)
        elif field in ("date", "verified"):
            value = parse_date(value).isoformat()
        else:
            value = text(value)
        req.append({"range": f"B{keys[key]}", "values": [[value]]})
    _safe_update(ws, req)


# --- data rows, details, receipts, week list ----------------------------------------------------

_REF = re.compile(r"(?<![A-Za-z0-9_$])(\$?)([A-Z]{1,3})(\$?)(\d+)(?![\d(])")


def shift_formula(formula, from_row, to_row):
    """Copy a formula from `from_row` to `to_row` like Sheets does: every relative row reference moves by
    the same distance. Absolute rows ($2) and text in quotes stay as they are."""
    delta = to_row - from_row
    parts = formula.split('"')
    for i in range(0, len(parts), 2):          # even parts are outside string literals
        parts[i] = _REF.sub(lambda m: m.group(0) if m.group(3)
                            else f"{m.group(1)}{m.group(2)}{int(m.group(4)) + delta}", parts[i])
    return '"'.join(parts)


def _is_formula(cell):
    return isinstance(cell, str) and cell.startswith("=")


def _free_rows(grid, formulas, count):
    """Rows directly below the last row with a value in column A that hold no data.

    Pre-filled formulas (e.g. formula columns copied down below the data) and unticked
    checkboxes (False) do not count as data; any other value does and blocks the append.
    """
    last = max((i + 1 for i, r in enumerate(grid) if r and text(r[0])), default=1)
    rows = list(range(last + 1, last + 1 + count))
    for target in rows:
        values = grid[target - 1] if target <= len(grid) else []
        forms = formulas[target - 1] if target <= len(formulas) else []
        for j, c in enumerate(values):
            f = forms[j] if j < len(forms) else ""
            if c is not False and text(c) and not _is_formula(f):
                raise ConflictError(f"Rij {target} is niet leeg; er is niets toegevoegd. Controleer de Sheet.")
    return last, rows


def _user_entered(value):
    """Value for USER_ENTERED input that is always stored literally: text gets a leading apostrophe, so
    user input such as '=HYPERLINK(...)' can never become a formula and dates stay text."""
    if isinstance(value, str) and value != "":
        return "'" + value
    return value


def _prepare_append(ws, required):
    grid, col, headers = _fresh(ws, required)
    formulas = ws.get_all_values(value_render_option="FORMULA")
    return grid, formulas, col, headers


def _append_rows(ws, rows_by_header, required, prepared=None):
    """Write new data rows in ONE request (no half-written rows). Formula columns of the last data row are
    copied (row-shifted) only where the target row has no formula yet; existing formulas are never touched."""
    grid, formulas, col, headers = prepared or _prepare_append(ws, required)
    last, targets = _free_rows(grid, formulas, len(rows_by_header))
    template = formulas[last - 1] if 2 <= last <= len(formulas) else []
    req = []
    for target, values in zip(targets, rows_by_header):
        req += [{"range": f"{col[h]}{target}", "values": [[_user_entered(v)]]} for h, v in values.items()]
        existing = formulas[target - 1] if target <= len(formulas) else []
        for i, cell in enumerate(template):
            already = i < len(existing) and _is_formula(existing[i])
            if _is_formula(cell) and headers[i] not in values and not already:
                req.append({"range": f"{column_letter(i)}{target}", "values": [[shift_formula(cell, last, target)]]})
    _safe_update(ws, req, value_input_option="USER_ENTERED")
    return targets


def _append_row(ws, values_by_header, required, prepared=None):
    return _append_rows(ws, [values_by_header], required, prepared)[0]


class PartialSaveError(ConflictError):
    """Part of a two-tab change was saved; the message says exactly which part."""


def add_product(book, product, today, location="", unit="", minimum=None, count=None, until=None, ean="",
                weekdoel=None):
    """Add a product to Voorraad (and to Aanvullijst when weekdoel is given).

    Both appends are checked before anything is written. A product already in Voorraad is refused, unless a
    weekdoel is given and it is not yet on Aanvullijst: then only the Aanvullijst row is added (retry path).
    A count given here is a physical count (Bevestigd, Telling app); without a count it is Onbekend.
    """
    name = text(product)
    if not name or not text(unit):
        raise ValueError("Productnaam en eenheid zijn verplicht")
    if count is not None and number(count) is not None and number(count) < 0:
        raise ValueError("Voorraad mag niet negatief zijn")
    goal = None
    if weekdoel is not None:
        goal = number(weekdoel)
        if goal is None or goal < 0:
            raise ValueError("Weekdoel moet 0 of meer zijn")
    stock_ws = book.worksheet("Voorraad")
    known = count is not None and number(count) is not None
    values = {"Product": name, "Locatie": text(location), "Aantal nu": number(count) if known else "",
              "Eenheid": text(unit), "Minimum": "" if number(minimum) is None else number(minimum),
              "Zekerheid": "Bevestigd" if known else "Onbekend", "Laatst gezien": today.isoformat() if known else "",
              "Bron": APP_COUNT if known else "", "Houdbaar tot": until.isoformat() if until else "",
              "Status": "Actief", "EAN": text(ean)}
    stock_prep = _prepare_append(stock_ws, list(values))
    in_stock = any(r and text(r[0]).casefold() == name.casefold() for r in stock_prep[0][1:])
    list_values = {"Product": name, "Eenheid": text(unit), "Weekdoel": goal, "In mand": False}
    list_prep = None
    if goal is not None:
        list_ws = book.worksheet("Aanvullijst")
        list_prep = _prepare_append(list_ws, list(list_values))
        on_list = any(r and text(r[0]).casefold() == name.casefold() for r in list_prep[0][1:])
        if on_list:
            raise ConflictError(f"'{name}' staat al op de aanvullijst; er is niets toegevoegd.")
        _free_rows(list_prep[0], list_prep[1], 1)          # refuse before writing anything
    if in_stock:
        if goal is None:
            raise ConflictError(f"'{name}' staat al in Voorraad (eventueel gearchiveerd); er is niets toegevoegd.")
        _append_row(list_ws, list_values, list(list_values), list_prep)
        return None
    row = _append_row(stock_ws, values, list(values), stock_prep)
    if goal is not None:
        try:
            _append_row(list_ws, list_values, list(list_values))
        except Exception as exc:
            raise PartialSaveError(f"'{name}' is wel aan Voorraad toegevoegd, maar niet aan de aanvullijst "
                                   f"({exc}). Probeer opnieuw met hetzelfde weekdoel om alleen de aanvullijst "
                                   "aan te vullen.") from exc
    return row


def write_details(book, row, product, changes, snapshot):
    """Update Voorraad detail cells (Locatie, Houdbaar tot, EAN, Status) after re-checking them."""
    allowed = {"Locatie", "Houdbaar tot", "EAN", "Status", "Minimum"}
    if not changes or set(changes) - allowed:
        raise ValueError("Ongeldige wijziging")
    ws = book.worksheet("Voorraad")
    grid, col, headers = _fresh(ws, ["Product"] + sorted(changes))
    _check_product(grid, headers, row, product)
    req = []
    for h, value in changes.items():
        now = _cell(grid, headers, row, h)
        before = (snapshot or {}).get(h)
        same = parse_date(now) == parse_date(before) if h == "Houdbaar tot" else text(now) == text(before)
        if not same:
            raise ConflictError(f"'{h}' van '{product}' is intussen gewijzigd. Vernieuw de data.")
        if h == "Houdbaar tot":
            value = parse_date(value).isoformat() if parse_date(value) else ""
        elif h == "Minimum":
            value = "" if number(value) is None else number(value)
        else:
            value = text(value)
        req.append({"range": f"{col[h]}{row}", "values": [[value]]})
    _safe_update(ws, req)


def archive_product(book, row, product, snapshot, restore=False):
    """Soft delete: Status 'Gearchiveerd' (nothing is removed; restore sets 'Actief')."""
    write_details(book, row, product, {"Status": "Actief" if restore else ARCHIVED}, snapshot)


def write_price(book, product, supplier, pack_price, pack, unit, kind, valid_through, verified, source, snapshot=None):
    """Store a checked price in Prijzen: updates the (product, supplier) row or adds one.

    snapshot: the Prijzen row as loaded (or None when the app saw no row); it must still match.
    """
    per_unit = pack_to_row_unit_price(pack_price, pack)
    if not text(supplier) or not text(product):
        raise ValueError("Product en leverancier zijn verplicht")
    if kind not in (PRICE_NORMAL, PRICE_OFFER):
        raise ValueError("Prijssoort moet Normaal of Aanbieding zijn")
    until = parse_date(valid_through)
    if until is None or until < verified:
        raise ValueError("Geldig t/m ontbreekt of ligt vóór de controledatum")
    packing = f"{_plain(number(pack))} {text(unit)}"
    if pack_size(packing, unit) is None:
        raise ValueError("Inhoud van de verpakking ontbreekt of is ongeldig")
    if not text(source):
        raise ValueError("Bron van de prijs is verplicht")
    values = {"Product": text(product), "Leverancier": text(supplier), "Prijs verpakking €": round(number(pack_price), 2),
              "Verpakking": packing, "Prijs/eenheid €": per_unit, "Prijssoort": kind, "Geldig t/m": until.isoformat(),
              "Prijsdatum": verified.isoformat(), "Bron": text(source)}
    ws = book.worksheet("Prijzen")
    grid, col, headers = _fresh(ws, list(values))
    rows = [i + 1 for i, r in enumerate(grid) if i and r
            and text(r[headers.index("Product")]).casefold() == text(product).casefold()
            and text(r[headers.index("Leverancier")]).casefold() == text(supplier).casefold()]
    if len(rows) > 1:
        raise ConflictError(f"'{product}' bij {supplier} staat meerdere keren in Prijzen; ruim dat eerst op.")
    if rows:
        row = rows[0]
        if snapshot is None or any(text(_cell(grid, headers, row, h)) != text(snapshot.get(h))
                                   for h in ("Prijs/eenheid €", "Prijsdatum", "Geldig t/m", "Verpakking")):
            raise ConflictError(f"Prijs van '{product}' bij {supplier} is intussen gewijzigd. Vernieuw de data.")
        _safe_update(ws, [{"range": f"{col[h]}{row}", "values": [[v]]} for h, v in values.items()
                          if h not in ("Product", "Leverancier")])
        return row
    if snapshot is not None:
        raise ConflictError(f"Prijsregel van '{product}' bij {supplier} is verdwenen. Vernieuw de data.")
    return _append_row(ws, values, list(values))


def write_receipts(book, entries, today):
    """Append receipt/order lines to Bonnen in one request, skipping lines already in the Sheet
    (same supplier + order number + product, re-checked on a fresh read). Returns (written, skipped)."""
    ws = book.worksheet("Bonnen")
    cols = ["Besteldatum", "Leverancier", "Product", "Aantal", "Eenheid", "Betaald €", "Bestelnummer", "Status",
            "Verwerkt op"]
    prepared = _prepare_append(ws, cols)
    grid, headers = prepared[0], prepared[3]
    seen = {receipt_key({h: r[headers.index(h)] if headers.index(h) < len(r) else "" for h in cols})
            for r in grid[1:] if r}
    new = [e for e in entries if receipt_key(e) not in seen]
    rows = [{"Besteldatum": parse_date(e["Besteldatum"]).isoformat(), "Leverancier": text(e["Leverancier"]),
             "Product": text(e["Product"]), "Aantal": number(e["Aantal"]), "Eenheid": text(e.get("Eenheid")),
             "Betaald €": round(number(e["Betaald €"]), 2), "Bestelnummer": text(e["Bestelnummer"]),
             "Status": "Ingevoerd in app", "Verwerkt op": today.isoformat()} for e in new]
    if rows:
        _append_rows(ws, rows, cols, prepared)
    return len(rows), len(entries) - len(rows)


def write_weeklijst(book, rows):
    """Replace the content of the app-owned Weeklijst tab (A2:F); header row 1 is checked, not written."""
    ws = book.worksheet("Weeklijst")
    grid, col, headers = _fresh(ws, ["Onderdeel", "Item", "Aantal", "Eenheid", "Bedrag €", "Opmerking"])
    letters = [col[h] for h in ("Onderdeel", "Item", "Aantal", "Eenheid", "Bedrag €", "Opmerking")]
    old_last = max((i + 1 for i, r in enumerate(grid) if any(text(c) for c in r)), default=1)
    req = []
    for i in range(max(len(rows), old_last - 1)):
        values = rows[i] if i < len(rows) else [""] * 6
        for letter, v in zip(letters, values):
            req.append({"range": f"{letter}{i + 2}", "values": [["" if v is None else v]]})
    _safe_update(ws, req)


def write_supplier_check(book, supplier, amount, checked, extra, snapshot):
    """Leveranciers: verified delivery cost (+ date) and travel/time cost for one supplier row."""
    if amount is not None and (number(amount) is None or number(amount) < 0):
        raise ValueError("Bezorgkosten mogen niet negatief zijn")
    if extra is not None and (number(extra) is None or number(extra) < 0):
        raise ValueError("Reis- en tijdkosten mogen niet negatief zijn")
    ws = book.worksheet("Leveranciers")
    cols = ["LEVERANCIERS", "Bezorgkosten gecontroleerd €", "Bezorging gecontroleerd op", "Reis- en tijdkosten €"]
    grid, col, headers = _fresh(ws, cols)
    rows = [i + 1 for i, r in enumerate(grid) if i and r and text(r[0]).casefold() == text(supplier).casefold()]
    if len(rows) != 1:
        raise ConflictError(f"Leverancier '{supplier}' niet (eenduidig) gevonden in Leveranciers")
    row = rows[0]
    for h in cols[1:]:
        if text(_cell(grid, headers, row, h)) != text((snapshot or {}).get(h)):
            raise ConflictError(f"'{h}' van {supplier} is intussen gewijzigd. Vernieuw de data.")
    req = []
    if amount is not None:
        req += [{"range": f"{col['Bezorgkosten gecontroleerd €']}{row}", "values": [[round(number(amount), 2)]]},
                {"range": f"{col['Bezorging gecontroleerd op']}{row}", "values": [[checked.isoformat()]]}]
    if extra is not None:
        req.append({"range": f"{col['Reis- en tijdkosten €']}{row}", "values": [[round(number(extra), 2)]]})
    if req:
        _safe_update(ws, req)


def write_menu(book, row, day, diner, old):
    """Weekmenu: change the planned dinner for one day (column 'Diner')."""
    ws = book.worksheet("Weekmenu")
    grid, col, headers = _fresh(ws, ["Dag", "Diner"])
    if text(_cell(grid, headers, row, "Dag")) != text(day):
        raise ConflictError(f"Rij {row} is niet meer '{day}'. Vernieuw de data.")
    if text(_cell(grid, headers, row, "Diner")) != text(old):
        raise ConflictError(f"Diner van {day} is intussen gewijzigd. Vernieuw de data.")
    if not text(diner):
        raise ValueError("Diner mag niet leeg zijn")
    _safe_update(ws, [{"range": f"{col['Diner']}{row}", "values": [[text(diner)]]}])
