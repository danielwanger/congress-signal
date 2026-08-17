"""
CongressSignal — Rendite-Analyse (historisch)
----------------------------------------------------------------------
Einmaliges Analyse-Script, KEIN Teil des laufenden Alert-Bots.
Berechnet Renditen aus der bereits erzeugten historical_trades CSV.

METHODIK:
- Aktien [ST]: FIFO-Matching von Käufen (P) gegen spätere Verkäufe (S)
  pro Person+Ticker. Bei Match: echte realisierte Rendite. Ohne
  passenden Verkauf: Mark-to-Market gegen aktuellen Kurs, als
  "unrealisiert" gekennzeichnet.
- Optionen [OP]: Black-Scholes-NÄHERUNG, da keine kostenlose Quelle
  für historische Optionspreise existiert. Nutzt historische
  Kursvolatilität der Aktie als Ersatz für die "echte" implizite
  Volatilität — das ist eine Annäherung, keine echte Marktbewertung,
  und kann spürbar von echten Optionspreisen abweichen.
- Investierter Betrag: Mittelwert der gemeldeten Range (echte
  Unsicherheit, keine exakte Zahl aus den Daten verfügbar).

BENÖTIGT: pip install yfinance scipy pandas
LAUFZEIT: Kann je nach Anzahl unterschiedlicher Ticker mehrere Minuten
dauern (ein API-Call pro Ticker für historische Kurse, dann gecacht).

Aufruf: python analyze_returns.py <pfad-zur-csv> [Nachname]
Output: data/trades_with_returns.csv (bzw. _<nachname>.csv bei Filter)
"""

import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

RISK_FREE_RATE = 0.045  # grobe Annahme, aktueller US-Kurzläufer-Zins

