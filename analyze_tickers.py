"""
Diagnose-Script: Nur zur Analyse, kein Teil des laufenden Trackers.

Geht alle PTR-Filings des Jahres durch (wie tracker_pdf.py), sammelt
aber JEDEN vorkommenden Ticker (unabhängig von Watchlist/Committee-
Match) und zeigt eine Häufigkeitsstatistik. Damit lässt sich empirisch
prüfen, ob Kongressmitglieder überwiegend bekannte Large-Caps handeln
oder auch viele Nebenwerte — das entscheidet, wie groß unsere manuelle
Sektor-Ticker-Liste realistisch sein muss.

Aufruf: python analyze_tickers.py
"""

from collections import Counter

from tracker_pdf import extract_transactions, fetch_filing_index

filings = fetch_filing_index()
print(f"{len(filings)} PTR-Filings insgesamt. Extrahiere Ticker...")

ticker_counter = Counter()
failed = 0

for i, filing in enumerate(filings, start=1):
    try:
        transactions = extract_transactions(filing["pdf_url"])
        for tx in transactions:
            ticker_counter[tx["ticker"]] += 1
    except Exception as exc:
        failed += 1
        continue
    if i % 50 == 0:
        print(f"  ... {i}/{len(filings)} verarbeitet")

print(f"\n{len(ticker_counter)} unterschiedliche Ticker gefunden ({failed} Filings fehlgeschlagen).")
print("\nTop 40 häufigste Ticker:")
for ticker, count in ticker_counter.most_common(40):
    print(f"  {ticker}: {count}x")

print(f"\nAlle {len(ticker_counter)} Ticker (alphabetisch), zum Copy-Paste:")
print(sorted(ticker_counter.keys()))