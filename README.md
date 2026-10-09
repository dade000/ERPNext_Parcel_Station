# ERPNext Parcel Station

**Packplatz und Versand für ERPNext**: Lieferscheine scannen, Pakete wiegen,
Versandlabels bei GLS, der Österreichischen Post und FedEx erstellen, drucken und
die Sendungen bis zur Zustellung verfolgen. Alles direkt im ERPNext-Desk.

Die App ist im Betrieb eines kleinen österreichischen Versandhändlers entstanden
und läuft dort täglich produktiv. Deshalb ist sie für Carrier vorbereitet, die
in Österreich üblich sind, und die Oberfläche ist auf Deutsch.

---

## Funktionen

### Packplatz (Seite „Parcel Station“)

- **Arbeitsliste „Zu packen“**: Alle Lieferscheine mit Versandart, für die noch
  kein gültiges Label existiert. Sie wird live mit dem Desk und anderen
  Packplätzen abgeglichen und zeigt den Fortschritt des Tages.
- **Scan-Ablauf**: Lieferschein scannen, Artikel scannen, Mengen werden
  geprüft. Product Bundles werden in ihre gepackten Einzelartikel aufgelöst,
  Nicht-Lagerartikel lassen sich per Artikelgruppe als Ware freigeben.
- **Gewicht und Maße**: Das Gewicht kommt von der Waage (über die Hardware
  Bridge oder einen HTTP-Endpunkt). Maße werden von Hand eingegeben oder als
  Barcode gescannt.
- **Label auf Knopfdruck**: Die App erstellt das Shipment, fordert das Label beim
  Carrier an und druckt es auf Wunsch automatisch. Schlägt der Carrier fehl,
  wird dieselbe Sendung erneut versucht, statt eine Dublette anzulegen.