AMOUNT_RANGE_REGEX = re.compile(r"\$?([\d,]+)")
STRIKE_REGEX = re.compile(r"strike price of \$?([\d,\.]+)", re.IGNORECASE)
EXPIRATION_REGEX = re.compile(r"expiration date of (\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)
# Bei Ausübungen ("Exercised... purchased 1/14/25...") steckt das ECHTE
# Einstiegsdatum der Option im Beschreibungstext, nicht im trade_date
# (das ist bei Ausübungen das Ausübungsdatum, meist nahe am Verfall).
PURCHASED_DATE_REGEX = re.compile(r"purchased (\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)


def parse_flexible_date(date_str: str) -> pd.Timestamp:
    """Versucht zweistelliges, dann vierstelliges Jahresformat."""
    parsed = pd.to_datetime(date_str, format="%m/%d/%y", errors="coerce")
    if pd.isna(parsed):
        parsed = pd.to_datetime(date_str, format="%m/%d/%Y", errors="coerce")
    return parsed


def parse_amount_midpoint(amount_range: str) -> float:
    """Mittelwert aus einer Range wie '$1,001 - $15,000' oder Einzelwert wie '$15'."""
    numbers = [float(n.replace(",", "")) for n in AMOUNT_RANGE_REGEX.findall(str(amount_range))]
    if not numbers:
        return np.nan
    return sum(numbers) / len(numbers)


def get_price_history_cache(tickers: list[str]) -> dict:
    """Lädt historische Kurse für alle benötigten Ticker EINMAL im Voraus,
    um nicht pro Zeile einen neuen API-Call zu machen."""
    cache = {}
    for i, ticker in enumerate(tickers, start=1):
        try:
            data = yf.Ticker(ticker).history(start="2020-01-01", auto_adjust=True)
            if not data.empty:
                data.index = data.index.tz_localize(None)
                cache[ticker] = data["Close"]
        except Exception as exc:
            print(f"  [{i}/{len(tickers)}] Kursdaten für {ticker} fehlgeschlagen: {exc}")
        if i % 25 == 0:
            print(f"  ... {i}/{len(tickers)} Ticker geladen")
    return cache


def price_on_or_after(price_series: pd.Series, date: pd.Timestamp) -> float | None:
    """Nächster verfügbarer Schlusskurs an oder nach dem gegebenen Datum
    (Handelstage — Wochenenden/Feiertage überspringen)."""
    if price_series is None:
        return None
    future = price_series[price_series.index >= date]
    return future.iloc[0] if not future.empty else None


def black_scholes_call(spot: float, strike: float, days_to_expiry: float, vol: float, r: float) -> float:
    if days_to_expiry <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0)
    t = days_to_expiry / 365
    d1 = (np.log(spot / strike) + (r + 0.5 * vol**2) * t) / (vol * np.sqrt(t))
    d2 = d1 - vol * np.sqrt(t)
    return spot * norm.cdf(d1) - strike * np.exp(-r * t) * norm.cdf(d2)


def estimate_historical_volatility(price_series: pd.Series, as_of: pd.Timestamp, window_days: int = 90) -> float:
    """Annualisierte historische Volatilität als Näherung für implizite Vol."""
    window = price_series[price_series.index <= as_of].tail(window_days)
    if len(window) < 10:
        return 0.3  # grober Fallback, falls zu wenig Historie vorhanden
    log_returns = np.log(window / window.shift(1)).dropna()
    return log_returns.std() * np.sqrt(252)


def compute_stock_returns(df_stock: pd.DataFrame, price_cache: dict) -> pd.DataFrame:
    """
    Läuft chronologisch durch alle Buy/Sell-Events pro Person+Ticker und
    hält eine FIFO-Warteschlange offener Käufe. Trifft ein Sell auf eine
    Warteschlange, wird der ÄLTESTE offene Kauf damit gematcht — das
    garantiert automatisch, dass exit_date > trade_date gilt (im
    Gegensatz zur vorherigen Version, die Buys/Sells nur nach
    Listenposition statt echter Zeitreihenfolge gepaart hat).

    Sells ohne offenen Kauf in der Warteschlange (z.B. Position aus der
    Zeit vor unserem 2020er-Datenfenster) werden übersprungen und am
    Ende gezählt, statt fälschlich einem späteren Kauf zugeordnet zu
    werden.
    """
    results = []
    unmatched_sells = 0
    grouped = df_stock.groupby(["filer_first", "filer_last", "ticker"])

    for (first, last, ticker), group in grouped:
        group = group.sort_values("trade_date")
        price_series = price_cache.get(ticker)
        open_buys: list[dict] = []

        for _, row in group.iterrows():
            ttype = str(row["transaction_type"])[:1]

            if ttype == "P":
                open_buys.append(row)
                continue

            if ttype != "S":
                continue  # z.B. "E" (Exchange) — hier nicht behandelt

            if not open_buys:
                unmatched_sells += 1
                continue  # Verkauf ohne bekannten Kauf in unserem Datenfenster

            buy = open_buys.pop(0)  # ältester offener Kauf zuerst (FIFO)
            entry_price = price_on_or_after(price_series, buy["trade_date"]) if price_series is not None else None
            exit_price = price_on_or_after(price_series, row["trade_date"]) if price_series is not None else None

            return_pct = None
            if entry_price and exit_price and entry_price > 0:
                return_pct = (exit_price - entry_price) / entry_price * 100

            results.append(
                {
                    "filer_first": first, "filer_last": last, "ticker": ticker,
                    "asset_type": "ST", "trade_date": buy["trade_date"], "exit_date": row["trade_date"],
                    "entry_price": entry_price, "exit_price": exit_price,
                    "return_pct": return_pct, "realized": True,
                    "amount_midpoint": buy["amount_midpoint"],
                }
            )

        # Was am Ende noch offen ist: nie verkauft -> Mark-to-Market gegen letzten bekannten Kurs
        last_known_price = price_series.iloc[-1] if price_series is not None and not price_series.empty else None
        last_known_date = price_series.index[-1] if price_series is not None and not price_series.empty else None

        for buy in open_buys:
            entry_price = price_on_or_after(price_series, buy["trade_date"]) if price_series is not None else None
            return_pct = None
            if entry_price and last_known_price and entry_price > 0:
                return_pct = (last_known_price - entry_price) / entry_price * 100

            results.append(
                {
                    "filer_first": first, "filer_last": last, "ticker": ticker,
                    "asset_type": "ST", "trade_date": buy["trade_date"], "exit_date": last_known_date,
                    "entry_price": entry_price, "exit_price": last_known_price,
                    "return_pct": return_pct, "realized": False,
                    "amount_midpoint": buy["amount_midpoint"],
                }
            )

    if unmatched_sells:
        print(f"  Hinweis: {unmatched_sells} Verkäufe ohne passenden Kauf im Datenfenster übersprungen "
              f"(vermutlich Position von vor 2020 gekauft).")

    return pd.DataFrame(results)


def compute_option_returns(df_options: pd.DataFrame, price_cache: dict) -> pd.DataFrame:
    results = []
    today = pd.Timestamp.now().normalize()

    for _, row in df_options.iterrows():
        ticker = row["ticker"]
        strike_match = STRIKE_REGEX.search(str(row["description"]))
        expiry_match = EXPIRATION_REGEX.search(str(row["description"]))

        if not strike_match or not expiry_match or ticker not in price_cache:
            continue  # kein Strike/Ablaufdatum extrahierbar -> überspringen

        strike = float(strike_match.group(1).replace(",", ""))
        expiry = parse_flexible_date(expiry_match.group(1))
        if pd.isna(expiry):
            continue

        # Bei "Exercised...purchased X" das echte Kaufdatum als Einstieg
        # nutzen; sonst (normaler "Purchased N call options"-Fall) ist
        # das trade_date bereits der korrekte Einstiegspunkt.
        purchased_match = PURCHASED_DATE_REGEX.search(str(row["description"]))
        if purchased_match:
            entry_date = parse_flexible_date(purchased_match.group(1))
            if pd.isna(entry_date):
                entry_date = row["trade_date"]
        else:
            entry_date = row["trade_date"]

        price_series = price_cache[ticker]
        entry_spot = price_on_or_after(price_series, entry_date)
        if entry_spot is None:
            continue

        entry_vol = estimate_historical_volatility(price_series, entry_date)
        entry_days = (expiry - entry_date).days
        entry_value = black_scholes_call(entry_spot, strike, entry_days, entry_vol, RISK_FREE_RATE)

        # Bewertung "heute" ODER am Ablaufdatum, je nachdem was früher ist
        valuation_date = min(expiry, today)
        exit_spot = price_on_or_after(price_series, valuation_date)
        if exit_spot is None:
            continue

        if valuation_date >= expiry:
            exit_value = max(exit_spot - strike, 0)  # am Verfall: intrinsischer Wert
            realized = True
        else:
            exit_vol = estimate_historical_volatility(price_series, valuation_date)
            exit_days = (expiry - valuation_date).days
            exit_value = black_scholes_call(exit_spot, strike, exit_days, exit_vol, RISK_FREE_RATE)
            realized = False

        return_pct = None
        if entry_value > 0.50:  # unter 50 Cent: Prozent-Rendite wird bedeutungslos verzerrt
            return_pct = (exit_value - entry_value) / entry_value * 100

        results.append(
            {
                "filer_first": row["filer_first"], "filer_last": row["filer_last"], "ticker": ticker,
                "asset_type": "OP (Näherung)", "trade_date": entry_date, "exit_date": valuation_date,
                "entry_price": entry_value, "exit_price": exit_value,
                "return_pct": return_pct, "realized": realized,
                "amount_midpoint": row["amount_midpoint"],
            }
        )
    return pd.DataFrame(results)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Aufruf: python analyze_returns.py <pfad-zur-csv> [Nachname]")

    input_path = Path(sys.argv[1])
    name_filter = sys.argv[2].lower() if len(sys.argv) > 2 else None

    df = pd.read_csv(input_path)

    if name_filter:
        before = len(df)
        df = df[df["filer_last"].str.lower().str.contains(name_filter, na=False)]
        print(f"Gefiltert auf '{name_filter}': {len(df)} von {before} Zeilen.")
        if df.empty:
            sys.exit(f"Keine Trades für '{name_filter}' gefunden — Schreibweise prüfen.")
        print(f"Gefundene Personen: {sorted(set(df['filer_first'] + ' ' + df['filer_last']))}\n")

    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%m/%d/%Y", errors="coerce")
    df["amount_midpoint"] = df["amount_range"].apply(parse_amount_midpoint)
    df = df.dropna(subset=["trade_date"])

    df_stock = df[(df["asset_type"] == "ST") & (df["transaction_type"].str[0].isin(["P", "S"]))].copy()
    df_options = df[df["asset_type"] == "OP"].copy()

    all_tickers = sorted(set(df_stock["ticker"]) | set(df_options["ticker"]))
    print(f"{len(all_tickers)} unterschiedliche Ticker, lade Kurshistorie...")
    price_cache = get_price_history_cache(all_tickers)
    print(f"Kursdaten für {len(price_cache)}/{len(all_tickers)} Ticker erfolgreich geladen.\n")

    print("Berechne Aktien-Renditen (FIFO-Matching)...")
    stock_returns = compute_stock_returns(df_stock, price_cache)
    print(f"  {len(stock_returns)} Aktien-Positionen berechnet.\n")

    print("Berechne Options-Renditen (Black-Scholes-Näherung)...")
    option_returns = compute_option_returns(df_options, price_cache)
    print(f"  {len(option_returns)} Options-Positionen berechnet (approximiert).\n")

    combined = pd.concat([stock_returns, option_returns], ignore_index=True)

    suffix = f"_{name_filter}" if name_filter else ""
    output_path = Path(__file__).parent / "data" / f"trades_with_returns{suffix}.csv"
    combined.to_csv(output_path, index=False)
    print(f"Geschrieben nach: {output_path}")

    print("\n=== Kurze Zusammenfassung ===")
    print(f"Median Rendite (Aktien, realisiert): {stock_returns[stock_returns['realized']]['return_pct'].median():.1f}%")
    print(f"Median Rendite (Aktien, unrealisiert/MtM): {stock_returns[~stock_returns['realized']]['return_pct'].median():.1f}%")
    if len(option_returns) > 0:
        print(f"Median Rendite (Optionen, approximiert): {option_returns['return_pct'].median():.1f}%")


if __name__ == "__main__":
    main()