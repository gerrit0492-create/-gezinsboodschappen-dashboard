# Gezinsboodschappen-dashboard

Mobiel (iPhone-vriendelijk) Streamlit-dashboard voor voorraad en weekboodschappen van een huishouden.
Deze repository bevat **alleen applicatiecode**. Alle gegevens (voorraad, prijzen, menu, bonnen) staan in een
**eigen, privé Google Sheet** van de gebruiker; er staat geen data, wachtwoord of sleutel in deze repository.

## Toegang en beveiliging

- **Inloggen met Google** via Streamlit's ingebouwde OIDC (`st.login`), daarna een **expliciete allowlist**
  (`ALLOWED_EMAILS`, alleen geverifieerde e-mailadressen). Alles is dicht tot beide kloppen (deny by default):
  zonder toegang wordt de Google Sheet niet eens geopend. Er is geen gedeeld wachtwoord.
- Vóór het inloggen toont de app alleen de titel en een inlogknop. Foutmeldingen tonen geen inhoud, alleen het
  type fout. De gedeelde cache met Sheet-data wordt pas na de toegangscontrole aangesproken.
- **Sleutelloze Sheet-toegang**: de app gebruikt het eigen Google-toegangstoken van de ingelogde gebruiker
  (`st.user.tokens["access"]`, scope `https://www.googleapis.com/auth/spreadsheets`). Er is geen serviceaccount en
  geen sleutelbestand. Elke toegestane gebruiker moet de Sheet zelf mogen bewerken (Delen → Bewerker).
- Het token wordt alleen server-side gebruikt, nooit getoond of gelogd; gegevens worden per gebruiker en per token
  gecachet, nooit gedeeld tussen gebruikers. Uitloggen trekt het token ook bij Google in. Alle geheimen staan uitsluitend in de Streamlit-secrets van de host.
- **Sessieduur**: Streamlit bewaart het toegangstoken bij het inloggen en vernieuwt het niet. Google-toegangstokens
  verlopen na ongeveer een uur; daarna meldt de app "sessie verlopen" en log je opnieuw in. Er worden geen
  refresh-tokens gebruikt, dus de 7-dagenlimiet van refresh-tokens in Google's OAuth-testmodus speelt niet.
- De scope `spreadsheets` geeft technisch toegang tot alle spreadsheets van de gebruiker; de app opent alleen
  `SHEET_ID`. Een smallere scope (`drive.file`) werkt alleen voor bestanden die via de app zijn gemaakt of gekozen
  en is daarom niet bruikbaar voor een bestaande Sheet zonder extra bestandskiezer.
- De app plaatst nooit zelf een bestelling, verstuurt geen e-mail en doet geen betaalde AI-calls.

## Functies

- **Overzicht**: GO/NO-GO met een lijst "dit moet nog", budget, verwacht bedrag, bevestigde bezorgkosten, besparing
  t.o.v. andere supermarkten, bijna-op, houdbaarheid, weekmenu en een knop om tab `Weeklijst` bij te werken.
- **Voorraad**: tellen wat nodig is, per product telling/locatie/houdbaarheid, "opgebruikt" (= bevestigde 0),
  archiveren met bevestiging (niets wordt verwijderd), product toevoegen.
- **Bestellen**: afvinkbare boodschappenlijst en de bezorgcontrole (leverancier, datum, tijdvak, bron, bedrag).
- **Prijzen**: prijs per verpakking + inhoud per supermarkt met bron en datum; omrekening naar €/kg, €/liter of
  €/stuk; vergelijking van hetzelfde mandje (hele verpakkingen) incl. bezorg- en reis-/tijdkosten.
- **Menu**: diner per dag aanpasbaar (tab `Weekmenu`).
- **Scan**: gratis streepjescode-scan (één of meer EAN/UPC-codes per foto, lokaal gelezen met zxing-cpp), met een
  naamsuggestie van Open Food Facts. Geen herkenning zonder barcode en geen schatting van hoeveelheden: aantallen
  bevestig je zelf.
- **Bonnen**: regels van een bestelbevestiging invoeren; regels die al in de Sheet staan worden overgeslagen.

## GO / NO-GO

GO alleen als alles klopt; anders NO-GO met de redenen. "Bestelweek" = de 6 dagen vóór de bezorgdatum t/m vandaag.

- Bezorgcontrole met leverancier, bezorgdatum, tijdvak, bron en controledatum in de bestelweek, en bezorgkosten
  **exact €0,00**.
- Elk te bestellen product staat in `Voorraad` en elke actieve regel ervan is bevestigd geteld in de bestelweek
  (een foto is nooit een telling). Een getelde 0 is geldig; onbekend nooit. Eenheden en hoeveelheden kloppen.
