"""
CongressSignal — Historischer Datensatz-Export
----------------------------------------------------------------------
Einmaliges Analyse-Script, KEIN Teil des laufenden Alert-Bots.

Lädt alle PTR-Filings (Periodic Transaction Reports) aller House-
Mitglieder für einen Jahresbereich, parst alle Transaktionen und
schreibt sie als CSV — gedacht für Offline-Musteranalyse (z.B. durch
externe Personen wie Pascal), nicht für Live-Alerts.

Läuft NICHT automatisiert (kein Cronjob), da:
- Deutlich längere Laufzeit als ein einzelner Jahres-Lauf (mehrere
  Jahre × hunderte Filings)
- Einmaliger Bedarf, kein wiederkehrender Alert-Zweck

Aufruf: python build_historical_dataset.py
Output: data/historical_trades_2020_2026.csv

Bekannte Einschränkung: Ältere PDF-Formulare könnten ein anderes
Tabellenlayout haben als das 2026er-Format, gegen das der Parser
entwickelt wurde. Fehlschläge pro Jahr/Filing werden geloggt, aber
nicht den Lauf abbrechen — am Ende steht eine Zusammenfassung, wie
viele Filings pro Jahr erfolgreich waren.
"""

import csv
import io
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import requests

# Wiederverwendung der bereits verifizierten Parsing-Logik
from tracker_pdf import extract_transactions

START_YEAR = 2014
END_YEAR = 2026  # inklusive

OUTPUT_FILE = Path(__file__).parent / "data" / f"historical_trades_{START_YEAR}_{END_YEAR}.csv"

CSV_FIELDS = [
    "year", "filer_first", "filer_last", "doc_id", "ticker", "asset",
    "asset_type", "owner_code", "transaction_type", "trade_date",
    "notification_date", "amount_range", "description", "source_url",
]


def fetch_filing_index_for_year(year: int) -> list[dict]:
    """
    Wie fetch_filing_index() in tracker_pdf.py, aber für ein beliebiges
    Jahr statt der global konfigurierten FILING_YEAR — und OHNE
    Watchlist-Filter, da wir hier den kompletten Datensatz wollen.

    Das XML-Schema hat sich über die Jahre geändert:
    - Ab ca. 2015: FilingType == "P" kennzeichnet PTR-Filings.
    - 2012/2013: FilingType kennt kein "P", DisclosureType == "PTR"
      wird stattdessen genutzt. ABER: Die zugehörigen PDF-URLs unter
      ptr-pdfs/{jahr}/{doc_id}.pdf lieferten in einem Testlauf für
      2013 durchgängig 404 (alle 2318 Filings) — vermutlich ein
      anderer/nicht digitalisierter URL-Pfad für diese frühe Ära.
      Deshalb bewusst NICHT ab 2012, sondern erst ab 2014 gezogen.
      Der DisclosureType-Filter bleibt trotzdem als Fallback im Code,
      falls einzelne spätere Jahre ihn noch brauchen.

    Bekannte Einschränkung für Jahre vor ca. 2018: Manche PTR-PDFs sind
    eingescannte Bilder ohne extrahierbaren Text (keine OCR eingebaut)
    und liefern dann 0 Transaktionen trotz erfolgreichem Download.
    Zusätzlich fehlen bei älteren Formularen die [ST]/[OP]-Kennzeichnung
    und der "Description"-Text strukturell — das ist keine Parsing-
    Lücke, sondern in den Originaldokumenten schlicht nicht vorhanden.
    """
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    pdf_base = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}"

    response = requests.get(url, timeout=60)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml_names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise ValueError(f"Kein XML-Index im ZIP für {year} gefunden.")
        xml_bytes = archive.read(xml_names[0])

    root = ElementTree.fromstring(xml_bytes)
    filings = []
    for member in root.findall(".//Member"):
        last = (member.findtext("Last") or "").strip()
        first = (member.findtext("First") or "").strip()
        filing_type = (member.findtext("FilingType") or "").strip()
        disclosure_type = (member.findtext("DisclosureType") or "").strip()
        doc_id = (member.findtext("DocID") or "").strip()

        is_ptr = (filing_type == "P") or (disclosure_type == "PTR")
        if not is_ptr:
            continue

        filings.append(
            {
                "last": last,
                "first": first,
                "doc_id": doc_id,
                "pdf_url": f"{pdf_base}/{doc_id}.pdf",
            }
        )
    return filings


def main() -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    year_summary: dict[int, dict[str, int]] = {}

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for year in range(START_YEAR, END_YEAR + 1):
            print(f"\n=== Jahr {year} ===")
            try:
                filings = fetch_filing_index_for_year(year)
            except (requests.RequestException, ValueError) as exc:
                print(f"  Jahr {year} komplett übersprungen: {exc}")
                year_summary[year] = {"filings_total": 0, "filings_ok": 0, "rows": 0}
                continue

            print(f"  {len(filings)} PTR-Filings gefunden.")
            ok_count = 0
            year_rows = 0

            for i, filing in enumerate(filings, start=1):
                try:
                    transactions = extract_transactions(filing["pdf_url"])
                except Exception as exc:
                    print(f"  [{i}/{len(filings)}] Fehlgeschlagen ({filing['doc_id']}): {exc}")
                    continue

                ok_count += 1
                for tx in transactions:
                    writer.writerow(
                        {
                            "year": year,
                            "filer_first": filing["first"],
                            "filer_last": filing["last"],
                            "doc_id": filing["doc_id"],
                            "ticker": tx["ticker"],
                            "asset": tx["asset"],
                            "asset_type": tx["asset_type"],
                            "owner_code": tx["owner_code"],
                            "transaction_type": tx["transaction_type"],
                            "trade_date": tx["trade_date"],
                            "notification_date": tx["notification_date"],
                            "amount_range": tx["amount_range"],
                            "description": tx["description"],
                            "source_url": filing["pdf_url"],
                        }
                    )
                    year_rows += 1

                if i % 50 == 0:
                    print(f"  ... {i}/{len(filings)} Filings verarbeitet")

            print(f"  Jahr {year} fertig: {ok_count}/{len(filings)} Filings OK, {year_rows} Transaktionen.")
            year_summary[year] = {
                "filings_total": len(filings),
                "filings_ok": ok_count,
                "rows": year_rows,
            }
            total_rows += year_rows

    print("\n=== Zusammenfassung ===")
    for year, stats in year_summary.items():
        print(f"  {year}: {stats['filings_ok']}/{stats['filings_total']} Filings OK, {stats['rows']} Transaktionen")
    print(f"\nGesamt: {total_rows} Transaktionen geschrieben nach {OUTPUT_FILE}")


if __name__ == "__main__":
    main()