- **Drucken**: Das Format richtet sich nach dem gewählten Drucker:
  - ZPL roh an einen Labeldrucker über die
    [ERPNext Hardware Bridge](https://github.com/dade000/erpnext-hardware-bridge),
  - ZPL roh an eine CUPS-Queue,
  - PDF vom Carrier über den Druckdialog des Browsers (für Packplätze ohne
    Labeldrucker).

  Labels werden immer genau so gedruckt, wie der Carrier sie liefert, und nie
  umgerechnet. Zolldokumente gehen an einen eigenen A4-Drucker.

### Carrier

| Carrier | Label | Storno | Tracking | Besonderheiten |
|---|---|---|---|---|
| **GLS** (ShipIT REST) | ✓ | ✓ | ✓ (Polling) | ParcelShop-Zustellung und -Suche, FlexDelivery, Incoterm für Nicht-EU |
| **Österreichische Post** (PLC SOAP) | ✓ | ✓ | ✓ (POSTTRACK-Dateien per SFTP) | Zollangaben, Hinterlegung bei Post-Partnern |
| **FedEx** (REST API) | ✓ | ✓ | ✓ (Track API) | Electronic Trade Documents (papierlos), DAP/DDP, automatische Abholbuchung |

- **Carrier Service** (DocType) beschreibt eine Versandart: Carrier, Produktcode,
  Versandartikel für die Rechnung, erlaubte Zielländer, Lieferzeit in Tagen
  sowie Verzollung (DAP oder DDP). Ein Service wird pro Zielland gepflegt, auch
  wenn der Produktcode gleich bleibt.
- **Zentrales Routing** (`parcel/carrier_routing.py`): An einer einzigen Stelle
  wird entschieden, an welchen Carrier ein Shipment geht.
- **Liefertermine** werden aus den Lieferzeiten des Carrier Service für Auftrag,
  Lieferschein und Shipment berechnet.
- **Einfuhrabgaben (DDP)**: Zoll und Einfuhrumsatzsteuer für Länder außerhalb
  der EU können geschätzt und schon beim Kauf mitkassiert werden. Der Carrier
  stellt die tatsächlichen Abgaben dann dem Absender in Rechnung.

### Sendungsverfolgung und Kundenkommunikation

- **Tracking-Events** aller Carrier landen einheitlich im *Parcel Tracking
  Event*. Am Shipment stehen ein normalisierter Status (unterwegs, zugestellt,
  zur Abholung bereit, Problem, Retoure …) und eine kurze Statusinfo.
- **Täglicher Tracking-Bericht** per Mail mit allem, was ein Mensch ansehen
  muss: Probleme und Retouren, hängende Sendungen, nicht zugeordnete Events.
  Ist alles in Ordnung, kommt keine Mail.
- **Versandbenachrichtigung**: Zweimal täglich bekommt jeder Kunde eine
  Sammelmail für alle neu gelabelten Pakete. Sie verwendet Email Templates in
  DE/EN, optional mit Lieferschein-PDF und einem Link auf eine
  Sendungsseite.
- **Abholerinnerung**: Kunden, deren Paket länger als eine einstellbare Zahl von
  Tagen in einer Filiale oder einem ParcelShop liegt, bekommen eine Erinnerung.
- **FedEx-Abholung**: Montag bis Freitag um 12:00 wird automatisch ein Courier
  Pickup für alle offenen FedEx-Sendungen gebucht. Manuelles Buchen und
  Stornieren ist auch direkt am Packplatz möglich.
- **Tracking-API** für einen Webshop:
  `erpnext_parcel_station.parcel.tracking.api.get_order_tracking`
  (siehe [docs/tracking_api.md](docs/tracking_api.md)).

Alle Benachrichtigungen und die automatische Abholung sind nach der Installation
**ausgeschaltet** und werden in den jeweiligen Settings aktiviert.

---

## Voraussetzungen

- Frappe Framework und ERPNext **v15**
- Python ≥ 3.10
- `paramiko` (wird mitinstalliert; für den SFTP-Abruf der Post)
- optional `pycups` und ein erreichbarer CUPS-Server, wenn über CUPS gedruckt
  werden soll
- optional die [ERPNext Hardware Bridge](https://github.com/dade000/erpnext-hardware-bridge)
  auf dem Packplatz-PC für Waage und Labeldrucker
- Zugangsdaten der Carrier (GLS ShipIT, Post PLC, FedEx Developer Portal)

## Installation

```bash
cd ~/frappe-bench
bench get-app https://github.com/dade000/ERPNext_Parcel_Station
bench --site <deine-site> install-app erpnext_parcel_station
bench --site <deine-site> migrate
bench build --app erpnext_parcel_station
```

## Einrichtung

1. **Parcel Station Settings**: Absenderadresse, Standard-Carrier und
   Artikelgruppen, die gepackt werden dürfen. Außerdem Schalter und Email
   Templates für Tracking-Bericht, Versandbenachrichtigung und Abholerinnerung.
2. **Carrier-Settings**: Zugangsdaten und Endpunkte pro Carrier:
   - *GLS Settings*: API-Benutzer, Customer-ID, Contact-ID, Tracking.
   - *Austrian Post Settings*: Client-ID, OrgUnit-ID/-GUID, SFTP-Zugang für
     POSTTRACK-Dateien.
   - *FedEx Settings*: Sandbox- und Produktiv-Zugangsdaten (getrennt für Ship
     und Track API), Labelformat, Zoll, Abholfenster.
3. **Carrier** und **Carrier Service** anlegen, dabei Versandartikel und
   Zielländer zuordnen.
4. Am Auftrag oder Lieferschein den **Carrier Service** wählen. Der Lieferschein
   erscheint danach in der Arbeitsliste des Packplatzes.
5. Optional: **Scale Settings** für eine Waage mit HTTP-Endpunkt (ohne Hardware
   Bridge) und *Network Printer Settings* für CUPS-Drucker.

Der Packplatz liegt unter **`/app/parcel-station`**.

## Zeitgesteuerte Jobs

| Zeit | Job |
|---|---|
| stündlich 06:15–22:15 | POSTTRACK-Dateien der Post per SFTP abholen |
| stündlich 06:22–22:22 | FedEx- und GLS-Tracking abfragen |
| 08:30 | Tracking-Bericht |
| 09:00 | Abholerinnerungen |
| 12:30 und 17:30 | Versandbenachrichtigungen |
| 12:00 Mo–Fr | FedEx-Abholung buchen |

## Tests

```bash
bench --site <test-site> run-tests --app erpnext_parcel_station
```

Die meisten Tests arbeiten mit Mocks und brauchen keine Carrier-Zugänge.
Achtung: ERPNext bereitet eine Site vor den Tests vor (`before_tests`) und
verändert dabei Stammdaten. Tests daher nur auf einer eigenen Test-Site
ausführen, nie auf einer Site mit echten Daten.

## Mitwirken

Issues und Pull Requests sind willkommen. Das Repo verwendet `pre-commit`
(ruff, eslint, prettier, pyupgrade):

```bash
cd apps/erpnext_parcel_station
pre-commit install
```

## Lizenz

[MIT](license.txt) © 2025–2026 Daniel Devich
