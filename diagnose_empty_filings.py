"""
Diagnose-Script: Findet Filings aus einem Jahr, die beim Parsing
keine Exception geworfen haben (also "OK" zählten), aber 0
Transaktionen geliefert haben — und zeigt die rohen pdfplumber-Inhalte
von ein paar Stichproben, um die Ursache zu verstehen.

Kein Teil des laufenden Bots, nur zur Fehlersuche.

Aufruf: python diagnose_empty_filings.py <jahr> <pfad-zur-vorhandenen-csv>
"""

import io
import sys
import zipfile
from xml.etree import ElementTree

import pandas as pd
import pdfplumber
import requests

from tracker_pdf import extract_transactions

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2020
CSV_PATH = sys.argv[2] if len(sys.argv) > 2 else "data/historical_trades_2015_2026.csv"
N_SAMPLES = 5


def fetch_all_filings(year: int) -> list[dict]:
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    pdf_base = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}"

    response = requests.get(url, timeout=60)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml_names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
        xml_bytes = archive.read(xml_names[0])

    root = ElementTree.fromstring(xml_bytes)
    filings = []
    for member in root.findall(".//Member"):
        filing_type = (member.findtext("FilingType") or "").strip()
        disclosure_type = (member.findtext("DisclosureType") or "").strip()
        if filing_type != "P" and disclosure_type != "PTR":
            continue
        doc_id = (member.findtext("DocID") or "").strip()
        filings.append(
            {
                "doc_id": doc_id,
                "last": (member.findtext("Last") or "").strip(),
                "first": (member.findtext("First") or "").strip(),
                "pdf_url": f"{pdf_base}/{doc_id}.pdf",
            }
        )
    return filings


def main():
    print(f"Lade bestehende CSV: {CSV_PATH}")
    df = pd.read_csv(CSV_PATH)
    known_doc_ids_with_data = set(df[df["year"] == YEAR]["doc_id"].astype(str))
    print(f"{len(known_doc_ids_with_data)} Filings mit Daten für {YEAR} bereits bekannt.\n")

    print(f"Lade kompletten Filing-Index für {YEAR}...")
    all_filings = fetch_all_filings(YEAR)
    print(f"{len(all_filings)} PTR-Filings insgesamt gefunden.\n")

    empty_candidates = [f for f in all_filings if f["doc_id"] not in known_doc_ids_with_data]
    print(f"{len(empty_candidates)} Filings OHNE Daten in der CSV (Verdachtsfälle).\n")

    print(f"=== Untersuche {N_SAMPLES} Stichproben im Detail ===\n")
    for filing in empty_candidates[:N_SAMPLES]:
        print(f"--- {filing['first']} {filing['last']} — {filing['pdf_url']} ---")
        try:
            transactions = extract_transactions(filing["pdf_url"])
            print(f"extract_transactions() Ergebnis: {len(transactions)} Zeilen")
        except Exception as exc:
            print(f"extract_transactions() FEHLER: {exc}")

        # Rohe Tabellendaten zusätzlich zeigen
        try:
            response = requests.get(filing["pdf_url"], timeout=30)
            with pdfplumber.open(io.BytesIO(response.content)) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    tables = page.extract_tables()
                    text = page.extract_text() or ""
                    print(f"  Seite {page_num}: {len(tables)} Tabelle(n), {len(text)} Zeichen Rohtext")
                    if tables:
                        for row in tables[0][:3]:
                            print(f"    Zeile: {row}")
                    elif text:
                        print(f"    Textauszug: {text[:200]!r}")
        except Exception as exc:
            print(f"  Rohdaten-Abruf fehlgeschlagen: {exc}")
        print()


if __name__ == "__main__":
    main()