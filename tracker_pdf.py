"""
CongressSignal — PDF-Parsing-Variante (kostenlos, offizielle Quelle)
----------------------------------------------------------------------
Nutzt statt einer bezahlten API direkt die offiziellen Periodic
Transaction Reports (PTRs) des House Clerk.

WICHTIG — bitte vor dem ersten produktiven Lauf verifizieren:
Der Bulk-Index-Endpunkt (BULK_INDEX_URL) folgt einem seit Jahren
dokumentierten, aber inoffiziellen Muster:
    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{JAHR}FD.zip
Das ZIP enthält eine {JAHR}FD.xml mit einem Eintrag pro Filing
(Name, DocID, FilingType) sowie die einzelnen PDFs.
Dieses Skript konnte in der Entwicklungsumgebung NICHT gegen die
echte Seite getestet werden (Netzwerk-Sandbox blockiert house.gov).
Vor dem Cron-Einsatz also unbedingt einmal lokal laufen lassen und
- prüfen ob der Download klappt (HTTP 200, gültiges ZIP)
- die Text-Extraktion an 2-3 echten PDFs stichprobenartig prüfen
- ggf. TRANSACTION_LINE_REGEX an das tatsächliche PDF-Layout anpassen
  (Layouts können sich zwischen Jahren/Formularversionen unterscheiden)

Nur FilingType "P" (Periodic Transaction Report) ist relevant —
andere Typen (jährliche FD-Reports, Kandidaten-Filings) werden
übersprungen.
"""

import io
import json
import os
import re
import sys
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

FILING_YEAR = os.environ.get("FILING_YEAR", "2026")
BULK_INDEX_URL = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{FILING_YEAR}FD.zip"
PDF_BASE_URL = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{FILING_YEAR}"

STATE_FILE = Path(__file__).parent / "state" / "seen_filings.json"

# Grobes Muster für eine Transaktionszeile in einer PTR-PDF, z.B.:
# "NVIDIA CORP (NVDA) [ST] P 06/20/2025 06/25/2025 $500,001 - $1,000,000"
TRANSACTION_LINE_REGEX = re.compile(
    r"(?P<asset>[A-Z][A-Za-z0-9&.,'\-\s]+?)\s*\((?P<ticker>[A-Z]{1,6})\)\s*"
    r"\[(?P<asset_type>\w+)\]\s*"
    r"(?P<transaction_type>P|S|E)\s+"
    r"(?P<trade_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notification_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount_range>\$[\d,]+\s*-\s*\$[\d,]+)"
)


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
# Index laden und auf Watchlist + PTR-Typ filtern
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
        if WATCHLIST and not any(name in last.lower() for name in WATCHLIST):
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
    response = requests.get(pdf_url, timeout=60)
    response.raise_for_status()

    transactions = []
    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for match in TRANSACTION_LINE_REGEX.finditer(text):
                transactions.append(match.groupdict())
    return transactions


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram nicht konfiguriert, Nachricht wird nur geloggt:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, data=payload, timeout=15)
    response.raise_for_status()


def format_message(filer: str, tx: dict, source_url: str) -> str:
    return (
        f"*Neuer Congress-Trade erkannt*\n"
        f"👤 {filer}\n"
        f"📈 {tx['ticker']} — {tx['asset'].strip()}\n"
        f"🔁 Typ: {tx['transaction_type']}\n"
        f"💰 Betrag (Range): {tx['amount_range']}\n"
        f"🗓 Trade-Datum: {tx['trade_date']}\n"
        f"📄 Quelle: {source_url}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    seen_ids = load_seen_ids()
    filings = fetch_filing_index()
    print(f"{len(filings)} PTR-Filings passend zur Watchlist gefunden.")

    for filing in filings:
        filer_name = f"{filing['first']} {filing['last']}".strip()

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

            send_telegram_message(format_message(filer_name, tx, filing["pdf_url"]))
            seen_ids.add(tx_id)

    save_seen_ids(seen_ids)


if __name__ == "__main__":
    main()
