"""
CongressSignal — PDF-Parsing-Variante (kostenlos, offizielle Quelle)
----------------------------------------------------------------------
Nutzt statt einer bezahlten API direkt die offiziellen Periodic
Transaction Reports (PTRs) des House Clerk.

WICHTIG — bitte vor dem ersten produktiven Lauf verifizieren:
Der Bulk-Index-Endpunkt (BULK_INDEX_URL) folgt einem seit Jahren
dokumentierten, aber inoffiziellen Muster:
    Index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{JAHR}FD.zip
    PDFs:  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{JAHR}/{DocID}.pdf
Das ZIP enthält eine {JAHR}FD.xml mit einem Eintrag pro Filing
(Name, DocID, FilingType).
Beide URLs wurden am 13.08.2026 manuell gegen echte Filings
verifiziert (u.a. Pelosi-PTRs 2026). Trotzdem: PDF-Layouts können
sich zwischen Formularversionen ändern — bei 0 Treffern trotz
vorhandener Filings die Tabellenstruktur in parse_transaction_row()
prüfen.

Nur FilingType "P" (Periodic Transaction Report) ist relevant —
andere Typen (jährliche FD-Reports, Kandidaten-Filings) werden
übersprungen.
"""

import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pdfplumber
import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

WATCHLIST = [
    name.strip().lower()
    for name in os.environ.get("WATCHLIST", "Pelosi").split(",")
    if name.strip()
]

FILING_YEAR = os.environ.get("FILING_YEAR") or "2026"
BULK_INDEX_URL = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{FILING_YEAR}FD.zip"
PDF_BASE_URL = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{FILING_YEAR}"

STATE_FILE = Path(__file__).parent / "state" / "seen_filings.json"

DATA_DIR = Path(__file__).parent / "data"
COMMITTEE_SECTOR_MAP_FILE = DATA_DIR / "committee_sector_map.json"
MEMBER_COMMITTEES_FILE = DATA_DIR / "member_committees.json"


def load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


COMMITTEE_SECTOR_MAP = load_json_file(COMMITTEE_SECTOR_MAP_FILE)
MEMBER_COMMITTEES = load_json_file(MEMBER_COMMITTEES_FILE)


def check_conflict(filer_first: str, filer_last: str, ticker: str) -> str | None:
    """
    Prüft, ob der Filer in einem unserer gemappten Ausschüsse sitzt UND
    der gehandelte Ticker zum Branchen-Mapping dieses Ausschusses passt.
    Gibt bei Treffer einen kurzen Beschreibungstext zurück, sonst None.

    Matching erfolgt über "Vorname Nachname" gegen die Sync-Daten aus
    sync_committees.py — siehe dortige Einschränkungen zu Namensabweichungen.
    """
    full_name = f"{filer_first} {filer_last}".strip()
    committee_ids = MEMBER_COMMITTEES.get(full_name, [])
    for committee_id in committee_ids:
        mapping = COMMITTEE_SECTOR_MAP.get(committee_id)
        if mapping and ticker in mapping["tickers"]:
            sectors = ", ".join(mapping["sectors"])
            return f"{mapping['committee_name']} — Sektor-Überschneidung: {sectors}"
    return None

TICKER_REGEX = re.compile(r"\(([A-Z]{1,6})\)")
DATE_REGEX = re.compile(r"\d{2}/\d{2}/\d{4}")
TRANSACTION_TYPE_REGEX = re.compile(r"^[PSE]$")
DOLLAR_AMOUNT_REGEX = re.compile(r"\$[\d,]+")
ASSET_TYPE_REGEX = re.compile(r"\[([A-Z]{1,4})\]")
OWNER_CODE_REGEX = re.compile(r"(?<![A-Za-z])(SP|JT|DC)(?![A-Za-z])")
PARTIAL_REGEX = re.compile(r"\(partial\)", re.IGNORECASE)
# "D" gefolgt von Steuerzeichen (Font-Encoding-Artefakt in den PDFs)
# und ":" markiert den Beginn des Beschreibungstexts, z.B.
# "D\x00\x00...: Purchased 20 call options..." bzw. sauber "Description: ...".
DESCRIPTION_REGEX = re.compile(r"D[\x00-\x1f]*\s*:\s*(.+)", re.DOTALL)


# ---------------------------------------------------------------------------
# State Handling
# ---------------------------------------------------------------------------
def load_seen_ids() -> set:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_ids(ids: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(ids)))


