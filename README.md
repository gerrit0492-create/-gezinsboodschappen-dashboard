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
- **Sleutelloze, beperkte Sheet-toegang**: de app gebruikt het eigen Google-toegangstoken van de ingelogde gebruiker
  (`st.user.tokens["access"]`) met **alleen** de scope `https://www.googleapis.com/auth/drive.file`. Daarmee kan de app
  uitsluitend bestanden gebruiken die de gebruiker er zelf voor kiest. Elke toegestane gebruiker kiest de voorraad-Sheet
  **eenmalig** in Google Picker; Google onthoudt die keuze per gebruiker. Brede scopes (`spreadsheets`, `drive`) worden
  door de app geweigerd: de scopes moeten exact `openid email profile` plus `drive.file` zijn. Er is geen
  serviceaccount en geen sleutelbestand. Kiest iemand per ongeluk een ander bestand, dan gebruikt de app dat niet,
  maar Google onthoudt die keuze; intrekken kan via https://myaccount.google.com/permissions.
- Was de app eerder met de brede `spreadsheets`-scope gebruikt, trek die oude toestemming dan in via
  https://myaccount.google.com/permissions en log opnieuw in.
- De app gebruikt nooit het bestand dat de Picker teruggeeft: het opent alleen `SHEET_ID` uit de secrets en vergelijkt
  de keuze daarmee. Een verkeerde of gemanipuleerde keuze geeft geen toegang. Elke gebruiker moet de Sheet ook zelf
  mogen bewerken (Delen → Bewerker).
- De bestandskeuze opent op een eigen Google-pagina via OAuth met uitsluitend `drive.file`. De callback
  is kort geldig, ondertekend en aan de bestaande login gebonden. PKCE beschermt de autorisatiecode.
  De app wisselt die server-side in en gebruikt voor de Sheet altijd het toegangstoken van de bestaande login.
  Streamlit bewaart het logintoken in zijn ondertekende HttpOnly login-cookie. Gegevens worden per gebruiker
  en per token gecachet.
- **Sessieduur**: Google-toegangstokens verlopen na ongeveer een uur; daarna meldt de app "sessie verlopen" en log je
  opnieuw in (de bestandskeuze blijft bewaard). Er worden geen refresh-tokens gebruikt, dus de 7-dagenlimiet van
  Google's OAuth-testmodus speelt niet. Uitloggen trekt het token bewust niet in bij Google: dat zou ook de
  bestandskeuze wissen.
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

1. Google Cloud-project: schakel de **Google Sheets API** en de **Google Picker API** in. Geen serviceaccount nodig.
2. **Google Auth Platform**: Audience *External*, status *Testing*, toegestane accounts als **testgebruikers**.
   Onder **Data Access** alleen de scope `https://www.googleapis.com/auth/drive.file` (niet-gevoelig; geen
   "app niet geverifieerd"-melding voor deze scope).
3. Gebruik dezelfde **Web application**-client. Voeg onder *Authorized redirect URIs* naast de bestaande
   `https://<app-naam>.streamlit.app/oauth2callback` ook `https://<app-naam>.streamlit.app/` toe.
   De tweede URI ontvangt uitsluitend de bestandskeuze. De afsluitende slash hoort erbij.
4. Deel de Google Sheet met elk toegestaan account als **Bewerker**.
5. https://share.streamlit.io → *Create app* → deze repository, bestand `app.py`.
6. Vul onder *App settings → Secrets* `.streamlit/secrets.toml.example` in. De bestandskeuze gebruikt
   dezelfde OAuth-client en heeft geen Picker API-key nodig.
7. Test: inloggen, eenmalig het voorraadbestand kiezen, één telling opslaan en in de Sheet terugzien, herstarten,
   opnieuw inloggen zonder opnieuw te hoeven kiezen, en een niet-toegestaan account weigeren.

## Lokaal ontwikkelen

```bash
pip install -r requirements.txt pytest
python -m pytest -q        # unit-tests, fake-Sheet-adaptertests en Streamlit AppTest; geen netwerk of secrets nodig
```
