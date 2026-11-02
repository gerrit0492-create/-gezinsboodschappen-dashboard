"""Gezinsboodschappen — mobile-first Streamlit dashboard over the private Google Sheet.

Access: Google login (OIDC) + explicit e-mail allowlist, checked before any Sheet data is read.
Sheet access is keyless and least-privilege: the logged-in user's own Google access token with only the
drive.file scope; each user picks the configured stock Sheet once in Google Picker.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

import auth
import scan
from picker import check_pick, picker_step
from core import (CHECK_WINDOW_DAYS, DELIVERY_KEYS, MAX_BUDGET, PRICE_NORMAL, PRICE_OFFER, classify_inventory,
                  compare_suppliers, confirmations, delivery_check, effective_budget, expiring, go_decision,
                  is_active, is_allowed, latest_price, low_stock, match_barcodes, new_receipt_rows, normalised_price,
                  number, pack_size, pack_to_row_unit_price, parse_date, parse_receipt_lines, savings,
                  schema_problems, settings_map, shopping_totals, stock_state, supplier_rows, table_rows, text,
                  valid_amount, weeklijst_rows)
from sheet_store import (ConflictError, PartialSaveError, add_product, archive_product, classify_error, connect, read_all,
                         write_basket,
                         write_delivery, write_details, write_menu, write_price, write_receipts, write_stock,
                         write_supplier_check, write_weeklijst)

st.set_page_config(page_title="Gezinsboodschappen", page_icon="🛒", layout="centered")


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


# --- access control: deny by default, before any private data ------------------------------------
user = auth.current_user()
if not user.get("is_logged_in"):
    st.title("🛒 Gezinsboodschappen")
    if not auth.login_configured():
        st.error("Inloggen is nog niet ingericht ([auth] in de app-secrets). Er wordt geen data getoond.")
        st.stop()
    st.write("Log in met je Google-account. Alleen vooraf toegestane gezinsaccounts krijgen toegang.")
    st.button("Inloggen met Google", type="primary", width="stretch", on_click=auth.login)
    st.stop()
allowlist = list(secret("ALLOWED_EMAILS") or [])
if not is_allowed(user, allowlist):
    st.title("🛒 Gezinsboodschappen")
    st.error("Geen toegang voor dit account. Vraag de beheerder om je e-mailadres toe te voegen.")
    st.button("Uitloggen", on_click=auth.logout)
    st.stop()

today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()

with st.sidebar:
    st.success(f"Ingelogd: {user.get('email')}")
    st.button("Uitloggen", on_click=auth.logout, width="stretch")
    if st.button("↻ Vernieuw data", width="stretch"):
        st.cache_data.clear()
        st.cache_resource.clear()  # also reconnect after an expired/failed Sheets session
        st.rerun()
    st.caption("Bron: private Google Sheet. Geen automatische bestelling, geen betaalde AI.")


def saved(message):
    """Show a confirmation after the rerun that reloads fresh Sheet data."""
    st.cache_data.clear()
    st.session_state["flash"] = message
    st.rerun()


ACCESS_MESSAGES = {
    "expired": "Je Google-sessie voor de Sheet is verlopen (dat gebeurt na ongeveer een uur). Log uit en log "
               "opnieuw in.",
    "scope": "Bij het inloggen is geen toestemming gegeven voor bestanden die je met deze app gebruikt. Log uit, "
             "log opnieuw in en sta die toegang toe.",
    "forbidden": "Geen toegang tot de Sheet: kies het voorraadbestand eenmalig in Google Drive, en zorg dat dit "
                 "Google-account de Sheet mag bewerken (Delen → Bewerker).",
    "not_found": "De Sheet is niet gevonden: controleer SHEET_ID en of dit Google-account toegang heeft.",
    "rate_limited": "Google is even te druk (limiet bereikt). Probeer het over een minuut opnieuw.",
}


def save_failed(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    kind = classify_error(exc)
    if isinstance(exc, PartialSaveError):
        st.error(f"Deels opgeslagen: {exc}")
    elif isinstance(exc, (ConflictError, ValueError)):
        st.error(f"Niet opgeslagen: {exc}")
    elif kind in ACCESS_MESSAGES:
        st.error(f"Niet opgeslagen: {ACCESS_MESSAGES[kind]}")    # Google rejected the request: nothing changed
    elif type(exc).__name__ == "WorksheetNotFound" or (isinstance(status, int) and 400 <= status < 500):
        st.error(f"Niet opgeslagen: Google weigerde de wijziging ({type(exc).__name__} {status or ''}). "
                 "Controleer de Sheet-inrichting.")
    else:
        st.error(f"Opslaan onzeker ({type(exc).__name__}): het is niet zeker of de wijziging is aangekomen. "
                 "Klik 'Vernieuw data' en controleer de waarde vóór je opnieuw opslaat.")


sid = secret("SHEET_ID")
if not sid:
    st.error("Google Sheets-koppeling niet ingericht: SHEET_ID ontbreekt in de secrets.")
    st.stop()
token = auth.access_token()
if not token:
    st.error("Je sessie heeft geen toegang tot Google Drive-bestanden. Log uit en log opnieuw in, en sta toegang "
             "toe tot bestanden die je met deze app gebruikt.")
    st.button("Uitloggen en opnieuw inloggen", on_click=auth.logout, type="primary", width="stretch")
    st.stop()
# Caches are keyed per user AND per token: a Sheet read with one person's token is never served to another
# session. Only a hash of the token is used as key; the token itself is never shown, logged or stored.
token_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
user_key = text(user.get("email")).casefold()


@st.cache_resource(ttl=3600, max_entries=20, show_spinner=False)   # tokens live ~1h; don't keep them longer
def _open_sheet(token_key, _token, _sheet_id):
    return connect(_token, _sheet_id)


def spreadsheet():
    return _open_sheet(token_key, token, str(sid))


@st.cache_data(ttl=60, max_entries=20, show_spinner=False)
def snapshot(user_key, token_key):
    return read_all(spreadsheet())


def pick_step():
    """One-time Google Picker choice of the stock Sheet (drive.file). Only reached after login, verified e-mail,
    allowlist and token checks; the picked ID is only compared with SHEET_ID, never used for access."""
    st.subheader("Eenmalig: kies het voorraadbestand")
    st.write("Deze app mag alleen bestanden gebruiken die jij er zelf voor kiest. Kies één keer de voorraad-Sheet "
             "in Google Drive; Google onthoudt die keuze daarna voor jouw account.")
    if st.session_state.get("pick_tried"):
        st.warning("Na het kiezen is er nog geen toegang. Controleer of je het juiste bestand koos en of dit "
                   "Google-account de Sheet mag bewerken (Delen → Bewerker).")
    api_key, app_id = auth.picker_config()
    if not api_key:
        st.error("Bestandskeuze is niet ingericht: [picker] api_key en app_id ontbreken in de secrets.")
        return
    status, message = check_pick(picker_step(token, api_key, app_id, str(sid)), sid)
    if status == "ok":
        st.session_state["pick_tried"] = True
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()
    elif status:
        st.warning(message)
    st.caption("Zie je het bestand niet in de lijst? Dan heeft dit Google-account geen toegang tot de Sheet: laat de "
               "eigenaar hem met je delen als Bewerker. Lukt kiezen op de iPhone niet, doe het dan eenmalig op een "
               "computer; daarna werkt de app ook op de iPhone.")


try:
    data = snapshot(user_key, token_key)
except Exception as exc:
    kind = classify_error(exc)
    if kind in ("forbidden", "not_found"):
        pick_step()            # without a drive.file grant Google hides the file (403/404) until it is picked
        st.button("Uitloggen", on_click=auth.logout, width="stretch")
        st.stop()
    st.session_state.pop("pick_tried", None)
    st.error(ACCESS_MESSAGES.get(kind, "De private Google Sheet kon niet worden geladen."))
    if kind in ("expired", "scope"):
        st.button("Uitloggen en opnieuw inloggen", on_click=auth.logout, type="primary", width="stretch")
    else:
        st.caption(f"Technisch detail: {type(exc).__name__}")
    st.stop()
st.session_state.pop("pick_tried", None)      # access works (again)

# Conflict baselines come from what this user SAW (the previous run), not from data reloaded at submit time:
# another session's save in between is then detected instead of silently overwritten.
seen_data = st.session_state.get("seen_data") or data
seen_rows = {tab: {int(x["_row"]): x for x in table_rows(seen_data.get(tab, []))}
             for tab in ("Voorraad", "Aanvullijst", "Weekmenu")}
seen_settings = settings_map(seen_data.get("Instellingen", []))
seen_prices = table_rows(seen_data.get("Prijzen", []))
seen_suppliers = {int(x["_row"]): x for x in supplier_rows(seen_data.get("Leveranciers", []))}


def seen(tab, row, product, key="Product"):
    """The row as this user saw it; refuse when they never saw this row with this product."""
    item = seen_rows[tab].get(int(row)) if tab in seen_rows else seen_suppliers.get(int(row))
    if item is None or text(item.get(key)).casefold() != text(product).casefold():
        raise ConflictError("De gegevens zijn intussen veranderd. Vernieuw de data en probeer opnieuw.")
    return item


inventory = table_rows(data.get("Voorraad", []))
active = [x for x in inventory if is_active(x)]
shopping = table_rows(data.get("Aanvullijst", []))
menu = table_rows(data.get("Weekmenu", []))
suppliers = supplier_rows(data.get("Leveranciers", []))
prices = table_rows(data.get("Prijzen", []))
receipts = table_rows(data.get("Bonnen", []))
settings = settings_map(data.get("Instellingen", []))


def setting(key):
    return (settings.get(key) or {}).get("value")


schema = schema_problems(data)
delivery = delivery_check(settings, today)
reference = delivery["date"] if delivery["date"] and delivery["date"] >= today else today
metrics = classify_inventory(active, reference, today)
budget = effective_budget(setting("Weekbudget"))
spend = shopping_totals(shopping, inventory, delivery, today, prices)
go, reasons = go_decision(spend, budget, schema)
comparison = compare_suppliers(shopping, inventory, prices, suppliers, delivery, today)
saving = savings(comparison, delivery["supplier"]) if delivery["supplier"] else []
needed_names = {text(x.get("Product")).casefold() for x in shopping
                if number(x.get("Te bestellen")) is None or number(x.get("Te bestellen")) > 0}
state_label = {"onbekend": "onbekend", "verouderd": "niet in deze bestelweek geteld",
               "geen telling": "bron is geen telling (bv. foto)"}
supplier_names = [text(s.get("LEVERANCIERS")) for s in suppliers]

st.title("🛒 Gezinsboodschappen")
st.caption(f"Weekbudget max. €{budget:.2f} incl. €0,00 bezorging" if budget else "Weekbudget onbekend")
if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))
if schema:
    st.error("Sheet-inrichting nodig (de app voegt zelf geen kolommen of tabbladen toe):\n\n- " + "\n- ".join(schema))

home, stock_tab, order_tab, price_tab, menu_tab, scan_tab, receipt_tab = st.tabs(
    ["Overzicht", "Voorraad", "Bestellen", "Prijzen", "Menu", "Scan", "Bonnen"])

# --- Overzicht --------------------------------------------------------------------------------------
with home:
    if go:
        st.success(f"GO — {reasons[0]}")
    else:
        st.warning("NO-GO — dit moet nog:\n\n- " + "\n- ".join(reasons))
    a, b = st.columns(2)
    a.metric("Budget", f"€{budget:.2f}" if budget else "onbekend")
    a.metric("Verwacht boodschappen", f"€{spend['shopping_total']:.2f}" if spend["shopping_total"] is not None else "nog onbekend")
    b.metric("Bezorgkosten (bevestigd)", f"€{delivery['amount']:.2f}" if delivery["ok"] else "niet bevestigd")
    b.metric("Totaal incl. bezorging", f"€{spend['grand_total']:.2f}" if spend["grand_total"] is not None else "nog onbekend")
    if spend["grand_total"] is not None and budget:
        st.progress(min(spend["grand_total"] / budget, 1.0), text=f"€{budget - spend['grand_total']:.2f} onder budget")
    with st.container(border=True):
        st.markdown("**Besparing t.o.v. andere supermarkten**")
        if saving:
            for s in saving:
                st.write(f"• {s['advice']}")
        else:
            st.caption("Nog geen vergelijking mogelijk: leg eerst bezorging en prijzen per supermarkt vast.")
    st.markdown(f"**Voorraad:** {metrics['known']} van {metrics['total']} actueel geteld · {metrics['unknown']} opnieuw tellen")
    low = low_stock(active, reference, today)
    if low:
        st.write("Bijna op: " + ", ".join(f"{x['Product']} ({number(x.get('Aantal nu')):g} {x.get('Eenheid', '')})" for x in low))
    soon = expiring(active, today)
    if soon:
        st.write("Houdbaarheid: " + ", ".join(f"{x['Product']} t/m {d:%d-%m}" for x, d in soon))
    missing = spend["missing_inventory"] + spend["missing_stock"]
    if missing:
        st.write("Ontbreekt/onzeker voor de bestelling: " + ", ".join(missing))
    with st.expander("Weekmenu"):
        for m in menu:
            st.write(f"**{m.get('Dag', '')}** · {m.get('Ritme', '')}: {m.get('Diner', '')}")
    st.caption("De GO is een interne controle; de app plaatst nooit zelf een bestelling.")
    if st.button("📧 Weeklijst bijwerken", width="stretch"):
        try:
            issues = [(n, spend["stock_reasons"].get(n, "staat niet in Voorraad")) for n in missing]
            write_weeklijst(spreadsheet(), weeklijst_rows(menu, spend, comparison, saving, go, reasons, delivery,
                                                          budget, issues, today))
            saved("Weeklijst bijgewerkt in de Sheet")
        except Exception as exc:
            save_failed(exc)

# --- Voorraad ---------------------------------------------------------------------------------------
with stock_tab:
    recount = [x for x in active if stock_state(x, reference, today) != "ok"]
    recount.sort(key=lambda x: text(x.get("Product")).casefold() not in needed_names)
    with st.expander(f"Tellen wat nodig is ({len(recount)})", expanded=bool(recount)):
        st.caption(f"Leeg laten = blijft onbekend. Een getelde 0 is geldig. Tellingen gelden voor bezorging tot "
                   f"{CHECK_WINDOW_DAYS} dagen later.")
        with st.form("recount_form"):
            entered = {}
            for x in recount:
                label = f"{x['Product']} ({x.get('Eenheid', '')}) · {state_label[stock_state(x, reference, today)]}"
                entered[int(x["_row"])] = st.number_input(label, min_value=0.0, step=1.0, value=None,
                                                          key=f"uc_{x['_row']}")
            pressed = st.form_submit_button("💾 Tellingen bevestigen", type="primary", width="stretch")
        if pressed:
            try:
                changes = confirmations(recount, entered)
                if not changes:
                    raise ValueError("Niets ingevuld; alles blijft onbekend")
                base = {r: seen("Voorraad", r, p) for r, p, _, _ in changes}
                changes = [(r, p, number(base[r].get("Aantal nu")), new) for r, p, _, new in changes]
                write_stock(spreadsheet(), changes, today, base)
                saved(f"{len(changes)} telling(en) bevestigd")
            except Exception as exc:
                save_failed(exc)

    if active:
        choice = st.selectbox("Product bekijken of wijzigen", active, format_func=lambda x:
                              f"{x['Product']} · {x.get('Locatie') or 'geen locatie'}")
        row = int(choice["_row"])
        count = number(choice.get("Aantal nu"))
        st.caption(f"{'onbekend' if count is None else f'{count:g}'} {choice.get('Eenheid', '')} · "
                   f"{state_label.get(stock_state(choice, reference, today), 'actueel geteld')} · "
                   f"houdbaar t/m {choice.get('Houdbaar tot') or '—'}")
        with st.form(f"product_form_{row}", clear_on_submit=True):
            new_count = st.number_input("Nieuwe telling", min_value=0.0, step=1.0, value=None, key=f"nc_{row}")
            locations = ["Koelkast", "Vriezer", "Voorraadkast"]
            current_loc = text(choice.get("Locatie"))
            loc = st.selectbox("Locatie", [current_loc] + [x for x in locations if x != current_loc] if current_loc else locations,
                               key=f"loc_{row}")
            until = st.date_input("Houdbaar tot", value=parse_date(choice.get("Houdbaar tot")), format="DD-MM-YYYY",
                                  key=f"hb_{row}")
            pressed = st.form_submit_button("💾 Opslaan", width="stretch")
        if pressed:
            try:
                base = seen("Voorraad", row, choice["Product"])
                if new_count is not None:
                    write_stock(spreadsheet(), [(row, text(choice["Product"]), number(base.get("Aantal nu")), new_count)],
                                today, {row: base})
                details = {}
                if text(loc) != text(choice.get("Locatie")):
                    details["Locatie"] = loc
                if until != parse_date(choice.get("Houdbaar tot")):
                    details["Houdbaar tot"] = until
                if details:
                    try:
                        write_details(spreadsheet(), row, text(choice["Product"]), details, base)
                    except Exception as exc:
                        if new_count is None:
                            raise
                        raise PartialSaveError(f"telling {new_count:g} is opgeslagen, locatie/houdbaarheid niet "
                                               f"({exc})") from exc
                if new_count is None and not details:
                    raise ValueError("Niets gewijzigd")
                saved(f"{choice['Product']} bijgewerkt")
            except Exception as exc:
                save_failed(exc)
        c1, c2 = st.columns(2)
        if c1.button("Opgebruikt (telling 0)", key=f"used_{row}", width="stretch"):
            try:
                base = seen("Voorraad", row, choice["Product"])
                write_stock(spreadsheet(), [(row, text(choice["Product"]), number(base.get("Aantal nu")), 0.0)],
                            today, {row: base})
                saved(f"{choice['Product']} op 0 gezet (bevestigd)")
            except Exception as exc:
                save_failed(exc)
        sure = c2.checkbox("Zeker archiveren", key=f"arch_ok_{row}")
        if c2.button("Archiveren", key=f"arch_{row}", width="stretch", disabled=not sure):
            try:
                archive_product(spreadsheet(), row, text(choice["Product"]), seen("Voorraad", row, choice["Product"]))
                saved(f"{choice['Product']} gearchiveerd (niets verwijderd; terug te zetten)")
            except Exception as exc:
                save_failed(exc)

    archived_rows = [x for x in inventory if not is_active(x)]
    if archived_rows:
        with st.expander(f"Gearchiveerd ({len(archived_rows)})"):
            pick = st.selectbox("Terugzetten", archived_rows, format_func=lambda x: x["Product"])
            if st.button("Terugzetten", width="stretch"):
                try:
                    archive_product(spreadsheet(), int(pick["_row"]), text(pick["Product"]),
                                    seen("Voorraad", pick["_row"], pick["Product"]), restore=True)
                    saved(f"{pick['Product']} teruggezet")
                except Exception as exc:
                    save_failed(exc)

    with st.expander("➕ Product toevoegen"):
        with st.form("add_form", clear_on_submit=True):
            name = st.text_input("Productnaam")
            unit = st.selectbox("Eenheid", ["stuks", "gram", "kg", "liter", "ml", "pakken", "blikken", "potten"])
            loc = st.selectbox("Locatie", ["Koelkast", "Vriezer", "Voorraadkast"])
            cnt = st.number_input("Getelde hoeveelheid (leeg = onbekend)", min_value=0.0, step=1.0, value=None)
            minimum = st.number_input("Minimum (bijna op onder)", min_value=0.0, step=1.0, value=None)
            goal = st.number_input("Weekdoel op aanvullijst (leeg = niet op de lijst)", min_value=0.0, step=1.0, value=None)
            pressed = st.form_submit_button("➕ Toevoegen", width="stretch")
        if pressed:
            try:
                add_product(spreadsheet(), name, today, location=loc, unit=unit, minimum=minimum, count=cnt,
                            weekdoel=goal)
                saved(f"{name} toegevoegd")
            except Exception as exc:
                save_failed(exc)

# --- Bestellen --------------------------------------------------------------------------------------
with order_tab:
    st.caption("Afvinken wat in het mandje zit. Bedragen alleen bij een geldige prijs voor de gekozen supermarkt en bezorgdag.")
    order_lines = [x for x in shopping if number(x.get("Te bestellen")) is None or number(x.get("Te bestellen")) > 0]
    if order_lines:
        line_info = {ln["product"]: ln for ln in spend["lines"]}
        with st.form("basket_form"):
            ticks = {}
            for x in order_lines:
                info = line_info.get(text(x.get("Product")), {})
                qty = number(x.get("Te bestellen"))
                amount = info.get("bedrag")
                label = (f"{x['Product']} — {'?' if qty is None else f'{qty:g}'} {x.get('Eenheid', '')}"
                         f" · {'€%.2f' % amount if amount is not None else info.get('opmerking') or 'controle nodig'}")
                shown = x.get("In mand") is True or text(x.get("In mand")).lower() in ("true", "waar")
                # the Sheet value is part of the key: after any refresh the box shows the Sheet again instead of
                # an old widget state, so saving can never undo a tick made in another session
                ticks[int(x["_row"])] = st.checkbox(label, value=shown, key=f"mand_{x['_row']}_{shown}")
            pressed = st.form_submit_button("💾 Mandje bewaren", width="stretch")
        if pressed:
            by_row = {int(x["_row"]): x for x in order_lines}
            changes = []
            try:
                for r, new in ticks.items():
                    base = seen("Aanvullijst", r, by_row[r]["Product"])
                    old = base.get("In mand") is True or text(base.get("In mand")).lower() in ("true", "waar")
                    if bool(new) != old:
                        changes.append((r, text(by_row[r]["Product"]), old, bool(new)))
                if not changes:
                    raise ValueError("Geen wijzigingen")
                write_basket(spreadsheet(), changes)
                saved("Mandje bewaard")
            except Exception as exc:
                save_failed(exc)
    else:
        st.info("Niets te bestellen volgens de aanvullijst.")
    if spend["archived"]:
        st.caption("Niet besteld (gearchiveerd): " + ", ".join(spend["archived"]))

    with st.expander("Bezorgmoment en bezorgkosten" + (" ✅" if delivery["ok"] else " (open)"), expanded=not delivery["ok"]):
        if delivery["problems"]:
            st.warning("Bezorgcontrole: " + "; ".join(delivery["problems"]))
        st.caption("Neem het bedrag precies over uit de checkout voor dít moment. GO alleen bij exact €0,00.")
        with st.form("delivery_form", clear_on_submit=True):
            supplier = st.selectbox("Leverancier", supplier_names) if supplier_names else st.text_input("Leverancier")
            ship_date = st.date_input(f"Bezorgdatum (binnen {CHECK_WINDOW_DAYS} dagen)", value=None, min_value=today,
                                      max_value=today + timedelta(days=CHECK_WINDOW_DAYS), format="DD-MM-YYYY")
            ship_slot = st.text_input("Bezorgmoment", placeholder="bijv. 18:00–20:00")
            ship_in = st.number_input("Bezorgkosten € (checkout)", min_value=0.0, step=0.01, format="%.2f", value=None)
            ship_src = st.text_input("Bron van de controle", placeholder="bijv. checkout-scherm / screenshot")
            saw_it = st.checkbox("Ik heb dit bedrag nu in de checkout voor dit moment gezien")
            pressed = st.form_submit_button("💾 Bezorgcontrole opslaan", width="stretch")
        if pressed:
            try:
                if not saw_it:
                    raise ValueError("Bevestig dat je het bedrag in de checkout hebt gezien")
                record = {"amount": valid_amount(ship_in), "verified": today, "supplier": supplier,
                          "date": ship_date, "slot": ship_slot, "source": ship_src}
                write_delivery(spreadsheet(), record,
                               {f: (seen_settings.get(k) or {}).get("value") for f, k in DELIVERY_KEYS.items()})
                saved("Bezorgcontrole opgeslagen" + ("" if record["amount"] == 0 else
                      f" — let op: €{record['amount']:.2f} bezorgkosten betekent NO-GO"))
            except Exception as exc:
                save_failed(exc)

# --- Prijzen ----------------------------------------------------------------------------------------
with price_tab:
    if not delivery["date"] or delivery["date"] < today:
        st.info("Leg eerst bij Bestellen de bezorgdatum vast; prijzen gelden voor die dag.")
    else:
        st.caption(f"Prijzen gelden voor bezorging/aankoop op {delivery['date']:%d-%m-%Y}. Vul de prijs per verpakking "
                   "en de inhoud in; de app rekent om naar €/kg, €/liter of €/stuk en rondt af op hele verpakkingen.")
        priced = [x for x in shopping if text(x.get("Eenheid")) and text(x.get("Product")).casefold() in needed_names]
        if priced and supplier_names:
            sup = st.selectbox("Supermarkt", supplier_names, key="price_supplier")
            item = st.selectbox("Product", priced, format_func=lambda x: x["Product"], key="price_product")
            unit = text(item.get("Eenheid"))
            current = latest_price(prices, item.get("Product"), sup)
            if current:
                norm = normalised_price(current.get("Prijs/eenheid €"), unit)
                st.caption(f"Laatst: {current.get('Verpakking')} · " + (f"€{norm[0]:.2f}/{norm[1]}" if norm else "?")
                           + f" · gecontroleerd {current.get('Prijsdatum')} · geldig t/m {current.get('Geldig t/m')}"
                           + f" · bron: {current.get('Bron') or '—'}")
            key = f"{sup}_{item['_row']}"
            with st.form(f"price_form_{key}", clear_on_submit=True):
                pack_price = st.number_input("Prijs per verpakking €", min_value=0.0, step=0.01, format="%.2f",
                                             value=None, key=f"pp_{key}")
                content = st.number_input(f"Inhoud verpakking in {unit}", min_value=0.0, step=1.0,
                                          value=pack_size(current.get("Verpakking"), unit), key=f"pc_{key}")
                kind = st.radio("Soort prijs", [PRICE_NORMAL, PRICE_OFFER], horizontal=True, key=f"pk_{key}")
                offer_end = st.date_input("Aanbieding geldig t/m", value=None, min_value=today, format="DD-MM-YYYY",
                                          key=f"pe_{key}")
                source = st.text_input("Bron", placeholder="bijv. webshop Jumbo, bezorgdag gekozen", key=f"pb_{key}")
                saw_it = st.checkbox(f"Ik zie deze prijs nu bij {sup} voor {delivery['date']:%d-%m-%Y}", key=f"ps_{key}")
                pressed = st.form_submit_button("💾 Prijs opslaan", width="stretch")
            if pressed:
                try:
                    if not saw_it:
                        raise ValueError("Bevestig dat je de prijs voor deze dag hebt gezien")
                    if kind == PRICE_OFFER:
                        if offer_end is None or offer_end < delivery["date"]:
                            raise ValueError("Aanbieding moet minstens tot de bezorgdatum geldig zijn")
                        until = offer_end
                    else:
                        until = delivery["date"]   # a normal price is only proven for this exact day
                    seen_price = latest_price(seen_prices, item.get("Product"), sup)
                    snap = next((p for p in seen_prices if p.get("_row") == seen_price.get("_price_row")), None)
                    write_price(spreadsheet(), text(item["Product"]), sup, pack_price, content, unit, kind, until,
                                today, source, snap)
                    per_unit = pack_to_row_unit_price(pack_price, content)
                    norm = normalised_price(per_unit, unit)
                    saved(f"Prijs {item['Product']} bij {sup}: €{number(pack_price):.2f} per {content:g} {unit}"
                          + (f" = €{norm[0]:.2f}/{norm[1]}" if norm else ""))
                except Exception as exc:
                    save_failed(exc)
    st.markdown("**Vergelijking (zelfde mandje, hele verpakkingen)**")
    for r in comparison:
        with st.container(border=True):
            if r["complete"]:
                st.write(f"**{r['supplier']}**: €{r['total']:.2f} (boodschappen €{r['groceries']:.2f} + "
                         f"bezorging €{r['delivery']:.2f} + reis/tijd €{r['extra']:.2f})")
            else:
                st.write(f"**{r['supplier']}**: onvolledig — " + "; ".join(r["problems"]))
    st.caption("Alleen aantoonbare prijzen met bron en datum; geen live koppeling met supermarkten.")
    with st.expander("Bezorg- en reiskosten per supermarkt"):
        if suppliers:
            s_pick = st.selectbox("Supermarkt", suppliers, format_func=lambda x: x["LEVERANCIERS"], key="sup_check")
            with st.form(f"supplier_form_{s_pick['_row']}", clear_on_submit=True):
                fee = st.number_input("Bezorgkosten € gezien in checkout (leeg = niet wijzigen)", min_value=0.0,
                                      step=0.01, format="%.2f", value=None)
                extra = st.number_input("Reis- en tijdkosten € (leeg = niet wijzigen)", min_value=0.0, step=0.5,
                                        format="%.2f", value=None)
                pressed = st.form_submit_button("💾 Opslaan", width="stretch")
            if pressed:
                try:
                    if fee is None and extra is None:
                        raise ValueError("Niets ingevuld")
                    write_supplier_check(spreadsheet(), s_pick["LEVERANCIERS"], fee, today, extra,
                                         seen("Leveranciers", s_pick["_row"], s_pick["LEVERANCIERS"], "LEVERANCIERS"))
                    saved(f"{s_pick['LEVERANCIERS']} bijgewerkt")
                except Exception as exc:
                    save_failed(exc)

# --- Menu -------------------------------------------------------------------------------------------
with menu_tab:
    st.caption("Het weekmenu komt uit tab Weekmenu van de Sheet; pas het diner per dag hieronder aan.")
    for m in menu:
        with st.container(border=True):
            st.markdown(f"**{m.get('Dag', '')} · {m.get('Ritme', '')}**")
            st.write(text(m.get("Diner")))
            if m.get("Gebruik voorraad eerst"):
                st.caption(text(m.get("Gebruik voorraad eerst")))
    if menu:
        with st.expander("Diner wijzigen"):
            day = st.selectbox("Dag", menu, format_func=lambda x: x["Dag"])
            with st.form(f"menu_form_{day['_row']}", clear_on_submit=True):
                diner = st.text_input("Nieuw diner", value=text(day.get("Diner")))
                pressed = st.form_submit_button("💾 Opslaan", width="stretch")
            if pressed:
                try:
                    write_menu(spreadsheet(), int(day["_row"]), day["Dag"], diner,
                               seen("Weekmenu", day["_row"], day["Dag"], "Dag").get("Diner"))
                    saved(f"Menu {day['Dag']} bijgewerkt")
                except Exception as exc:
                    save_failed(exc)

# --- Scan -------------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False, max_entries=20)
def decode_cached(image_bytes):
    return scan.decode_barcodes(image_bytes)


@st.cache_data(show_spinner=False, ttl=24 * 3600, max_entries=500)
def lookup_cached(code):
    return scan.lookup_product(code)


with scan_tab:
    st.caption("Gratis streepjescode-scan: één of meer barcodes per foto worden lokaal gelezen. De app herkent geen "
               "producten zonder barcode en schat geen hoeveelheden; aantallen bevestig je zelf.")
    photo = st.camera_input("Foto van producten met zichtbare barcodes")
    upload = st.file_uploader("…of kies een foto", type=["jpg", "jpeg", "png"])
    picture = photo or upload
    if picture:
        try:
            codes = decode_cached(picture.getvalue())
        except Exception as exc:
            codes = []
            st.error(f"Foto kon niet gelezen worden ({type(exc).__name__}).")
        known, unknown = match_barcodes(codes, active)
        st.write(f"{len(codes)} barcode(s) gevonden · {len(known)} bekend · {len(unknown)} onbekend")
        if known:
            with st.form("scan_form", clear_on_submit=True):
                entered = {int(x["_row"]): st.number_input(f"{x['Product']} ({x.get('Eenheid', '')}) — nu "
                                                           f"{x.get('Aantal nu') if x.get('Aantal nu') != '' else 'onbekend'}",
                                                           min_value=0.0, step=1.0, value=None, key=f"sc_{x['_row']}")
                           for x in known}
                pressed = st.form_submit_button("💾 Alleen ingevulde aantallen bevestigen", type="primary",
                                                width="stretch")
            if pressed:
                try:
                    changes = confirmations(known, entered)
                    if not changes:
                        raise ValueError("Niets ingevuld")
                    base = {r: seen("Voorraad", r, p) for r, p, _, _ in changes}
                    changes = [(r, p, number(base[r].get("Aantal nu")), new) for r, p, _, new in changes]
                    write_stock(spreadsheet(), changes, today, base)
                    saved(f"{len(changes)} telling(en) bevestigd via scan")
                except Exception as exc:
                    save_failed(exc)
        for code in unknown:
            with st.container(border=True):
                try:
                    info = lookup_cached(code)
                except scan.LookupUnavailable:
                    info = None
                    st.caption("Open Food Facts is nu niet bereikbaar; probeer later opnieuw.")
                st.write(f"Barcode {code}: " + (f"{info['name']} {info['quantity']} (Open Food Facts, suggestie)"
                                               if info else "onbekend product"))
                target = st.selectbox("Koppelen aan product", [None] + active, key=f"link_{code}",
                                      format_func=lambda x: "— kies —" if x is None else x["Product"])
                if target is not None and st.button("Barcode koppelen", key=f"linkbtn_{code}"):
                    try:
                        write_details(spreadsheet(), int(target["_row"]), text(target["Product"]), {"EAN": code},
                                      seen("Voorraad", target["_row"], target["Product"]))
                        saved(f"Barcode gekoppeld aan {target['Product']}")
                    except Exception as exc:
                        save_failed(exc)

# --- Bonnen -----------------------------------------------------------------------------------------
with receipt_tab:
    st.caption("Automatisch inlezen uit Gmail is niet gekoppeld (aparte Gmail-toestemming nodig). Neem de regels "
               "van een Jumbo/AH-bestelbevestiging over; dubbele regels (zelfde winkel + bestelnummer + product) "
               "worden overgeslagen.")
    with st.form("receipt_form", clear_on_submit=True):
        r_date = st.date_input("Besteldatum", value=today, max_value=today, format="DD-MM-YYYY")
        r_sup = st.selectbox("Leverancier", supplier_names) if supplier_names else st.text_input("Leverancier")
        r_nr = st.text_input("Bestelnummer")
        r_lines = st.text_area("Regels: product; aantal; eenheid; bedrag", placeholder="Kaas; 1; stuks; 4,99")
        pressed = st.form_submit_button("💾 Bon opslaan", width="stretch")
    if pressed:
        try:
            entries, skipped = new_receipt_rows(receipts, parse_receipt_lines(r_lines, r_date, r_sup, r_nr))
            written, dup = write_receipts(spreadsheet(), entries, today) if entries else (0, 0)
            saved(f"{written} regel(s) opgeslagen, {len(skipped) + dup} dubbel overgeslagen")
        except Exception as exc:
            save_failed(exc)
    if receipts:
        st.caption(f"{len(receipts)} bonregels in de Sheet; laatste: "
                   + ", ".join(f"{x.get('Product')} ({x.get('Leverancier')})" for x in receipts[-3:]))

st.caption(f"Vandaag: {today:%d-%m-%Y} · Max. €{MAX_BUDGET:.2f} · Geen automatische bestelling of betaalde AI-calls")
st.session_state["seen_data"] = data   # what this user saw; baseline for the next save
