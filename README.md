# Energie Adviseur

Zelflerende integratie voor Home Assistant. Hij leert de programma's van je wasmachine, droger en vaatwasser uit hun stroomverbruik en adviseert wanneer je ze het goedkoopst aanzet. Daarnaast leert hij de warmtevraag van je huis uit het gasverbruik en vergelijkt hij stoken op gas met stoken met de airco's.

De integratie geeft alleen advies; hij schakelt zelf niets.

## Installeren

### Via de Marketplace (Home Assistant 2026.11 of nieuwer)

1. Open [deze link](https://my.home-assistant.io/redirect/marketplace_repository/?owner=basschoren&repository=energie-adviseur&category=integration) op een apparaat waarop je bij Home Assistant bent ingelogd. De Marketplace biedt aan om de repository als aangepaste repository toe te voegen.
2. Installeer *Energie Adviseur* vanuit **Instellingen → Marketplace**.
3. Ga naar **Instellingen → Apparaten & diensten → Integratie toevoegen** en zoek op *Energie Adviseur*.

Op Home Assistant 2026.10 voeg je dezelfde repository in HACS toe als aangepaste repository van het type *Integratie*.

### Handmatig

1. Kopieer de map `custom_components/energie_adviseur` naar de map `config/custom_components/` van Home Assistant (bijvoorbeeld via de Samba- of File editor-app).
2. Herstart Home Assistant.
3. Ga naar **Instellingen → Apparaten & diensten → Integratie toevoegen** en zoek op *Energie Adviseur*.

## Instellen

Voeg de integratie meerdere keren toe; elke keer maak je één onderdeel aan.

| Volgorde | Onderdeel | Wat je invult |
|---|---|---|
| 1 | Algemene instellingen | Prijssensor, zonne-forecast, basisverbruik, aftrek op teruglevering, optioneel een lokale AI |
| 2 | Droger | Vermogenssensor en drempels |
| 3 | Wasmachine | Vermogenssensor, drempels en de gekoppelde droger |
| 4 | Vaatwasser en overige apparaten | Vermogenssensor en drempels |
| 5 | Verwarming | Gasmeter, binnen- en buitentemperatuur, verbruik van de airco's, gasprijs, COP |

Instellingen wijzig je later via **Configureren** bij het onderdeel.

### Prijssensor
De sensor moet de prijzen van vandaag en morgen als lijst in zijn attributen hebben. Dat is zo bij onder meer Nordpool, ENTSO-e, Frank Energie, Zonneplan en de Tibber-prijssensoren uit HACS. De sensor *Huidige stroomprijs* van Energie Adviseur toont in zijn attributen hoeveel prijsblokken er zijn gevonden; staat daar 0, dan past het formaat niet.

### Aftrek op teruglevering
Dit is hoeveel een teruggeleverde kWh minder oplevert dan een afgenomen kWh kost. Bij 0 maakt het voor de kosten niet uit of je eigen zonnestroom gebruikt, en telt de zonne-forecast dus niet mee. Vul hier het werkelijke verschil in zodra je voor teruglevering minder krijgt dan je voor afname betaalt.

## Programma's leren

1. Draai een programma. Na afloop verschijnt de melding *onbekend programma*.
2. Typ een naam bij **Laatste cyclus benoemen**, of kies een bestaand programma bij **Laatste cyclus toewijzen**.
3. Vanaf dan herkent de integratie dat programma aan duur, verbruik en profielvorm. Elke herkende cyclus scherpt het gemiddelde aan.

Eerdere cycli inlezen kan met de dienst `energie_adviseur.import_history` (standaard de laatste 10 dagen; verder terug bewaart Home Assistant de ruwe metingen meestal niet). Onbekende cycli staan als attribuut bij de sensor *Laatste programma*; benoem er één per programma met `energie_adviseur.label_cycle` en de rest wordt automatisch meegenomen.

### De pagina Energie Adviseur

In de zijbalk van Home Assistant staat de pagina **Energie Adviseur** (alleen voor beheerders). Per apparaat kun je daar:

- **Historie inlezen**: kies het aantal dagen en klik op *Historie inlezen*.
- **Cycli een naam geven**: typ een naam achter de cyclus of kies een eerder gebruikte naam uit de suggesties. Een nieuwe naam wordt als programma bewaard en staat de volgende keer in de lijst.
- **Cycli splitsen**: klik in de grafiek op het moment waar geknipt moet worden. Je ziet de duur en het verbruik van beide delen voordat je bevestigt, en je kunt één deel laten vervallen. De oranje band is de rustigste periode; met *Rustigste periode gebruiken* knip je die er in één keer tussenuit.

- **Cycli verwijderen**: met *Verwijderen* haal je een cyclus weg, bijvoorbeeld als dezelfde beurt twee keer is herkend. Het programma wordt opnieuw berekend en historie inlezen haalt de cyclus niet terug.

- **Splitsing ongedaan maken**: bij elk deel van een gesplitste cyclus staat *Splitsing ongedaan maken*. Dat zet de cyclus van voor de splitsing terug, zodat je opnieuw kunt splitsen. Bij meerdere splitsingen ga je stap voor stap terug.
- **Opnieuw ophalen**: leest de periode van een cyclus opnieuw uit de historie van Home Assistant. Gebruik dit voor cycli die zijn ingelezen of gesplitst met een versie ouder dan 0.5: die zijn alleen per 5 minuten bewaard en hebben geen bewaarde cyclus van voor de splitsing. Dit kan zolang Home Assistant de ruwe metingen nog heeft (standaard ongeveer 10 dagen).

Splitsen gaat per 30 seconden. Na een klik in de grafiek stel je het moment fijn af met het minutenveld of met de pijltjestoetsen (Shift verspringt 5 minuten). Namen die je aan delen had gegeven, vervallen als je de splitsing ongedaan maakt.

Na een update van de integratie moet je Home Assistant herstarten voordat de pagina verschijnt.

### Cycli splitsen met een actie

Soms telt de integratie twee beurten als één cyclus, bijvoorbeeld als je direct na elkaar wast of als het apparaat na afloop blijft nadraaien (kreukbeveiliging). Bij elke onbekende cyclus staat daarom een `rustigste_periode`: het moment midden in de cyclus waarop het verbruik het laagst was.

Zonder de pagina kan het ook, met de actie `energie_adviseur.split_cycle`:

```yaml
action: energie_adviseur.split_cycle
data:
  appliance: Wasmachine
  cycle_id: "1794127739"
```

- Zonder `minutes` wordt geknipt op de rustigste periode; die periode zelf vervalt.
- Met `minutes` knip je op een zelf gekozen aantal minuten na de start, per halve minuut (bijvoorbeeld `97.5`).
- Met de actie `energie_adviseur.restore_cycle` maak je een splitsing ongedaan of haal je de cyclus opnieuw uit de historie.
- Met `keep: first` of `keep: second` bewaar je maar één deel, bijvoorbeeld om het nadraaien weg te gooien.

De delen worden daarna meteen vergeleken met de programma's die je al hebt geleerd. Bij `appliance` mag je de naam van het apparaat invullen.

Wordt een programma niet herkend of verward met een ander, pas dan de *Tolerantie bij herkennen* aan. Programma's met sterk wisselende duur, zoals sensor-drogen, kun je het best per belading een eigen naam geven.

## Advies gebruiken

Per apparaat:

- **Programma voor advies**: kies wat je wilt draaien.
- **Beste starttijd**, **Verwachte kosten**, **Besparing t.o.v. nu starten**.
- **Nu gunstig om te starten**: aan als nu starten hooguit 2 cent of 5% duurder is dan het beste moment.

Wasdag (bij de wasmachine):

- Zet **Aantal wasjes vandaag** en kies het **Droogprogramma**.
- **Wasplanning** toont de eerstvolgende stap; het attribuut `planning` bevat alle start- en eindtijden.
- De teller loopt af wanneer een was klaar is. Een was die klaar is maar nog niet gedroogd, blijft in de planning staan.

Verwarming:

- **Stookadvies** zegt of airco of gas nu goedkoper is; het attribuut `airco_voordeliger` geeft de perioden.
- **Warmtevraag per graaddag** verschijnt na minstens 7 dagen metingen. Bij de eerste start wordt tot 120 dagen historie ingelezen uit de statistieken.
- De COP van de airco's is een aanname die je zelf instelt. **Geschatte COP (indicatief)** is een ruwe schatting uit dagen waarop de airco's draaiden; zon, wind en stookgedrag beïnvloeden die.

## Lokale AI

De berekeningen gebruiken geen AI. Kies je bij de algemene instellingen een AI Task- of gespreksentiteit van Ollama, dan herschrijft die het dagadvies tot een lopende tekst. Lukt dat niet, dan blijft het advies op basis van de regels staan.

Voor een i5-6600T (4 kernen, geen grafische kaart) past een klein model van 3 à 4 miljard parameters:

```bash
ollama pull gemma3:4b
```

Met 8 GB werkgeheugen of minder is `llama3.2:3b` de veiligere keuze. Zet in de Ollama-integratie de besturing van Home Assistant (Assist) voor deze agent uit; dat scheelt veel rekentijd.

## Dashboard

Voorbeeld van een kaart per apparaat (pas de entiteitsnamen aan):

```yaml
type: entities
title: Wasmachine
entities:
  - entity: sensor.wasmachine_status
  - entity: select.wasmachine_programma_voor_advies
  - entity: sensor.wasmachine_beste_starttijd
  - entity: sensor.wasmachine_verwachte_kosten
  - entity: sensor.wasmachine_besparing_t_o_v_nu_starten
  - entity: binary_sensor.wasmachine_nu_gunstig_om_te_starten
  - entity: number.wasmachine_aantal_wasjes_vandaag
  - entity: select.wasmachine_droogprogramma
  - entity: sensor.wasmachine_wasplanning
  - entity: text.wasmachine_laatste_cyclus_benoemen
```

Dagadvies als tekst:

```yaml
type: markdown
title: Energieadvies
content: "{{ state_attr('sensor.energie_adviseur_dagadvies', 'advies') }}"
```

## Automatiseren

Na elke cyclus vuurt de integratie de gebeurtenis `energie_adviseur_cycle_finished` af met `name`, `program`, `duration_min` en `energy_kwh`. Daarmee kun je bijvoorbeeld een melding op je telefoon sturen als de was klaar is.

## Ontwikkelen

- `python -m unittest discover -s tests` test de rekenkern (geen Home Assistant nodig).
- `pytest tests_ha` test de integratie tegen een Home Assistant-kern; hiervoor zijn `pytest-homeassistant-custom-component` en `home-assistant-frontend` nodig. Dit werkt alleen op Linux, macOS of WSL, niet op Windows zelf.
- Bij elke push controleert GitHub Actions de repository met de HACS-actie, hassfest en beide testsets.
