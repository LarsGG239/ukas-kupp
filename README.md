# Ukas kupp

Henter ukas tilbudsaviser og prisfall for handlelista di to ganger i uka (mandag og torsdag morgen) og lager en nettside med de beste tilbudene.

**Kilder**
- **Tilbudsavisene** (Tjek, dataene bak Mattilbud/eTilbudsavis): ukas avistilbud fra Kiwi, Rema 1000, Extra og de andre kjedene i nærheten, med førpris når kjeden oppgir den. Dette er et uoffisielt API uten dokumentasjon, så det kan endre seg eller slutte å virke. Ment for privat bruk.
- **Kassalapp** (valgfritt): prisfall målt mot vanlig pris de siste 120 dagene, og billigste alternativ per kg/l.

## Oppsett (ca. 10 minutter)

1. **Repo:** Lag et nytt *offentlig* repo på GitHub og last opp alle filene herfra, også mappen `.github`.
2. **Sted:** Sjekk `sted.txt`. Standard er Trondheim sentrum med 8 km radius.
3. **Kassalapp (valgfritt):** Lag en nøkkel på https://kassal.app/api og legg den inn under Settings → Secrets and variables → Actions → New repository secret, med navnet `KASSALAPP_API_KEY`. Uten nøkkel brukes bare tilbudsavisene.
4. **Første kjøring:** Actions → «Ukas kupp» → Run workflow. Vent til den er grønn, og sjekk loggen for hvilke aviser som ble funnet.
5. **Nettside:** Settings → Pages → Source: «Deploy from a branch», branch `main`, mappe `/docs`. Etter et minutt ligger siden på `https://<brukernavn>.github.io/<repo>/`.

Legg siden til på hjemskjermen på mobilen, så fungerer den som en app.

## Daglig bruk

- `handleliste.txt`: én vare per linje. Alle ordene må finnes i varenavnet, så «kaffe» treffer «Evergood kaffe», mens «melk» også treffer «sjokomelk».
- `butikker.txt`: kjedene som tas med.
- `sted.txt`: hvor du handler.

Siden oppdateres automatisk når du lagrer en av filene, også fra GitHub-appen.

## Kjøre lokalt

    python kupp.py                              # bare tilbudsavisene
    KASSALAPP_API_KEY=din_nøkkel python kupp.py # begge kildene

Åpne deretter `docs/index.html`. Ingen pakker må installeres.