# ---------------------------------------------------------------------------
# Index laden — auf PTR-Typ filtern, aber NICHT mehr nach Watchlist.
# Watchlist entscheidet weiter unten nur noch, wer IMMER gemeldet wird
# (z.B. Pelosi); alle anderen Filings werden trotzdem geparst, aber nur
# bei einem Ausschuss/Branchen-Match tatsächlich verschickt.
# ---------------------------------------------------------------------------
def fetch_filing_index() -> list[dict]:
    response = requests.get(BULK_INDEX_URL, timeout=60)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml_names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            sys.exit("Kein XML-Index im ZIP gefunden — Format hat sich vermutlich geändert.")
        xml_bytes = archive.read(xml_names[0])

    root = ElementTree.fromstring(xml_bytes)
    filings = []
    for member in root.findall(".//Member"):
        last = (member.findtext("Last") or "").strip()
        first = (member.findtext("First") or "").strip()
        filing_type = (member.findtext("FilingType") or "").strip()
        doc_id = (member.findtext("DocID") or "").strip()
        year = (member.findtext("Year") or "").strip()

        if filing_type != "P":  # nur Periodic Transaction Reports
            continue

        filings.append(
            {
                "last": last,
                "first": first,
                "doc_id": doc_id,
                "year": year,
                "pdf_url": f"{PDF_BASE_URL}/{doc_id}.pdf",
            }
        )
    return filings


# ---------------------------------------------------------------------------
# PDF laden und Transaktionen extrahieren
# ---------------------------------------------------------------------------
def extract_transactions(pdf_url: str) -> list[dict]:
    """
    Nutzt pdfplumbers Tabellenerkennung statt reinem Fließtext-Regex,
    weil PTR-PDFs mehrspaltige Tabellen enthalten, deren Zellinhalte
    beim reinen Text-Extract nicht zuverlässig in Lesereihenfolge
    herauskommen.
    """
    response = requests.get(pdf_url, timeout=60)
    response.raise_for_status()

    transactions = []
    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    tx = parse_transaction_row(row)
                    if tx:
                        transactions.append(tx)
    return transactions