- Elke prijs komt uit `Prijzen` van dezelfde leverancier, is in de bestelweek gecontroleerd en geldt op de
  bezorgdatum (normale prijs: exact die dag; aanbieding: einddatum ≥ bezorgdatum).
- Totaal (hele verpakkingen, centen) incl. bezorging ≤ min(`Weekbudget`, €150,00).

## Verwacht Sheet-schema

De app voegt zelf geen kolommen of tabbladen toe; ontbreekt iets, dan meldt de app "Sheet-inrichting nodig" (NO-GO).

| Tab | Benodigde kolommen (rij 1) |
|---|---|
| `Voorraad` | `Product`, `Locatie`, `Aantal nu`, `Eenheid`, `Minimum`, `Zekerheid`, `Laatst gezien`, `Bron`, `Houdbaar tot`, `Status`, `EAN` |
| `Aanvullijst` | `Product`, `Eenheid`, `Weekdoel`, `Voorraad nu`, `Te bestellen`, `In mand` |
| `Prijzen` | `Product`, `Leverancier`, `Prijs verpakking €`, `Verpakking`, `Prijs/eenheid €`, `Prijssoort`, `Geldig t/m`, `Prijsdatum`, `Bron` |
| `Leveranciers` | `LEVERANCIERS`, `Koopwijze`, `Bezorgkosten gecontroleerd €`, `Bezorging gecontroleerd op`, `Reis- en tijdkosten €` |
| `Bonnen` | `Besteldatum`, `Leverancier`, `Product`, `Aantal`, `Eenheid`, `Betaald €`, `Bestelnummer`, `Status`, `Verwerkt op` |
| `Instellingen` | kolom A/B: `Weekbudget`, `Werkelijke bezorgkosten`, `Bezorgkosten gecontroleerd op`, `Bezorging leverancier`, `Bezorgdatum`, `Bezorgmoment`, `Bezorgkosten bron` |
| `Weekmenu` | `Dag`, `Ritme`, `Diner` (optioneel `Gebruik voorraad eerst`) |
| `Weeklijst` | `Onderdeel`, `Item`, `Aantal`, `Eenheid`, `Bedrag €`, `Opmerking` (wordt door de app gevuld) |

## Opslag

Elke schrijfactie leest eerst opnieuw en vergelijkt met wat de gebruiker op het scherm zag; is er intussen iets
veranderd (andere sessie, verschoven rij, verplaatste kolom), dan wordt niets geschreven. Formules worden nooit
overschreven; tekst wordt altijd letterlijk opgeslagen (geen formule-injectie). Nieuwe rijen gaan in één verzoek.
Beperking: dit is controleren-dan-schrijven, geen atomische compare-and-swap.

## Installeren op Streamlit Community Cloud (publieke app, afgeschermde inhoud)

1. Google Cloud-project: schakel de **Google Sheets API** in. Er is **geen serviceaccount of sleutel** nodig.
2. **Google Auth Platform → Branding/Audience**: type *External*, publicatiestatus *Testing*, en voeg de toegestane
   accounts toe als **testgebruikers**. Onder **Data Access** de scope
   `https://www.googleapis.com/auth/spreadsheets` toevoegen. Omdat dit een gevoelige scope is en de app niet door
   Google geverifieerd is, toont Google bij het inloggen een waarschuwing "app niet geverifieerd"; testgebruikers
   kunnen via *Geavanceerd → Doorgaan* verder.
3. **Clients → Create client → Web application** met redirect-URI `https://<app-naam>.streamlit.app/oauth2callback`.
4. Deel de Google Sheet met elk toegestaan account als **Bewerker**.
5. https://share.streamlit.io → *Create app* → deze repository, branch `main`, bestand `app.py`.
6. *App settings → Secrets*: vul `.streamlit/secrets.toml.example` in (`ALLOWED_EMAILS`, `SHEET_ID`, `[auth]` met
   eigen `cookie_secret`, `client_id`, `client_secret`, `expose_tokens` en `client_kwargs`).
7. Test: inloggen met een toegestaan én een niet-toegestaan account; bij inloggen toegang tot Spreadsheets
   toestaan; één telling opslaan en in de Sheet terugzien; app herstarten en opnieuw controleren; na ruim een uur
   controleren dat "sessie verlopen" verschijnt en opnieuw inloggen werkt.

## Lokaal ontwikkelen

```bash
pip install -r requirements.txt pytest
python -m pytest -q        # unit-tests, fake-Sheet-adaptertests en Streamlit AppTest; geen netwerk of secrets nodig
```
