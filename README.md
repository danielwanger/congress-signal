# CongressSignal

Alert-Bot, der neu gemeldete Aktien-Trades von US-Kongressmitgliedern
(Standard-Filter: Pelosi, frei konfigurierbar) überwacht und bei neuen
Einträgen eine Telegram-Nachricht schickt.

Läuft **komplett kostenlos** auf Basis der offiziellen Periodic
Transaction Reports (PTRs) des House Clerk — kein bezahltes API nötig.

## Funktionsweise

1. `tracker_pdf.py` lädt den jährlichen Bulk-Index des House Clerk
   (`{JAHR}FD.zip`), der alle Filings als XML auflistet.
2. Gefiltert wird auf Filing-Typ „P" (Periodic Transaction Report) und
   auf Nachnamen aus der Watchlist.
3. Für jedes passende Filing wird die zugehörige PDF geladen und mit
   `pdfplumber` nach Transaktionszeilen durchsucht.
4. Neue Transaktionen (nicht in `state/seen_filings.json`) lösen eine
   Telegram-Nachricht aus.

Wichtig: Kongressmitglieder haben laut STOCK Act 45 Tage Zeit, einen
Trade zu melden. Die Benachrichtigung kommt also zeitversetzt zum
eigentlichen Trade — das betrifft jede Datenquelle gleichermaßen,
nicht nur diese.

## ⚠️ Vor dem produktiven Einsatz unbedingt verifizieren

Der Bulk-Index-Endpunkt und das PDF-Layout folgen einem seit Jahren
bekannten, aber **inoffiziellen** Muster. Das Skript konnte in der
Entwicklungsumgebung nicht gegen die echte Seite getestet werden
(Netzwerk-Sandbox ohne Zugriff auf `house.gov`). Bitte vor dem ersten
Cron-Lauf lokal:

1. `python tracker_pdf.py` einmal manuell ausführen und prüfen, ob der
   ZIP-Download klappt (HTTP 200).
2. Die Konsolen-Ausgabe / Telegram-Nachrichten stichprobenartig gegen
   2–3 echte PDFs auf [disclosures-clerk.house.gov](https://disclosures-clerk.house.gov/FinancialDisclosure)
   prüfen.
3. Falls keine Treffer erkannt werden, obwohl PDFs existieren: das
   Regex `TRANSACTION_LINE_REGEX` in `tracker_pdf.py` an das
   tatsächliche PDF-Textlayout anpassen (Formulare ändern sich
   gelegentlich zwischen Jahren).

## Setup

### 1. Telegram-Bot anlegen

1. In Telegram mit [@BotFather](https://t.me/BotFather) chatten, `/newbot` senden,
   Namen vergeben → Bot-Token kopieren.
2. Mit dem neuen Bot eine Unterhaltung starten (`/start` schicken).
3. Eigene Chat-ID herausfinden, z. B. über [@userinfobot](https://t.me/userinfobot).

### 2. Lokal testen

```bash
git clone <dein-repo-url>
cd congress-signal
pip install -r requirements.txt
cp .env.example .env   # Werte eintragen
export $(cat .env | xargs)
python tracker_pdf.py
```

### 3. Automatisierung über GitHub Actions

Repo-Settings → *Secrets and variables* → *Actions*:

- **Secrets**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Variables** (optional): `WATCHLIST` (z. B. `Pelosi,Crenshaw`), `FILING_YEAR`

Der Workflow (`.github/workflows/check_trades.yml`) läuft danach automatisch
alle 6 Stunden und lässt sich zusätzlich manuell über den "Run workflow"-Button
im Actions-Tab auslösen.

## Konfiguration

| Variable | Beschreibung |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot-Token von @BotFather |
| `TELEGRAM_CHAT_ID` | Ziel-Chat für Benachrichtigungen |
| `WATCHLIST` | Komma-getrennte Nachnamen, leer = alle Trades |
| `FILING_YEAR` | Jahr für den Bulk-Index (Standard: aktuelles Jahr) |

## Grenzen

- 45-Tage-Meldefrist ist gesetzlich vorgegeben, kein Bug dieses Tools.
- Beträge werden nur als Range gemeldet, keine exakten Summen.
- PDF-Textextraktion kann bei handschriftlichen/gescannten Filings
  fehlschlagen — betrifft nur einen kleinen Teil der Einreichungen.
- Kein automatisches Trading — bewusst nur Alerting.
- Transaktionszeilen, die exakt auf einer Seitengrenze im PDF liegen,
  können vereinzelt übersehen werden (Tabellen werden pro Seite
  extrahiert, nicht seitenübergreifend zusammengeführt). Betrifft laut
  Stichprobe ca. 1 von 18 Zeilen — bekannte Lücke, noch nicht behoben.
- Der Beschreibungstext (Strike-Preis, Ablaufdatum bei Optionen) wird
  bei "sauber" gesplitteten Tabellenzeilen manchmal nicht erfasst, da
  er dort in einer separaten Fortsetzungszeile steht, die nicht mit
  der Haupt-Transaktionszeile zusammengeführt wird. Betrifft laut
  Stichprobe vor allem einzelne Zeilen — Feld bleibt dann leer statt
  falsch.
- Nur House-Disclosures (Repräsentantenhaus). Senat-Filings laufen
  über ein separates System (`efdsearch.senate.gov`) und sind hier
  noch nicht eingebunden.

## Lizenz

MIT