def parse_transaction_row(row: list) -> dict | None:
    """
    Erwartet eine Tabellenzeile im Stil der House-PTR-Tabelle:
    [ID, Owner, Asset (inkl. Ticker/Typ), Transaction Type, Date,
     Notification Date, Amount, Cap Gains?]
    Zellinhalte können None oder mehrzeilige Strings sein. Die genaue
    Spaltenzahl variiert zwischen Filings, daher wird nur ein Minimum
    geprüft statt exakt 8 Spalten vorauszusetzen.
    """
    if not row or len(row) < 3:
        return None

    cells = [str(c).strip() if c else "" for c in row]
    full_text = " ".join(cells)

    ticker_match = TICKER_REGEX.search(full_text)
    if not ticker_match:
        return None  # keine Aktie in dieser Zeile (z.B. Header oder Fußnote)

    dates = DATE_REGEX.findall(full_text)
    if len(dates) < 1:
        return None

    amount_matches = DOLLAR_AMOUNT_REGEX.findall(full_text.replace("\n", " "))
    if len(amount_matches) >= 2:
        # In manchen Zeilen steht z.B. der Ticker zwischen den beiden
        # Beträgen (pdfplumber-Layout-Artefakt), daher werden hier bewusst
        # die ersten zwei $-Beträge im Text genommen statt ein
        # zusammenhängendes "$X - $Y"-Muster zu verlangen.
        amount_range = f"{amount_matches[0]} - {amount_matches[1]}"
    elif len(amount_matches) == 1:
        # Exchange-Transaktionen (Typ E, z.B. Spinoffs) haben oft einen
        # einzelnen exakten Betrag statt einer Range.
        amount_range = amount_matches[0]
    else:
        return None

    # Transaction Type: zuerst als eigenständige Zelle suchen ...
    type_match = None
    for cell in cells:
        if TRANSACTION_TYPE_REGEX.match(cell.strip()):
            type_match = cell.strip()
            break
    # ... Fallback: als eigenständiges Wort irgendwo im Zeilentext
    # (P/S/E als Ganzwort, nicht Teil eines längeren Tokens wie "OP")
    if not type_match:
        word_match = re.search(r"(?<![A-Za-z])[PSE](?![A-Za-z])", full_text)
        if word_match:
            type_match = word_match.group(0)

    # "(partial)"-Zusatz bei Teilverkäufen z.B. "S (partial)"
    if type_match and PARTIAL_REGEX.search(full_text):
        type_match = f"{type_match} (partial)"

    # Asset-Type: [ST]=Aktie, [OP]=Option, etc. — steht direkt hinter der
    # Ticker-Klammer, z.B. "(AAPL) [ST]"
    asset_type_match = ASSET_TYPE_REGEX.search(
        full_text[ticker_match.end():ticker_match.end() + 20]
    )
    asset_type = asset_type_match.group(1) if asset_type_match else "?"

    # Owner-Code: SP=Ehepartner, JT=Gemeinsam, DC=Kind, sonst Filer selbst
    owner_match = OWNER_CODE_REGEX.search(full_text)
    owner_code = owner_match.group(1) if owner_match else "Filer"

    # Beschreibungstext (bei Optionen z.B. Strike-Preis, Ablaufdatum)
    description_match = DESCRIPTION_REGEX.search(full_text)
    description = ""
    if description_match:
        description = description_match.group(1)
        description = description.replace("\x00", "").strip()
        description = re.sub(r"\s+", " ", description)

    # Asset-Name: alles vor der Ticker-Klammer aus der Zelle, die den Ticker enthält
    asset_name = full_text
    for cell in cells:
        if ticker_match.group(0) in cell:
            asset_name = cell.split(ticker_match.group(0))[0].strip(" -\n")
            break

    # Bei "garbled" Zeilen (v.a. Typ P) landen Owner-Code, Transaction-Type,
    # Daten und Betrag mit im Asset-Text. Hier wird alles ab dem ersten
    # "<Typ> <Datum>"-Muster abgeschnitten und ein führender Owner-Code
    # (SP/JT/DC) entfernt, damit nur der Firmenname übrig bleibt.
    asset_name = re.split(r"\s+[PSE]\s+\d{2}/\d{2}/\d{4}", asset_name)[0]
    asset_name = re.sub(r"^(SP|JT|DC)\s+", "", asset_name).strip(" -\n")

    return {
        "asset": asset_name or "Unbekannt",
        "asset_type": asset_type,
        "owner_code": owner_code,
        "ticker": ticker_match.group(1),
        "transaction_type": type_match or "?",
        "trade_date": dates[0],
        "notification_date": dates[1] if len(dates) > 1 else dates[0],
        "amount_range": amount_range,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    """
    Gibt True zurück, wenn die Nachricht tatsächlich an Telegram
    verschickt wurde, sonst False (z.B. wenn nicht konfiguriert).
    Nur bei True darf die Transaktion als "gesehen" markiert werden —
    sonst gehen Trades verloren, die nur geloggt statt gesendet wurden.

    Telegram limitiert auf grob 1 Nachricht/Sekunde pro Chat. Bei vielen
    neuen Trades auf einmal (z.B. erster Lauf, oder ein Filing mit
    vielen Zeilen) reicht das schnell nicht — daher eine kleine Pause
    vor jedem Send plus automatischer Retry bei HTTP 429.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram nicht konfiguriert, Nachricht wird nur geloggt:")
        print(text)
        return False

    time.sleep(1)  # einfache Drosselung, um 429 gar nicht erst zu provozieren

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }

    for attempt in range(3):
        response = requests.post(url, data=payload, timeout=15)
        if response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 3)
            print(f"Telegram Rate-Limit erreicht, warte {retry_after}s (Versuch {attempt + 1}/3)...")
            time.sleep(retry_after + 1)
            continue
        if not response.ok:
            print(f"Telegram-API-Fehler ({response.status_code}): {response.text}")
        response.raise_for_status()
        return True

    print("Telegram-Nachricht nach 3 Versuchen weiterhin rate-limited, überspringe.")
    return False



def format_message(filer: str, tx: dict, source_url: str, conflict: str | None = None) -> str:
    owner_label = {
        "SP": "Ehepartner",
        "JT": "Gemeinsames Konto",
        "DC": "Kind",
    }.get(tx["owner_code"])

    header = f"👤 {filer}" if owner_label is None else f"👤 {filer} ({owner_label})"

    lines = [
        "Neuer Congress-Trade erkannt",
        header,
        f"📈 {tx['ticker']} — {tx['asset'].strip()} [{tx['asset_type']}]",
        f"🔁 Typ: {tx['transaction_type']}",
        f"💰 Betrag (Range): {tx['amount_range']}",
        f"🗓 Trade-Datum: {tx['trade_date']}",
    ]
    if conflict:
        lines.append(f"🚩 Match: {conflict}")
    if tx["notification_date"] != tx["trade_date"]:
        lines.append(f"📬 Gemeldet am: {tx['notification_date']}")
    if tx["description"]:
        lines.append(f"📝 {tx['description']}")
    lines.append(f"📄 Quelle: {source_url}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    seen_ids = load_seen_ids()
    filings = fetch_filing_index()
    print(f"{len(filings)} PTR-Filings insgesamt gefunden (alle Mitglieder).")

    sent_count = 0
    for filing in filings:
        filer_name = f"{filing['first']} {filing['last']}".strip()
        is_watchlisted = bool(WATCHLIST) and any(
            name in filing["last"].lower() for name in WATCHLIST
        )

        try:
            transactions = extract_transactions(filing["pdf_url"])
        except requests.RequestException as exc:
            print(f"Konnte PDF nicht laden ({filing['pdf_url']}): {exc}")
            continue

        for tx in transactions:
            tx_id = "|".join(
                [filing["doc_id"], tx["ticker"], tx["trade_date"], tx["transaction_type"]]
            )
            if tx_id in seen_ids:
                continue

            conflict = check_conflict(filing["first"], filing["last"], tx["ticker"])

            # Nur senden, wenn entweder auf der Watchlist (z.B. Pelosi,
            # immer gemeldet) ODER ein Ausschuss/Branchen-Match vorliegt.
            # Alle anderen Trades werden geparst, aber bewusst nicht
            # verschickt — kein Alert-Spam für alle 435 Mitglieder.
            if not is_watchlisted and not conflict:
                continue

            was_sent = send_telegram_message(
                format_message(filer_name, tx, filing["pdf_url"], conflict)
            )
            if was_sent:
                seen_ids.add(tx_id)
                sent_count += 1

    print(f"{sent_count} Nachrichten verschickt (Watchlist + Conflict-Matches).")
    save_seen_ids(seen_ids)


if __name__ == "__main__":
    main()